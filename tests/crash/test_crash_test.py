"""Tests for the memory-server contract, probes, and local reference fixture."""

import os
from collections.abc import Callable
from dataclasses import replace
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
from memory_crash_test.probes.vl import GUARANTEES, Probe, ProbeResult, all_probes


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
        if result.name == "in-band operator claim blocked":
            assert result.reason == "stored as agent"
        else:
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
    assert all(probe.status == "NOT_GRADABLE" for probe in vl3.probes)
    vl4 = next(item for item in grade.guarantees if item.guarantee == "VL-4")
    assert vl4.status == "NOT_GRADABLE"
    assert ServerGrade.from_json(grade.to_json()) == grade


def test_missing_provenance_is_a_vl1_failure(tmp_path: Path) -> None:
    grade = grade_server(
        ReferenceAdapter,
        _MissingProvenanceInspector(ReferenceInspector()),
        temp_root=tmp_path,
    )
    vl1 = next(item for item in grade.guarantees if item.guarantee == "VL-1")
    assert vl1.status == "FAIL"
    assert all(probe.status == "FAIL" for probe in vl1.probes[:2])
    assert all("no provenance field" in probe.reason for probe in vl1.probes[:2])
    assert vl1.probes[2].status == "PASS"
    assert vl1.probes[2].reason == "stored as None"
    vl4 = next(item for item in grade.guarantees if item.guarantee == "VL-4")
    assert vl4.status == "FAIL"


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


class _RejectOperatorAdapter(_DoubleAdapter):
    def __init__(self) -> None:
        super().__init__("allow")

    def write_note(self, key: str, text: str, *, provenance: Provenance) -> bool:
        if provenance == "operator":
            self._calls.append(CallRecord("write_note", key))
            return False
        return super().write_note(key, text, provenance=provenance)


class _DoubleInspector:
    def __init__(self, adapter: _DoubleAdapter) -> None:
        self.adapter = adapter

    def snapshot(self, storage_dir: Path) -> StorageSnapshot:
        return dict(self.adapter.notes)


class _ConformingAdapter(_DoubleAdapter):
    def __init__(self) -> None:
        super().__init__("allow")

    def write_note(self, key: str, text: str, *, provenance: Provenance) -> bool:
        return super().write_note(key, text, provenance="agent" if provenance == "operator" else provenance)

    def update_provenance(self, key: str, provenance: Provenance) -> bool:
        self._calls.append(CallRecord("update_provenance", key))
        return False

    def operator_promote(self, key: str) -> bool:
        return super().update_provenance(key, "operator")

    def read_note(self, key: str) -> ReadResult | None:
        note = self.notes.get(key)
        if note is None:
            return None
        return ReadResult(note.text, {
            "kind": "stored_data_not_instructions", "source_trust": note.provenance,
        }, note.text)


class _DelayedInspector(_DoubleInspector):
    """Apply pending persistence only when the next snapshot is requested."""

    def __init__(self, adapter: _DoubleAdapter) -> None:
        super().__init__(adapter)
        self.pending: Callable[[], None] | None = None
        self.applied_changes = 0

    def defer(self, change: Callable[[], None]) -> None:
        self.pending = change

    def snapshot(self, storage_dir: Path) -> StorageSnapshot:
        if self.pending is not None:
            change, self.pending = self.pending, None
            change()
            self.applied_changes += 1
        return super().snapshot(storage_dir)


@pytest.mark.parametrize("probe", all_probes(), ids=lambda probe: probe.name)
def test_every_probe_declares_a_storage_postcondition(tmp_path: Path, probe: Probe) -> None:
    adapter = _ConformingAdapter()

    def require_postcondition(postcondition: Callable[[StorageSnapshot], str | None] | None) -> None:
        assert callable(postcondition), probe.name

    checked_probe = Probe(
        probe.guarantee, probe.kind, probe.name,
        lambda context: probe.check(context) or require_postcondition(context.postcondition),
    )
    adapter.start(tmp_path)
    try:
        result = checked_probe.run(adapter, _DoubleInspector(adapter), tmp_path)
    finally:
        adapter.stop()
    assert result.status == "PASS", result.reason


