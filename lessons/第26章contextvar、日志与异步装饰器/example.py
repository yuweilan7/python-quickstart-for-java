"""第 26 章：contextvars、日志与异步装饰器。

所有并发顺序都由 Event 或已经发生的状态控制；耗时只用于观察，不做精确数值断言。
示例只使用标准库，不连接真实 API、数据库、日志平台或分布式追踪服务。
"""

# 推荐按编号从 1 读到 10：前五节先建立 Context 的传播模型，后五节再把它用于日志与装饰器。
# 整个文件的数据流是“请求 Task 设置上下文 -> 子 Task/线程读取 -> Filter 注入日志 -> 断言核对”。
# Event 负责规定并发步骤的先后，因此实验结果来自明确的 happens-before 关系，而不是碰巧的调度。

import asyncio
import contextvars
import inspect
import logging
import sys
import threading

from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, Token
from dataclasses import dataclass
from functools import wraps
from time import perf_counter_ns
from typing import Any, cast


# ContextVar 应在模块顶层声明：Context 会持有变量本身的引用，不应在每次请求的闭包里重复创建。
REQUEST_ID: ContextVar[str] = ContextVar[str]("request_id", default="-")
RUN_ID: ContextVar[str] = ContextVar[str]("run_id", default="-")
TOOL_NAME: ContextVar[str] = ContextVar[str]("tool_name", default="-")

# 这个变量故意没有默认值：使用前必须先 set，否则 get() 会抛 LookupError。
# 它保存可变 dict，用于第 4 节证明复制 Context 不等于深拷贝其中的值。
MUTABLE_BAG: ContextVar[dict[str, list[str]]] = ContextVar[dict[str, list[str]]](
    "mutable_bag"
)

# 教学反例：模块全局名字只有一个进程级绑定，所有 asyncio Task 都读写同一个值。
GLOBAL_REQUEST_ID = "-"

LOGGER: logging.Logger = logging.getLogger("chapter26")
CAPTURED_RECORDS: list[logging.LogRecord] = []


def print_section(title: str) -> None:
    """集中打印章节标题。"""
    print(f"\n=== {title} ===")


class ContextFieldsFilter(logging.Filter):
    """在输出边界把当前上下文字段补到每条 LogRecord。"""

    def filter(self, record: logging.LogRecord) -> bool:
        # 让调用方通过 extra 显式给出的字段优先；普通业务日志才从当前 Context 补值。
        if not hasattr(record, "request_id"):
            record.__dict__["request_id"] = REQUEST_ID.get()
        if not hasattr(record, "run_id"):
            record.__dict__["run_id"] = RUN_ID.get()
        if not hasattr(record, "tool_name"):
            record.__dict__["tool_name"] = TOOL_NAME.get()

        if not hasattr(record, "task_name"):
            try:
                task: asyncio.Task[Any] | None = asyncio.current_task()
            except RuntimeError:
                task = None
            task_name: str = (
                task.get_name()
                if task is not None
                else f"thread:{threading.current_thread().name}"
            )
            record.__dict__["task_name"] = task_name
        return True


class CapturingHandler(logging.Handler):
    """保存记录，供示例用状态断言验证日志归属和取消状态。"""

    def emit(self, record: logging.LogRecord) -> None:
        CAPTURED_RECORDS.append(record)


def configure_logging() -> None:
    """只配置本章自己的 logger，不改应用的 root logger。"""
    LOGGER.handlers.clear()
    LOGGER.filters.clear()
    CAPTURED_RECORDS.clear()

    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    LOGGER.addFilter(ContextFieldsFilter())

    console: logging.StreamHandler[Any] = logging.StreamHandler(sys.stdout)
    console.setFormatter(
        logging.Formatter(
            "%(levelname)s request=%(request_id)s run=%(run_id)s "
            "task=%(task_name)s tool=%(tool_name)s | %(message)s"
        )
    )
    LOGGER.addHandler(console)
    LOGGER.addHandler(CapturingHandler())


@dataclass(slots=True)
class ToolGate:
    """用显式事件控制工具何时开始等待、何时继续。"""

    started: asyncio.Event
    may_finish: asyncio.Event

    @classmethod
    def create(cls) -> ToolGate:
        return cls(asyncio.Event(), asyncio.Event())


