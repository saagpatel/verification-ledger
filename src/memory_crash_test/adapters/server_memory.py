"""Native MCP operations for the pinned knowledge-graph server."""
import json
import os
import subprocess
from pathlib import Path

from mcp.types import TextContent

from memory_crash_test._json import mapping, sequence, string
from memory_crash_test.adapters.base import (
    EVIDENCE_LIMIT, CallRecord, Provenance, ReadResult, ServerRelease, UnsupportedCapability,
)
from memory_crash_test.mcp_client import MCPClient

PIN = "2.1.0"
PACKAGE = "@modelcontextprotocol/server-memory"


def install() -> Path:
    root = Path(os.environ.get("MCT_CACHE", str(Path.home() / ".cache/memory-crash-test"))).expanduser().resolve()
    prefix = root / "server-memory" / PIN
    package = prefix / "node_modules" / "@modelcontextprotocol" / "server-memory"
    if not (package / "package.json").is_file() or not (package / "dist/index.js").is_file():
        prefix.mkdir(parents=True, exist_ok=True)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(prefix),
               "npm_config_cache": str(prefix / "npm-cache")}
        subprocess.run([
            "npm", "install", "--prefix", str(prefix), "--ignore-scripts", "--no-audit",
            "--no-fund", f"{PACKAGE}@{PIN}",
        ], check=True, timeout=300, env=env)
    return package


class ServerMemoryAdapter:
    def __init__(self) -> None:
        self._package = install()
        metadata: object = json.loads((self._package / "package.json").read_text(encoding="utf-8"))
        version = string(mapping(metadata)["version"])
        if version != PIN:
            raise ValueError(f"installed server-memory version {version} does not match pin {PIN}")
        self.release = ServerRelease(PACKAGE, version, "npm")
        self._client: MCPClient | None = None
        self._storage: Path | None = None
        self._calls: list[CallRecord] = []

    def start(self, storage_dir: Path) -> None:
        if self._client is not None:
            raise RuntimeError("server already started")
        self._calls.clear()
        self._storage = storage_dir.resolve()
        self._storage.mkdir(parents=True, exist_ok=True)
        self._launch()

    def _launch(self) -> None:
        if self._storage is None:
            raise RuntimeError("server has no storage directory")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        for name, directory in (("HOME", "home"), ("TMPDIR", "tmp"), ("XDG_CONFIG_HOME", "config"),
                                ("XDG_CACHE_HOME", "cache"), ("XDG_DATA_HOME", "data")):
            path = self._storage / directory
            path.mkdir(exist_ok=True)
            env[name] = str(path)
        env["MEMORY_FILE_PATH"] = str(self._storage / "memory.jsonl")
        self._client = MCPClient("node", [str(self._package / "dist/index.js")], env=env, cwd=self._storage)

    def stop(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            client.close()

    def restart(self) -> None:
        self.stop()
        self._launch()

    def _call(self, name: str, args: dict[str, object]) -> dict[str, object]:
        self._calls.append(CallRecord(name, json.dumps(args, sort_keys=True)[:EVIDENCE_LIMIT]))
        if self._client is None:
            raise RuntimeError("server is stopped")
        result = self._client.call_tool(name, args)
        if result.isError:
            raise RuntimeError(f"MCP tool {name} returned an error")
        if result.structuredContent is not None:
            return mapping(result.structuredContent)
        for content in result.content:
            if isinstance(content, TextContent):
                value: object = json.loads(content.text)
                return mapping(value)
        raise ValueError("tool returned no JSON object")

    def write_note(self, key: str, text: str, *, provenance: Provenance) -> bool:
        result = self._call("create_entities", {"entities": [{"name": key, "entityType": provenance, "observations": [text]}]})
        return any(mapping(entity).get("name") == key for entity in sequence(result["entities"]))

    def update_provenance(self, key: str, provenance: Provenance) -> bool:
        response = self._call("open_nodes", {"names": [key]})
        entities = sequence(response["entities"])
        if not entities:
            return False
        observations = [string(item) for item in sequence(mapping(entities[0])["observations"])]
        self._call("delete_entities", {"entityNames": [key]})
        result = self._call("create_entities", {"entities": [{"name": key, "entityType": provenance, "observations": observations}]})
        return any(mapping(entity).get("name") == key for entity in sequence(result["entities"]))

    def operator_promote(self, key: str) -> bool:
        raise UnsupportedCapability("server-memory has no native operator-only promotion ceremony")

    def read_note(self, key: str) -> ReadResult | None:
        response = self._call("open_nodes", {"names": [key]})
        entities = sequence(response["entities"])
        if not entities:
            return None
        observations = sequence(mapping(entities[0])["observations"])
        return ReadResult("\n".join(string(item) for item in observations), None,
                          json.dumps(response, sort_keys=True)[:EVIDENCE_LIMIT])

    def list_keys(self) -> list[str]:
        return [string(mapping(entity)["name"]) for entity in sequence(self._call("read_graph", {})["entities"])]

    def delete_note(self, key: str) -> bool:
        self._call("delete_entities", {"entityNames": [key]})
        return True

    def calls(self) -> list[CallRecord]:
        return list(self._calls)
