"""Native behavior over local stdio; default test runs remain offline."""

import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from memory_crash_test._json import mapping
from memory_crash_test.adapters.base import CallRecord, Provenance
from memory_crash_test.adapters.basic_memory import BasicMemoryAdapter
from memory_crash_test.grade.grader import ServerGrade, grade_server
from memory_crash_test.inspect.base import EvidenceSnapshot, StoredNote, Unreadable
from memory_crash_test.inspect.basic_memory import BasicMemoryInspector
from memory_crash_test.probes.vl import all_probes


@pytest.fixture
def sample_project(tmp_path: Path) -> Path:
    shutil.copytree(Path(__file__).parent / "data/basic-memory", tmp_path, dirs_exist_ok=True)
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
        """)
    return tmp_path


def test_inspector_uses_files_and_records_secondary_disagreement(sample_project: Path) -> None:
    tmp_path = sample_project
    inspector = BasicMemoryInspector()
    snapshot = inspector.snapshot(tmp_path)
    assert isinstance(snapshot, EvidenceSnapshot)
    assert snapshot["alpha"] == StoredNote(
        "alpha", "First test note\n\n- references [[beta]]", "agent",
    )
    # A tag/body claim is not the designated provenance field.
    assert snapshot["beta"].provenance is None

    with sqlite3.connect(tmp_path / "config/memory.db") as connection:
        connection.executescript("""
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


def test_accepted_sqlite_operator_outranks_markdown_agent(sample_project: Path) -> None:
    """An accepted SQLite revision can outrank a Markdown file that has not caught up."""
    tmp_path = sample_project
    file_text = (tmp_path / "notes/alpha.md").read_text(encoding="utf-8")
    accepted = file_text.replace("source_trust: agent", "source_trust: operator", 1)
    accepted = accepted.replace("First test note", "accepted sqlite body", 1)
    with sqlite3.connect(tmp_path / "config/memory.db") as connection:
        connection.execute(
            """INSERT INTO note_content VALUES
                ('alpha.md', ?, 2, 1, 'accepted', 'file', 'pending', NULL)""",
            (accepted,),
        )
        connection.execute(
            """INSERT INTO accepted_project_note_change VALUES
                ('alpha.md', 'update', 2, 'accepted', NULL, 1)"""
        )
        connection.execute(
            "INSERT INTO entity VALUES ('alpha.md', ?)",
            ('{"source_trust":"agent"}',),
        )
    snapshot = BasicMemoryInspector().snapshot(tmp_path)
    assert isinstance(snapshot, EvidenceSnapshot)
    assert set(snapshot) == {"alpha", "beta"}
    assert snapshot["alpha"].provenance == "operator"
    assert snapshot["alpha"].text == "First test note\n\n- references [[beta]]"
    assert snapshot["beta"].provenance is None
    secondary = snapshot.evidence["sqlite"]
    assert isinstance(secondary, dict)
    assert secondary["disagreements"] == [
        "accepted SQLite Markdown differs from file alpha.md",
        "publication is unsettled for alpha.md",
        "SQLite accepted/file checksums differ for alpha.md",
        "file bytes disagree with SQLite file checksum for alpha.md",
        "Markdown file beta.md has no accepted SQLite note",
        "accepted SQLite provenance differs from Markdown for alpha.md",
    ]


class _SampleWriteAdapter(BasicMemoryAdapter):
    """Only the positive probe's write/calls path; no native server is started."""

    def __init__(self, storage_dir: Path, accepted_content: str | bytes | None) -> None:
        self._sample_dir = storage_dir
        self._accepted_content = accepted_content
        self._calls: list[CallRecord] = []

    def write_note(self, key: str, text: str, *, provenance: Provenance) -> bool:
        self._calls.append(CallRecord("write_note", key))
        (self._sample_dir / f"notes/{key}.md").write_text(
            f"---\nsource_trust: {provenance}\n---\n{text}", encoding="utf-8",
        )
        with sqlite3.connect(self._sample_dir / "config/memory.db") as connection:
            connection.execute(
                "INSERT INTO note_content VALUES (?, ?, 1, 1, NULL, NULL, 'synced', NULL)",
                (f"{key}.md", self._accepted_content),
            )
        return True


