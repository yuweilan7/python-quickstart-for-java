"""第 28 章的可运行 FastAPI 服务。

业务只调用本地 FakeLLM 和 HTTPX MockTransport 上的本地工具替身，不需要 API Key。
`create_app()` 允许测试和断连实验注入可控工具后端；模块级 `app` 供 Uvicorn 启动。
"""

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Annotated, Literal, Protocol, Self, cast
from uuid import uuid4

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger: Logger = logging.getLogger("chapter28.service")

ToolName = Literal["search", "weather", "clock", "unstable"]
FailureMode = Literal["strict", "best_effort"]
StreamEventName = Literal["start", "tool_result", "answer", "error", "done"]

_request_id: ContextVar[str] = ContextVar("chapter28_request_id", default="-")


def current_request_id() -> str:
    """让深层函数读取当前请求标识，而不把它存进全局可变变量。"""

    return _request_id.get()


def _default_tools() -> list[ToolName]:
    return ["search", "weather"]


class QueryRequest(BaseModel):
    """POST 请求体：外部 JSON 在进入编排器前先成为可信结构。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(min_length=1, max_length=120)
    tools: list[ToolName] = Field(default_factory=_default_tools, min_length=1, max_length=4)
    max_concurrency: int = Field(default=2, ge=1, le=3)
    timeout_ms: int = Field(default=500, ge=50, le=2_000)
    failure_mode: FailureMode = "best_effort"

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized: str = value.strip()
        if not normalized:
            raise ValueError("query 不能只包含空白")
        return normalized

    @field_validator("tools")
    @classmethod
    def reject_duplicate_tools(cls, value: list[ToolName]) -> list[ToolName]:
        if len(value) != len(set(value)):
            raise ValueError("tools 不能重复")
        return value


class ToolResult(BaseModel):
    """单个工具的稳定输出合同。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: ToolName
    ok: bool
    output: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def result_fields_match_status(self) -> Self:
        if self.ok and (self.output is None or self.error is not None):
            raise ValueError("成功结果必须只有 output")
        if not self.ok and (self.error is None or self.output is not None):
            raise ValueError("失败结果必须只有 error")
        return self


class QueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    answer: str
    results: list[ToolResult]
    max_parallel_tools: int
    background_audit_scheduled: bool


