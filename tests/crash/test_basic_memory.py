"""Native behavior over local stdio; default test runs remain offline."""

import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from memory_crash_test.adapters.basic_memory import BasicMemoryAdapter
from memory_crash_test.grade.grader import ServerGrade, grade_server
from memory_crash_test.inspect.base import EvidenceSnapshot, StoredNote, Unreadable
from memory_crash_test.inspect.basic_memory import BasicMemoryInspector


def test_inspector_uses_files_and_records_secondary_disagreement(tmp_path: Path) -> None:
    shutil.copytree(Path(__file__).parent / "data/basic-memory", tmp_path, dirs_exist_ok=True)
    inspector = BasicMemoryInspector()
    snapshot = inspector.snapshot(tmp_path)
    assert isinstance(snapshot, EvidenceSnapshot)
    assert snapshot["alpha"] == StoredNote(
        "alpha", "First test note\n\n- references [[beta]]", "agent",
    )
    # A tag/body claim is not the designated provenance field.
    assert snapshot["beta"].provenance is None

    (tmp_path / "config").mkdir()
    with sqlite3.connect(tmp_path / "config/memory.db") as connection:
        connection.executescript("""
            CREATE TABLE note_content(file_path, markdown_content, db_version,
                file_version, db_checksum, file_checksum, file_write_status,
                last_materialization_error);
            CREATE TABLE entity(file_path, entity_metadata);
            CREATE TABLE accepted_project_note_change(file_path, operation,
                db_version, db_checksum, materialized_at, partition_position);
            CREATE TABLE relation(id, from_id, to_id, to_name, relation_type);
            INSERT INTO note_content VALUES
                ('ghost.md', 'accepted but unpublished', 2, 1, 'new', 'old', 'pending', NULL);
            INSERT INTO entity VALUES ('alpha.md', '{"source_trust":"operator"}');
            INSERT INTO accepted_project_note_change VALUES
                ('beta.md', 'delete', 2, NULL, NULL, 1);
            INSERT INTO relation VALUES (1, 1, NULL, 'ghost', 'references');
        """)
    snapshot = inspector.snapshot(tmp_path)
    assert isinstance(snapshot, EvidenceSnapshot)
    assert set(snapshot) == {"alpha", "beta"}  # DB/relations cannot rescue or remove files.
    secondary = snapshot.evidence["sqlite"]
    assert isinstance(secondary, dict)
    assert secondary["disagreements"] == [
        "accepted SQLite note ghost.md has no Markdown file",
        "publication is unsettled for ghost.md",
        "SQLite accepted/file checksums differ for ghost.md",
        "Markdown file alpha.md has no accepted SQLite note",
        "Markdown file beta.md has no accepted SQLite note",
        "SQLite provenance differs from Markdown for alpha.md",
        "delete tombstone still has Markdown file beta.md",
    ]
    assert secondary["relation"]
    (tmp_path / "notes/alpha.md").write_text("---\nsource_trust: [\n---\nbody")
    assert isinstance(inspector.snapshot(tmp_path), Unreadable)


@pytest.mark.live
def test_start_write_restart_stopped_inspection(tmp_path: Path) -> None:
    adapter = BasicMemoryAdapter()
    inspector = BasicMemoryInspector()
    try:
        adapter.start(tmp_path)
        assert adapter.release.version == "1.0.0"
        assert adapter.write_note("smoke", "local smoke text", provenance="ingested")
        adapter.restart()
        assert adapter.list_keys() == ["smoke"]
        result = adapter.read_note("smoke")
        assert result is not None and result.text == "local smoke text"
        assert result.envelope is None
        assert adapter.read_note("never-written") is None
    finally:
        adapter.stop()
    snapshot = inspector.snapshot(tmp_path)
    assert isinstance(snapshot, EvidenceSnapshot)
    assert snapshot["smoke"] == StoredNote("smoke", "local smoke text", "ingested")
    assert snapshot.evidence["clean_stop_snapshots"]
    assert "DENY_WRITE" not in str(snapshot.evidence.get("guard.log", ""))


@pytest.mark.live
def test_full_basic_memory_grade(tmp_path: Path) -> None:
    grade = grade_server(BasicMemoryAdapter, BasicMemoryInspector(), temp_root=tmp_path)
    assert grade.release.name == "basic-memory"
    assert grade.release.version == "1.0.0"
    assert len(grade.guarantees) == 4
    for guarantee in grade.guarantees:
        assert guarantee.status in {"PASS", "FAIL", "NOT_GRADABLE"}
        for probe in guarantee.probes:
            assert probe.before is not None and probe.after is not None
            assert isinstance(probe.before, EvidenceSnapshot)
            assert isinstance(probe.after, EvidenceSnapshot)
    assert ServerGrade.from_json(grade.to_json()).to_json() == grade.to_json()
    # Emit exact observations for the worker handoff without asserting a quota.
    print([(item.guarantee, item.status) for item in grade.guarantees])
    (tmp_path / "grade.json").write_text(grade.to_json(), encoding="utf-8")
    assert json.loads(grade.to_json())["release"]["version"] == "1.0.0"