@dataclass(frozen=True, slots=True)
class ToolObservation:
    """把工具标签、实际看到的请求和运行标识一起交回调用方。"""

    label: str
    observed_request_id: str
    observed_run_id: str


# ---------------------------------------------------------------------------
# 1. 全局变量为何在单线程 asyncio 中串值
# ---------------------------------------------------------------------------
async def globally_scoped_tool(label: str, gate: ToolGate) -> ToolObservation:
    gate.started.set()
    await gate.may_finish.wait()
    observed: str = GLOBAL_REQUEST_ID
    LOGGER.info(
        "全局反例读取上下文 label=%s",
        label,
        extra={
            "request_id": observed,
            "run_id": "bad-global",
            "tool_name": "globally_scoped_tool",
        },
    )
    return ToolObservation(label, observed, "bad-global")


async def run_bad_request(
    request_id: str,
    *,
    first_request_has_set: asyncio.Event,
    second_request_has_set: asyncio.Event,
    is_first: bool,
) -> tuple[ToolObservation, ...]:
    global GLOBAL_REQUEST_ID

    if is_first:
        GLOBAL_REQUEST_ID = request_id
        first_request_has_set.set()
        await second_request_has_set.wait()
    else:
        await first_request_has_set.wait()
        GLOBAL_REQUEST_ID = request_id
        second_request_has_set.set()

    gates: tuple[ToolGate, ToolGate] = (ToolGate.create(), ToolGate.create())
    tasks: tuple[asyncio.Task[ToolObservation], ...] = tuple(
        asyncio.create_task(
            globally_scoped_tool(f"{request_id}-tool-{index}", gate),
            name=f"global:{request_id}:{index}",
        )
        for index, gate in enumerate(gates, start=1)
    )
    await asyncio.gather(*(gate.started.wait() for gate in gates))
    for gate in gates:
        gate.may_finish.set()
    return tuple(await asyncio.gather(*tasks))


async def demonstrate_global_leak() -> None:
    print_section("1. 反例：单线程中的全局 request_id 仍会串值")
    global GLOBAL_REQUEST_ID

    previous_value: str = GLOBAL_REQUEST_ID
    first_has_set = asyncio.Event()
    second_has_set = asyncio.Event()
    try:
        request_a: asyncio.Task[tuple[ToolObservation, ...]] = asyncio.create_task(
            run_bad_request(
                "REQ-A",
                first_request_has_set=first_has_set,
                second_request_has_set=second_has_set,
                is_first=True,
            ),
            name="global-request:A",
        )
        request_b: asyncio.Task[tuple[ToolObservation, ...]] = asyncio.create_task(
            run_bad_request(
                "REQ-B",
                first_request_has_set=first_has_set,
                second_request_has_set=second_has_set,
                is_first=False,
            ),
            name="global-request:B",
        )
        results_a, results_b = await asyncio.gather(request_a, request_b)
    finally:
        GLOBAL_REQUEST_ID = previous_value

    observed_a: list[str] = [item.observed_request_id for item in results_a]
    observed_b: list[str] = [item.observed_request_id for item in results_b]
    print("REQ-A 的两个工具实际读到：", observed_a)
    print("REQ-B 的两个工具实际读到：", observed_b)
    assert observed_a == ["REQ-B", "REQ-B"]
    assert observed_b == ["REQ-B", "REQ-B"]
    assert GLOBAL_REQUEST_ID == previous_value


# ---------------------------------------------------------------------------
# 2. get / set / reset / token 与嵌套恢复
# ---------------------------------------------------------------------------
def demonstrate_token_lifecycle() -> None:
    print_section("2. ContextVar：set 返回 token，finally 按层恢复")
    initial: str = REQUEST_ID.get()

    outer_token: Token[str] = REQUEST_ID.set("REQ-OUTER")
    try:
        print("外层值：", REQUEST_ID.get())
        print("外层此前没有显式绑定：", outer_token.old_value is Token.MISSING)

        inner_token: Token[str] = REQUEST_ID.set("REQ-INNER")
        try:
            print("嵌套覆盖后：", REQUEST_ID.get())
        finally:
            REQUEST_ID.reset(inner_token)
        print("内层退出，恢复外层：", REQUEST_ID.get())

        # Python 3.14 起 Token 实现同步上下文管理器协议。
        with REQUEST_ID.set("REQ-3.14-WITH"):
            print("3.14 with token 内：", REQUEST_ID.get())
        print("3.14 with token 后：", REQUEST_ID.get())
    finally:
        REQUEST_ID.reset(outer_token)

    print("整段结束，恢复初始值：", REQUEST_ID.get())
    assert REQUEST_ID.get() == initial == "-"


