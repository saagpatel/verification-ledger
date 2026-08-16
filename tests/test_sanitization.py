"""Sanitization guard — a hard CI gate against leaking private operator data.

The Verification Ledger is a clean-room distillation. It must never reference the
operator's live coordination store, its private fleet system names, or its auth
material. If this test fails, a leak was introduced — fix the leak, do not weaken
the guard.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Leak indicators: the live database path/name, private fleet system names, and
# auth material belonging to the private reference store this project distilled.
FORBIDDEN: tuple[str, ...] = (
    ".local/share/bridge-db",
    "bridge.db",
    "notion_os",
    "notion-os",
    "personal_ops",
    "personal-ops",
    "githubrepoauditor",
    "principals.json",
    "principal_token",
)

_SCAN_SUFFIXES = frozenset(
    {".py", ".md", ".toml", ".yml", ".yaml", ".txt", ".sh", ".json", ".cfg", ".ini", ""}
)
_SKIP_DIRS = frozenset(
    {".git", ".venv", "__pycache__", ".ruff_cache", ".pytest_cache", "dist", "build"}
)
_SELF = Path(__file__).resolve()


def _scannable_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS or part.endswith(".egg-info") for part in path.parts):
            continue
        if path.suffix not in _SCAN_SUFFIXES:
            continue
        if path.resolve() == _SELF:
            continue  # this guard names the forbidden strings as data
        files.append(path)
    return files


def test_no_private_data_in_repo() -> None:
    offenders: list[str] = []
    for path in _scannable_files():
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for needle in FORBIDDEN:
            if needle in text:
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}: contains forbidden token {needle!r}")
    assert not offenders, "sanitization guard tripped:\n" + "\n".join(offenders)


def test_guard_actually_scans_files() -> None:
    # An empty scan always passes and would silently disable this gate.
    files = _scannable_files()
    assert len(files) >= 5, f"expected to scan the repo; only found {len(files)} files"
