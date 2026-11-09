"""Tests for the memory-server contract, probes, and local reference fixture."""

import os
from pathlib import Path
import json
import sys
import time
from typing import Literal

import pytest

from memory_crash_test.adapters.base import (
    CallRecord,
    MemoryServerAdapter,
    Provenance,
    ReadResult,
    ServerRelease,
)
from memory_crash_test.fixtures.reference_server.adapter import ReferenceAdapter
from memory_crash_test.fixtures.reference_server.inspector import ReferenceInspector
from memory_crash_test.grade.grader import ServerGrade, grade_server
from memory_crash_test.mcp_client import MCPClient
from memory_crash_test.inspect.base import (
    StorageInspector,
    StorageSnapshot,
    StoredNote,
    Unreadable,
)
from memory_crash_test.probes.vl import GUARANTEES, ProbeResult, all_probes


@pytest.fixture(scope="module")
def reference_grade(tmp_path_factory: pytest.TempPathFactory) -> ServerGrade:
    """The grader creates one fresh local server and store per probe."""
    return grade_server(
        ReferenceAdapter,
        ReferenceInspector(),
        temp_root=tmp_path_factory.mktemp("reference-grade"),
    )


def test_protocol_assignments_and_complete_probe_set() -> None:
    adapter: MemoryServerAdapter = ReferenceAdapter()
    inspector: StorageInspector = ReferenceInspector()
    assert adapter.release == ServerRelease("memory-crash-test-reference", "1.0.0", "fixture")
    assert len(all_probes()) == 12
    assert {probe.guarantee for probe in all_probes()} == set(GUARANTEES)
    assert inspector is not None


def test_each_reference_probe_passes_with_inspector_evidence(reference_grade: ServerGrade) -> None:
    assert all(len(grade.probes) > 0 for grade in reference_grade.guarantees)
    probes: list[ProbeResult] = [probe for grade in reference_grade.guarantees for probe in grade.probes]
    assert len(probes) == len(all_probes())
    for result in probes:
        assert result.status == "PASS", (result.guarantee, result.name, result.reason)
        assert isinstance(result.before, dict)
        assert isinstance(result.after, dict)
        assert result.reason == ""
    assert any(call.tool_name == "write_note" for result in probes for call in result.calls)


def test_grader_json_round_trip(reference_grade: ServerGrade) -> None:
    decoded = ServerGrade.from_json(reference_grade.to_json())
    assert decoded == reference_grade
    assert decoded.to_json() == reference_grade.to_json()


class _UnreadableInspector:
    def snapshot(self, storage_dir: Path) -> Unreadable:
        return Unreadable("storage is opaque")


class _FieldUnreadableInspector:
    def __init__(self, base: ReferenceInspector) -> None:
        self.base = base

    def snapshot(self, storage_dir: Path) -> StorageSnapshot | Unreadable:
        snapshot = self.base.snapshot(storage_dir)
        if isinstance(snapshot, Unreadable):
            return snapshot
        return {
            key: StoredNote(note.key, note.text, Unreadable("provenance column unavailable"))
            for key, note in snapshot.items()
        }


class _MissingProvenanceInspector:
    def __init__(self, base: ReferenceInspector) -> None:
        self.base = base

    def snapshot(self, storage_dir: Path) -> StorageSnapshot | Unreadable:
        snapshot = self.base.snapshot(storage_dir)
        if isinstance(snapshot, Unreadable):
            return snapshot
        return {
            key: StoredNote(note.key, note.text, None)
            for key, note in snapshot.items()
        }


def test_unreadable_storage_makes_every_guarantee_not_gradable(tmp_path: Path) -> None:
    grade = grade_server(ReferenceAdapter, _UnreadableInspector(), temp_root=tmp_path)
    assert [item.status for item in grade.guarantees] == ["NOT_GRADABLE"] * 4
    assert all("opaque" in probe.reason for item in grade.guarantees for probe in item.probes)
    assert ServerGrade.from_json(grade.to_json()) == grade