# ---------------------------------------------------------------------------
# 3. Task 创建时复制 Context：后续 set 只改各自 Context
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TaskContextObservation:
    at_child_start: str
    after_child_set: str
    after_child_reset: str


async def child_context_probe(
    started: asyncio.Event,
    may_change: asyncio.Event,
) -> TaskContextObservation:
    inherited: str = REQUEST_ID.get()
    started.set()
    await may_change.wait()

    child_token: Token[str] = REQUEST_ID.set("REQ-CHILD-LOCAL")
    try:
        child_local: str = REQUEST_ID.get()
    finally:
        REQUEST_ID.reset(child_token)
    return TaskContextObservation(inherited, child_local, REQUEST_ID.get())


async def demonstrate_task_context_copy() -> None:
    print_section("3. Task 在创建时复制上下文，父子后续修改相互隔离")

    initial_token: Token[str] = REQUEST_ID.set("REQ-PARENT-OUTER")
    try:
        before_create_token: Token[str] = REQUEST_ID.set("REQ-BEFORE-CREATE")
        try:
            started = asyncio.Event()
            may_change = asyncio.Event()
            child: asyncio.Task[TaskContextObservation] = asyncio.create_task(
                child_context_probe(started, may_change),
                name="context-copy-child",
            )

            after_create_token: Token[str] = REQUEST_ID.set("REQ-AFTER-CREATE")
            try:
                await started.wait()
                may_change.set()
                child_values: TaskContextObservation = await child
                parent_value: str = REQUEST_ID.get()
            finally:
                REQUEST_ID.reset(after_create_token)
        finally:
            REQUEST_ID.reset(before_create_token)
    finally:
        REQUEST_ID.reset(initial_token)

    print("子 Task 开始时：", child_values.at_child_start)
    print("子 Task 自己 set 后：", child_values.after_child_set)
    print("子 Task reset 后：", child_values.after_child_reset)
    print("父 Task 在子 Task 修改后：", parent_value)
    assert child_values == TaskContextObservation(
        "REQ-BEFORE-CREATE",
        "REQ-CHILD-LOCAL",
        "REQ-BEFORE-CREATE",
    )
    assert parent_value == "REQ-AFTER-CREATE"
    assert REQUEST_ID.get() == "-"


# ---------------------------------------------------------------------------
# 4. 复制 Context 不会深拷贝 ContextVar 中的值
# ---------------------------------------------------------------------------
async def append_first(shared_changed: asyncio.Event, peer_changed: asyncio.Event) -> int:
    bag: dict[str, list[str]] = MUTABLE_BAG.get()
    bag["events"].append("child-A")
    shared_changed.set()
    await peer_changed.wait()
    return id(bag)


async def append_second(shared_changed: asyncio.Event, peer_changed: asyncio.Event) -> int:
    await shared_changed.wait()
    bag: dict[str, list[str]] = MUTABLE_BAG.get()
    bag["events"].append("child-B")
    peer_changed.set()
    return id(bag)


async def demonstrate_shallow_context_copy() -> None:
    print_section("4. Context 的映射被复制，可变值本身没有深拷贝")
    shared_bag: dict[str, list[str]] = {"events": []}
    bag_token: Token[dict[str, list[str]]] = MUTABLE_BAG.set(shared_bag)
    try:
        snapshot: contextvars.Context = contextvars.copy_context()
        first_changed = asyncio.Event()
        second_changed = asyncio.Event()
        task_a: asyncio.Task[int] = asyncio.create_task(
            append_first(first_changed, second_changed), name="mutable-context:A"
        )
        task_b: asyncio.Task[int] = asyncio.create_task(
            append_second(first_changed, second_changed), name="mutable-context:B"
        )
        first_identity, second_identity = await asyncio.gather(task_a, task_b)

        print("copy_context 中仍是同一 dict：", snapshot[MUTABLE_BAG] is shared_bag)
        print("父 Task 最终也看到：", shared_bag["events"])
        print("父子引用身份相同：", id(shared_bag) == first_identity == second_identity)
        assert snapshot[MUTABLE_BAG] is shared_bag
        assert shared_bag["events"] == ["child-A", "child-B"]
        assert id(shared_bag) == first_identity == second_identity
    finally:
        MUTABLE_BAG.reset(bag_token)


