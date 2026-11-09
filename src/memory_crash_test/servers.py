"""Explicit local registry; later packets can add vetted server bindings."""

from collections.abc import Callable
from dataclasses import dataclass

from memory_crash_test.adapters.base import MemoryServerAdapter
from memory_crash_test.adapters.basic_memory import BasicMemoryAdapter
from memory_crash_test.fixtures.reference_server.adapter import ReferenceAdapter
from memory_crash_test.fixtures.reference_server.inspector import ReferenceInspector
from memory_crash_test.inspect.base import StorageInspector
from memory_crash_test.inspect.basic_memory import BasicMemoryInspector


@dataclass(frozen=True)
class ServerBinding:
    adapter_factory: Callable[[], MemoryServerAdapter]
    inspector_factory: Callable[[], StorageInspector]


SERVERS: dict[str, ServerBinding] = {
    "reference": ServerBinding(ReferenceAdapter, ReferenceInspector),
    "basic-memory": ServerBinding(BasicMemoryAdapter, BasicMemoryInspector),
}
