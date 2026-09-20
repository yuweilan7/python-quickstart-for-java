"""第 21 章：取消、超时与截止时间。

示例只使用本地 Event 和可观察资源状态，不访问真实网络或线程。短超时只负责
触发 asyncio 的超时机制；断言验证事件顺序和最终状态，不断言精确耗时。
"""

# 建议按 run_examples() 中的编号顺序阅读：先看正常完成的基线，再逐步加入主动取消、
# 相对超时、绝对截止时间和 shield。所有实验都把关键动作写入 events，最后用断言验证
# “先发生什么、后发生什么”；短小的超时数值只负责触发机制，不是要测事件循环性能。
# asyncio 提供 Task、Event 和超时上下文；time 只在“阻塞事件循环”的反例中使用。
import asyncio
import time

# Awaitable[T] 表示“可被 await、完成后产生 T”的对象，既可描述 coroutine，也可描述 Task。
from collections.abc import Awaitable

# Enum 建模资源的离散状态；auto() 自动分配成员值，本例只关心成员身份而不关心具体数字。
from enum import Enum, auto


# 三个枚举成员描述 FakeIOResource 从创建到释放的单向生命周期。
# Enum 成员是单例，因此后面用 is / is not 比较成员身份，而不是比较字符串或整数值。
class ResourceState(Enum):
    """模拟 I/O 资源的最小生命周期。"""

    NEW = auto()
    OPEN = auto()
    RELEASED = auto()


# 自定义异常不需要额外方法：继承 TimeoutError 后，既能被精确捕获为“工具预算耗尽”，
# 也保留了超时异常的类别关系。类体中的 docstring 已足以构成合法类体。
class ToolBudgetExceeded(TimeoutError):
    """表示工具自己的预算先于请求总截止时间耗尽。"""


# 标题打印函数没有异步工作；-> None 表示它只产生输出，不返回有意义的业务值。
def print_section(title: str) -> None:
    print(f"\n=== {title} ===")


# events 是调用方传入的同一个可变列表。append() 会原地写入共享的事件轨迹，print() 则让
# 运行者同步看到轨迹；后面的 index() 和生成器表达式会基于这份列表验证因果顺序。
def record(events: list[str], message: str) -> None:
    """事件同时打印和保存，便于肉眼观察并做因果顺序断言。"""
    events.append(message)
    print(f"  事件 | {message}")


class FakeIOResource:
    """只模拟本地资源状态，不声称等价于 socket、HTTP 或远程服务。"""

    def __init__(self, name: str, events: list[str]) -> None:
        # self 保存每个资源实例自己的属性；events 不会被复制，因此资源层和调用层记录到
        # 同一个列表中。初始状态 NEW 与计数 0 为后面的“一次且仅一次释放”断言提供基线。
        self.name: str = name
        self.events: list[str] = events
        self.state: Literal[ResourceState.NEW] = ResourceState.NEW
        self.release_count = 0

    def open(self) -> None:
        # 非 NEW 状态意味着资源已经打开过或释放过。先检查再改状态，能让生命周期错误立即
        # 暴露，而不是悄悄覆盖旧状态。
        if self.state is not ResourceState.NEW:
            raise RuntimeError(f"{self.name}: 资源只能打开一次")
        self.state: Literal[ResourceState.OPEN] = ResourceState.OPEN
        record(self.events, f"{self.name}: 资源已打开")

    def release(self) -> None:
        # 本例把重复释放视为错误，借此验证 finally 没有运行两次。真实资源若要求幂等关闭，
        # 可以采用不同策略，但仍应明确记录所有权。
        if self.state is ResourceState.RELEASED:
            raise RuntimeError(f"{self.name}: 资源被重复释放")
        self.state: Literal[ResourceState.RELEASED] = ResourceState.RELEASED
        self.release_count += 1
        record(self.events, f"{self.name}: 资源已释放")


async def simulated_io(
    resource: FakeIOResource,
    work_started: asyncio.Event,
    work_may_finish: asyncio.Event,
    cleanup_started: asyncio.Event,
    cleanup_may_finish: asyncio.Event | None = None,
) -> str:
    """三层链路的 I/O 层：取消时先记录，再由 finally 释放资源。"""
    # 四个 Event 都是本地“闸门”：set() 只把状态改为已设置并唤醒等待者，wait() 则在未设置
    # 时挂起当前 Task。用它们协调实验，可准确知道工作和清理走到了哪里，不必靠 sleep 猜测。
    resource.open()
    try:
        # 先记录并通知调用方“I/O 已就位”，随后等待 work_may_finish。正常实验会打开这个
        # 闸门；取消实验保持它关闭，使 Task 稳定地停在这个 await 上等待取消注入。
        record(resource.events, f"{resource.name}: I/O 已进入等待阶段")
        work_started.set()
        await work_may_finish.wait()
        record(resource.events, f"{resource.name}: I/O 正常完成")
        return f"{resource.name} 的结果"
    except asyncio.CancelledError as error:
        # cancel(message) 传入的消息可出现在 error.args 中；!r 使用 repr 形式显示参数元组，
        # 便于区分空参数 () 和带消息的 ('用户按下停止',)。这里只为记录传播方向。
        # 裸 raise 会原样重抛当前异常；若在此返回普通值，Task 会被标记为成功而不是取消。
        record(resource.events, f"{resource.name}: I/O 层收到 CancelledError{error.args!r}")
        raise
    finally:
        # finally 在正常返回和异常传播两条路径都会执行，因此资源释放只需放在一个位置。
        # cleanup_started 让外层观察“取消已经注入、但资源还没释放”的中间阶段。
        record(resource.events, f"{resource.name}: finally 开始清理")
        cleanup_started.set()
        if cleanup_may_finish is not None:
            # 某些真实清理也是异步的。任务在这段时间仍未完成，也尚未进入
            # cancelled() 为真的最终状态。is not None 是在判断是否提供了可选闸门；
            # Event 本身不应拿来做真假值判断。
            await cleanup_may_finish.wait()
        resource.release()