# ---------------------------------------------------------------------------
# 5. asyncio.to_thread 的上下文复制时机和 API 边界
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ThreadObservation:
    label: str
    request_id: str
    thread_name: str


def read_context_in_thread(label: str) -> ThreadObservation:
    return ThreadObservation(label, REQUEST_ID.get(), threading.current_thread().name)


async def demonstrate_to_thread_context() -> None:
    print_section("5. to_thread：在 coroutine 真正开始时复制当前 Context")

    call_token: Token[str] = REQUEST_ID.set("REQ-WHEN-CALLED")
    try:
        deferred: Coroutine[Any, Any, ThreadObservation] = asyncio.to_thread(
            read_context_in_thread, "直接 await"
        )
        await_token: Token[str] = REQUEST_ID.set("REQ-WHEN-AWAITED")
        try:
            directly_awaited: ThreadObservation = await deferred
        finally:
            REQUEST_ID.reset(await_token)
    finally:
        REQUEST_ID.reset(call_token)

    task_token: Token[str] = REQUEST_ID.set("REQ-WHEN-TASK-CREATED")
    try:
        thread_task: asyncio.Task[ThreadObservation] = asyncio.create_task(
            asyncio.to_thread(read_context_in_thread, "先创建 Task"),
            name="to-thread-proxy",
        )
        parent_changed_token: Token[str] = REQUEST_ID.set("REQ-PARENT-CHANGED-LATER")
        try:
            scheduled_as_task: ThreadObservation = await thread_task
            parent_after_creation: str = REQUEST_ID.get()
        finally:
            REQUEST_ID.reset(parent_changed_token)
    finally:
        REQUEST_ID.reset(task_token)

    executor_token: Token[str] = REQUEST_ID.set("REQ-EXECUTOR-CALLER")
    try:
        loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="chapter26") as executor:
            direct_executor: ThreadObservation = await loop.run_in_executor(
                executor,
                read_context_in_thread,
                "run_in_executor 直接提交",
            )
            copied: contextvars.Context = contextvars.copy_context()

            def read_in_copied_context() -> ThreadObservation:
                return copied.run(
                    read_context_in_thread,
                    "run_in_executor + Context.run",
                )

            explicit_executor: ThreadObservation = await loop.run_in_executor(
                executor,
                read_in_copied_context,
            )
    finally:
        REQUEST_ID.reset(executor_token)

    print("调用后改值、再直接 await，线程看到：", directly_awaited.request_id)
    print("create_task 后父 Task 改值，线程看到：", scheduled_as_task.request_id)
    print("同期父 Task 自己看到：", parent_after_creation)
    print("run_in_executor 直接提交，线程看到：", direct_executor.request_id)
    print("显式 Context.run 提交，线程看到：", explicit_executor.request_id)
    assert directly_awaited.request_id == "REQ-WHEN-AWAITED"
    assert scheduled_as_task.request_id == "REQ-WHEN-TASK-CREATED"
    assert parent_after_creation == "REQ-PARENT-CHANGED-LATER"
    assert direct_executor.request_id == "-"
    assert explicit_executor.request_id == "REQ-EXECUTOR-CALLER"
    assert REQUEST_ID.get() == "-"


