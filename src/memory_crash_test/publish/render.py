"""Render crash-test grade cards and write them to a directory the caller names."""

from __future__ import annotations

import html
import json
import re
import shlex
from collections.abc import Sequence
from pathlib import Path

from memory_crash_test.adapters.base import ServerRelease
from memory_crash_test.grade.grader import GuaranteeGrade, ServerGrade
from memory_crash_test.inspect.base import Provenance, StorageSnapshot, Unreadable
from memory_crash_test.probes.vl import ProbeResult
from memory_crash_test.servers import SERVERS

TITLES: dict[str, str] = {
    "VL-1": "Untrusted text can't claim operator trust",
    "VL-2": "An agent can't promote its own notes",
    "VL-3": "A stranger's text can't become an order",
    "VL-4": "Saved notes never quietly vanish",
}

_UNSAFE_SEGMENT = re.compile(r"[^a-z0-9._-]+")

_CSS = """
:root {
  color-scheme: light dark;
  --bg: #f3f4f6;
  --card: #ffffff;
  --text: #1c1917;
  --muted: #57534e;
  --line: #d6d3d1;
  --pre: #f5f5f4;
  --pass: #166534;
  --fail: #b91c1c;
  --na: #57534e;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #171412;
    --card: #241f1c;
    --text: #fafaf9;
    --muted: #d6d3d1;
    --line: #44403c;
    --pre: #1c1917;
    --pass: #4ade80;
    --fail: #fca5a5;
    --na: #a8a29e;
  }
}
*, *::before, *::after { box-sizing: border-box; }
html { background: var(--bg); }
body {
  margin: 0 auto;
  max-width: 42rem;
  padding: 1rem;
  color: var(--text);
  background: var(--bg);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 1rem;
  line-height: 1.45;
}
h1, h2, h3, p { overflow-wrap: anywhere; }
h1 { margin: 0.2rem 0 0.8rem; font-size: 1.8rem; line-height: 1.15; }
h2 { margin: 0 0 0.75rem; font-size: 1.15rem; }
h3 { margin: 0; font-size: 1rem; }
a { color: inherit; }
a:focus-visible, summary:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }
.eyebrow, .code, dt {
  margin: 0;
  color: var(--muted);
  font-size: 0.75rem;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.mast { margin-bottom: 1.25rem; padding-bottom: 1rem; border-bottom: 3px solid var(--text); }
dl { margin: 0; }
dl div { margin: 0.45rem 0; }
dd { margin: 0; }
.rating {
  margin: 0 0 0.75rem;
  border: 1px solid var(--line);
  border-top-width: 4px;
  border-radius: 12px;
  background: var(--card);
}
.rating.pass { border-top-color: var(--pass); }
.rating.fail { border-top-color: var(--fail); }
.rating.na { border-top-color: var(--na); }
summary { padding: 0.9rem 1rem; cursor: pointer; }
.claim {
  display: block;
  margin-top: 0.35rem;
  font-size: 1.3rem;
  font-weight: 700;
  line-height: 1.25;
}
.why { display: block; margin-top: 0.35rem; color: var(--muted); }
.light {
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  font-weight: 700;
}
.lamp {
  width: 0.85rem;
  height: 0.85rem;
  border-radius: 50%;
  background: currentColor;
  box-shadow: inset 0 0 0 1px rgba(0, 0, 0, 0.35);
}
.light.pass { color: var(--pass); }
.light.fail { color: var(--fail); }
.light.na { color: var(--na); }
.probes { padding: 0 1rem 1rem; }
.probe {
  margin-top: 0.75rem;
  padding-top: 0.75rem;
  border-top: 1px solid var(--line);
}
.probe p, .index p { margin: 0.25rem 0; }
pre, code {
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.85rem;
}
pre {
  margin: 0.4rem 0 0;
  max-width: 100%;
  max-height: 16rem;
  overflow: auto;
  padding: 0.75rem;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  background: var(--pre);
  border-radius: 8px;
}
.calls { margin: 0.35rem 0 0; padding-left: 1.2rem; }
.footer {
  margin-top: 1.25rem;
  padding: 1rem;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--card);
}
.index { margin: 0; padding: 0; list-style: none; }
.index li {
  margin: 0 0 0.75rem;
  padding: 0.9rem 1rem;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--card);
}
"""


def render_card(grade: ServerGrade) -> str:
    """Return a self-contained HTML document for one server release."""
    rows = "\n".join(_guarantee_row(item) for item in grade.guarantees)
    body = "\n".join([_header(grade), "<section>", "<h2>Guarantees</h2>", rows, "</section>", _footer(grade)])
    title = f"{grade.release.name} {grade.release.version}"
    return _document(title, body)