async def call_tool(
    resource: FakeIOResource,
    work_started: asyncio.Event,
    work_may_finish: asyncio.Event,
    cleanup_started: asyncio.Event,
    cleanup_may_finish: asyncio.Event | None = None,
) -> str:
    """工具层直接 await I/O；取消沿同一个等待链向内传播。"""
    # 这里没有 create_task()，所以 call_tool 与 simulated_io 属于同一个 Task 的协程调用栈。
    # result 只有在 I/O 正常返回时才会绑定；取消会直接跳到 except 分支。
    record(resource.events, f"{resource.name}: 工具层开始")
    try:
        result: str = await simulated_io(
            resource,
            work_started,
            work_may_finish,
            cleanup_started,
            cleanup_may_finish,
        )
        record(resource.events, f"{resource.name}: 工具层得到结果")
        return result
    except asyncio.CancelledError:
        # 工具层可以记录自己的语义上下文，但仍用裸 raise 保留异常类型、消息和 traceback。
        record(resource.events, f"{resource.name}: 工具层继续传播取消")
        raise


async def request_entry(
    resource: FakeIOResource,
    work_started: asyncio.Event,
    work_may_finish: asyncio.Event,
    cleanup_started: asyncio.Event,
    cleanup_may_finish: asyncio.Event | None = None,
) -> str:
    """入口层不把取消伪装成普通业务失败。"""
    # 入口 -> 工具 -> I/O 三层连续 await。任何一层收到取消后原样抛出，最终拥有 Task 的
    # 调用方才能区分“用户停止”和普通业务错误。
    record(resource.events, f"{resource.name}: 入口层开始")
    try:
        return await call_tool(
            resource,
            work_started,
            work_may_finish,
            cleanup_started,
            cleanup_may_finish,
        )
    except asyncio.CancelledError:
        record(resource.events, f"{resource.name}: 入口层继续传播取消")
        raise


async def demonstrate_normal_completion() -> None:
    # 正常路径是后续实验的对照组：工作闸门会被主动放行，没有任何 cancel 或 timeout。
    print_section("1. 正常完成：入口 -> 工具 -> 模拟 I/O -> finally")
    # 每个实验创建独立的列表、资源和 Event，避免上一段的状态污染下一段。
    events: list[str] = []
    resource = FakeIOResource("normal", events)
    work_started = asyncio.Event()
    work_may_finish = asyncio.Event()
    cleanup_started = asyncio.Event()

    # 调用 async def 只会得到 coroutine 对象；create_task() 才把它交给事件循环并立即返回
    # 可管理的 Task。name 只用于诊断，不影响调度。
    task: Task[str] = asyncio.create_task(
        request_entry(resource, work_started, work_may_finish, cleanup_started),
        name="normal-tool-chain",
    )
    # 等 I/O 层明确报告已经开始，再打开完成闸门；set() 后所有当前及未来 wait() 都可通过。
    await work_started.wait()
    work_may_finish.set()
    # await Task 取得 request_entry() 最终返回的字符串，并保证其 finally 已经执行完毕。
    result: str = await task

    # 输出展示返回值、Task 终态和资源终态；断言把教学预期变成可执行验收条件。
    print("  调用结果:", result)
    print("  Task 状态: done=", task.done(), "cancelled=", task.cancelled())
    print("  资源状态:", resource.state.name)
    assert cleanup_started.is_set()
    assert resource.state is ResourceState.RELEASED
    assert resource.release_count == 1
    assert task.done() and not task.cancelled()


