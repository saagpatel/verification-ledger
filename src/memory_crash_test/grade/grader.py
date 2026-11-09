"""Fresh-process probe execution and the versioned grade JSON contract."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from memory_crash_test import SUITE_VERSION
from memory_crash_test._json import mapping, sequence, string
from memory_crash_test.adapters.base import CallRecord, MemoryServerAdapter, Provenance, ServerRelease
from memory_crash_test.inspect.base import (
    EvidenceSnapshot, GraphSnapshot, StorageInspector, StorageSnapshot, StoredNote, Unreadable,
)
from memory_crash_test.probes.vl import (
    GUARANTEES, Guarantee, ProbeKind, ProbeResult, Status, all_probes, safe_snapshot,
)


@dataclass(frozen=True)
class GuaranteeGrade:
    guarantee: Guarantee
    status: Status
    probes: list[ProbeResult]


@dataclass(frozen=True)
class ServerGrade:
    release: ServerRelease
    guarantees: list[GuaranteeGrade]
    run_at_utc: str
    suite_version: str
    # Omitted from JSON when None so reports written before this field still parse.
    maintainer_response: str | None = None

    def to_json(self) -> str:
        payload: dict[str, object] = {
            "release": {"name": self.release.name, "version": self.release.version, "source": self.release.source},
            "guarantees": [{
                "guarantee": grade.guarantee, "status": grade.status,
                "probes": [_probe_json(probe) for probe in grade.probes],
            } for grade in self.guarantees],
            "run_at_utc": self.run_at_utc,
            "suite_version": self.suite_version,
        }
        if self.maintainer_response is not None:
            payload["maintainer_response"] = self.maintainer_response
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> ServerGrade:
        decoded: object = json.loads(text)
        root = mapping(decoded)
        release = mapping(root["release"])
        guarantees: list[GuaranteeGrade] = []
        for value in sequence(root["guarantees"]):
            item = mapping(value)
            guarantees.append(GuaranteeGrade(
                _guarantee(item["guarantee"]), _status(item["status"]),
                [_parse_probe(probe) for probe in sequence(item["probes"])],
            ))
        response = string(root["maintainer_response"]) if "maintainer_response" in root else None
        return cls(
            ServerRelease(string(release["name"]), string(release["version"]), string(release["source"])),
            guarantees, string(root["run_at_utc"]), string(root["suite_version"]), response,
        )


def grade_server(
    adapter_factory: Callable[[], MemoryServerAdapter], inspector: StorageInspector,
    *, temp_root: Path | None = None,
) -> ServerGrade:
    """Run every probe in isolation. Unreadable evidence takes grade precedence."""
    results: list[ProbeResult] = []
    release: ServerRelease | None = None
    for probe in all_probes():
        adapter = adapter_factory()
        if release is None:
            release = adapter.release
        with TemporaryDirectory(prefix="memory-crash-test-", dir=temp_root) as temporary:
            storage_dir = Path(temporary)
            before = safe_snapshot(inspector, storage_dir)
            result: ProbeResult | None = None
            try:
                if adapter.release != release:
                    raise ValueError("adapter factory changed the pinned server release")
                adapter.start(storage_dir)
                result = probe.run(adapter, inspector, storage_dir)
            except Exception as exc:
                result = ProbeResult(
                    probe.guarantee, probe.kind, probe.name, "FAIL", before,
                    safe_snapshot(inspector, storage_dir), adapter.calls(),
                    _one_line(f"probe lifecycle raised {type(exc).__name__}: {exc}"),
                )
            finally:
                try:
                    adapter.stop()
                except Exception as exc:
                    assert result is not None
                    result = replace(result, status="FAIL", reason=_one_line(
                        f"{result.reason}; stop raised {type(exc).__name__}: {exc}"
                    ).lstrip("; "))
            assert result is not None
            results.append(result)
    assert release is not None
    grades: list[GuaranteeGrade] = []
    for guarantee in GUARANTEES:
        probes = [result for result in results if result.guarantee == guarantee]
        status: Status = "PASS"
        if any(result.status == "NOT_GRADABLE" for result in probes):
            status = "NOT_GRADABLE"
        elif any(result.status == "FAIL" for result in probes):
            status = "FAIL"
        grades.append(GuaranteeGrade(guarantee, status, probes))
    return ServerGrade(release, grades, datetime.now(timezone.utc).isoformat(), SUITE_VERSION)


def _one_line(text: str) -> str:
    return " ".join(text.splitlines())


def snapshot_json(snapshot: StorageSnapshot | Unreadable) -> dict[str, object]:
    if isinstance(snapshot, Unreadable):
        return {"unreadable": snapshot.reason}
    notes: dict[str, object] = {}
    for key, note in snapshot.items():
        provenance = note.provenance
        notes[key] = {
            "key": note.key, "text": note.text,
            "provenance": {"unreadable": provenance.reason} if isinstance(provenance, Unreadable) else provenance,
        }
    result: dict[str, object] = {"notes": notes}
    if isinstance(snapshot, GraphSnapshot):
        result["relations"] = snapshot.relations
        if snapshot.evidence is not None:
            result["evidence"] = snapshot.evidence
    elif isinstance(snapshot, EvidenceSnapshot):
        result["evidence"] = snapshot.evidence
    return result


def _probe_json(probe: ProbeResult) -> dict[str, object]:
    return {
        "guarantee": probe.guarantee, "kind": probe.kind, "name": probe.name,
        "status": probe.status, "reason": probe.reason,
        "before": snapshot_json(probe.before), "after": snapshot_json(probe.after),
        "calls": [{"tool_name": call.tool_name, "args": call.args} for call in probe.calls],
    }


def _parse_snapshot(value: object) -> StorageSnapshot | Unreadable:
    data = mapping(value)
    if "unreadable" in data:
        return Unreadable(string(data["unreadable"]))
    notes: StorageSnapshot = (
        EvidenceSnapshot(evidence=mapping(data["evidence"])) if "evidence" in data else {}
    )
    for key, item in mapping(data["notes"]).items():
        note = mapping(item)
        provenance = note["provenance"]
        stored_provenance = (
            None if provenance is None else provenance if provenance in ("operator", "agent", "ingested")
            else Unreadable(string(mapping(provenance)["unreadable"]))
        )
        # Use the literal parser even after membership testing to narrow unknown JSON.
        notes[key] = StoredNote(
            string(note["key"]), string(note["text"]),
            stored_provenance if stored_provenance is None or isinstance(stored_provenance, Unreadable)
            else _provenance(stored_provenance),
        )
    if "relations" in data:
        relations = [{key: string(val) for key, val in mapping(item).items()}
                     for item in sequence(data["relations"])]
        evidence = notes.evidence if isinstance(notes, EvidenceSnapshot) else None
        return GraphSnapshot(notes, relations, evidence=evidence)
    return notes


def _provenance(value: object) -> Provenance:
    if value == "operator":
        return "operator"
    if value == "agent":
        return "agent"
    if value == "ingested":
        return "ingested"
    raise ValueError("invalid provenance")


def _guarantee(value: object) -> Guarantee:
    if value == "VL-1":
        return "VL-1"
    if value == "VL-2":
        return "VL-2"
    if value == "VL-3":
        return "VL-3"
    if value == "VL-4":
        return "VL-4"
    raise ValueError("invalid guarantee")


def _status(value: object) -> Status:
    if value == "PASS":
        return "PASS"
    if value == "FAIL":
        return "FAIL"
    if value == "NOT_GRADABLE":
        return "NOT_GRADABLE"
    raise ValueError("invalid status")


def _kind(value: object) -> ProbeKind:
    if value == "positive":
        return "positive"
    if value == "adversarial":
        return "adversarial"
    raise ValueError("invalid probe kind")


def _parse_probe(value: object) -> ProbeResult:
    data = mapping(value)
    calls: list[CallRecord] = []
    for call in sequence(data["calls"]):
        item = mapping(call)
        calls.append(CallRecord(string(item["tool_name"]), string(item["args"])))
    return ProbeResult(
        _guarantee(data["guarantee"]), _kind(data["kind"]), string(data["name"]),
        _status(data["status"]), _parse_snapshot(data["before"]), _parse_snapshot(data["after"]),
        calls, string(data["reason"]),
    )
