"""第 25 章的本地 HTTP 服务。

客户端代码才是本章重点。这个 fixture 只负责制造真实的回环 TCP 传输、可控慢响应、
HTTP/1.1 连接复用和 JSONL 流，并确保所有等待都有清理上限。
"""

import json
import sys
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socket import socket
from typing import Self, cast
from urllib.parse import parse_qs, urlsplit


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """服务端观察到的一次请求；客户端端口可辅助观察 TCP 连接复用。"""

    path: str
    client_port: int


@dataclass(slots=True)
class FixtureState:
    """由事件协调客户端与服务线程，不靠 sleep 猜先后顺序。"""

    slow_request_started: threading.Event = field(default_factory=threading.Event)
    release_slow_response: threading.Event = field(default_factory=threading.Event)
    slow_request_finished: threading.Event = field(default_factory=threading.Event)

    first_stream_line_sent: threading.Event = field(default_factory=threading.Event)
    release_stream_tail: threading.Event = field(default_factory=threading.Event)
    stream_request_finished: threading.Event = field(default_factory=threading.Event)

    sync_request_started: threading.Event = field(default_factory=threading.Event)
    event_loop_heartbeat: threading.Event = field(default_factory=threading.Event)

    server_thread_started: threading.Event = field(default_factory=threading.Event)
    _records: list[RequestRecord] = field(default_factory=list)
    _records_lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, path: str, client_port: int) -> None:
        with self._records_lock:
            self._records.append(RequestRecord(path, client_port))

    def records_snapshot(self) -> list[RequestRecord]:
        with self._records_lock:
            return list(self._records)

    def reset_slow_exchange(self) -> None:
        """前一轮服务线程结束后，给下一个慢请求使用一组未触发事件。"""
        if self.slow_request_started.is_set() and not self.slow_request_finished.is_set():
            raise RuntimeError("上一轮慢请求尚未结束")
        self.slow_request_started.clear()
        self.release_slow_response.clear()
        self.slow_request_finished.clear()


class FixtureServer(ThreadingHTTPServer):
    """给标准库 HTTP 服务器附加测试状态。"""

    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        state: FixtureState,
    ) -> None:
        self.state: FixtureState = state
        super().__init__(server_address, handler_class)

    def handle_error(
        self,
        request: socket | tuple[bytes, socket],
        client_address: object,
    ) -> None:
        """提前关闭流/超时会重置测试连接；这是预期路径，不打印服务端 traceback。"""
        error: BaseException | None = sys.exception()
        if isinstance(
            error,
            (BrokenPipeError, ConnectionResetError, ConnectionAbortedError),
        ):
            return
        super().handle_error(request, client_address)


class FixtureRequestHandler(BaseHTTPRequestHandler):
    """提供固定响应；不属于需要学习的生产服务端实现。"""

    protocol_version = "HTTP/1.1"

    @property
    def fixture_server(self) -> FixtureServer:
        if not isinstance(self.server, FixtureServer):
            raise TypeError("fixture handler 必须由 FixtureServer 承载")
        return self.server

    def log_message(self, format_string: str, *args: object) -> None:
        """关闭并发服务线程日志，保持教学输出顺序可读。"""

    def do_GET(self) -> None:
        state = self.fixture_server.state
        target = urlsplit(self.path)
        state.record(target.path, self.client_address[1])

        if target.path == "/document":
            query = parse_qs(target.query).get("query", ["Agent"])[0]
            self._send_json(
                HTTPStatus.OK,
                {"id": 25, "title": f"{query} 可靠 I/O", "score": 0.95},
            )
            return
        if target.path == "/slow":
            self._send_slow_response()
            return
        if target.path == "/events":
            self._send_jsonl_stream()
            return
        if target.path == "/sync-wait":
            self._wait_for_event_loop_heartbeat()
            return

        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"code": "NOT_FOUND", "message": "本地 fixture 没有这个端点"},
        )

    def _send_slow_response(self) -> None:
        state = self.fixture_server.state
        state.slow_request_started.set()
        # 这是清理保险上限。正常实验会在客户端 finally 中显式放行。
        released = state.release_slow_response.wait(timeout=2.0)
        try:
            if released:
                self._send_json(HTTPStatus.OK, {"message": "慢响应已放行"})
        finally:
            state.slow_request_finished.set()

    def _send_jsonl_stream(self) -> None:
        state = self.fixture_server.state
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        try:
            self._write_chunk({"type": "token", "text": "你好"})
            state.first_stream_line_sent.set()
            # 客户端读到第一行并退出 stream 上下文后才放行后续写入。
            if state.release_stream_tail.wait(timeout=2.0):
                self._write_chunk({"type": "done", "usage": 1})
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except BrokenPipeError, ConnectionResetError, ConnectionAbortedError:
            # 提前停止流时，客户端关闭连接后服务端继续写入失败是预期路径。
            pass
        finally:
            state.stream_request_finished.set()

    def _write_chunk(self, payload: object) -> None:
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        self.wfile.write(f"{len(line):x}\r\n".encode("ascii"))
        self.wfile.write(line)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _wait_for_event_loop_heartbeat(self) -> None:
        state = self.fixture_server.state
        state.sync_request_started.set()
        # 同步客户端若堵住事件循环，计划中的 heartbeat 无法及时运行。
        heartbeat_seen = state.event_loop_heartbeat.wait(timeout=0.25)
        self._send_json(HTTPStatus.OK, {"heartbeat_seen": heartbeat_seen})

    def _send_json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except BrokenPipeError, ConnectionResetError, ConnectionAbortedError:
            # read timeout 后客户端可能已关闭连接。
            pass


class LocalHttpFixture:
    """自动选择端口并拥有服务线程的同步上下文管理器。"""

    def __init__(self) -> None:
        self.state = FixtureState()
        self.base_url = ""
        self.closed = False
        self._server: FixtureServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Self:
        server = FixtureServer(("127.0.0.1", 0), FixtureRequestHandler, self.state)
        self._server = server
        host, port = cast(tuple[str, int], server.server_address)
        self.base_url = f"http://{host}:{port}"

        def serve() -> None:
            self.state.server_thread_started.set()
            server.serve_forever(poll_interval=0.02)

        thread = threading.Thread(target=serve, name="chapter25-http-fixture", daemon=True)
        self._thread = thread
        thread.start()
        if not self.state.server_thread_started.wait(timeout=1.0):
            self.__exit__(None, None, None)
            raise RuntimeError("本地 HTTP fixture 未能启动")
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        # 先打开全部闸门，异常路径也不会遗留永久等待的服务线程。
        self.state.release_slow_response.set()
        self.state.release_stream_tail.set()
        self.state.event_loop_heartbeat.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                raise RuntimeError("本地 HTTP fixture 服务线程没有按期退出")
        self.closed = True