@pytest.mark.parametrize("accepted_content", [
    b"non-text accepted content", None,
    "---\nsource_trust: [\n---\nbody",
    "---\nsource_trust: unknown\n---\nbody",
])
def test_unreadable_accepted_provenance_is_not_gradable(
    sample_project: Path, accepted_content: str | bytes | None,
) -> None:
    inspector = BasicMemoryInspector()
    probe = next(probe for probe in all_probes() if probe.name == "agent provenance preserved")
    result = probe.run(_SampleWriteAdapter(sample_project, accepted_content), inspector, sample_project)
    assert result.status == "NOT_GRADABLE"
    snapshot = result.after
    assert isinstance(snapshot, EvidenceSnapshot)
    assert set(snapshot) == {"alpha", "beta", "note"}
    assert snapshot["note"].text == "synthetic note"
    assert isinstance(snapshot["note"].provenance, Unreadable)
    assert " ".join(snapshot["note"].provenance.reason.splitlines()) == result.reason
    assert snapshot["alpha"].provenance == "agent"
    assert snapshot["beta"].provenance is None


def test_unreadable_markdown_provenance_outranks_sqlite_operator(sample_project: Path) -> None:
    path = sample_project / "notes/alpha.md"
    markdown = path.read_text(encoding="utf-8")
    with sqlite3.connect(sample_project / "config/memory.db") as connection:
        connection.execute(
            "INSERT INTO note_content VALUES ('alpha.md', ?, 1, 1, NULL, NULL, 'synced', NULL)",
            (markdown.replace("source_trust: agent", "source_trust: operator"),),
        )
    path.write_text(markdown.replace("source_trust: agent", "source_trust: unknown"), encoding="utf-8")
    snapshot = BasicMemoryInspector().snapshot(sample_project)
    assert isinstance(snapshot, EvidenceSnapshot)
    assert isinstance(snapshot["alpha"].provenance, Unreadable)
    assert "Markdown" in snapshot["alpha"].provenance.reason


@pytest.mark.parametrize("failure", ["missing", "read-error", "query", "metadata"])
def test_sqlite_failure_makes_all_provenance_unreadable(sample_project: Path, failure: str) -> None:
    inspector = BasicMemoryInspector()
    before = inspector.snapshot(sample_project)
    assert isinstance(before, EvidenceSnapshot)
    database = sample_project / "config/memory.db"
    if failure == "missing":
        database.unlink()
    elif failure == "read-error":
        database.write_bytes(b"not a SQLite database")
    else:
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO note_content VALUES ('alpha.md', ?, 1, 1, NULL, NULL, 'synced', NULL)",
                ("---\nsource_trust: operator\n---\naccepted body",),
            )
            if failure == "query":
                connection.execute("DROP TABLE relation")
            else:
                # A late failure must override provenance already merged from note_content.
                connection.execute("INSERT INTO entity VALUES ('alpha.md', 'invalid JSON')")
    snapshot = inspector.snapshot(sample_project)
    assert isinstance(snapshot, EvidenceSnapshot)
    assert set(snapshot) == set(before)
    secondary = mapping(snapshot.evidence["sqlite"])
    reason = secondary["unreadable"]
    assert isinstance(reason, str) and reason
    for key, note in snapshot.items():
        assert note.key == before[key].key
        assert note.text == before[key].text
        assert note.provenance == Unreadable(reason)
    if failure == "query":
        assert "no such table: relation" in reason


def test_no_database_before_start_is_an_empty_readable_snapshot(tmp_path: Path) -> None:
    snapshot = BasicMemoryInspector().snapshot(tmp_path)
    assert isinstance(snapshot, EvidenceSnapshot)
    assert snapshot == {}
    assert snapshot.evidence["sqlite"] == {"present": False, "disagreements": []}
    assert not (tmp_path / "config/memory.db").exists()


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
