"""经真实回环 TCP + Uvicorn 观察普通响应与流式响应的断连边界。

这是版本相关的集成实验，不把本次观察提升成所有 ASGI 服务器的永久保证。
"""

import asyncio
import json
import socket
from dataclasses import dataclass, field
from importlib.metadata import version
from typing import cast

import uvicorn
from app import ToolName, create_app
from fastapi import FastAPI


def print_section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


@dataclass(slots=True)
class CallProbe:
    """一个请求的工具状态；所有转换都发生在同一个事件循环线程。"""

    expected: int
    started: set[ToolName] = field(default_factory=set)
    cancelled: set[ToolName] = field(default_factory=set)
    completed: set[ToolName] = field(default_factory=set)
    finished: set[ToolName] = field(default_factory=set)
    all_started: asyncio.Event = field(default_factory=asyncio.Event)
    all_finished: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    def mark_started(self, tool: ToolName) -> None:
        self.started.add(tool)
        if len(self.started) == self.expected:
            self.all_started.set()

    def mark_finished(self, tool: ToolName) -> None:
        self.finished.add(tool)
        if len(self.finished) == self.expected:
            self.all_finished.set()


class DisconnectProbeBackend:
    """远端工具替身；只有 release 或取消能结束，因果关系完全可观察。"""

    def __init__(self) -> None:
        self.probes: dict[str, CallProbe] = {
            "ordinary-disconnect": CallProbe(expected=2),
            "stream-disconnect": CallProbe(expected=2),
        }

    async def call(self, tool: ToolName, query: str) -> str:
        probe: CallProbe = self.probes[query]
        probe.mark_started(tool)
        try:
            await probe.release.wait()
        except asyncio.CancelledError:
            probe.cancelled.add(tool)
            raise
        else:
            probe.completed.add(tool)
            return f"{tool} 已由实验闸门放行"
        finally:
            probe.mark_finished(tool)


@dataclass(slots=True)
class RawExchange:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    async def close(self) -> None:
        self.writer.close()
        await self.writer.wait_closed()


async def open_raw_post(
    port: int,
    path: str,
    request_id: str,
    payload: dict[str, object],
) -> RawExchange:
    reader: StreamReader
    writer: StreamWriter
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    body: bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    request_head: bytes = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"X-Request-ID: {request_id}\r\n"
        "Connection: keep-alive\r\n"
        "\r\n"
    ).encode("ascii")
    writer.write(request_head + body)
    await writer.drain()
    return RawExchange(reader, writer)


async def read_first_chunk(exchange: RawExchange) -> tuple[str, dict[str, object]]:
    head: bytes = await exchange.reader.readuntil(b"\r\n\r\n")
    status_line: str = head.split(b"\r\n", 1)[0].decode("ascii")
    size_line: bytes = await exchange.reader.readline()
    chunk_size = int(size_line.strip(), 16)
    chunk: bytes = await exchange.reader.readexactly(chunk_size)
    await exchange.reader.readexactly(2)
    return status_line, cast(dict[str, object], json.loads(chunk))


async def wait_until_started(
    server: uvicorn.Server,
    server_task: asyncio.Task[None],
) -> None:
    """轮询只等待 Uvicorn 自身的状态位，不参与断连因果断言。"""

    async with asyncio.timeout(3.0):
        while not server.started:
            if server_task.done():
                await server_task
                raise RuntimeError("Uvicorn 在进入 started 前退出")
            await asyncio.sleep(0.01)


