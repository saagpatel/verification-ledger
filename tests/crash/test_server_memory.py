"""Native server smoke/grade and offline JSONL inspection."""
from pathlib import Path

import pytest

from memory_crash_test.adapters.server_memory import ServerMemoryAdapter
from memory_crash_test.grade.grader import grade_server
from memory_crash_test.inspect.base import GraphSnapshot, StoredNote
from memory_crash_test.inspect.server_memory import ServerMemoryInspector


def test_inspector_sample(tmp_path: Path) -> None:
    sample = Path(__file__).parent / "data/memory.jsonl"
    (tmp_path / "memory.jsonl").write_text(sample.read_text(encoding="utf-8"), encoding="utf-8")
    snapshot = ServerMemoryInspector().snapshot(tmp_path)
    assert snapshot == {
        "alpha": StoredNote("alpha", "hello", "agent"),
        "beta": StoredNote("beta", "world", "ingested"),
    }
    assert isinstance(snapshot, GraphSnapshot)
    assert snapshot.relations == [{"from": "alpha", "to": "beta", "relationType": "references"}]


@pytest.mark.live
def test_restart(tmp_path: Path) -> None:
    adapter = ServerMemoryAdapter()
    try:
        adapter.start(tmp_path)
        assert adapter.write_note("alpha", "hello", provenance="agent")
        adapter.restart()
        assert adapter.read_note("alpha") is not None
        assert ServerMemoryInspector().snapshot(tmp_path) == {"alpha": StoredNote("alpha", "hello", "agent")}
    finally:
        adapter.stop()


@pytest.mark.live
def test_full_grade(tmp_path: Path) -> None:
    grade = grade_server(ServerMemoryAdapter, ServerMemoryInspector(), temp_root=tmp_path)
    assert [(item.guarantee, item.status) for item in grade.guarantees] == [
        ("VL-1", "FAIL"), ("VL-2", "FAIL"), ("VL-3", "FAIL"), ("VL-4", "PASS"),
    ]
    for guarantee in grade.guarantees:
        assert guarantee.status in ("PASS", "FAIL", "NOT_GRADABLE")
        assert guarantee.probes
        for probe in guarantee.probes:
            assert isinstance(probe.before, dict) and isinstance(probe.after, dict)