async def demonstrate_cancellation_phases() -> None:
    # 这一段故意把异步清理暂停，分离“cancel 请求已接受”“异常已注入”“清理中”和
    # “Task 最终 cancelled”四个容易混淆的时刻。
    print_section("2. 用户主动停止：取消请求与最终取消是不同阶段")
    events: list[str] = []
    resource = FakeIOResource("user-stop", events)
    work_started = asyncio.Event()
    work_may_finish = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_may_finish = asyncio.Event()

    # 额外传入 cleanup_may_finish，使 simulated_io 的 finally 可以被测试代码控制。
    task: Task[str] = asyncio.create_task(
        request_entry(
            resource,
            work_started,
            work_may_finish,
            cleanup_started,
            cleanup_may_finish,
        ),
        name="user-stoppable-chain",
    )

    # Event 证明 I/O 已经进入等待阶段，取消不依赖“猜它大概启动了”。cancel() 的字符串
    # 会随 CancelledError 传播；布尔返回值只说明本次取消请求能否被登记。
    await work_started.wait()
    cancellation_accepted: bool = task.cancel("用户按下停止")
    record(events, "调用方: cancel() 已返回")

    # cancel() 只登记请求；当前代码尚未把执行权交回循环，目标 Task 还没机会
    # 在 await 位置收到 CancelledError。done() 表示是否已有最终结果/异常/取消状态，
    # cancelled() 只在 coroutine 最终以 CancelledError 结束后才为 True。
    print("  cancel() 接受请求:", cancellation_accepted)
    print("  刚返回时: done=", task.done(), "cancelled=", task.cancelled())
    assert cancellation_accepted
    assert not task.done()
    assert not task.cancelled()
    assert not cleanup_started.is_set()
    assert not any("I/O 层收到 CancelledError" in event for event in events)

    # await cleanup_started.wait() 会把执行权交还事件循环。目标 Task 因而有机会在
    # work_may_finish.wait() 处收到取消，逐层传播并进入 finally；清理闸门此时仍关闭。
    await cleanup_started.wait()
    print("  finally 进行中: done=", task.done(), "cancelled=", task.cancelled())
    print("  finally 进行中资源仍为:", resource.state.name)
    assert resource.state is ResourceState.OPEN
    assert not task.done()
    assert not task.cancelled()

    # 放行清理后再 await task，既收集取消，也与资源释放完成建立明确的先后关系。
    cleanup_may_finish.set()
    try:
        await task
    except asyncio.CancelledError as error:
        # 此处是 Task 所有者的取消边界：它确认取消和 finally 都已完成，不再重新抛给
        # run_examples()，这样后续教学实验还能继续运行。
        record(events, f"调用方: await Task 收到 CancelledError{error.args!r}")
    else:
        # try 的 else 仅在 await 没有抛异常时执行；走到这里说明取消被错误地吞掉了。
        raise AssertionError("正确实现不应把用户取消伪装成成功")

    print("  清理后: done=", task.done(), "cancelled=", task.cancelled())
    print("  清理后资源:", resource.state.name)
    assert resource.state is ResourceState.RELEASED
    assert resource.release_count == 1
    assert task.done() and task.cancelled()

    # index() 返回事件第一次出现的位置；next(生成器) 则取第一个满足文本条件的位置。
    # 最后只比较相对大小，从而验证因果链，而不依赖具体调度时间或事件列表的绝对长度。
    cancel_returned: int = events.index("调用方: cancel() 已返回")
    cancellation_injected: int = next(
        index for index, event in enumerate(events) if "I/O 层收到 CancelledError" in event
    )
    cleanup_finished: int = events.index("user-stop: 资源已释放")
    caller_observed: int = next(
        index for index, event in enumerate(events) if event.startswith("调用方: await Task")
    )
    assert cancel_returned < cancellation_injected < cleanup_finished < caller_observed


async def parent_directly_awaits_task(
    child: asyncio.Task[str],
    parent_is_waiting: asyncio.Event,
    events: list[str],
) -> str:
    # parent_is_waiting 在直接 await 之前置位，让外层可以确定父 Task 已进入这条等待边。
    # 父 Task 若被取消，取消会传递给它当前正在等待的 child Task。
    record(events, "parent: 即将直接 await child Task")
    parent_is_waiting.set()
    try:
        return await child
    except asyncio.CancelledError:
        record(events, "parent: child 清理结束后收到并传播 CancelledError")
        raise


async def demonstrate_direct_await_propagation() -> None:
    # 这里显式创建两个 Task，专门观察取消如何沿“父 Task 正在 await 子 Task”这条边传播。
    print_section("3. 取消正在直接 await 子 Task 的父 Task")
    events: list[str] = []
    resource = FakeIOResource("child", events)
    work_started = asyncio.Event()
    work_may_finish = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_may_finish = asyncio.Event()
    parent_is_waiting = asyncio.Event()

    # 保存引用让所有者可以观察和最终收集子 Task；仅保存并不等于清理已经结束。
    # child 先启动并停在 I/O 闸门，确保随后创建 parent 时等待的是一个未完成 Task。
    child_task: Task[str] = asyncio.create_task(
        call_tool(
            resource,
            work_started,
            work_may_finish,
            cleanup_started,
            cleanup_may_finish,
        ),
        name="directly-awaited-child",
    )
    await work_started.wait()
    # parent_is_waiting 消除调度猜测：只有父协程执行到 child 的直接 await 前才继续实验。
    parent_task: Task[str] = asyncio.create_task(
        parent_directly_awaits_task(child_task, parent_is_waiting, events),
        name="parent-awaiting-child",
    )
    await parent_is_waiting.wait()

    # 父 Task 的 cancel() 会调用它当前所等待 child 的取消逻辑。cancelling() 返回待处理的
    # 取消请求数；大于 0 证明请求已经到达 child，但不代表 child 的清理已经完成。
    parent_task.cancel("停止父任务")
    assert child_task.cancelling() > 0
    await cleanup_started.wait()

    print("  子 Task 引用仍在，但清理尚未结束:", not child_task.done())
    print("  父 Task 也在等待传播完成:", not parent_task.done())
    assert resource.state is ResourceState.OPEN
    assert not child_task.done()
    assert not parent_task.done()

    # child 的 finally 被放行后，child 先结束取消；父 Task 的 await 随后抛出取消并继续向外。
    cleanup_may_finish.set()
    try:
        await parent_task
    except asyncio.CancelledError:
        # 等待 parent 等价于间接等完 child 的这条传播链；调用方在这里收集最终取消。
        record(events, "调用方: 已等待父 Task 完成取消")
    else:
        raise AssertionError("父 Task 的取消不应消失")

    # 直接 await 形成传播链：父 Task 被取消时，正在等待的 child 也收到取消。
    # 父任务完成前已经等到了 child 的异步 finally 结束。
    print("  child: done=", child_task.done(), "cancelled=", child_task.cancelled())
    print("  parent: done=", parent_task.done(), "cancelled=", parent_task.cancelled())
    assert child_task.done() and child_task.cancelled()
    assert parent_task.done() and parent_task.cancelled()
    assert resource.state is ResourceState.RELEASED