async def demonstrate_ordinary_disconnect(
    port: int,
    backend: DisconnectProbeBackend,
) -> None:
    print_section("1. 普通 JSON 路由：本次版本中断连没有自动取消处理任务")
    probe: CallProbe = backend.probes["ordinary-disconnect"]
    exchange: RawExchange = await open_raw_post(
        port,
        "/query",
        "net-ordinary",
        {
            "query": "ordinary-disconnect",
            "tools": ["search", "weather"],
            "max_concurrency": 2,
            "timeout_ms": 2_000,
        },
    )

    try:
        async with asyncio.timeout(2.0):
            await probe.all_started.wait()
        await exchange.close()

        try:
            async with asyncio.timeout(0.25):
                await probe.all_finished.wait()
        except TimeoutError:
            still_running_in_window = True
        else:
            still_running_in_window = False

        print("客户端关闭后 0.25 秒内仍在等待:", still_running_in_window)
        print("此时工具取消集合:", sorted(probe.cancelled))
        assert still_running_in_window
        assert not probe.cancelled
    finally:
        probe.release.set()
        async with asyncio.timeout(2.0):
            await probe.all_finished.wait()

    print("实验主动放行后的完成集合:", sorted(probe.completed))
    assert probe.completed == {"search", "weather"}


async def demonstrate_stream_disconnect(
    port: int,
    backend: DisconnectProbeBackend,
    application: FastAPI,
) -> None:
    print_section("2. StreamingResponse：本次版本组合取消生成器及其 TaskGroup")
    probe: CallProbe = backend.probes["stream-disconnect"]
    exchange: RawExchange = await open_raw_post(
        port,
        "/query/stream",
        "net-stream",
        {
            "query": "stream-disconnect",
            "tools": ["search", "weather"],
            "max_concurrency": 2,
            "timeout_ms": 2_000,
        },
    )

    try:
        status_line: str
        first_event: dict[str, object]
        status_line, first_event = await read_first_chunk(exchange)
        async with asyncio.timeout(2.0):
            await probe.all_started.wait()
        print("已收到:", status_line, first_event)
        assert status_line == "HTTP/1.1 200 OK"
        assert first_event["event"] == "start"

        await exchange.close()
        async with asyncio.timeout(2.0):
            await probe.all_finished.wait()
            await application.state.trace.wait_for_step("net-stream", "dependency:released")
    finally:
        probe.release.set()

    trace: Any = await application.state.trace.steps_for("net-stream")
    print("工具取消集合:", sorted(probe.cancelled))
    print("请求依赖记录:", trace)
    assert probe.cancelled == {"search", "weather"}
    assert "dependency:released" in trace
    assert "response:body-sent" not in trace


async def run_experiment() -> None:
    print("运行版本:", {name: version(name) for name in ("fastapi", "starlette", "uvicorn")})
    backend = DisconnectProbeBackend()
    application: FastAPI = create_app(backend_factory=lambda client: backend)

    listening_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listening_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listening_socket.bind(("127.0.0.1", 0))
    listening_socket.listen(128)
    listening_socket.setblocking(False)
    port: int = cast(tuple[str, int], listening_socket.getsockname())[1]

    config = uvicorn.Config(
        app=application,
        host="127.0.0.1",
        port=port,
        http="h11",
        lifespan="on",
        log_level="error",
        access_log=False,
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    server_task: Task[None] = asyncio.create_task(
        server.serve(sockets=[listening_socket]),
        name="uvicorn",
    )

    try:
        await wait_until_started(server, server_task)
        print("本地 Uvicorn 已启动:", f"http://127.0.0.1:{port}")
        await demonstrate_ordinary_disconnect(port, backend)
        await demonstrate_stream_disconnect(port, backend, application)
    finally:
        for probe in backend.probes.values():
            probe.release.set()
        server.should_exit = True
        try:
            async with asyncio.timeout(4.0):
                await server_task
        finally:
            listening_socket.close()

    print_section("3. 本次实测能说什么")
    print("普通路由：客户端断开后，在有限观察窗内工具仍运行，随后由实验主动放行。")
    print("流式路由：断开触发当前服务器/响应实现的取消，TaskGroup 回收两个工具。")
    print("边界：不同 ASGI 服务器、版本、响应类型和远程 SDK 必须重新测；本地 Task 取消")
    print("      也不证明远端已经停止或副作用已回滚。")


def main() -> None:
    asyncio.run(run_experiment())


if __name__ == "__main__":
    main()
