"""第 12 章的本地 HTTP fixture。

本文件刻意把线程、socket 和 HTTP 服务端细节隔离起来。学习客户端代码时，只需把
``LocalHttpFixture`` 当成一个进入 ``with`` 后可用、离开后自动关闭的本地 API。
"""

import json
import socket
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self, cast
from urllib.parse import parse_qs, urlsplit


@dataclass(frozen=True)
class RequestRecord:
    """fixture 观察到的一次请求；只保存验证连接复用所需的公开信息。"""

    method: str
    target: str
    client_port: int


@dataclass
class FixtureState:
    """在线程之间共享事件，并以加锁快照暴露请求记录。"""

    slow_request_started: threading.Event = field(default_factory=threading.Event)
    release_slow_response: threading.Event = field(default_factory=threading.Event)
    slow_request_finished: threading.Event = field(default_factory=threading.Event)
    server_thread_started: threading.Event = field(default_factory=threading.Event)
    _records: list[RequestRecord] = field(default_factory=list)
    _records_lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, method: str, target: str, client_port: int) -> None:
        """请求可能由不同服务线程处理，所以写入共享列表时需要锁。"""
        with self._records_lock:
            self._records.append(RequestRecord(method, target, client_port))

    def records_snapshot(self) -> list[RequestRecord]:
        """返回副本，避免调用者在锁外迭代一个正在变化的列表。"""
        with self._records_lock:
            return list(self._records)


class FixtureServer(ThreadingHTTPServer):
    """给标准库服务器附加本章状态；工作线程不会阻止进程退出。"""

    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        state: FixtureState,
    ) -> None:
        self.state: FixtureState = state
        super().__init__(server_address, handler_class)


class FixtureRequestHandler(BaseHTTPRequestHandler):
    """提供文档查询和无副作用回显端点；不是本章要求掌握的服务端实现。"""

    # HTTP/1.1 加上准确的 Content-Length，允许 Session 复用同一条本地 TCP 连接。
    protocol_version = "HTTP/1.1"

    @property
    def fixture_server(self) -> FixtureServer:
        if not isinstance(self.server, FixtureServer):
            raise TypeError("fixture handler 必须由 FixtureServer 承载")
        return self.server

    def log_message(self, format_string: str, *args: object) -> None:
        """关闭标准库逐请求日志，避免教学输出被服务线程穿插。"""

    def do_GET(self) -> None:
        """GET /documents 是只读查询，没有修改资源的语义。"""
        self.fixture_server.state.record("GET", self.path, self.client_address[1])
        target: SplitResult = urlsplit(self.path)
        if target.path != "/documents":
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"code": "NOT_FOUND", "message": "本地 fixture 没有这个端点"},
            )
            return

        query_values: list[str] = parse_qs(
            target.query, keep_blank_values=True
        ).get("query", [])
        query: str = query_values[0] if query_values else ""
        if not query:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"code": "INVALID_QUERY", "message": "query 不能为空"},
            )
            return

        if query == "http-error":
            # 错误响应故意也是合法 JSON，用来证明“能解析 JSON”不等于 HTTP 成功。
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"code": "NOT_FOUND", "message": "文档不存在"},
            )
            return
        if query == "invalid-json":
            self._send_bytes(
                HTTPStatus.OK,
                b'{"code": "OK", this-is-not-json',
                content_type="application/json; charset=utf-8",
            )
            return
        if query == "missing-field":
            self._send_json(
                HTTPStatus.OK,
                {
                    "code": "OK",
                    "data": {"document": {"id": 12, "content": "缺少 title"}},
                },
            )
            return
        if query == "null-field":
            self._send_json(
                HTTPStatus.OK,
                {
                    "code": "OK",
                    "data": {
                        "document": {
                            "id": 12,
                            "title": None,
                            "content": "title 为 null",
                        }
                    },
                },
            )
            return
        if query == "wrong-type":
            self._send_json(
                HTTPStatus.OK,
                {
                    "code": "OK",
                    "data": {
                        "document": {
                            "id": "12",
                            "title": "类型错误",
                            "content": "id 应为 int",
                        }
                    },
                },
            )
            return
        if query == "business-fail":
            self._send_json(
                HTTPStatus.OK,
                {
                    "code": "QUOTA_EXCEEDED",
                    "message": "本地演示额度不足",
                    "data": None,
                },
            )
            return
        if query == "slow":
            self._send_slow_document()
            return

        self._send_json(
            HTTPStatus.OK,
            {
                "code": "OK",
                "data": {
                    "document": {
                        "id": 12,
                        "title": f"{query} 查询结果",
                        "content": "一次 API 调用要逐层检查。",
                    }
                },
            },
        )

    def do_POST(self) -> None:
        """POST /echo 只回显收到的数据，不保存、创建或修改业务资源。"""
        self.fixture_server.state.record("POST", self.path, self.client_address[1])
        target: SplitResult = urlsplit(self.path)
        if target.path != "/echo":
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"code": "NOT_FOUND", "message": "本地 fixture 没有这个端点"},
            )
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        body_bytes: bytes = self.rfile.read(content_length)
        content_type: str = self.headers.get("Content-Type", "")
        body_text: str = body_bytes.decode("utf-8", errors="replace")

        json_value: object | None = None
        json_value_type: str | None = None
        form_value: dict[str, list[str]] | None = None
        if content_type.startswith("application/json"):
            try:
                json_value = json.loads(body_text)
            except json.JSONDecodeError:
                json_value_type = "解析失败"
            else:
                json_value_type = type(json_value).__name__
        elif content_type.startswith("application/x-www-form-urlencoded"):
            form_value = parse_qs(body_text, keep_blank_values=True)

        self._send_json(
            HTTPStatus.OK,
            {
                "method": "POST",
                "query_params": parse_qs(target.query, keep_blank_values=True),
                "content_type": content_type or None,
                "raw_body": body_text,
                "json_value": json_value,
                "json_value_type": json_value_type,
                "form_value": form_value,
                "demo_header": self.headers.get("X-Demo-Client"),
            },
        )

    def _send_slow_document(self) -> None:
        """用事件阻塞首字节，直到客户端超时后明确释放，不用 sleep 猜线程顺序。"""
        state: FixtureState = self.fixture_server.state
        state.slow_request_started.set()
        # 两秒只是 fixture 自我清理的上限；正常示例由客户端 finally 立即释放这个事件。
        released_by_client: bool = state.release_slow_response.wait(timeout=2.0)
        try:
            if released_by_client:
                # 客户端已主动放弃该响应；即使内核暂时接收了写入，也不再尝试复用这条连接。
                self.close_connection = True
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "code": "OK",
                        "data": {
                            "document": {
                                "id": 12,
                                "title": "慢响应",
                                "content": "客户端已经不再等待这份响应。",
                            }
                        },
                    },
                )
        finally:
            state.slow_request_finished.set()

    def _send_json(self, status: HTTPStatus, payload: object) -> None:
        """统一用 UTF-8 编码 JSON，并让中文在抓包/输出中保持可读。"""
        body: bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(
            status, body, content_type="application/json; charset=utf-8"
        )

    def _send_bytes(
        self, status: HTTPStatus, body: bytes, *, content_type: str
    ) -> None:
        """总是发送长度，且把超时后客户端断开视为 fixture 的预期路径。"""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 慢响应实验中，客户端读超时会先关闭连接；服务端稍后写入失败是预期结果。
            self.close_connection = True
            return


