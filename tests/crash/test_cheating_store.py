"""The suite must identify each deliberately dishonest storage behavior."""

import json
from pathlib import Path

import pytest

from memory_crash_test.fixtures.cheating_store.adapter import CheatingAdapter
from memory_crash_test.fixtures.reference_server.adapter import ReferenceAdapter
from memory_crash_test.fixtures.reference_server.inspector import ReferenceInspector
from memory_crash_test.fixtures.reference_server.storage import load, save
from memory_crash_test.grade.grader import grade_server
from memory_crash_test.inspect.base import Provenance


@pytest.mark.parametrize("mode", ["all", "drop_on_restart", "phantom_read", "collateral_delete"])
def test_cheats_fail_retention_with_evidence(mode: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MCT_CHEAT_MODES", mode)
    writes: list[tuple[str, bool]] = []

    class RecordingAdapter(CheatingAdapter):
        def write_note(self, key: str, text: str, *, provenance: Provenance) -> bool:
            result = super().write_note(key, text, provenance=provenance)
            writes.append((key, result))
            return result

    grade = grade_server(RecordingAdapter, ReferenceInspector(), temp_root=tmp_path)
    assert {g.guarantee: g.status for g in grade.guarantees} == {
        "VL-1": "PASS", "VL-2": "PASS", "VL-3": "PASS", "VL-4": "FAIL",
    }
    calls = [json.loads(call.args)["key"] for g in grade.guarantees for probe in g.probes
             for call in probe.calls if call.tool_name == "write_note"]
    assert calls == [key for key, _ in writes]
    assert writes and all(result for _, result in writes)
    failures = [p for g in grade.guarantees if g.guarantee == "VL-4" for p in g.probes if p.status == "FAIL"]
    assert failures
    expected_probe = {
        "all": "12 notes survive restart and are readable",
        "drop_on_restart": "12 notes survive restart and are readable",
        "phantom_read": "delete removes exactly one note",
        "collateral_delete": "delete removes exactly one note",
    }[mode]
    assert any(p.name == expected_probe for p in failures)
    assert any(
        isinstance(p.before, dict) and isinstance(p.after, dict)
        and any(key in p.reason and key not in p.after for key in calls)
        for p in failures
    )


def test_reference_contrast(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MCT_CHEAT_MODES", raising=False)
    grade = grade_server(ReferenceAdapter, ReferenceInspector(), temp_root=tmp_path)
    assert all(item.status == "PASS" for item in grade.guarantees)


def test_modes_exercise_their_specific_lies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MCT_CHEAT_MODES", "drop_on_restart")
    adapter = CheatingAdapter()
    adapter.start(tmp_path)
    try:
        assert adapter.write_note("first", "one", provenance="agent")
        assert adapter.write_note("second", "two", provenance="agent")
        assert adapter.read_note("second") is not None and "second" in adapter.list_keys()
        assert "second" not in load(tmp_path)
        adapter.restart()
        assert adapter.read_note("second") is None
    finally:
        adapter.stop()


def test_phantom_after_external_delete(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MCT_CHEAT_MODES", "phantom_read")
    adapter = CheatingAdapter()
    adapter.start(tmp_path)
    try:
        assert adapter.write_note("cached", "text", provenance="agent")
        save(tmp_path, {})
        assert adapter.read_note("cached") is not None
        assert "cached" in adapter.list_keys()
    finally:
        adapter.stop()


def test_collateral_deletes_oldest_after_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MCT_CHEAT_MODES", "collateral_delete")
    adapter = CheatingAdapter()
    adapter.start(tmp_path)
    try:
        for key in ("z-oldest", "a-newer", "target"):
            assert adapter.write_note(key, key, provenance="agent")
        adapter.restart()
        assert adapter.delete_note("target")
        assert set(load(tmp_path)) == {"a-newer"}
    finally:
        adapter.stop()
