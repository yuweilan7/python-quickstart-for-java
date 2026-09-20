"""供第 27 章测试的最小 Agent 风格业务模块。

这里把第 14、21～26 章的关键行为缩成一个本地包，避免从编号目录跨章导入。
实现只访问进程内 transport 或调用方给出的临时 SQLite 文件。
"""

import asyncio
import sqlite3
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Iterable,
    Mapping,
)
from contextlib import aclosing, asynccontextmanager, closing
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator


class SearchArgs(BaseModel):
    """不可信模型参数进入工具前必须通过的运行时边界。"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(strict=True, min_length=1)
    limit: int = Field(default=3, strict=True, ge=1, le=5)

    @field_validator("query")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        normalized: str = value.strip()
        if not normalized:
            raise ValueError("query 不能只包含空白")
        return normalized


class ModelReply(BaseModel):
    """可控 HTTP 响应通过 JSON 解析后仍要验证结构。"""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(strict=True, min_length=1)


class ToolBusinessError(Exception):
    """合同内允许成为“部分失败结果”的已知业务错误。"""


class ToolProgramError(RuntimeError):
    """协议或程序不变量被破坏，必须让整个相关工作失败。"""


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    name: str
    value: str | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None


type ToolCall = Callable[[], Coroutine[Any, Any, str]]


async def _as_optional_outcome(name: str, call: ToolCall) -> ToolOutcome:
    """只把合同明确允许的业务失败转换为数据。"""
    try:
        return ToolOutcome(name=name, value=await call(), error=None)
    except ToolBusinessError as error:
        return ToolOutcome(name=name, value=None, error=str(error))


async def run_optional_tools(tools: Mapping[str, ToolCall]) -> list[ToolOutcome]:
    """工具相互独立：保留成功和已知业务失败，未知缺陷仍传播。"""
    async with asyncio.TaskGroup() as group:
        tasks: dict[str, asyncio.Task[ToolOutcome]] = {
            name: group.create_task(_as_optional_outcome(name, call), name=f"optional:{name}")
            for name, call in tools.items()
        }
    return [tasks[name].result() for name in tools]


async def run_required_tools(tools: Mapping[str, ToolCall]) -> dict[str, str]:
    """工具全部必需：任一失败时由 TaskGroup 取消并等待其兄弟。"""
    async with asyncio.TaskGroup() as group:
        tasks: dict[str, asyncio.Task[str]] = {
            name: group.create_task(call(), name=f"required:{name}")
            for name, call in tools.items()
        }
    return {name: tasks[name].result() for name in tools}


async def run_required_tools_before(
    tools: Mapping[str, ToolCall],
    *,
    deadline: float,
) -> dict[str, str]:
    """用同一个绝对截止时间覆盖任务组执行与退出清理。"""
    async with asyncio.timeout_at(deadline):
        return await run_required_tools(tools)


async def run_with_limit[T, R](
    items: Iterable[T],
    *,
    max_in_flight: int,
    operation: Callable[[T], Awaitable[R]],
) -> list[R]:
    """TaskGroup 管生命周期，Semaphore 只管 operation 的在途数量。"""
    if max_in_flight < 1:
        raise ValueError("max_in_flight 必须至少为 1")

    semaphore = asyncio.Semaphore(max_in_flight)

    async def call_one(item: T) -> R:
        async with semaphore:
            return await operation(item)

    async with asyncio.TaskGroup() as group:
        tasks: list[asyncio.Task[R]] = [group.create_task(call_one(item)) for item in items]
    return [task.result() for task in tasks]


@dataclass(slots=True)
class ToolLifecycle:
    """用 Event 暴露任务的开始、放行和清理阶段。"""

    started: asyncio.Event = field(default_factory=asyncio.Event)
    may_finish: asyncio.Event = field(default_factory=asyncio.Event)
    cleaned: asyncio.Event = field(default_factory=asyncio.Event)
    events: list[str] = field(default_factory=list)


async def wait_for_tool(lifecycle: ToolLifecycle) -> str:
    """一个必须在正常、失败或取消路径执行 finally 的资源型操作。"""
    lifecycle.events.append("started")
    lifecycle.started.set()
    try:
        await lifecycle.may_finish.wait()
        lifecycle.events.append("completed")
        return "tool-result"
    finally:
        lifecycle.events.append("cleaned")
        lifecycle.cleaned.set()


@dataclass(slots=True)
class StreamState:
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    close_count: int = 0
    events: list[str] = field(default_factory=list)


async def token_stream(state: StreamState) -> AsyncGenerator[str]:
    """本地异步生成器替身；不模拟真实网络分块、连接池或协议错误。"""
    state.events.append("stream-open")
    try:
        for token in ("agent", " ", "ready"):
            await asyncio.sleep(0)
            yield token
    finally:
        state.close_count += 1
        state.events.append("stream-closed")
        state.closed.set()


@asynccontextmanager
async def owned_token_stream(state: StreamState) -> AsyncIterator[AsyncGenerator[str]]:
    """创建者拥有流；消费者提前退出也会在作用域出口 await aclose。"""
    stream: AsyncGenerator[str, None] = token_stream(state)
    async with aclosing(stream):
        yield stream


async def read_first_token(state: StreamState) -> str:
    async with owned_token_stream(state) as stream:
        async for token in stream:
            return token
    raise RuntimeError("流没有产生任何 token")


async def buggy_read_first_token(state: StreamState) -> tuple[str, AsyncGenerator[str]]:
    """教学缺陷：提前返回但把仍打开的流留给调用方。

    返回流引用只是为了让受控失败测试能在 finally 中回收它，不能当作正确 API。
    """
    stream: AsyncGenerator[str, None] = token_stream(state)
    first: str = await anext(stream)
    return first, stream


request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


async def observe_request_context(
    request_id: str,
    barrier: asyncio.Barrier,
) -> tuple[str, str]:
    """两个请求在同一屏障交错，仍各自读取自己的 ContextVar 值。"""
    token = request_id_var.set(request_id)
    try:
        before: str = request_id_var.get()
        await barrier.wait()
        after: str = request_id_var.get()
        return before, after
    finally:
        request_id_var.reset(token)


def flatten_exception_group(group: BaseExceptionGroup[BaseException]) -> list[BaseException]:
    """递归取得异常组叶子，避免只检查最外层容器。"""
    leaves: list[BaseException] = []
    for error in group.exceptions:
        if isinstance(error, BaseExceptionGroup):
            leaves.extend(flatten_exception_group(error))
        else:
            leaves.append(error)
    return leaves


def classify_failure(error: BaseException) -> str:
    """分类顺序让取消不被宽泛的普通错误分支吞掉。"""
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, ToolBusinessError):
        return "business"
    if isinstance(error, TimeoutError):
        return "timeout"
    return "program-defect"


class AsyncTextCall(Protocol):
    async def __call__(self, prompt: str) -> str: ...


async def ask_model(model_call: AsyncTextCall, prompt: str) -> str:
    """依赖必须返回 awaitable；普通 Mock 的字符串返回值不能被 await。"""
    return await model_call(prompt)


async def call_model_api(prompt: str) -> str:
    """真实项目中的外部边界；离线测试应在使用位置替换它。"""
    raise RuntimeError(f"离线示例不会调用真实模型：{prompt}")


async def answer_with_default_model(prompt: str) -> str:
    return await call_model_api(prompt)


async def fetch_model_reply(
    client: httpx.AsyncClient,
    raw_args: object,
) -> ModelReply:
    """验证参数，再通过调用方拥有的 AsyncClient 访问 HTTP 边界。"""
    args: SearchArgs = SearchArgs.model_validate(raw_args)
    response = await client.get("/model", params={"query": args.query, "limit": args.limit})
    response.raise_for_status()
    return ModelReply.model_validate(response.json())


def initialize_notes(database_path: Path) -> None:
    """创建调用方指定的 SQLite 文件；测试会传入 tmp_path 下的路径。"""
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO notes (id, body) VALUES (?, ?)",
            ((1, "pytest fixture"), (2, "async cleanup")),
        )


def read_note(database_path: Path, note_id: int) -> str | None:
    with closing(sqlite3.connect(database_path)) as connection:
        row: Any = connection.execute(
            "SELECT body FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()
    return None if row is None else str(row[0])
