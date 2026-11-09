"""Pinned Basic Memory 1.0.0, using its native local bm_* tools unchanged."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import TextIO

from mcp.types import CallToolResult, TextContent

from memory_crash_test._json import mapping, sequence, string
from memory_crash_test.adapters.base import (
    EVIDENCE_LIMIT, CallRecord, Provenance, ReadResult, ServerRelease, UnsupportedCapability,
)
from memory_crash_test.inspect.base import EvidenceSnapshot
from memory_crash_test.inspect.basic_memory import split_markdown
from memory_crash_test.mcp_client import MCPClient

PIN = "1.0.0"


def install() -> Path:
    """Reuse the pinned cache; only a missing executable triggers installation."""
    root = Path(os.environ.get("MCT_CACHE", "~/.cache/memory-crash-test")).expanduser().resolve()
    venv = root / "basic-memory" / PIN
    if not (venv / "bin/basic-memory").is_file():
        env = {**os.environ, "UV_CACHE_DIR": str(root / "uv"), "UV_PYTHON_DOWNLOADS": "never"}
        subprocess.run(
            ["uv", "venv", str(venv), "--python", sys.executable],
            env=env, check=True, timeout=60,
        )
        subprocess.run([
            "uv", "pip", "install", "--python", str(venv / "bin/python"),
            "--index-url", "https://pypi.org/simple", f"basic-memory=={PIN}",
        ], env=env, check=True, timeout=300)
    return venv


# Python audit guards supplement the enclosing filesystem sandbox. Native code
# may bypass audit hooks; this is not a claim of an OS-level security boundary.
_BOOTSTRAP = '''import os
from pathlib import Path
import sys

root = Path(os.environ["MCT_STORAGE_ROOT"]).resolve()
log = open(root / "guard.log", "a", encoding="utf-8")

def check_path(value):
    if isinstance(value, int):
        return
    path = Path(os.fsdecode(value)).resolve()
    if path != Path("/dev/null") and not path.is_relative_to(root):
        log.write("DENY_WRITE " + str(path) + "\\n")
        log.flush()
        raise PermissionError("write outside MCT storage")

def audit(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.bind", "subprocess.Popen"}:
        log.write("DENY_NETWORK_OR_PROCESS " + event + "\\n")
        log.flush()
        raise PermissionError("local stdio probe forbids network and subprocesses")
    if event == "open":
        mode, flags = args[1], args[2]
        if (isinstance(mode, str) and any(c in mode for c in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            check_path(args[0])
    elif event == "sqlite3.connect":
        check_path(args[0])
    elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime", "os.truncate"}:
        check_path(args[0])
    elif event in {"os.rename", "os.link", "os.symlink"}:
        check_path(args[0])
        check_path(args[1])

sys.addaudithook(audit)
import onnxruntime
onnxruntime.disable_telemetry_events()
'''


class BasicMemoryAdapter:
    def __init__(self) -> None:
        self._venv = install()
        metadata = subprocess.run([
            str(self._venv / "bin/python"), "-I", "-B", "-c",
            "from importlib.metadata import version; print(version('basic-memory'))",
        ], check=True, capture_output=True, text=True, timeout=20)
        version = metadata.stdout.strip()
        if version != PIN:
            raise ValueError(f"cached basic-memory distribution is {version!r}, expected {PIN}")
        self.release = ServerRelease("basic-memory", version, "https://pypi.org/project/basic-memory/1.0.0/")
        self._client: MCPClient | None = None
        self._storage_dir: Path | None = None
        self._env: dict[str, str] = {}
        self._calls: list[CallRecord] = []
        self._stderr: TextIO | None = None

    def start(self, storage_dir: Path) -> None:
        if self._client is not None:
            raise RuntimeError("server already started")
        self._calls.clear()
        self._storage_dir = root = storage_dir.resolve()
        for name in ("home", "config", "cache", "data", "tmp", "notes", "bootstrap"):
            (root / name).mkdir(parents=True, exist_ok=True)
        config = root / "config/config.json"
        if not config.exists():
            config.write_text(json.dumps({
                "projects": {"main": {"path": str(root / "notes"), "mode": "local"}},
                "default_project": "main", "auto_update": False,
                "logfire_enabled": False, "logfire_send_to_logfire": False,
                "reranker_enabled": False, "search_default_mode": "text",
            }), encoding="utf-8")
        (root / "bootstrap/sitecustomize.py").write_text(_BOOTSTRAP, encoding="utf-8")
        self._env = {
            "PATH": os.defpath, "HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
            "CFFIXED_USER_HOME": str(root / "home"),
            "DARWIN_USER_CACHE_DIR": str(root / "cache"),
            "DARWIN_USER_TEMP_DIR": str(root / "tmp"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_DATA_HOME": str(root / "data"), "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(root / "bootstrap"), "MCT_STORAGE_ROOT": str(root),
            "BASIC_MEMORY_CONFIG_DIR": str(root / "config"),
            "BASIC_MEMORY_HOME": str(root / "notes"), "BASIC_MEMORY_FORCE_LOCAL": "true",
            "BASIC_MEMORY_AUTO_UPDATE": "false", "BASIC_MEMORY_LOGFIRE_ENABLED": "false",
            "BASIC_MEMORY_LOGFIRE_SEND_TO_LOGFIRE": "false",
            "BASIC_MEMORY_CLOUD_PROMO_OPT_OUT": "true", "BASIC_MEMORY_INDEX_CHANGES": "true",
            "FASTMCP_HOME": str(root / "home/.fastmcp"), "FASTMCP_CHECK_FOR_UPDATES": "off",
            "FASTMCP_SHOW_SERVER_BANNER": "false", "HF_HUB_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1", "HF_HOME": str(root / "cache/huggingface"),
            "FASTEMBED_CACHE_PATH": str(root / "cache/fastembed"),
            "OTEL_SDK_DISABLED": "true", "DO_NOT_TRACK": "1",
        }
        self._launch()

    def _launch(self) -> None:
        assert self._storage_dir is not None
        self._stderr = (self._storage_dir / "server.stderr").open("a", encoding="utf-8")
        try:
            self._client = MCPClient(
                str(self._venv / "bin/basic-memory"),
                ["mcp", "--transport", "stdio", "--project", "main"],
                env=self._env, cwd=self._storage_dir, timeout=60,
                shutdown_timeout=10, errlog=self._stderr,
            )
        except BaseException:
            self._stderr.close()
            raise

    def stop(self) -> None:
        client, self._client = self._client, None
        try:
            if client is not None:
                client.close()
        finally:
            if self._stderr is not None:
                self._stderr.close()
                self._stderr = None

    def restart(self) -> None:
        self.stop()
        # Preserve stopped disk evidence before startup's native scan can act.
        from memory_crash_test.inspect.basic_memory import BasicMemoryInspector

        assert self._storage_dir is not None
        snapshot = BasicMemoryInspector().snapshot(self._storage_dir)
        if isinstance(snapshot, EvidenceSnapshot):
            snapshot.evidence.pop("clean_stop_snapshots", None)
        from memory_crash_test.grade.grader import snapshot_json

        with (self._storage_dir / "restart-snapshots.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(snapshot_json(snapshot), sort_keys=True) + "\n")
        self._launch()

    @staticmethod
    def _identifier(key: str) -> str:
        if re.fullmatch(r"[a-z0-9-]+", key) is None:
            raise ValueError("note keys must be safe generated slugs")
        return f"{key}.md"

    def _call(self, name: str, args: dict[str, object]) -> CallToolResult:
        args = {**args, "project": "main", "output_format": "json"}
        self._calls.append(CallRecord(name, json.dumps(args, sort_keys=True)[:EVIDENCE_LIMIT]))
        if self._client is None:
            raise RuntimeError("server is stopped")
        return self._client.call_tool(name, args)

    @staticmethod
    def _payload(result: CallToolResult) -> dict[str, object]:
        if result.structuredContent is not None:
            payload = mapping(result.structuredContent)
        else:
            blocks = [block.text for block in result.content if isinstance(block, TextContent)]
            if len(blocks) != 1:
                raise ValueError("expected one JSON tool response")
            payload = mapping(json.loads(blocks[0]))
        if "result" in payload:
            payload = mapping(payload["result"])
        return payload

    def write_note(self, key: str, text: str, *, provenance: Provenance) -> bool:
        return not self._call("bm_write", {
            "identifier": self._identifier(key), "title": key, "content": text,
            "metadata": {"source_trust": provenance, "embed": False}, "tags": ["durable"],
        }).isError

    def update_provenance(self, key: str, provenance: Provenance) -> bool:
        return not self._call("bm_edit", {
            "identifier": self._identifier(key), "command": "set_metadata",
            "metadata": {"source_trust": provenance},
        }).isError

    def operator_promote(self, key: str) -> bool:
        raise UnsupportedCapability("basic-memory 1.0.0 has no native out-of-band operator ceremony")

    def read_note(self, key: str) -> ReadResult | None:
        result = self._call("bm_read", {"identifier": self._identifier(key)})
        payload = self._payload(result)
        if result.isError:
            if payload.get("error") == "NOT_FOUND":
                return None
            raise RuntimeError(f"bm_read returned an error: {payload}")
        _, body = split_markdown(string(payload["content"]))
        return ReadResult(body, None, result.model_dump_json()[:EVIDENCE_LIMIT])

    def list_keys(self) -> list[str]:
        keys: list[str] = []
        page = 1
        while True:
            result = self._call("bm_find", {"name": "*.md", "page": page, "page_size": 100})
            if result.isError:
                raise RuntimeError(f"bm_find returned an error: {result.model_dump_json()[:EVIDENCE_LIMIT]}")
            payload = self._payload(result)
            for value in sequence(payload["nodes"]):
                node = mapping(value)
                if node.get("type") == "file":
                    path = string(node["file_path"])
                    # The fixed project contains only suite-generated root notes.
                    if path.startswith("main/"):
                        path = path.removeprefix("main/")
                    key = path.removesuffix(".md")
                    self._identifier(key)
                    keys.append(key)
            if payload.get("has_more") is False:
                return keys
            if payload.get("has_more") is not True or not sequence(payload["nodes"]):
                raise ValueError("invalid bm_find pagination")
            page += 1

    def delete_note(self, key: str) -> bool:
        return not self._call("bm_delete", {"identifier": self._identifier(key)}).isError

    def calls(self) -> list[CallRecord]:
        return list(self._calls)