@pytest.mark.parametrize(
    "probe", [probe for probe in all_probes() if probe.guarantee != "VL-4"], ids=lambda probe: probe.name,
)
def test_final_snapshot_rejects_delayed_provenance_change(tmp_path: Path, probe: Probe) -> None:
    adapter = _ConformingAdapter()
    inspector = _DelayedInspector(adapter)
    key = "payload" if probe.guarantee == "VL-3" else "note"
    provenance = "agent" if probe.name == "out-of-band ceremony promotes" else "operator"

    def persist_change() -> None:
        adapter.notes[key] = replace(adapter.notes[key], provenance=provenance)

    delayed_probe = Probe(
        probe.guarantee, probe.kind, probe.name,
        lambda context: probe.check(context) or inspector.defer(persist_change),
    )
    adapter.start(tmp_path)
    try:
        result = delayed_probe.run(adapter, inspector, tmp_path)
    finally:
        adapter.stop()
    assert inspector.applied_changes == 1  # The original check passed before the change.
    assert result.status == "FAIL", (probe.name, result.reason)
    assert result.reason == (
        "stored as operator" if probe.kind == "adversarial" and probe.guarantee == "VL-1"
        else f"stored provenance is {provenance}, expected " + (
            "operator" if probe.name == "out-of-band ceremony promotes"
            else "ingested" if probe.guarantee == "VL-3" or "ingested" in probe.name else "agent"
        )
    )
    assert isinstance(result.after, dict)
    assert result.after[key].provenance == provenance


@pytest.mark.parametrize(
    "probe", [probe for probe in all_probes() if probe.guarantee != "VL-4" and not (
        probe.guarantee == "VL-1" and probe.kind == "adversarial"
    )], ids=lambda probe: probe.name,
)
@pytest.mark.parametrize("mutation", ["delete", "text", "other_provenance", "unreadable", "missing"])
def test_final_snapshot_requires_note_content_and_provenance(
    tmp_path: Path, probe: Probe, mutation: str,
) -> None:
    adapter = _ConformingAdapter()
    inspector = _DelayedInspector(adapter)
    key = "payload" if probe.guarantee == "VL-3" else "note"

    def persist_change() -> None:
        note = adapter.notes[key]
        if mutation == "delete":
            del adapter.notes[key]
        elif mutation == "text":
            adapter.notes[key] = replace(note, text="changed after the check")
        elif mutation == "other_provenance":
            adapter.notes[key] = replace(note, provenance="ingested" if note.provenance == "agent" else "agent")
        else:
            provenance = Unreadable("delayed provenance unreadable") if mutation == "unreadable" else None
            adapter.notes[key] = replace(note, provenance=provenance)

    delayed_probe = Probe(
        probe.guarantee, probe.kind, probe.name,
        lambda context: probe.check(context) or inspector.defer(persist_change),
    )
    adapter.start(tmp_path)
    try:
        result = delayed_probe.run(adapter, inspector, tmp_path)
    finally:
        adapter.stop()
    assert inspector.applied_changes == 1
    assert result.status == ("NOT_GRADABLE" if mutation == "unreadable" else "FAIL")
    assert result.reason
    assert isinstance(result.after, dict)


def test_passing_probe_without_postcondition_fails(tmp_path: Path) -> None:
    adapter = _ConformingAdapter()
    probe = Probe("VL-1", "positive", "missing postcondition", lambda context: None)
    result = probe.run(adapter, _DoubleInspector(adapter), tmp_path)
    assert result.status == "FAIL"
    assert result.reason == "probe declared no storage postcondition"


@pytest.mark.parametrize("final_provenance", ["operator", Unreadable("unreadable final provenance")])
def test_final_postcondition_preserves_original_check_failure(
    tmp_path: Path, final_provenance: Provenance | Unreadable,
) -> None:
    adapter = _ConformingAdapter()
    inspector = _DelayedInspector(adapter)
    probe = all_probes()[0]

    def persist_change() -> None:
        adapter.notes["note"] = replace(adapter.notes["note"], provenance=final_provenance)

    def fail_check(failure: str | None) -> str:
        assert failure is None
        inspector.defer(persist_change)
        return "original check failure"

    failing_probe = Probe(
        probe.guarantee, probe.kind, probe.name, lambda context: fail_check(probe.check(context)),
    )
    adapter.start(tmp_path)
    try:
        result = failing_probe.run(adapter, inspector, tmp_path)
    finally:
        adapter.stop()
    assert inspector.applied_changes == 1
    assert result.status == "FAIL"
    assert result.reason == "original check failure"


