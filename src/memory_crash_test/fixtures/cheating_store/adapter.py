"""Use the reference transport and operator ceremony with the cheating server."""

import os
import sys

from memory_crash_test.adapters.base import ServerRelease
from memory_crash_test.fixtures.reference_server.adapter import ReferenceAdapter
from memory_crash_test.mcp_client import MCPClient


class CheatingAdapter(ReferenceAdapter):
    release = ServerRelease("mct-cheating-store", "fixture", "fixture")

    def _launch(self) -> None:
        if self._storage_dir is None:
            raise RuntimeError("server has no storage directory")
        self._client = MCPClient(sys.executable, [
            "-m", "memory_crash_test.fixtures.cheating_store", str(self._storage_dir),
            os.environ.get("MCT_CHEAT_MODES", "all"),
        ])