class LocalHttpFixture:
    """自动选择本地端口，并负责启动、关闭和等待服务线程。"""

    def __init__(self) -> None:
        self.state = FixtureState()
        self._server: FixtureServer | None = None
        self._thread: threading.Thread | None = None
        self.base_url: str = ""
        self.closed = False

    def __enter__(self) -> Self:
        server = FixtureServer(
            ("127.0.0.1", 0), FixtureRequestHandler, self.state
        )
        self._server = server
        # FixtureServer 固定 AF_INET；cast 排除 typeshed 仍保留的 IPv6 四元组可能性。
        host, port = cast(tuple[str, int], server.server_address)
        self.base_url = f"http://{host}:{port}"

        def serve() -> None:
            self.state.server_thread_started.set()
            server.serve_forever(poll_interval=0.02)

        thread = threading.Thread(
            target=serve, name="chapter12-http-fixture", daemon=True
        )
        self._thread = thread
        thread.start()
        if not self.state.server_thread_started.wait(timeout=1.0):
            self.__exit__(None, None, None)
            raise RuntimeError("本地 HTTP fixture 未能启动")
        return self

    def __exit__(
        self, exc_type: object, exc_value: object, traceback: object
    ) -> None:
        # 无论示例从哪条异常路径离开，都先解除慢请求，再关闭监听端口。
        self.state.release_slow_response.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                raise RuntimeError("本地 HTTP fixture 服务线程没有按期退出")
        self.closed = True


@contextmanager
def refusing_connection_url() -> Generator[str, Any, None]:
    """提供一个会立即断开连接的本地端口，稳定观察 ConnectionError。"""
    # 某些 Windows 安全策略会静默丢弃发往已关闭本地端口的 SYN，使“拒绝连接”变成
    # ConnectTimeout。这里让临时监听器完成握手后立即关闭连接；客户端仍稳定落入
    # Requests 的 ConnectionError，而不依赖公网或操作系统的 closed-port 策略。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(1.0)
        host, port = listener.getsockname()

        def reject_once() -> None:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            connection.close()

        thread = threading.Thread(
            target=reject_once, name="chapter12-refusing-connection", daemon=True
        )
        thread.start()
        try:
            yield f"http://{host}:{port}"
        finally:
            thread.join(timeout=1.0)