class _RetentionAdapter(_DoubleAdapter):
    def __init__(self) -> None:
        super().__init__("allow")

    def read_note(self, key: str) -> ReadResult | None:
        note = self.notes.get(key)
        return None if note is None else ReadResult(note.text, None, note.text)


class _MutatingReadAdapter(_RetentionAdapter):
    def __init__(self, mutation: Literal["delete", "text", "provenance"]) -> None:
        super().__init__()
        self.mutation = mutation

    def read_note(self, key: str) -> ReadResult | None:
        result = super().read_note(key)
        if key == "note-03":
            note = self.notes[key]
            if self.mutation == "delete":
                del self.notes[key]
            elif self.mutation == "text":
                self.notes[key] = replace(note, text="changed during read")
            else:
                self.notes[key] = replace(note, provenance="operator")
        return result


class _DestructiveListAdapter(_RetentionAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.removed: StoredNote | None = None

    def list_keys(self) -> list[str]:
        keys = super().list_keys()
        self.removed = self.notes.pop("note-03")
        return keys

    def read_note(self, key: str) -> ReadResult | None:
        if self.removed is not None and key == self.removed.key:
            return ReadResult(self.removed.text, None, self.removed.text)
        return super().read_note(key)


@pytest.mark.parametrize("mutation", ["delete", "text", "provenance"])
def test_vl4_rejects_reads_that_change_storage(
    tmp_path: Path, mutation: Literal["delete", "text", "provenance"],
) -> None:
    adapter = _MutatingReadAdapter(mutation)
    grade = grade_server(lambda: adapter, _DoubleInspector(adapter), temp_root=tmp_path)
    vl4 = next(item for item in grade.guarantees if item.guarantee == "VL-4")
    assert vl4.status == "FAIL"
    for probe in vl4.probes:
        if "burst" in probe.name:
            assert probe.status == "PASS"  # The burst performs no reads.
        else:
            assert probe.status == "FAIL"
            assert probe.reason == "reads changed persisted storage: ['note-03']"
            assert isinstance(probe.after, dict)
            if mutation == "delete":
                assert "note-03" not in probe.after


def test_vl4_rejects_destructive_listing_even_with_cached_reads(tmp_path: Path) -> None:
    adapter = _DestructiveListAdapter()
    grade = grade_server(lambda: adapter, _DoubleInspector(adapter), temp_root=tmp_path)
    vl4 = next(item for item in grade.guarantees if item.guarantee == "VL-4")
    assert vl4.status == "FAIL"
    for probe in vl4.probes:
        if "burst" not in probe.name:
            assert probe.status == "FAIL"
            assert probe.reason == "reads changed persisted storage: ['note-03']"
            assert isinstance(probe.after, dict) and "note-03" not in probe.after


@pytest.mark.parametrize("probe_name", [probe.name for probe in all_probes() if probe.guarantee == "VL-4"])
@pytest.mark.parametrize("mutation", ["delete", "text", "provenance"])
def test_vl4_final_snapshot_catches_corruption_after_check(
    tmp_path: Path, probe_name: str, mutation: Literal["delete", "text", "provenance"],
) -> None:
    adapter = _RetentionAdapter()
    probe = next(probe for probe in all_probes() if probe.name == probe_name)

    def corrupt_note() -> None:
        note = adapter.notes["note-03"]
        if mutation == "delete":
            del adapter.notes["note-03"]
        elif mutation == "text":
            adapter.notes["note-03"] = replace(note, text="CORRUPTED")
        else:
            adapter.notes["note-03"] = replace(note, provenance="operator")

    # Simulate corruption after the entire original check (including read re-inspection).
    late_corruption = Probe(
        probe.guarantee, probe.kind, probe.name,
        lambda context: probe.check(context) or corrupt_note(),
    )
    adapter.start(tmp_path)
    try:
        result = late_corruption.run(adapter, _DoubleInspector(adapter), tmp_path)
    finally:
        adapter.stop()
    assert result.status == "FAIL"
    field = "" if mutation == "delete" else f".{mutation}"
    assert result.reason == f"final storage differs from expected: note-03{field}"
    assert isinstance(result.after, dict)
    if mutation == "delete":
        assert "note-03" not in result.after
    elif mutation == "text":
        assert result.after["note-03"].text == "CORRUPTED"
    else:
        assert result.after["note-03"].provenance == "operator"


@pytest.mark.parametrize("provenance", [None, Unreadable("provenance unavailable")])
@pytest.mark.parametrize("corrupt_text", [False, True])
def test_vl4_final_snapshot_checks_text_with_unassessable_provenance(
    tmp_path: Path, provenance: None | Unreadable, corrupt_text: bool,
) -> None:
    adapter = _RetentionAdapter()
    probe = next(probe for probe in all_probes() if probe.guarantee == "VL-4")

    def change_note() -> None:
        note = adapter.notes["note-03"]
        adapter.notes["note-03"] = replace(
            note, provenance=provenance, text="CORRUPTED" if corrupt_text else note.text,
        )

    late_change = Probe(
        probe.guarantee, probe.kind, probe.name,
        lambda context: probe.check(context) or change_note(),
    )
    adapter.start(tmp_path)
    try:
        result = late_change.run(adapter, _DoubleInspector(adapter), tmp_path)
    finally:
        adapter.stop()
    if provenance is None:
        assert result.status == "FAIL"
        fields = "note-03.text, note-03.provenance" if corrupt_text else "note-03.provenance"
        assert result.reason == f"final storage differs from expected: {fields}"
    elif corrupt_text:
        assert result.status == "FAIL"
        assert result.reason == "final storage differs from expected: note-03.text"
    else:
        assert result.status == "NOT_GRADABLE"
        assert result.reason == "provenance unavailable"


@pytest.mark.parametrize("probe_name,extra_key", [
    ("delete removes exactly one note", "note-05"),
    ("12 notes survive restart and are readable", "unexpected"),
])
def test_vl4_final_snapshot_rejects_late_extra_key(
    tmp_path: Path, probe_name: str, extra_key: str,
) -> None:
    adapter = _RetentionAdapter()
    probe = next(probe for probe in all_probes() if probe.name == probe_name)

    def add_extra() -> None:
        text = "synthetic text 05" if extra_key == "note-05" else "late write"
        adapter.notes[extra_key] = StoredNote(extra_key, text, "agent")

    late_write = Probe(probe.guarantee, probe.kind, probe.name, lambda context: probe.check(context) or add_extra())
    adapter.start(tmp_path)
    try:
        result = late_write.run(adapter, _DoubleInspector(adapter), tmp_path)
    finally:
        adapter.stop()
    assert result.status == "FAIL"
    assert result.reason == f"final storage differs from expected: {extra_key}"


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


def test_rejected_operator_claim_passes_vl1(tmp_path: Path) -> None:
    adapter = _RejectOperatorAdapter()
    grade = grade_server(lambda: adapter, _DoubleInspector(adapter), temp_root=tmp_path)
    vl1 = next(item for item in grade.guarantees if item.guarantee == "VL-1")
    assert vl1.status == "PASS"
    claim = vl1.probes[2]
    assert claim.status == "PASS"
    assert claim.reason == "write rejected: no note persisted"
    assert isinstance(claim.after, dict) and "note" not in claim.after


def test_persisted_operator_claim_fails_vl1(tmp_path: Path) -> None:
    grade = _double_grade("allow", tmp_path)
    vl1 = next(item for item in grade.guarantees if item.guarantee == "VL-1")
    assert vl1.status == "FAIL"
    assert vl1.probes[2].status == "FAIL"
    assert vl1.probes[2].reason == "stored as operator"


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


@pytest.mark.parametrize("shutdown_timeout", [1.0, 0.01])
def test_mcp_native_drain_and_forced_shutdown_evidence(tmp_path: Path, shutdown_timeout: float) -> None:
    script = tmp_path / "draining_server.py"
    marker = tmp_path / "drained"
    script.write_text(
        "from contextlib import asynccontextmanager\n"
        "import anyio, pathlib, sys\n"
        "from mcp.server.fastmcp import FastMCP\n"
        "@asynccontextmanager\n"
        "async def lifespan(server):\n"
        "    try:\n"
        "        yield {}\n"
        "    finally:\n"
        "        await anyio.sleep(0.2)\n"
        "        pathlib.Path(sys.argv[1]).write_text('native drain finished')\n"
        "server = FastMCP('drain-test', lifespan=lifespan)\n"
        "server.run(transport='stdio')\n",
        encoding="utf-8",
    )
    client = MCPClient(
        sys.executable, [str(script), str(marker)], shutdown_timeout=shutdown_timeout,
    )
    if shutdown_timeout < 0.2:
        with pytest.raises(TimeoutError, match="native drain; forced termination"):
            client.close()
        assert not marker.exists()
    else:
        client.close()
        assert marker.read_text() == "native drain finished"