async def catches_exception_only(started: asyncio.Event, events: list[str]) -> None:
    """证明 except Exception 不会拦截 CancelledError。"""
    # 新建且永不 set 的 Event 让任务稳定挂起，直到外部取消。CancelledError 直接继承
    # BaseException，不属于 Exception，因此 except 分支不执行，但 finally 仍会执行。
    try:
        started.set()
        await asyncio.Event().wait()
    except Exception:  # noqa: BLE001 - 本函数专门验证它不会捕获取消。
        record(events, "错误: except Exception 捕获了取消")
    finally:
        record(events, "except Exception 示例: finally 已运行")


async def swallowing_tool(
    resource: FakeIOResource,
    work_started: asyncio.Event,
    continuation_allowed: asyncio.Event,
    cancellation_swallowed: asyncio.Event,
) -> str:
    """教学反例：捕获 BaseException 后继续，令已停止的工具仍产生副作用。"""
    # 这是刻意保留的错误写法：BaseException 连取消和进程退出类异常也会捕获；捕获后又
    # 没有 raise，导致控制流落到后续副作用。外层用 Event 限制它，避免示例永久挂起。
    resource.open()
    try:
        # 告知调用方可以安全发出 cancel，然后在一个永不自行完成的 wait() 上接收取消。
        work_started.set()
        record(resource.events, "反例工具: 等待第一次工作")
        try:
            await asyncio.Event().wait()
        except BaseException as error:  # noqa: BLE001 - 故意展示危险反例。
            # type(error).__name__ 只记录实际异常类名。这里 set() 表示“异常已被吞”，
            # 并非表示 Task 已完成；执行会继续越过 except。
            record(resource.events, f"反例工具: 错误地吞掉 {type(error).__name__}")
            cancellation_swallowed.set()

        # continuation_allowed 模拟调用方后来放行一项有副作用的操作。正确代码重新抛出
        # CancelledError 后不会到达这里。
        record(resource.events, "反例工具: 取消后仍继续")
        await continuation_allowed.wait()
        record(resource.events, "反例工具: 取消后仍提交了副作用")
        return "伪成功"
    finally:
        # 即使错误地吞了取消，finally 仍会在普通 return 前执行，所以资源仍能释放；
        # “资源已释放”并不能证明取消语义正确。
        resource.release()


async def demonstrate_exception_hierarchy_and_swallowing() -> None:
    # 先用运行时类型关系验证异常层级，再运行一个不会吞取消的边界和一个会吞取消的反例。
    print_section("4. CancelledError 层级与吞掉取消的受控反例")
    # __mro__ 是方法解析顺序元组：索引 0 是类本身，索引 1 是直接父类。
    direct_parent: type = asyncio.CancelledError.__mro__[1]
    print("  CancelledError 的直接父类:", direct_parent.__name__)
    assert issubclass(asyncio.CancelledError, BaseException)
    assert not issubclass(asyncio.CancelledError, Exception)

    # 第一小段取消 catches_exception_only。若 except Exception 错误地捕获取消，事件列表会
    # 出现以“错误:”开头的记录，最后的 any(...) 断言就会失败。
    exception_events: list[str] = []
    exception_started = asyncio.Event()
    exception_task: Task[None] = asyncio.create_task(
        catches_exception_only(exception_started, exception_events),
        name="except-exception-demo",
    )
    await exception_started.wait()
    exception_task.cancel()
    try:
        await exception_task
    except asyncio.CancelledError:
        # 取消越过 except Exception 到达 Task 所有者，证明它没有被当作普通业务异常处理。
        record(exception_events, "调用方: except Exception 未拦截取消")
    assert not any(event.startswith("错误:") for event in exception_events)

    # 第二小段使用独立状态复现吞取消。cancellation_swallowed 只通知测试代码反例已越过
    # 错误捕获点，continuation_allowed 则控制后续副作用何时发生。
    bad_events: list[str] = []
    bad_resource = FakeIOResource("swallow", bad_events)
    work_started = asyncio.Event()
    continuation_allowed = asyncio.Event()
    cancellation_swallowed = asyncio.Event()
    bad_task: Task[str] = asyncio.create_task(
        swallowing_tool(
            bad_resource,
            work_started,
            continuation_allowed,
            cancellation_swallowed,
        ),
        name="bad-cancellation-swallowing",
    )
    await work_started.wait()
    bad_task.cancel("用户早已停止")
    await cancellation_swallowed.wait()

    # Task 仍在等待 continuation_allowed，因此既没成功也没进入最终取消状态。
    print("  取消已被吞掉，Task 仍未结束:", not bad_task.done())
    assert not bad_task.done()
    # 放行后反例会返回普通字符串；await 因而拿到“伪成功”，不会抛 CancelledError。
    continuation_allowed.set()
    result: str = await bad_task

    # cancelled() 为 False 说明 Task 的最终状态是成功完成。事件与资源断言分别证明
    # 错误副作用确实发生、finally 仍执行，这两件事必须分开判断。
    print("  反例返回:", result)
    print("  反例最终 cancelled():", bad_task.cancelled())
    assert result == "伪成功"
    assert not bad_task.cancelled()
    assert "反例工具: 取消后仍提交了副作用" in bad_events
    assert bad_resource.state is ResourceState.RELEASED