def test_unreadable_provenance_is_not_gradable_for_vl1(tmp_path: Path) -> None:
    grade = grade_server(
        ReferenceAdapter,
        _FieldUnreadableInspector(ReferenceInspector()),
        temp_root=tmp_path,
    )
    vl1 = next(item for item in grade.guarantees if item.guarantee == "VL-1")
    assert vl1.status == "NOT_GRADABLE"
    assert all(probe.status == "NOT_GRADABLE" for probe in vl1.probes)
    vl3 = next(item for item in grade.guarantees if item.guarantee == "VL-3")
    assert vl3.probes[0].status == "PASS"
    assert vl3.probes[1].status == "NOT_GRADABLE"
    assert ServerGrade.from_json(grade.to_json()) == grade


def test_missing_provenance_is_a_vl1_failure(tmp_path: Path) -> None:
    grade = grade_server(
        ReferenceAdapter,
        _MissingProvenanceInspector(ReferenceInspector()),
        temp_root=tmp_path,
    )
    vl1 = next(item for item in grade.guarantees if item.guarantee == "VL-1")
    assert vl1.status == "FAIL"
    assert all(probe.status == "FAIL" for probe in vl1.probes)
    assert all("no provenance field" in probe.reason for probe in vl1.probes)
    vl4 = next(item for item in grade.guarantees if item.guarantee == "VL-4")
    assert vl4.status == "PASS"


def test_reference_inspector_reads_stopped_storage_and_marks_corruption(tmp_path: Path) -> None:
    storage_dir = tmp_path / "stopped-server"
    storage_dir.mkdir()
    storage_file = storage_dir / "notes.json"
    storage_file.write_text(
        json.dumps({"persisted": {
            "key": "persisted", "text": "local test note", "provenance": "agent",
        }}),
        encoding="utf-8",
    )
    inspector = ReferenceInspector()
    snapshot = inspector.snapshot(storage_dir)
    assert isinstance(snapshot, dict)
    assert snapshot["persisted"] == StoredNote("persisted", "local test note", "agent")

    storage_file.write_text("{corrupt", encoding="utf-8")
    unreadable = inspector.snapshot(storage_dir)
    assert isinstance(unreadable, Unreadable)
    assert "notes.json" in unreadable.reason


Mode = Literal["allow", "block"]


class _DoubleAdapter:
    """In-process adversarial doubles exercise both directions without a child."""

    def __init__(self, mode: Mode) -> None:
        self.mode = mode
        self.release = ServerRelease(f"{mode}-double", "1", "test")
        self.notes: dict[str, StoredNote] = {}
        self._calls: list[CallRecord] = []
        self.started = False

    def start(self, storage_dir: Path) -> None:
        self.notes = {}
        self._calls = []
        self.started = True

    def stop(self) -> None:
        self.started = False

    def restart(self) -> None:
        self.stop()
        self.started = True

    def write_note(self, key: str, text: str, *, provenance: Provenance) -> bool:
        self._calls.append(CallRecord("write_note", key))
        if self.mode == "block":
            return False
        self.notes[key] = StoredNote(key, text, provenance)
        return True

    def update_provenance(self, key: str, provenance: Provenance) -> bool:
        self._calls.append(CallRecord("update_provenance", key))
        if self.mode == "block" or key not in self.notes:
            return False
        note = self.notes[key]
        self.notes[key] = StoredNote(note.key, note.text, provenance)
        return True

    def operator_promote(self, key: str) -> bool:
        self._calls.append(CallRecord("operator_promote", key))
        return self.update_provenance(key, "operator")

    def read_note(self, key: str) -> ReadResult | None:
        note = self.notes.get(key)
        if note is None:
            if self.mode == "allow" and key == "never-written":
                return ReadResult("phantom", None, "phantom")
            return None
        trust = note.provenance
        envelope: dict[str, object] | None = None
        if self.mode == "allow":
            envelope = {"kind": "wrong_boundary", "source_trust": str(trust)}
        else:
            envelope = {"kind": "stored_data_not_instructions", "source_trust": str(trust)}
        return ReadResult(note.text, envelope, note.text)

    def list_keys(self) -> list[str]:
        return list(self.notes)

    def delete_note(self, key: str) -> bool:
        return self.notes.pop(key, None) is not None

    def calls(self) -> list[CallRecord]:
        return list(self._calls)


