# Packet 07 — Grade-card publisher (static HTML)

## Goal
Render a crash-test-style grade card for each `grade.json` the grader writes (one card per server and release), as static, self-contained HTML. Read `docs/memory-crash-test.md` for the grade JSON schema and `src/memory_crash_test/grade/grader.py` for `ServerGrade.from_json`.

## Scope
- `src/memory_crash_test/publish/`: `render_card(grade: ServerGrade) -> str` (a full HTML document) and `render_index(grades) -> str`. Add a CLI subcommand: `python -m memory_crash_test publish --grades <dir-or-files...> --out <dir>` writes `<out>/<server-slug>/<version>/index.html` per grade plus `<out>/index.html`.
- Card design, in the style of a car crash-test rating: a header with the server name, the **exact release tested** (name, version, source), the test date (UTC), and the suite version. Then four large rating rows, one per guarantee, using the plain-language titles:
  - VL-1 "Untrusted text can't claim operator trust"
  - VL-2 "An agent can't promote its own notes"
  - VL-3 "A stranger's text can't become an order"
  - VL-4 "Saved notes never quietly vanish"

  Each row shows a status light (PASS green, FAIL red, NOT_GRADABLE grey, with the text label too, never colour alone) and the failing probe's reason. Each row expands (`<details>`) to every probe: its name, kind (positive/adversarial), result, reason, the MCP calls made, and a **before/after stored-state diff** (keys added, removed, or changed, plus the full snapshots in a scrollable `<pre>`).
- A footer section, "Reproduce and respond": the exact command to rerun this grade locally, a note that every grade is pinned to the release shown and was produced only against a local instance on generated test data, and a maintainer-response line. It reads "Maintainer response: not yet requested" unless the grade JSON carries a `maintainer_response` field (add it as an optional, backward-compatible field on `ServerGrade` with a default of `None`, and document it).
- All text HTML-escaped. Stored note text is untrusted data, and the VL-3 payload contains instruction-like text, so it must render as inert escaped text. Add a test that a payload containing `<script>` renders escaped.
- No external assets: inline CSS, light and dark (`prefers-color-scheme`), system fonts, readable at 375 px wide.
- Generated output goes to a directory the user names. Add `out/` and `cards/` to `.gitignore`. Never commit rendered cards.
- Tests in `tests/crash/test_publish.py`: build `ServerGrade` objects in-process (PASS, FAIL and NOT_GRADABLE rows), and assert that the release label, all four titles, the status text, the before/after diff and the reproduce command appear, plus the escaping test and the index linking each card.

## Out of scope
Hosting or deploying anywhere. Changes to probes or grader beyond the optional `maintainer_response` field. Do not touch `src/verification_ledger/`.

## Acceptance
```
uv run ruff check
uv run pyright
uv run pytest -q
uv run python -m memory_crash_test grade --server reference --out "$TMPDIR/g" && uv run python -m memory_crash_test publish --grades "$TMPDIR/g" --out "$TMPDIR/cards" && test -s "$TMPDIR/cards/index.html"
```