async def timeout_scope(
    resource: FakeIOResource,
    work_started: asyncio.Event,
    cleanup_started: asyncio.Event,
    timeout_seconds: float,
) -> None:
    """在 timeout 范围内观察 CancelledError，在范围外观察 TimeoutError。"""
    # 这个 Event 永不由正常路径 set，因此 request_entry 会一直等待，直到超时机制取消
    # 执行 timeout_scope 的“当前 Task”。timeout() 不会为代码块另建 worker Task。
    work_may_finish = asyncio.Event()
    # 保存进入范围前的 Task 身份，用 is 验证进入异步上下文后仍在同一个 Task 中执行。
    current_before: Task[Any] | None = asyncio.current_task()
    try:
        # timeout_seconds 是相对秒数。到期后上下文管理器先向当前 Task 发出取消请求。
        async with asyncio.timeout(timeout_seconds):
            assert asyncio.current_task() is current_before
            try:
                await request_entry(resource, work_started, work_may_finish, cleanup_started)
            except asyncio.CancelledError:
                # 范围内部先看到取消。必须重新抛出，外层 timeout 管理器退出时才能识别
                # 这是自己触发的取消，并将它转换为内置 TimeoutError。
                record(resource.events, "timeout 范围内: 看到 CancelledError，继续抛出")
                raise
    except TimeoutError:
        # 只有离开 async with 后才会看到该上下文转换出的 TimeoutError。
        record(resource.events, "timeout 范围外: 捕获 TimeoutError")
    else:
        # try 的 else 代表范围没有抛出 TimeoutError；对永不完成的工作来说这是协议错误。
        raise AssertionError("未完成的 I/O 应触发范围超时")


async def external_cancel_inside_timeout(
    resource: FakeIOResource,
    work_started: asyncio.Event,
    cleanup_started: asyncio.Event,
) -> None:
    """长超时未到期时，外部 cancel 不能被上下文改写成 TimeoutError。"""
    # 60 秒远长于本地实验时间，因此稍后发生的取消来自 wrapper_task.cancel()，不是这个
    # timeout 范围。传入的新 Event 不会被 set，使 I/O 保持在可取消的等待点。
    try:
        async with asyncio.timeout(60):
            await request_entry(
                resource,
                work_started,
                asyncio.Event(),
                cleanup_started,
            )
    except TimeoutError:
        # 若走到这里，说明外部用户取消被错误地解释为该范围自己的超时。
        record(resource.events, "错误: 外部取消被误报成超时")
        raise


async def demonstrate_timeout_context() -> None:
    # 第一小段触发范围自己的 deadline；第二小段在长 timeout 尚未到期时从外部主动取消。
    print_section("5. asyncio.timeout() 取消当前 Task 并在范围外转换异常")
    events: list[str] = []
    resource = FakeIOResource("tool-timeout", events)
    work_started = asyncio.Event()
    cleanup_started = asyncio.Event()

    # timeout_scope 自己在范围外收集 TimeoutError，所以这里会正常返回。随后分别验证工作
    # 确实开始过、finally 确实开始并完成，以及异常转换发生在范围内记录之后。
    await timeout_scope(resource, work_started, cleanup_started, timeout_seconds=0.01)
    assert work_started.is_set()
    assert cleanup_started.is_set()
    assert resource.state is ResourceState.RELEASED
    inside_index: int = events.index("timeout 范围内: 看到 CancelledError，继续抛出")
    outside_index: int = events.index("timeout 范围外: 捕获 TimeoutError")
    assert inside_index < outside_index

    # 为外部取消实验创建独立 Task，才能由当前调用方调用 cancel()。
    external_events: list[str] = []
    external_resource = FakeIOResource("external-cancel", external_events)
    external_started = asyncio.Event()
    external_cleanup_started = asyncio.Event()
    wrapper_task: Task[None] = asyncio.create_task(
        external_cancel_inside_timeout(
            external_resource,
            external_started,
            external_cleanup_started,
        ),
        name="external-cancel-inside-timeout",
    )
    # 等待 I/O 确认已进入长 timeout 范围，再从范围外取消整个 wrapper Task。
    await external_started.wait()
    wrapper_task.cancel("来自 timeout 范围外的停止")
    try:
        await wrapper_task
    except asyncio.CancelledError:
        # 长 timeout 并未到期，所以它不会把别处发起的 CancelledError 改写为 TimeoutError。
        record(external_events, "调用方: 外部取消仍是 CancelledError")
    else:
        raise AssertionError("普通外部取消不应变成 TimeoutError")

    assert not any(event.startswith("错误:") for event in external_events)
    assert external_resource.state is ResourceState.RELEASED


