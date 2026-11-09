"""Synchronous SDK stdio client, with bounded calls and owned child cleanup.

One portal task owns both SDK contexts for their entire lifetime. Entering and
exiting them in separate portal calls would violate AnyIO cancel-scope ownership.
The SDK's stdio exit closes stdin, then terminates/kills an unresponsive child.
"""

from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from contextlib import AbstractContextManager
from functools import partial
from typing import cast

import anyio
from anyio.abc import TaskStatus
from anyio.from_thread import BlockingPortal, start_blocking_portal
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult


class MCPClient:
    def __init__(self, command: str, args: list[str], *, timeout: float = 20) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout
        self._parameters = StdioServerParameters(command=command, args=args)
        self._context: AbstractContextManager[BlockingPortal] = start_blocking_portal()
        self._portal = self._context.__enter__()
        self._stop: anyio.Event | None = None
        self._future: Future[None] | None = None
        self._closed = False
        try:
            self._future, self._session = self._portal.start_task(self._run)
        except BaseException as exc:
            self._context.__exit__(None, None, None)
            self._closed = True
            if _is_timeout(exc):
                raise TimeoutError("MCP initialization timed out") from exc
            raise

    async def _run(self, *, task_status: TaskStatus[ClientSession]) -> None:
        self._stop = anyio.Event()
        async with (
            stdio_client(self._parameters) as (read, write),
            ClientSession(read, write) as session,
        ):
            with anyio.fail_after(self.timeout):
                await session.initialize()
            task_status.started(session)
            await self._stop.wait()

    async def _bounded(self, operation: Callable[[], Awaitable[CallToolResult]]) -> CallToolResult:
        with anyio.fail_after(self.timeout):
            return await operation()

    def call_tool(self, name: str, args: dict[str, object]) -> CallToolResult:
        if self._closed:
            raise RuntimeError("client is closed")
        try:
            return self._portal.call(
                self._bounded, partial(self._session.call_tool, name, arguments=args)
            )
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._stop is not None:
                self._portal.call(self._stop.set)
            if self._future is not None:
                self._future.result()
        finally:
            self._context.__exit__(None, None, None)


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        group = cast(BaseExceptionGroup[BaseException], exc)
        return all(_is_timeout(child) for child in group.exceptions)
    return isinstance(exc, TimeoutError)
