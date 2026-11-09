"""Explicit local registry; later packets can add vetted server bindings."""

from collections.abc import Callable
from dataclasses import dataclass

from memory_crash_test.adapters.base import MemoryServerAdapter
from memory_crash_test.adapters.basic_memory import BasicMemoryAdapter
from memory_crash_test.adapters.server_memory import ServerMemoryAdapter
from memory_crash_test.fixtures.cheating_store.adapter import CheatingAdapter
from memory_crash_test.fixtures.reference_server.adapter import ReferenceAdapter
from memory_crash_test.fixtures.reference_server.inspector import ReferenceInspector
from memory_crash_test.inspect.base import StorageInspector
from memory_crash_test.inspect.basic_memory import BasicMemoryInspector
from memory_crash_test.inspect.server_memory import ServerMemoryInspector


@dataclass(frozen=True)
class ServerBinding:
    adapter_factory: Callable[[], MemoryServerAdapter]
    inspector_factory: Callable[[], StorageInspector]


SERVERS: dict[str, ServerBinding] = {
    "server-memory": ServerBinding(ServerMemoryAdapter, ServerMemoryInspector),
    "reference": ServerBinding(ReferenceAdapter, ReferenceInspector),
    "cheating-store": ServerBinding(CheatingAdapter, ReferenceInspector),
    "basic-memory": ServerBinding(BasicMemoryAdapter, BasicMemoryInspector),
}