async def wait_for_before_deadline[T](
    awaitable: Awaitable[T],
    *,
    attempt_budget: float,
    absolute_deadline: float,
) -> T:
    """单次尝试不能拿到超过上层绝对截止时间的相对预算。"""
    # [T] 声明泛型类型参数：输入 awaitable 完成后产生 T，本函数也返回同一种 T。
    # 参数列表中的 * 让后两个预算只能按名字传入，避免两个 float 因位置写反。
    loop: AbstractEventLoop = asyncio.get_running_loop()
    # loop.time() 是单调时钟。deadline 已过时差为负，用 max(..., 0.0) 截成零；再取
    # attempt_budget 与剩余总预算的较小值，保证本次等待不会重新获得一整份上层预算。
    remaining: float = max(0.0, absolute_deadline - loop.time())
    # wait_for 约束传入的明确 awaitable。到期时它会向目标发出取消，并继续等目标完成清理；
    # 本例目标会传播取消，所以等待边界随后得到 TimeoutError。
    return await asyncio.wait_for(awaitable, timeout=min(attempt_budget, remaining))


async def demonstrate_wait_for_cleanup() -> None:
    # 这里把“被限制的 target”和“执行 wait_for 的 waiter”都做成 Task，以便同时观察状态。
    print_section("6. wait_for() 取消目标，并等待目标清理结束")
    events: list[str] = []
    resource = FakeIOResource("wait-for", events)
    work_started = asyncio.Event()
    work_may_finish = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_may_finish = asyncio.Event()

    # target 的正常工作和异步清理闸门都保持关闭，使超时后能稳定停在 finally 中。
    target_task: Task[str] = asyncio.create_task(
        call_tool(
            resource,
            work_started,
            work_may_finish,
            cleanup_started,
            cleanup_may_finish,
        ),
        name="wait-for-target",
    )
    # 先等 target 真正进入工作阶段，再创建 waiter，避免 0.01 秒预算在 target 启动前就消耗。
    await work_started.wait()
    loop: AbstractEventLoop = asyncio.get_running_loop()
    # 这里单次尝试只允许 0.01 秒；上层绝对 deadline 尚余约 1 秒，所以 min() 最终采用
    # 更严格的单次预算。waiter_task 代表正在执行 wait_for 的所有者。
    waiter_task: Task[str] = asyncio.create_task(
        wait_for_before_deadline(
            target_task,
            attempt_budget=0.01,
            absolute_deadline=loop.time() + 1.0,
        ),
        name="wait-for-owner",
    )

    # wait_for 的计时到期后会取消 target；target 已进入 finally，但人为设置的
    # 清理闸门尚未打开，因此 wait_for 自己也不会提前报告 TimeoutError。
    await cleanup_started.wait()
    print("  清理闸门关闭时 target.done():", target_task.done())
    print("  清理闸门关闭时 wait_for.done():", waiter_task.done())
    assert not target_task.done()
    assert not waiter_task.done()
    assert resource.state is ResourceState.OPEN

    # 放行 target 的 finally 后，wait_for 才能确认目标以取消结束，并向 waiter 报告超时。
    cleanup_may_finish.set()
    try:
        await waiter_task
    except TimeoutError:
        # TimeoutError 是 wait_for 对“本次目标超时”的外部表达；target 自身终态仍是取消。
        record(events, "wait_for 调用方: 清理完成后收到 TimeoutError")
    else:
        raise AssertionError("单次尝试应已超时")

    # 两个断言分别检查控制流终态与资源终态，证明报告超时时清理已经完成。
    assert target_task.done() and target_task.cancelled()
    assert resource.state is ResourceState.RELEASED