# ---------------------------------------------------------------------------
# 6. 范围明确、类型关系保真的异步日志装饰器
# ---------------------------------------------------------------------------
def observe_async[**P, R](
    function: Callable[P, Awaitable[R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    """只包装“一次 await 得到一个最终结果”的异步可调用对象。"""

    @wraps(function)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        tool_token: Token[str] = TOOL_NAME.set(function.__name__)
        started_ns: int = perf_counter_ns()
        LOGGER.info("工具调用开始 status=started")
        try:
            result: R = await function(*args, **kwargs)
        except asyncio.CancelledError:
            elapsed_ms: float = (perf_counter_ns() - started_ns) / 1_000_000
            LOGGER.warning("工具调用取消 status=cancelled elapsed_ms=%.3f", elapsed_ms)
            raise
        except Exception as error:
            elapsed_ms = (perf_counter_ns() - started_ns) / 1_000_000
            LOGGER.exception(
                "工具调用失败 status=error elapsed_ms=%.3f error_type=%s",
                elapsed_ms,
                type(error).__name__,
            )
            raise
        else:
            elapsed_ms = (perf_counter_ns() - started_ns) / 1_000_000
            LOGGER.info("工具调用成功 status=success elapsed_ms=%.3f", elapsed_ms)
            return result
        finally:
            TOOL_NAME.reset(tool_token)

    return wrapper


def wrong_sync_timing[**P, R](
    function: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """教学反例：同步 wrapper 只测量了 coroutine 对象的创建。"""

    @wraps(function)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> Awaitable[R]:
        started_ns: int = perf_counter_ns()
        awaitable: Awaitable[R] = function(*args, **kwargs)
        elapsed_ms: float = (perf_counter_ns() - started_ns) / 1_000_000
        print(f"错误计时 coroutine 创建耗时={elapsed_ms:.3f} ms")
        return awaitable

    return wrapper


@wrong_sync_timing
async def gated_coroutine(started: asyncio.Event, may_finish: asyncio.Event) -> str:
    started.set()
    await may_finish.wait()
    return "真正的异步工作已完成"


type AnyAsyncTool = Callable[..., Awaitable[object]]


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """注册表的值同时保存外部名称与真正要调用的函数对象。"""

    name: str
    function: AnyAsyncTool


TOOL_REGISTRY: dict[str, RegisteredTool] = {}


def register_tool[**P, R](
    name: str,
) -> Callable[
    [Callable[P, Awaitable[R]]],
    Callable[P, Awaitable[R]],
]:
    """最小异步注册器：只保存当前收到的对象，再原样返回。"""

    def decorate(
        function: Callable[P, Awaitable[R]],
    ) -> Callable[P, Awaitable[R]]:
        if name in TOOL_REGISTRY:
            raise ValueError(f"工具名称已注册：{name}")
        TOOL_REGISTRY[name] = RegisteredTool(name, cast(AnyAsyncTool, function))
        return function

    return decorate


async def invoke_registered_tool(
    name: str,
    /,
    *args: object,
    **kwargs: object,
) -> object:
    try:
        metadata = TOOL_REGISTRY[name]
    except KeyError:
        raise LookupError(f"未注册工具：{name}") from None
    return await metadata.function(*args, **kwargs)


@register_tool("lookup")
@observe_async
async def lookup_documents(
    query: str,
    gate: ToolGate,
    /,
    *,
    limit: int = 2,
) -> ToolObservation:
    """在内存本地替身中模拟一次文档工具 I/O。"""
    LOGGER.info("文档查询开始 query_length=%d limit=%d", len(query), limit)
    gate.started.set()
    await gate.may_finish.wait()
    LOGGER.info("文档查询完成 result_count=%d", min(limit, 2))
    return ToolObservation(query, REQUEST_ID.get(), RUN_ID.get())


class ToolExecutionError(RuntimeError):
    """本地替身的受控失败。"""


@register_tool("fail")
@observe_async
async def failing_tool(gate: ToolGate) -> None:
    gate.started.set()
    await gate.may_finish.wait()
    raise ToolExecutionError("受控工具失败")


@register_tool("wait")
@observe_async
async def waiting_tool(gate: ToolGate) -> str:
    gate.started.set()
    await gate.may_finish.wait()
    return "不应在取消示例中成功"


# 这里故意反转顺序：注册表会先保存原函数，公开名字随后才被 observe_async 包装。
@observe_async
@register_tool("registered-before-observed")
async def registered_before_observed() -> str:
    """只用于观察反向顺序：注册表会保存未包装的原函数。"""
    return "order probe"


async def demonstrate_async_wrapper() -> None:
    print_section("6. async wrapper 的计时覆盖真正 await，wraps 保留元信息")
    started = asyncio.Event()
    may_finish = asyncio.Event()

    awaitable: Awaitable[str] = gated_coroutine(started, may_finish)
    print("同步 wrapper 返回时，原函数体已开始：", started.is_set())
    assert not started.is_set()

    task: asyncio.Task[str] = asyncio.ensure_future(awaitable)
    await started.wait()
    print("调度后函数体开始，但尚未完成：", started.is_set() and not task.done())
    may_finish.set()
    assert await task == "真正的异步工作已完成"

    print("装饰后的名字：", lookup_documents.__name__)
    print("inspect.signature：", inspect.signature(lookup_documents))
    print("lookup 注册的是最终 wrapper：", TOOL_REGISTRY["lookup"].function is lookup_documents)
    print(
        "反向顺序注册的是原函数：",
        TOOL_REGISTRY["registered-before-observed"].function
        is inspect.unwrap(registered_before_observed),
    )
    assert lookup_documents.__name__ == "lookup_documents"
    assert TOOL_REGISTRY["lookup"].function is lookup_documents
    assert TOOL_REGISTRY["registered-before-observed"].function is inspect.unwrap(
        registered_before_observed
    )


# ---------------------------------------------------------------------------
# 7. 两个请求、多个工具：正确日志归属
# ---------------------------------------------------------------------------
async def run_observed_request(
    request_id: str,
    run_id: str,
    queries: tuple[str, ...],
) -> tuple[ToolObservation, ...]:
    request_token: Token[str] = REQUEST_ID.set(request_id)
    run_token: Token[str] = RUN_ID.set(run_id)
    try:
        LOGGER.info("请求开始 tool_count=%d", len(queries))
        gates: tuple[ToolGate, ...] = tuple(ToolGate.create() for _ in queries)
        tasks: tuple[asyncio.Task[object], ...] = tuple(
            asyncio.create_task(
                invoke_registered_tool("lookup", query, gate, limit=2),
                name=f"context:{request_id}:lookup:{index}",
            )
            for index, (query, gate) in enumerate(
                zip(queries, gates, strict=True), start=1
            )
        )

        await asyncio.gather(*(gate.started.wait() for gate in gates))
        for gate in gates:
            gate.may_finish.set()

        dynamic_results: list[object] = list(await asyncio.gather(*tasks))
        results: list[ToolObservation] = []
        for result in dynamic_results:
            if not isinstance(result, ToolObservation):
                raise TypeError("lookup 注册项返回了意外类型")
            results.append(result)
        LOGGER.info("请求成功 success_count=%d", len(results))
        return tuple(results)
    finally:
        RUN_ID.reset(run_token)
        REQUEST_ID.reset(request_token)


async def demonstrate_concurrent_requests() -> None:
    print_section("7. 正例：两个请求各派生多个工具 Task，日志不串请求")
    request_a: asyncio.Task[tuple[ToolObservation, ...]] = asyncio.create_task(
        run_observed_request("REQ-100", "RUN-ALPHA", ("alpha", "beta")),
        name="request:REQ-100",
    )
    request_b: asyncio.Task[tuple[ToolObservation, ...]] = asyncio.create_task(
        run_observed_request("REQ-200", "RUN-BETA", ("gamma", "delta")),
        name="request:REQ-200",
    )
    results_a, results_b = await asyncio.gather(request_a, request_b)

    assert {item.observed_request_id for item in results_a} == {"REQ-100"}
    assert {item.observed_run_id for item in results_a} == {"RUN-ALPHA"}
    assert {item.observed_request_id for item in results_b} == {"REQ-200"}
    assert {item.observed_run_id for item in results_b} == {"RUN-BETA"}

    tool_records: list[logging.LogRecord] = [
        record
        for record in CAPTURED_RECORDS
        if str(getattr(record, "task_name", "")).startswith("context:")
    ]
    for record in tool_records:
        task_name = str(record.__dict__["task_name"])
        expected_request: str = task_name.split(":", maxsplit=3)[1]
        assert record.__dict__["request_id"] == expected_request
        assert record.__dict__["tool_name"] == "lookup_documents"

    print("已按 task 名逐条核对工具日志：", len(tool_records))
    print("入口退出后的默认 request_id：", REQUEST_ID.get())
    assert len(tool_records) == 16
    assert REQUEST_ID.get() == RUN_ID.get() == TOOL_NAME.get() == "-"


# ---------------------------------------------------------------------------
# 8. 异常与取消继续传播，且不能记成功
# ---------------------------------------------------------------------------
async def request_that_fails() -> None:
    request_token: Token[str] = REQUEST_ID.set("REQ-FAIL")
    run_token: Token[str] = RUN_ID.set("RUN-FAIL")
    try:
        gate = ToolGate.create()
        task: asyncio.Task[object] = asyncio.create_task(
            invoke_registered_tool("fail", gate),
            name="fail:registered-tool",
        )
        await gate.started.wait()
        gate.may_finish.set()
        await task
    finally:
        RUN_ID.reset(run_token)
        REQUEST_ID.reset(request_token)


async def demonstrate_failure_propagation() -> None:
    print_section("8. 普通异常：记录 traceback 后把原异常交给调用方")
    first_record: int = len(CAPTURED_RECORDS)
    try:
        await request_that_fails()
    except ToolExecutionError as error:
        print("调用方收到原异常：", type(error).__name__)
    else:
        raise AssertionError("受控工具失败应传播 ToolExecutionError")

    new_records: list[logging.LogRecord] = CAPTURED_RECORDS[first_record:]
    messages: list[str] = [record.getMessage() for record in new_records]
    assert any("status=error" in message for message in messages)
    assert not any("status=success" in message for message in messages)
    assert REQUEST_ID.get() == RUN_ID.get() == TOOL_NAME.get() == "-"


async def cancellable_request(gate: ToolGate) -> str:
    request_token: Token[str] = REQUEST_ID.set("REQ-CANCEL")
    run_token: Token[str] = RUN_ID.set("RUN-CANCEL")
    try:
        LOGGER.info("可取消请求开始")
        try:
            result: object = await invoke_registered_tool("wait", gate)
        except asyncio.CancelledError:
            LOGGER.warning("请求入口继续传播取消 status=cancelled")
            raise
        return cast(str, result)
    finally:
        RUN_ID.reset(run_token)
        REQUEST_ID.reset(request_token)


async def demonstrate_cancellation_propagation() -> None:
    print_section("9. 用户取消：工具和入口都记录 cancelled，不记录 success")
    first_record: int = len(CAPTURED_RECORDS)
    gate = ToolGate.create()
    request_task: asyncio.Task[str] = asyncio.create_task(
        cancellable_request(gate),
        name="cancel:REQ-CANCEL",
    )

    await gate.started.wait()
    request_task.cancel("用户停止请求")
    try:
        await request_task
    except asyncio.CancelledError:
        print("最外层收到 CancelledError，Task.cancelled()：", request_task.cancelled())
    else:
        raise AssertionError("取消必须继续传播到最外层")

    new_records: list[logging.LogRecord] = CAPTURED_RECORDS[first_record:]
    messages: list[str] = [record.getMessage() for record in new_records]
    assert sum("status=cancelled" in message for message in messages) == 2
    assert not any("status=success" in message for message in messages)
    assert request_task.cancelled()
    assert REQUEST_ID.get() == RUN_ID.get() == TOOL_NAME.get() == "-"


# ---------------------------------------------------------------------------
# 10. async generator 是另一种协议
# ---------------------------------------------------------------------------
async def token_stream() -> AsyncGenerator[str]:
    yield "token-1"
    yield "token-2"


async def demonstrate_async_generator_boundary() -> None:
    print_section("10. 边界：普通协程 wrapper 不直接适用于 async generator")
    stream: AsyncGenerator[str, None] = token_stream()
    print("token_stream 是异步生成器函数：", inspect.isasyncgenfunction(token_stream))
    print("调用结果可直接 await：", inspect.isawaitable(stream))

    first: str = await anext(stream)
    await stream.aclose()
    print("正确方式是 anext/async for，本次第一项：", first)
    assert first == "token-1"
    assert not inspect.isawaitable(stream)


async def run_examples() -> None:
    assert sys.version_info >= (3, 14)
    configure_logging()

    await demonstrate_global_leak()
    demonstrate_token_lifecycle()
    await demonstrate_task_context_copy()
    await demonstrate_shallow_context_copy()
    await demonstrate_to_thread_context()
    await demonstrate_async_wrapper()
    await demonstrate_concurrent_requests()
    await demonstrate_failure_propagation()
    await demonstrate_cancellation_propagation()
    await demonstrate_async_generator_boundary()

    print_section("完成")
    print("所有受控实验执行完毕，ContextVar 已恢复默认值。")
    assert GLOBAL_REQUEST_ID == REQUEST_ID.get() == RUN_ID.get() == TOOL_NAME.get() == "-"


def main() -> None:
    asyncio.run(run_examples())


if __name__ == "__main__":
    main()