class StreamEvent(BaseModel):
    """JSONL 中每一行的应用事件；网络 chunk 不等于这一行。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: StreamEventName
    request_id: str
    data: dict[str, object] = Field(default_factory=dict)


class ExecutionInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    route_kind: Literal["def", "async def"]
    route_thread: str
    helper_thread: str
    request_id: str


class ToolExecutionError(RuntimeError):
    """工具已经开始执行，但未得到可用结果。"""


class InternalServiceError(RuntimeError):
    """严格失败策略下，不应向客户端暴露实现细节的内部故障。"""


class QueryBudgetExceeded(TimeoutError):
    """一次请求共享的总预算已经耗尽。"""


class ToolBackend(Protocol):
    async def call(self, tool: ToolName, query: str) -> str: ...


BackendFactory = Callable[[httpx.AsyncClient], ToolBackend]


@dataclass(frozen=True, slots=True)
class TraceEvent:
    request_id: str
    step: str


@dataclass(slots=True)
class TraceRecorder:
    """共享的是观测设施；锁只保护很短的 append/snapshot 临界区。"""

    _events: list[TraceEvent] = field(default_factory=list)
    _changed: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def record(self, request_id: str, step: str) -> None:
        async with self._changed:
            self._events.append(TraceEvent(request_id, step))
            self._changed.notify_all()

    async def steps_for(self, request_id: str) -> list[str]:
        async with self._changed:
            return [event.step for event in self._events if event.request_id == request_id]

    async def wait_for_step(self, request_id: str, step: str) -> None:
        """让测试等待事实发生，而不是 sleep 后猜清理是否完成。"""

        async with self._changed:
            await self._changed.wait_for(
                lambda: any(
                    event.request_id == request_id and event.step == step for event in self._events
                )
            )


@dataclass(slots=True)
class ConcurrencyProbe:
    """记录可观察的在途数量，不把业务结果放进共享状态。"""

    active: int = 0
    max_active: int = 0
    first_started: asyncio.Event = field(default_factory=asyncio.Event)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @asynccontextmanager
    async def track(self) -> AsyncIterator[None]:
        async with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.first_started.set()
        try:
            yield
        finally:
            async with self._lock:
                self.active -= 1


@dataclass(slots=True)
class Services:
    """由应用 lifespan 拥有、由请求借用的共享服务。"""

    http_client: httpx.AsyncClient
    tool_backend: ToolBackend
    llm: FakeLLM
    global_tool_limit: asyncio.Semaphore
    global_probe: ConcurrencyProbe
    trace: TraceRecorder
    audit_log: list[str] = field(default_factory=list)
    _audit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def write_audit(self, request_id: str, query: str) -> None:
        """小型进程内任务：进程崩溃时没有持久化保证。"""

        async with self._audit_lock:
            self.audit_log.append(f"{request_id}:{query}")
        await self.trace.record(request_id, "background:audit-finished")


class FakeLLM:
    """无网络、无随机性的本地替身，只证明调用和数据流，不证明真实模型行为。"""

    async def summarize(self, query: str, results: list[ToolResult]) -> str:
        await asyncio.sleep(0)
        succeeded: list[str] = [result.tool for result in results if result.ok]
        return f"{query}：已汇总 {len(succeeded)} 个工具（{', '.join(succeeded) or '无'}）"


class HttpFakeToolBackend:
    """借用 lifespan 的 HTTPX 客户端；不拥有它，也不在单次调用后关闭它。"""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client: AsyncClient = client

    async def call(self, tool: ToolName, query: str) -> str:
        try:
            response: Response = await self._client.get(f"/tools/{tool}", params={"q": query})
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise ToolExecutionError(f"{tool} 调用失败") from error

        payload: Any = response.json()
        output: Any = payload.get("output")
        if not isinstance(output, str):
            raise ToolExecutionError(f"{tool} 返回了错误结构")
        return output


async def fake_tool_transport(request: httpx.Request) -> httpx.Response:
    """HTTPX 的应用内 transport 替身，不经过 DNS、TCP、TLS 或真实连接池。"""

    tool: Literal["search"] | Literal["weather"] = cast(
        ToolName, request.url.path.rsplit("/", 1)[-1]
    )
    query: Any = request.url.params.get("q", "")

    if query == "slow":
        await asyncio.sleep(0.20)
    else:
        delays: dict[ToolName, float] = {
            "search": 0.03,
            "weather": 0.02,
            "clock": 0.01,
            "unstable": 0.01,
        }
        await asyncio.sleep(delays[tool])

    if tool == "unstable":
        return httpx.Response(status.HTTP_503_SERVICE_UNAVAILABLE, json={"detail": "fake failure"})

    outputs: dict[ToolName, str] = {
        "search": f"找到与“{query}”相关的 2 条本地资料",
        "weather": "本地天气替身：晴，22°C",
        "clock": "本地时钟替身：固定教学时刻",
        "unstable": "不会到达这里",
    }
    return httpx.Response(status.HTTP_200_OK, json={"output": outputs[tool]})


@dataclass(slots=True)
class RequestLease:
    request_id: str
    released: bool = False

    def assert_open(self) -> None:
        if self.released:
            raise RuntimeError("请求资源已经清理，不能再被响应体使用")


def _valid_request_id(value: str | None) -> str:
    if value is not None:
        candidate: str = value.strip()
        allowed: bool = all(character.isalnum() or character in "-_." for character in candidate)
        if 1 <= len(candidate) <= 64 and allowed:
            return candidate
    return uuid4().hex


class RequestContextMiddleware:
    """纯 ASGI 中间件：为并发请求隔离 ContextVar，并给所有响应添加 request_id。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming_headers: dict[Any, Any] = dict(scope.get("headers", []))
        incoming_id: Any | None = incoming_headers.get(b"x-request-id")
        request_id: str = _valid_request_id(
            incoming_id.decode("ascii", errors="ignore") if incoming_id else None
        )
        token: Token[str] = _request_id.set(request_id)
        logger.info("request.start request_id=%s path=%s", request_id, scope.get("path"))

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["x-request-id"] = request_id
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                app_object: Any | None = scope.get("app")
                if app_object is not None:
                    trace: TraceRecorder = cast(TraceRecorder, app_object.state.trace)
                    await trace.record(request_id, "response:body-sent")

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            logger.info("request.end request_id=%s", request_id)
            _request_id.reset(token)


