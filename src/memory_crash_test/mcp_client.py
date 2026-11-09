"""Synchronous SDK stdio client, with bounded calls and owned child cleanup.

One portal task owns both SDK contexts for their entire lifetime. Entering and
exiting them in separate portal calls would violate AnyIO cancel-scope ownership.
The SDK's stdio exit closes stdin, then terminates/kills an unresponsive child.
"""

from collections.abc import AsyncGenerator, Awaitable, Callable
from concurrent.futures import Future
from contextlib import AbstractContextManager, asynccontextmanager
from functools import partial
from pathlib import Path
from typing import TextIO, cast

import anyio
from anyio.abc import TaskStatus
from anyio.from_thread import BlockingPortal, start_blocking_portal
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from anyio.streams.text import TextReceiveStream
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.shared.message import SessionMessage
from mcp.types import CallToolResult, JSONRPCMessage


class MCPClient:
    def __init__(
        self, command: str, args: list[str], *, timeout: float = 20,
        env: dict[str, str] | None = None, cwd: Path | None = None,
        shutdown_timeout: float | None = None, errlog: TextIO | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if shutdown_timeout is not None and shutdown_timeout <= 0:
            raise ValueError("shutdown_timeout must be positive")
        self.timeout = timeout
        self._parameters = StdioServerParameters(command=command, args=args, env=env, cwd=cwd)
        self._shutdown_timeout = shutdown_timeout
        self._errlog = errlog
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
        transport = (
            stdio_client(self._parameters) if self._shutdown_timeout is None
            else _draining_stdio(self._parameters, self._shutdown_timeout, self._errlog)
        )
        async with (
            transport as (read, write),
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
                try:
                    self._future.result()
                except BaseException as exc:
                    if self._shutdown_timeout is not None and _is_timeout(exc):
                        raise TimeoutError(
                            f"server did not exit after {self._shutdown_timeout:g}s native drain; forced termination"
                        ) from exc
                    raise
        finally:
            self._context.__exit__(None, None, None)


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        group = cast(BaseExceptionGroup[BaseException], exc)
        return all(_is_timeout(child) for child in group.exceptions)
    return isinstance(exc, TimeoutError)


@asynccontextmanager
async def _draining_stdio(
    parameters: StdioServerParameters, shutdown_timeout: float, errlog: TextIO | None,
) -> AsyncGenerator[tuple[
    MemoryObjectReceiveStream[SessionMessage | Exception], MemoryObjectSendStream[SessionMessage],
]]:
    """SDK-compatible stdio framing with a per-child, observable drain deadline.

    The SDK transport's fixed termination deadline is shorter than some native
    servers' drain. Own this child directly rather than patching an SDK global.
    """
    incoming, read = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write, outgoing = anyio.create_memory_object_stream[SessionMessage](0)
    process = await anyio.open_process(
        [parameters.command, *parameters.args],
        env=parameters.env if parameters.env is not None else get_default_environment(),
        cwd=parameters.cwd, stderr=errlog,
    )
    assert process.stdin is not None and process.stdout is not None
    stdin, stdout = process.stdin, process.stdout

    async def receive() -> None:
        buffer = ""
        async with incoming:
            async for chunk in TextReceiveStream(stdout):
                lines = (buffer + chunk).split("\n")
                buffer = lines.pop()
                for line in lines:
                    try:
                        message = SessionMessage(JSONRPCMessage.model_validate_json(line))
                    except Exception as exc:
                        await incoming.send(exc)
                    else:
                        await incoming.send(message)

    async def send() -> None:
        async with outgoing:
            async for message in outgoing:
                await stdin.send((message.message.model_dump_json(
                    by_alias=True, exclude_none=True,
                ) + "\n").encode())

    async with process, anyio.create_task_group() as tasks:
        tasks.start_soon(receive)
        tasks.start_soon(send)
        try:
            yield read, write
        finally:
            # Shield cleanup even if startup/a tool timed out in the session.
            with anyio.CancelScope(shield=True):
                await stdin.aclose()
                try:
                    with anyio.fail_after(shutdown_timeout):
                        await process.wait()
                    if process.returncode != 0:
                        raise RuntimeError(f"server exited with status {process.returncode}")
                except TimeoutError:
                    process.terminate()
                    with anyio.move_on_after(1):
                        await process.wait()
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                    raise TimeoutError(
                        f"server did not exit after {shutdown_timeout:g}s native drain; forced termination"
                    ) from None
                finally:
                    tasks.cancel_scope.cancel()
                    await read.aclose()
                    await write.aclose()
