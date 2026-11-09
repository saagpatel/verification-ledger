"""MCP-only in-band operations, plus a separate operator CLI process."""

import json
import subprocess
import sys
from pathlib import Path

from mcp.types import TextContent

from memory_crash_test._json import mapping, sequence, string
from memory_crash_test.adapters.base import (
    EVIDENCE_LIMIT, CallRecord, Provenance, ReadResult, ServerRelease,
)
from memory_crash_test.fixtures.reference_server import VERSION
from memory_crash_test.mcp_client import MCPClient


class ReferenceAdapter:
    release = ServerRelease("memory-crash-test-reference", VERSION, "fixture")

    def __init__(self) -> None:
        self._client: MCPClient | None = None
        self._storage_dir: Path | None = None
        self._calls: list[CallRecord] = []

    def start(self, storage_dir: Path) -> None:
        if self._client is not None:
            raise RuntimeError("server already started")
        self._calls.clear()
        self._storage_dir = storage_dir
        storage_dir.mkdir(parents=True, exist_ok=True)
        self._launch()

    def _launch(self) -> None:
        if self._storage_dir is None:
            raise RuntimeError("server has no storage directory")
        self._client = MCPClient(sys.executable, [
            "-m", "memory_crash_test.fixtures.reference_server", str(self._storage_dir),
        ])

    def stop(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            client.close()

    def restart(self) -> None:
        self.stop()
        self._launch()  # preserves storage and the evidence call log

    def _call(self, name: str, args: dict[str, object]) -> dict[str, object]:
        self._calls.append(CallRecord(name, json.dumps(args, sort_keys=True)[:EVIDENCE_LIMIT]))
        if self._client is None:
            raise RuntimeError("server is stopped")
        result = self._client.call_tool(name, args)
        if result.isError:
            raise RuntimeError(f"MCP tool {name} returned an error")
        structured: object = result.structuredContent
        if structured is not None:
            return mapping(structured)
        for content in result.content:
            if isinstance(content, TextContent):
                value: object = json.loads(content.text)
                return mapping(value)
        raise ValueError("tool returned no JSON object")

    def write_note(self, key: str, text: str, *, provenance: Provenance) -> bool:
        return self._call("write_note", {"key": key, "text": text, "provenance": provenance}).get("ok") is True

    def update_provenance(self, key: str, provenance: Provenance) -> bool:
        return self._call("update_provenance", {"key": key, "provenance": provenance}).get("ok") is True

    def operator_promote(self, key: str) -> bool:
        if self._storage_dir is None:
            raise RuntimeError("server has no storage directory")
        result = subprocess.run([
            sys.executable, "-m", "memory_crash_test.fixtures.reference_server.promote",
            str(self._storage_dir), key,
        ], check=False, capture_output=True, timeout=20)
        return result.returncode == 0

    def read_note(self, key: str) -> ReadResult | None:
        response = self._call("read_note", {"key": key})
        if response.get("note") is None:
            return None
        note = mapping(response["note"])
        envelope = note.get("envelope")
        return ReadResult(
            string(note["text"]), None if envelope is None else mapping(envelope),
            json.dumps(response, sort_keys=True)[:EVIDENCE_LIMIT],
        )

    def list_keys(self) -> list[str]:
        return [string(key) for key in sequence(self._call("list_keys", {})["keys"])]

    def delete_note(self, key: str) -> bool:
        return self._call("delete_note", {"key": key}).get("ok") is True

    def calls(self) -> list[CallRecord]:
        return list(self._calls)