def render_index(grades: Sequence[ServerGrade]) -> str:
    """Return an HTML index linking one card per grade."""
    if not grades:
        body = "<h1>Memory crash test grades</h1>\n<p>No grades to publish.</p>"
        return _document("Memory crash test grades", body)
    items = "\n".join(_index_item(grade) for grade in grades)
    body = "\n".join([
        "<h1>Memory crash test grades</h1>",
        "<p>One card per server release. Each grade is pinned to the release on its card.</p>",
        f"<ul class=\"index\">\n{items}\n</ul>",
    ])
    return _document("Memory crash test grades", body)


def load_grades(paths: Sequence[Path]) -> list[ServerGrade]:
    """Read grade reports from files, or from `grade.json` under directories."""
    grades: list[ServerGrade] = []
    for path in _grade_files(paths):
        try:
            grades.append(ServerGrade.from_json(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise ValueError(f"could not read grade {path}: {exc}") from exc
    return grades


def write_cards(grades: Sequence[ServerGrade], out: Path) -> None:
    """Write `<out>/<server-slug>/<version>/index.html` and `<out>/index.html`."""
    planned: dict[str, ServerGrade] = {}
    for grade in grades:
        href = _card_href(grade)
        previous = planned.get(href)
        if previous is not None:
            raise ValueError(
                f"multiple grades write {href}: "
                f"{previous.release.name} {previous.release.version} and "
                f"{grade.release.name} {grade.release.version}"
            )
        planned[href] = grade
    out.mkdir(parents=True, exist_ok=True)
    root = out.resolve()
    for href, grade in planned.items():
        destination = (out / href).resolve()
        if not destination.is_relative_to(root):
            raise ValueError(f"card path escapes the output directory: {href}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(render_card(grade), encoding="utf-8")
    index_grades = list(planned.values())
    (out / "index.html").write_text(render_index(index_grades), encoding="utf-8")


def _document(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_esc(title)}</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{body}\n"
        "</body>\n"
        "</html>\n"
    )


def _header(grade: ServerGrade) -> str:
    release = grade.release
    label = f"{release.name}, version {release.version}, source {release.source}"
    return "\n".join([
        '<header class="mast">',
        '<p class="eyebrow">Memory crash test</p>',
        f"<h1>{_esc(release.name)}</h1>",
        "<dl>",
        f"<div><dt>Release tested</dt><dd>{_esc(label)}</dd></div>",
        f"<div><dt>Tested</dt><dd>{_esc(grade.run_at_utc)} UTC</dd></div>",
        f"<div><dt>Suite version</dt><dd>{_esc(grade.suite_version)}</dd></div>",
        "</dl>",
        "</header>",
    ])


def _footer(grade: ServerGrade) -> str:
    if grade.maintainer_response is None:
        response = "Maintainer response: not yet requested"
    else:
        response = f"Maintainer response: {grade.maintainer_response}"
    return "\n".join([
        '<footer class="footer">',
        "<h2>Reproduce and respond</h2>",
        "<p>Rerun this grade locally:</p>",
        f"<pre>{_esc(_reproduce_command(grade))}</pre>",
        "<p>Every grade is pinned to the release shown and was produced only against "
        "a local instance on generated test data.</p>",
        f"<p>{_esc(response)}</p>",
        "</footer>",
    ])


def _guarantee_row(item: GuaranteeGrade) -> str:
    title = TITLES.get(item.guarantee, item.guarantee)
    reasons = "".join(
        f'<span class="why">{_esc(probe.name)}: {_esc(probe.reason)}</span>'
        for probe in item.probes
        if probe.status != "PASS" and probe.reason
    )
    probes = "\n".join(_probe_block(probe) for probe in item.probes) or "<p>No probes recorded.</p>"
    kind = _status_class(item.status)
    return "\n".join([
        f'<details id="{_esc_attr(item.guarantee)}" class="rating {kind}">',
        "<summary>",
        _status_chip(item.status),
        f'<span class="code">{_esc(item.guarantee)}</span>',
        f'<span class="claim">{_esc(title)}</span>',
        reasons,
        "</summary>",
        f'<div class="probes">\n{probes}\n</div>',
        "</details>",
    ])


def _probe_block(probe: ProbeResult) -> str:
    reason = probe.reason if probe.reason else "(none)"
    return "\n".join([
        '<article class="probe">',
        f"<h3>{_esc(probe.name)}</h3>",
        f"<p>Kind: {_esc(probe.kind)}</p>",
        f"<p>Result: {_status_chip(probe.status)}</p>",
        f"<p>Reason: {_esc(reason)}</p>",
        "<h4>MCP calls</h4>",
        _calls(probe),
        "<h4>Stored-state diff</h4>",
        f"<pre>{_esc(_diff_text(probe.before, probe.after))}</pre>",
        "<h4>Before</h4>",
        f"<pre>{_esc(_snapshot_text(probe.before))}</pre>",
        "<h4>After</h4>",
        f"<pre>{_esc(_snapshot_text(probe.after))}</pre>",
        "</article>",
    ])


def _calls(probe: ProbeResult) -> str:
    if not probe.calls:
        return "<p>No MCP calls recorded.</p>"
    items = "\n".join(
        f"<li><code>{_esc(call.tool_name)}</code> <code>{_esc(call.args)}</code></li>"
        for call in probe.calls
    )
    return f'<ul class="calls">\n{items}\n</ul>'


def _index_item(grade: ServerGrade) -> str:
    release = grade.release
    href = _card_href(grade)
    statuses = ", ".join(f"{item.guarantee} {item.status}" for item in grade.guarantees)
    return "\n".join([
        "<li>",
        f'<a href="{_esc_attr(href)}">{_esc(release.name)} {_esc(release.version)}</a>',
        f"<p>{_esc(release.name)}, version {_esc(release.version)}, source {_esc(release.source)}</p>",
        f"<p>{_esc(statuses)}</p>",
        "</li>",
    ])


def _status_chip(status: str) -> str:
    kind = _status_class(status)
    return (
        f'<span class="light {kind}"><span class="lamp" aria-hidden="true"></span>{_esc(status)}</span>'
    )


def _status_class(status: str) -> str:
    if status == "PASS":
        return "pass"
    if status == "FAIL":
        return "fail"
    return "na"


def _diff_text(before: StorageSnapshot | Unreadable, after: StorageSnapshot | Unreadable) -> str:
    if isinstance(before, Unreadable) or isinstance(after, Unreadable):
        lines = ["Stored-state diff unavailable because a snapshot is unreadable."]
        if isinstance(before, Unreadable):
            lines.append(f"before: {before.reason}")
        if isinstance(after, Unreadable):
            lines.append(f"after: {after.reason}")
        return "\n".join(lines)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(key for key in set(before) & set(after) if before[key] != after[key])
    lines = [
        "added: " + (", ".join(added) if added else "(none)"),
        "removed: " + (", ".join(removed) if removed else "(none)"),
        "changed: " + (", ".join(changed) if changed else "(none)"),
    ]
    for key in changed:
        lines.append(f"{key} text: {before[key].text} → {after[key].text}")
        lines.append(
            f"{key} provenance: {_provenance_text(before[key].provenance)} → "
            f"{_provenance_text(after[key].provenance)}"
        )
    return "\n".join(lines)


def _snapshot_text(snapshot: StorageSnapshot | Unreadable) -> str:
    return json.dumps(_snapshot_data(snapshot), indent=2, sort_keys=True, ensure_ascii=False)


def _snapshot_data(snapshot: StorageSnapshot | Unreadable) -> dict[str, object]:
    if isinstance(snapshot, Unreadable):
        return {"unreadable": snapshot.reason}
    notes: dict[str, object] = {}
    for key, note in snapshot.items():
        provenance: object
        if isinstance(note.provenance, Unreadable):
            provenance = {"unreadable": note.provenance.reason}
        else:
            provenance = note.provenance
        notes[key] = {"key": note.key, "text": note.text, "provenance": provenance}
    return {"notes": notes}


def _provenance_text(provenance: Provenance | None | Unreadable) -> str:
    if isinstance(provenance, Unreadable):
        return f"unreadable ({provenance.reason})"
    if provenance is None:
        return "null"
    return provenance


def _reproduce_command(grade: ServerGrade) -> str:
    server = shlex.quote(_cli_server_name(grade.release))
    return f"python -m memory_crash_test grade --server {server} --out ./result"


def _cli_server_name(release: ServerRelease) -> str:
    matches = [
        name
        for name, binding in SERVERS.items()
        if binding.adapter_factory().release == release
    ]
    if len(matches) == 1:
        return matches[0]
    return release.name


def _card_href(grade: ServerGrade) -> str:
    return f"{_segment(grade.release.name)}/{_segment(grade.release.version)}/index.html"


def _segment(value: str) -> str:
    slug = _UNSAFE_SEGMENT.sub("-", value.strip().lower()).strip("-.")
    if slug in {"", ".", ".."}:
        raise ValueError(f"cannot publish a card path for {value!r}")
    return slug


def _grade_files(paths: Sequence[Path]) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        found.append(path)

    for path in paths:
        if path.is_file():
            add(path)
            continue
        if not path.is_dir():
            raise ValueError(f"grade path not found: {path}")
        matches = sorted(item for item in path.rglob("grade.json") if item.is_file())
        if not matches:
            raise ValueError(f"no grade.json under {path}")
        for match in matches:
            add(match)
    if not found:
        raise ValueError("no grade files given")
    return found


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _esc_attr(text: str) -> str:
    return html.escape(text, quote=True)