async def tool_with_local_budget(
    resource: FakeIOResource,
    work_started: asyncio.Event,
    cleanup_started: asyncio.Event,
    *,
    request_deadline: float,
    tool_budget: float,
) -> str:
    """只在工具预算比请求剩余预算更严格时增加内层 timeout_at。"""
    # request_deadline 是外层算好并传进来的绝对时刻；tool_budget 是从“现在”开始计算的
    # 工具局部相对预算。两者必须都基于当前事件循环的单调时钟。
    loop: AbstractEventLoop = asyncio.get_running_loop()
    tool_deadline: float = loop.time() + tool_budget
    # 正常完成闸门故意保持关闭，让更早的那个 deadline 决定本次调用如何结束。
    work_may_finish = asyncio.Event()

    # 只有工具自己的截止时刻更早，它才拥有一个独立、可命名的超时边界。
    if tool_deadline < request_deadline:
        record(resource.events, "预算: 工具使用更早的绝对截止时间")
        try:
            # timeout_at 接受绝对单调时钟时刻；到期时取消执行当前范围的 Task。
            async with asyncio.timeout_at(tool_deadline):
                return await call_tool(
                    resource,
                    work_started,
                    work_may_finish,
                    cleanup_started,
                )
        except TimeoutError as error:
            # from error 建立显式异常链：对调用方暴露更具体的工具预算异常，同时保留底层
            # TimeoutError 作为 __cause__，便于日志和调试追溯。
            raise ToolBudgetExceeded(f"{resource.name}: 工具预算耗尽") from error

    # 工具没有更严格的局部预算时，只沿用外层请求范围，避免在相同截止时刻
    # 嵌套两个范围后误把“请求截止”标成“工具截止”。这里的 await 仍会响应外层 Task
    # 收到的取消，只是不在工具层把它重新解释为 ToolBudgetExceeded。
    record(resource.events, "预算: 工具沿用请求的绝对截止时间")
    return await call_tool(resource, work_started, work_may_finish, cleanup_started)


async def demonstrate_absolute_deadlines() -> None:
    # 两个子实验使用相反的预算大小：第一个由工具局部预算先到期，第二个由请求 deadline
    # 先到期，从而验证异常应由真正拥有该截止时间的边界命名。
    print_section("7. 单工具预算与整个请求的共同截止时间")
    # 同一事件循环的 time() 值才能彼此比较；它是单调时钟读数，不是 Unix 时间戳。
    loop: AbstractEventLoop = asyncio.get_running_loop()

    # 请求还有约 1 秒，而工具只分到 0.01 秒，所以 tool_with_local_budget 创建内层范围。
    tool_events: list[str] = []
    tool_resource = FakeIOResource("local-budget", tool_events)
    try:
        await tool_with_local_budget(
            tool_resource,
            asyncio.Event(),
            asyncio.Event(),
            request_deadline=loop.time() + 1.0,
            tool_budget=0.01,
        )
    except ToolBudgetExceeded as error:
        # type(...).__name__ 和 str(...) 分别展示领域异常类型与面向人的消息。
        print("  单工具预算结果:", type(error).__name__, str(error))
    else:
        raise AssertionError("更短的工具预算应先耗尽")
    assert tool_resource.state is ResourceState.RELEASED

    # 第二个请求只剩 0.01 秒，工具预算却有 1 秒。工具不创建更晚的内层范围，而是让
    # 外层 timeout_at(request_deadline) 统一终止整条当前调用链。
    request_events: list[str] = []
    request_resource = FakeIOResource("request-deadline", request_events)
    request_deadline: float = loop.time() + 0.01
    try:
        async with asyncio.timeout_at(request_deadline):
            await tool_with_local_budget(
                request_resource,
                asyncio.Event(),
                asyncio.Event(),
                request_deadline=request_deadline,
                tool_budget=1.0,
            )
    except TimeoutError:
        # 这个 except 位于请求 timeout_at 范围外，所以把异常解释为“整体请求超时”。
        record(request_events, "请求边界: 整体截止时间耗尽")
    else:
        raise AssertionError("整个 Agent 请求应在共同截止时间停止等待")

    # 事件成员断言确认走的是“沿用请求”分支，而不是碰巧由另一个局部超时结束。
    assert "预算: 工具沿用请求的绝对截止时间" in request_events
    assert request_resource.state is ResourceState.RELEASED


async def demonstrate_timeout_is_not_hard_interrupt() -> None:
    # 这一反例说明 timeout 依赖事件循环调度取消回调，不能抢占正在运行的同步代码。
    print_section("8. 超时不是硬中断：阻塞事件循环会让超时延后")
    events: list[str] = []
    loop: AbstractEventLoop = asyncio.get_running_loop()
    # started_at 只用于生成近似观察值；最终断言只验证事件顺序，不验证精确耗时。
    started_at: float = loop.time()

    try:
        async with asyncio.timeout(0.01):
            record(events, "阻塞反例: 进入 timeout 范围")
            # 教学反例：当前线程阻塞时，事件循环无法执行到期回调。0.03 秒大于 0.01 秒，
            # 但 time.sleep() 不会像 await 一样把控制权交还事件循环。
            time.sleep(0.03)  # noqa: ASYNC251
            record(events, "阻塞反例: 同步阻塞先完成")
            # 到这个 await 才重新出现协作调度机会；事件循环随后处理已经到期的取消。
            await asyncio.Event().wait()
    except TimeoutError:
        # 离开 timeout 范围后才得到 TimeoutError。elapsed 用 .3f 格式化为三位小数，
        # 只是帮助肉眼观察“晚于预算”，不同机器上的具体数值可以变化。
        elapsed: float = loop.time() - started_at
        record(events, f"阻塞反例: 之后才观察到 TimeoutError（约 {elapsed:.3f}s）")
    else:
        raise AssertionError("恢复协作等待后应观察到已经到期的超时")

    # 不断言 elapsed 的具体范围，只验证同步阻塞结束事件一定早于超时被观察到。
    blocking_finished: int = events.index("阻塞反例: 同步阻塞先完成")
    timeout_observed: int = next(
        index for index, event in enumerate(events) if "之后才观察到 TimeoutError" in event
    )
    assert blocking_finished < timeout_observed


