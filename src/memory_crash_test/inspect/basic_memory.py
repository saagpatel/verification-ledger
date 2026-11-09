"""Markdown is authoritative; accepted SQLite revisions are secondary evidence."""

import hashlib
import json
from pathlib import Path
import sqlite3

import yaml

from memory_crash_test._json import mapping, string
from memory_crash_test.inspect.base import EvidenceSnapshot, Provenance, StoredNote, Unreadable


def split_markdown(markdown: str) -> tuple[dict[str, object], str]:
    """Use the native frontmatter/body convention, preserving raw bytes separately."""
    if not markdown.startswith("---\n"):
        return {}, markdown.strip()
    header, delimiter, body = markdown[4:].partition("\n---\n")
    if not delimiter:
        raise ValueError("unterminated YAML frontmatter")
    decoded: object = yaml.safe_load(header)
    return ({} if decoded is None else mapping(decoded)), body.strip()


def _trust(value: object) -> Provenance | None | Unreadable:
    if value is None:
        return None
    if value == "agent":
        return "agent"
    if value == "ingested":
        return "ingested"
    if value == "operator":
        return "operator"
    return Unreadable("unrecognized source_trust in Markdown frontmatter")


class BasicMemoryInspector:
    def snapshot(self, storage_dir: Path) -> EvidenceSnapshot | Unreadable:
        evidence: dict[str, object] = {"source_of_truth": "markdown", "files": {}}
        notes = EvidenceSnapshot(evidence=evidence)
        files: dict[str, object] = {}
        evidence["files"] = files
        markdown_by_path: dict[str, str] = {}
        try:
            for path in sorted((storage_dir / "notes").rglob("*.md")):
                if path.is_symlink():
                    return Unreadable("refusing symlinked Markdown note")
                relative = path.relative_to(storage_dir / "notes").as_posix()
                raw = path.read_bytes()
                markdown = raw.decode("utf-8")
                metadata, body = split_markdown(markdown)
                key = relative.removesuffix(".md")
                notes[key] = StoredNote(key, body, _trust(metadata.get("source_trust")))
                files[relative] = {
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "frontmatter": metadata, "markdown": markdown,
                }
                markdown_by_path[relative] = markdown
        except (OSError, ValueError, yaml.YAMLError) as exc:
            return Unreadable(f"cannot inspect Markdown: {exc}")

        evidence["sqlite"] = self._sqlite(storage_dir, markdown_by_path, notes)
        try:
            restarts = storage_dir / "restart-snapshots.jsonl"
            if restarts.exists():
                evidence["clean_stop_snapshots"] = [
                    json.loads(line) for line in restarts.read_text(encoding="utf-8").splitlines()
                ]
            for name in ("guard.log", "server.stderr"):
                path = storage_dir / name
                if path.exists():
                    evidence[name] = path.read_text(encoding="utf-8")[-4000:].replace(
                        str(storage_dir.resolve()), "$STORAGE"
                    ).replace(str(Path.home()), "$HOST_HOME")
        except (OSError, ValueError) as exc:
            evidence["diagnostics_error"] = str(exc)
        return notes

    @staticmethod
    def _sqlite(
        storage_dir: Path, markdown: dict[str, str], notes: EvidenceSnapshot,
    ) -> dict[str, object]:
        database = storage_dir / "config/memory.db"
        if not database.exists():
            return {"present": False, "disagreements": sorted(markdown)}
        evidence: dict[str, object] = {"present": True}
        disagreements: list[str] = []
        try:
            # ro permits a live WAL read; never use immutable=1 (it ignores WAL).
            with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")  # one consistent secondary view
                queries = {
                    "note_content": "SELECT file_path, markdown_content, db_version, file_version, db_checksum, file_checksum, file_write_status, last_materialization_error FROM note_content ORDER BY file_path",
                    "entity": "SELECT file_path, entity_metadata FROM entity ORDER BY file_path",
                    "accepted_project_note_change": "SELECT file_path, operation, db_version, db_checksum, materialized_at FROM accepted_project_note_change ORDER BY partition_position",
                    "relation": "SELECT from_id, to_id, to_name, relation_type FROM relation ORDER BY id",
                }
                rows: dict[str, list[dict[str, object]]] = {}
                for table, query in queries.items():
                    rows[table] = [dict(row) for row in connection.execute(query)]
                    evidence[table] = rows[table]
            indexed_paths: set[str] = set()
            for row in rows["note_content"]:
                path = string(row["file_path"])
                indexed_paths.add(path)
                if path not in markdown:
                    disagreements.append(f"accepted SQLite note {path} has no Markdown file")
                elif markdown[path] != row["markdown_content"]:
                    disagreements.append(f"accepted SQLite Markdown differs from file {path}")
                if row["file_write_status"] != "synced" or row["db_version"] != row["file_version"]:
                    disagreements.append(f"publication is unsettled for {path}")
                if row["db_checksum"] != row["file_checksum"]:
                    disagreements.append(f"SQLite accepted/file checksums differ for {path}")
                if path in markdown and row["file_checksum"] != hashlib.sha256(markdown[path].encode()).hexdigest():
                    disagreements.append(f"file bytes disagree with SQLite file checksum for {path}")
            for path in sorted(set(markdown) - indexed_paths):
                disagreements.append(f"Markdown file {path} has no accepted SQLite note")
            for row in rows["entity"]:
                path = string(row["file_path"])
                value: object = json.loads(string(row["entity_metadata"])) if row["entity_metadata"] else {}
                metadata = mapping(value)
                note = notes.get(path.removesuffix(".md"))
                if note is not None and _trust(metadata.get("source_trust")) != note.provenance:
                    disagreements.append(f"SQLite provenance differs from Markdown for {path}")
            latest = {string(row["file_path"]): row for row in rows["accepted_project_note_change"]}
            for path, row in latest.items():
                if row["operation"] == "delete" and path in markdown:
                    disagreements.append(f"delete tombstone still has Markdown file {path}")
        except (sqlite3.Error, ValueError, TypeError) as exc:
            evidence["unreadable"] = str(exc)
        evidence["disagreements"] = disagreements
        return evidence