def get_services(request: Request) -> Services:
    services: Services | None = cast(
        Services | None, getattr(request.app.state, "services", None)
    )
    if services is None or services.http_client.is_closed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="应用 lifespan 尚未启动",
        )
    return services


ServicesDependency = Annotated[Services, Depends(get_services)]


async def open_request_lease(
    services: ServicesDependency,
) -> AsyncIterator[RequestLease]:
    """一次 yield 把获取与释放放在 FastAPI 管理的同一依赖作用域。"""

    request_id: str = current_request_id()
    lease = RequestLease(request_id)
    await services.trace.record(request_id, "dependency:acquired")
    try:
        yield lease
    finally:
        lease.released = True
        await services.trace.record(request_id, "dependency:released")


def _leaf_exceptions(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for nested in error.exceptions:
            leaves.extend(_leaf_exceptions(nested))
        return leaves
    return [error]


async def execute_tool(
    services: Services,
    request_limit: asyncio.Semaphore,
    request_probe: ConcurrencyProbe,
    tool: ToolName,
    query: str,
    failure_mode: FailureMode,
) -> ToolResult:
    """Semaphore 管容量；TaskGroup（调用方）管所有权与失败联动。"""

    try:
        async with (
            request_limit,
            services.global_tool_limit,
            request_probe.track(),
            services.global_probe.track(),
        ):
            output: str = await services.tool_backend.call(tool, query)
    except ToolExecutionError as error:
        if failure_mode == "best_effort":
            return ToolResult(tool=tool, ok=False, error=str(error))
        raise
    return ToolResult(tool=tool, ok=True, output=output)


async def run_tool_batch(
    services: Services,
    payload: QueryRequest,
) -> tuple[list[ToolResult], int]:
    """运行小批量工具；总预算由更外层拥有完整请求的编排器负责。"""

    request_limit = asyncio.Semaphore(payload.max_concurrency)
    request_probe = ConcurrencyProbe()
    tasks: dict[ToolName, asyncio.Task[ToolResult]] = {}

    try:
        async with asyncio.TaskGroup() as group:
            for tool in payload.tools:
                tasks[tool] = group.create_task(
                    execute_tool(
                        services,
                        request_limit,
                        request_probe,
                        tool,
                        payload.query,
                        payload.failure_mode,
                    ),
                    name=f"tool:{tool}",
                )
    except BaseExceptionGroup as error_group:
        leaves: list[BaseException] = _leaf_exceptions(error_group)
        if leaves and all(isinstance(error, ToolExecutionError) for error in leaves):
            raise InternalServiceError("严格模式下至少一个工具失败") from error_group
        raise

    return [tasks[tool].result() for tool in payload.tools], request_probe.max_active


async def orchestrate_query(
    services: Services,
    payload: QueryRequest,
) -> tuple[list[ToolResult], int, str]:
    """总预算覆盖等待许可、全部工具、TaskGroup 清理和 FakeLLM 汇总。"""

    try:
        async with asyncio.timeout(payload.timeout_ms / 1_000):
            results: list[ToolResult]
            max_parallel: int
            results, max_parallel = await run_tool_batch(services, payload)
            answer: str = await services.llm.summarize(payload.query, results)
    except TimeoutError as error:
        raise QueryBudgetExceeded("请求总预算已耗尽") from error
    return results, max_parallel, answer


def encode_stream_event(event: StreamEvent) -> bytes:
    """一条应用事件编码为一行；传输层仍可任意拆分这些 bytes。"""

    return (event.model_dump_json() + "\n").encode()


async def stream_query_events(
    services: Services,
    payload: QueryRequest,
    lease: RequestLease,
) -> AsyncIterator[bytes]:
    """流拥有本请求的 TaskGroup；停止消费会离开组并回收仍在运行的工具。"""

    request_id: str = lease.request_id
    request_limit = asyncio.Semaphore(payload.max_concurrency)
    request_probe = ConcurrencyProbe()
    queue: asyncio.Queue[ToolResult] = asyncio.Queue()
    results: list[ToolResult] = []

    async def run_and_publish(tool: ToolName) -> None:
        result: ToolResult = await execute_tool(
            services,
            request_limit,
            request_probe,
            tool,
            payload.query,
            payload.failure_mode,
        )
        await queue.put(result)

    try:
        async with asyncio.timeout(payload.timeout_ms / 1_000):
            async with asyncio.TaskGroup() as group:
                for tool in payload.tools:
                    group.create_task(run_and_publish(tool), name=f"stream-tool:{tool}")

                await request_probe.first_started.wait()
                lease.assert_open()
                yield encode_stream_event(
                    StreamEvent(
                        event="start",
                        request_id=request_id,
                        data={"tool_count": len(payload.tools)},
                    )
                )

                for _ in payload.tools:
                    result: ToolResult = await queue.get()
                    results.append(result)
                    lease.assert_open()
                    yield encode_stream_event(
                        StreamEvent(
                            event="tool_result",
                            request_id=request_id,
                            data=result.model_dump(),
                        )
                    )

            order: dict[str, int] = {tool: index for index, tool in enumerate(payload.tools)}
            results.sort(key=lambda result: order[result.tool])
            answer: str = await services.llm.summarize(payload.query, results)
            yield encode_stream_event(
                StreamEvent(event="answer", request_id=request_id, data={"text": answer})
            )
            yield encode_stream_event(
                StreamEvent(
                    event="done",
                    request_id=request_id,
                    data={"max_parallel_tools": request_probe.max_active},
                )
            )
    except TimeoutError:
        yield encode_stream_event(
            StreamEvent(
                event="error",
                request_id=request_id,
                data={"code": "budget_exceeded", "message": "流式请求总预算已耗尽"},
            )
        )
        yield encode_stream_event(StreamEvent(event="done", request_id=request_id))
        return
    except BaseExceptionGroup as error_group:
        leaves: list[BaseException] = _leaf_exceptions(error_group)
        if not leaves or not all(isinstance(error, ToolExecutionError) for error in leaves):
            raise
        yield encode_stream_event(
            StreamEvent(
                event="error",
                request_id=request_id,
                data={"code": "tool_failure", "message": "严格模式下工具失败"},
            )
        )
        yield encode_stream_event(StreamEvent(event="done", request_id=request_id))
        return


def sync_helper_thread_name() -> str:
    """直接调用它时就在当前线程执行；函数名旁没有框架魔法。"""

    return threading.current_thread().name


def create_app(backend_factory: BackendFactory | None = None) -> FastAPI:
    """应用工厂便于每个测试获得独立 lifespan 与共享资源。"""

    trace = TraceRecorder()
    lifecycle_events: list[str] = []

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        lifecycle_events.append("startup:begin")
        transport = httpx.MockTransport(fake_tool_transport)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://local-tools.invalid",
            timeout=httpx.Timeout(1.0),
            trust_env=False,
        ) as client:
            backend: ToolBackend | HttpFakeToolBackend = (
                backend_factory(client)
                if backend_factory is not None
                else HttpFakeToolBackend(client)
            )
            services = Services(
                http_client=client,
                tool_backend=backend,
                llm=FakeLLM(),
                global_tool_limit=asyncio.Semaphore(4),
                global_probe=ConcurrencyProbe(),
                trace=trace,
            )
            application.state.services = services
            application.state.last_http_client = client
            lifecycle_events.append("startup:ready")
            try:
                yield
            finally:
                lifecycle_events.append("shutdown:closing-client")
        lifecycle_events.append(f"shutdown:client-closed={client.is_closed}")

    application = FastAPI(
        title="第 28 章：本地 Agent API",
        version="1.0.0",
        lifespan=lifespan,
    )
    application.state.trace = trace
    application.state.lifecycle_events = lifecycle_events
    application.add_middleware(RequestContextMiddleware)

    @application.exception_handler(InternalServiceError)
    async def internal_service_error_handler(
        request: Request,
        error: InternalServiceError,
    ) -> JSONResponse:
        request_id: str = current_request_id()
        logger.error(
            "internal failure request_id=%s path=%s error=%s",
            request_id,
            request.url.path,
            error,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": {
                    "code": "internal_service_error",
                    "message": "工具编排发生内部故障",
                    "request_id": request_id,
                }
            },
        )

    @application.get("/health")
    async def health(services: ServicesDependency) -> dict[str, object]:
        return {"status": "ok", "shared_client_closed": services.http_client.is_closed}

    @application.get("/execution/sync", response_model=ExecutionInfo)
    def sync_execution_route() -> ExecutionInfo:
        route_thread: str = threading.current_thread().name
        return ExecutionInfo(
            route_kind="def",
            route_thread=route_thread,
            helper_thread=sync_helper_thread_name(),
            request_id=current_request_id(),
        )

    @application.get("/execution/async", response_model=ExecutionInfo)
    async def async_execution_route() -> ExecutionInfo:
        route_thread: str = threading.current_thread().name
        helper_thread: str = sync_helper_thread_name()
        return ExecutionInfo(
            route_kind="async def",
            route_thread=route_thread,
            helper_thread=helper_thread,
            request_id=current_request_id(),
        )

    @application.post(
        "/query",
        response_model=QueryResponse,
        status_code=status.HTTP_200_OK,
    )
    async def query(
        payload: QueryRequest,
        background_tasks: BackgroundTasks,
        services: ServicesDependency,
        lease: Annotated[RequestLease, Depends(open_request_lease, scope="function")],
    ) -> QueryResponse:
        lease.assert_open()
        if payload.query == "missing":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="没有匹配的本地资料",
            )

        try:
            results: list[ToolResult]
            max_parallel: int
            answer: str
            results, max_parallel, answer = await orchestrate_query(services, payload)
        except QueryBudgetExceeded as error:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="本次查询超过总预算",
            ) from error

        background_tasks.add_task(services.write_audit, lease.request_id, payload.query)
        return QueryResponse(
            request_id=lease.request_id,
            answer=answer,
            results=results,
            max_parallel_tools=max_parallel,
            background_audit_scheduled=True,
        )

    @application.post(
        "/query/stream",
        response_class=StreamingResponse,
        status_code=status.HTTP_200_OK,
    )
    async def query_stream(
        payload: QueryRequest,
        services: ServicesDependency,
        lease: Annotated[RequestLease, Depends(open_request_lease, scope="request")],
    ) -> StreamingResponse:
        lease.assert_open()
        if payload.query == "missing":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="没有匹配的本地资料",
            )
        return StreamingResponse(
            stream_query_events(services, payload, lease),
            status_code=status.HTTP_200_OK,
            media_type="application/x-ndjson",
        )

    return application


app: FastAPI = create_app()


if __name__ == "__main__":
    print(
        json.dumps(
            {
                "message": "请用 Uvicorn 启动本模块，命令见 README.md",
                "app": "app:app",
            },
            ensure_ascii=False,
        )
    )