async def shielded_owner(
    work_started: asyncio.Event,
    work_may_finish: asyncio.Event,
    caller_cancelled: asyncio.Event,
    task_holder: list[asyncio.Task[str]],
    events: list[str],
) -> None:
    """选学：所有者保存被 shield 的 Task，并在自己取消后仍负责收集它。"""

    # 嵌套 async def 只在 shielded_owner 被调用时定义，且可直接读取外层参数 events 和 Event。
    # child 等待 work_may_finish，因此 owner 被取消时它仍处于未完成状态。
    async def protected_work() -> str:
        record(events, "shield child: 开始")
        work_started.set()
        await work_may_finish.wait()
        record(events, "shield child: 正常完成")
        return "受保护结果"

    # owner 创建 child，所以 owner 负责保存和回收它。除局部变量外，再追加到调用方传入的
    # task_holder 列表，使实验外层也持有强引用并能观察 child 的状态。
    child_task: Task[str] = asyncio.create_task(protected_work(), name="shielded-child")
    task_holder.append(child_task)
    try:
        # shield(child_task) 返回一个受保护的等待对象：owner 的取消会中断这个 await，
        # 但不会沿这条等待边调用 child_task.cancel()。
        await asyncio.shield(child_task)
    except asyncio.CancelledError:
        # owner 自己仍然收到取消。先通知实验外层，再继续履行对 child 的回收责任。
        record(events, "shield caller: 自己仍收到 CancelledError")
        caller_cancelled.set()
        # shield 只隔离这一次等待的取消传播。调用方创建了 child，因此仍要
        # 保存引用、决定结束策略，并真正 await 它以收集结果或异常。
        # 直接 await 原 child，等它正常完成并收集结果；随后裸 raise 让 owner 保持取消终态。
        result: str = await child_task
        record(events, f"shield owner: 最终收集 {result}")
        raise


async def demonstrate_shield_ownership() -> None:
    # 本段同时观察两个独立终态：被 shield 的 child 正常完成，owner 仍以取消结束。
    print_section("9. 选学：shield 保护被等待对象，不取消调用方")
    events: list[str] = []
    work_started = asyncio.Event()
    work_may_finish = asyncio.Event()
    caller_cancelled = asyncio.Event()
    # 可变列表充当最小“引用容器”；owner 会把自己创建的 child 放入其中。
    task_holder: list[asyncio.Task[str]] = []
    owner_task: Task[None] = asyncio.create_task(
        shielded_owner(
            work_started,
            work_may_finish,
            caller_cancelled,
            task_holder,
            events,
        ),
        name="shield-owner",
    )
    # 确认 child 已经启动并等待，再取消 owner。caller_cancelled 则确认 owner 已在
    # shield await 处收到 CancelledError，随后才读取 child 状态。
    await work_started.wait()
    owner_task.cancel("取消调用方")
    await caller_cancelled.wait()

    # 本例只创建一个 child，所以索引 0 取得那一个强引用。此时 shield 已阻断向内取消，
    # child 应仍是未完成且未取消。
    child_task: Task[str] = task_holder[0]
    print("  调用方已收到取消时 child.cancelled():", child_task.cancelled())
    assert not child_task.done()
    assert not child_task.cancelled()

    # 放行 child 后，owner 内部会先 await 并记录 child 的结果，再重新抛出自己的取消。
    work_may_finish.set()
    try:
        await owner_task
    except asyncio.CancelledError:
        # 这是 owner 的外层所有者收集取消的边界。
        record(events, "shield 外层: 调用方最终保持取消状态")
    else:
        raise AssertionError("shield 不应吞掉调用方自己的取消")

    # result() 只在已完成且未取消、未失败的 Task 上安全调用；前一条断言先验证这个前提。
    assert child_task.done() and not child_task.cancelled()
    assert child_task.result() == "受保护结果"
    assert owner_task.cancelled()


async def run_examples() -> None:
    # 顺序 await 让九段实验逐个完成；每段都自行收集预期异常和创建的 Task，因此不会把
    # pending 工作泄漏到下一段。任一断言失败都会立即中止，避免打印误导性的完成消息。
    await demonstrate_normal_completion()
    await demonstrate_cancellation_phases()
    await demonstrate_direct_await_propagation()
    await demonstrate_exception_hierarchy_and_swallowing()
    await demonstrate_timeout_context()
    await demonstrate_wait_for_cleanup()
    await demonstrate_absolute_deadlines()
    await demonstrate_timeout_is_not_hard_interrupt()
    await demonstrate_shield_ownership()
    print("\n所有第 21 章示例与运行时断言均已完成。")


def main() -> None:
    # asyncio.run() 创建事件循环、把 run_examples coroutine 驱动到完成，最后关闭事件循环。
    # 同一个进程中通常只应在同步入口调用它，不能在已运行的事件循环里再次嵌套调用。
    asyncio.run(run_examples())


# 导入本模块时 __name__ 是模块名，只定义函数和类；直接执行文件时它才等于 "__main__"，
# 因而调用同步入口 main()。这让示例既可独立运行，也可被其他代码导入检查而不自动执行。
if __name__ == "__main__":
    main()