class _RaisingWriteAdapter(_DoubleAdapter):
    def __init__(self) -> None:
        super().__init__("allow")
        self.stop_calls = 0

    def write_note(self, key: str, text: str, *, provenance: Provenance) -> bool:
        raise RuntimeError("synthetic write failure")

    def stop(self) -> None:
        self.stop_calls += 1
        super().stop()


class _DoubleInspector:
    def __init__(self, adapter: _DoubleAdapter) -> None:
        self.adapter = adapter

    def snapshot(self, storage_dir: Path) -> StorageSnapshot:
        return dict(self.adapter.notes)


def _double_grade(mode: Mode, tmp_path: Path) -> ServerGrade:
    adapter = _DoubleAdapter(mode)
    inspector = _DoubleInspector(adapter)
    def factory() -> MemoryServerAdapter:
        return adapter

    return grade_server(factory, inspector, temp_root=tmp_path)


def test_allow_everything_fails_adversarial_guarantees(tmp_path: Path) -> None:
    grade = _double_grade("allow", tmp_path)
    by_name = {item.guarantee: item for item in grade.guarantees}
    assert by_name["VL-1"].status == "FAIL"
    assert by_name["VL-2"].status == "FAIL"
    assert by_name["VL-3"].status == "FAIL"
    assert by_name["VL-4"].status == "FAIL"


def test_block_everything_fails_positive_guarantees(tmp_path: Path) -> None:
    grade = _double_grade("block", tmp_path)
    by_name = {item.guarantee: item for item in grade.guarantees}
    assert by_name["VL-1"].status == "FAIL"
    assert by_name["VL-2"].status == "FAIL"
    assert by_name["VL-3"].status == "FAIL"
    assert by_name["VL-4"].status == "FAIL"


def test_raising_probe_fails_and_stops_adapter(tmp_path: Path) -> None:
    adapter = _RaisingWriteAdapter()

    def factory() -> MemoryServerAdapter:
        return adapter

    grade = grade_server(factory, _DoubleInspector(adapter), temp_root=tmp_path)
    assert all(item.status == "FAIL" for item in grade.guarantees)
    assert all(
        probe.status == "FAIL" and "synthetic write failure" in probe.reason
        for item in grade.guarantees
        for probe in item.probes
    )
    assert adapter.started is False
    assert adapter.stop_calls == len(all_probes())


def test_mcp_startup_timeout_terminates_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "server.pid"
    code = "import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
    with pytest.raises(TimeoutError):
        MCPClient(sys.executable, ["-c", code, str(pid_file)], timeout=0.2)

    deadline = time.monotonic() + 3
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pid_file.exists(), "startup child did not record its PID"
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_mcp_startup_failure_cleans_up_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "failed-server.pid"
    code = "import os, pathlib, sys; pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))"
    with pytest.raises(ExceptionGroup) as startup_error:
        MCPClient(sys.executable, ["-c", code, str(pid_file)], timeout=1)
    assert "Connection closed" in repr(startup_error.value)

    assert pid_file.exists(), "startup child did not record its PID"
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_mcp_tool_timeout_terminates_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "tool-server.pid"
    script = tmp_path / "slow_server.py"
    script.write_text(
        "import os, pathlib, sys, time\n"
        "from mcp.server.fastmcp import FastMCP\n"
        "server = FastMCP('slow-test')\n"
        "@server.tool()\n"
        "def slow_tool() -> str:\n"
        "    time.sleep(60)\n"
        "    return 'done'\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "server.run(transport='stdio')\n",
        encoding="utf-8",
    )
    client = MCPClient(sys.executable, [str(script), str(pid_file)], timeout=3)
    with pytest.raises(TimeoutError):
        client.call_tool("slow_tool", {})

    assert pid_file.exists(), "tool server did not record its PID"
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
