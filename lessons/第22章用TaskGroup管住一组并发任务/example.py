"""第 22 章：TaskGroup、结构化并发与异常组。

示例只用 Event 和本地替身控制执行阶段，不访问网络，也不依赖随机延迟。
每一节都用运行时断言验证任务所有权、失败传播和清理边界。
"""

# 建议按文件末尾 run_examples() 的调用顺序阅读。前面的类型和辅助函数负责搭建可控的
# “假工具”，后面的九个实验再分别放行、取消或制造失败，并通过 events 还原因果顺序。
# 全部实验都运行在同一个线程的事件循环里；这里的“并发”是协程在 await 处交还执行权后
# 交替推进，不等于 Java 同时启动了多个工作线程。

# 三个导入都来自标准库：asyncio 提供协程、Task、Event、TaskGroup、timeout 等并发原语；
# Awaitable[str] 描述“可以被 await，完成后得到 str”的对象类型；dataclass 装饰器则会根据
# 字段声明生成 __init__、__repr__、__eq__ 等样板方法。类型提示只供静态检查和阅读，
# Python 运行时不会像 Java 泛型那样自动验证传入对象及其最终结果。
import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass


# 这两个异常类型把“可预期的业务失败”和“代码/协议缺陷”分开。
# 后面的 best-effort 包装器只恢复前者，让后者继续触发 TaskGroup 的失败联动。
class ToolBusinessError(Exception):
    """调用成功到达工具，但得到可预期、可展示给调用方的业务失败。"""


class ToolProgramError(RuntimeError):
    """协议、解析或程序不变量被破坏，不能伪装成一次普通的部分失败。"""


# @dataclass(...) 在类定义完成时改造类，类似替轻量 POJO 生成样板代码，但它不是 Java 注解：
# 默认并不会根据 name: str 等类型提示做运行时校验。slots=True 让实例使用固定槽位保存字段，
# 因而不能临时添加未声明属性；这里也能尽早暴露示例中的字段名拼错。
@dataclass(slots=True)
class ControlledTool:
    """一个可由测试事件精确放行的工具替身。"""

    # name 用于日志；value 是放行后要返回的固定结果。
    name: str
    value: str

    # 每个工具共享同一 events 列表，因此 append 会汇入同一条时间线，而不是复制列表。
    events: list[str]

    # started 由工具 set、父协程 wait，用来确认工具已运行到 I/O 阶段。
    started: asyncio.Event

    # release 的方向相反，父协程 set 后，工具的 wait 才能继续。
    release: asyncio.Event

    # X | None 是联合类型，表示可保存一个异常实例或 None；默认 None 代表成功路径。
    error: Exception | None = None


# frozen=True 会阻止字段在构造后被重新赋值，让收集到的结果更接近 Java 的不可变 record；
# 但字段引用的对象若本身可变，frozen 并不会递归冻结它。本类字段恰好都是 str 或 None。
@dataclass(frozen=True, slots=True)
class ToolResult:
    """best-effort 策略的显式结果；错误和值不会混在同一字符串里。"""

    # 成功时 value 有值且 error 为 None；业务失败时两者相反。显式字段比用特殊字符串
    # 同时表示“值”和“错误”更容易判断，也避免把空结果误当成功。
    name: str
    value: str | None
    error: str | None

    # @property 让调用方以 result.ok 读取计算结果，看起来像字段；实际每次读取都会调用方法。
    @property
    def ok(self) -> bool:
        # is 比较对象身份；None 是单例哨兵，因此应写 is None，而不是套用 Java 的 == null。
        return self.error is None


# 普通 def 是同步函数，调用后立即打印，不产生协程对象。
def print_section(title: str) -> None:
    # f-string 会计算花括号中的 title；开头的 \n 让不同实验之间空一行。
    print(f"\n== {title} ==")


def record(events: list[str], message: str) -> None:
    """事件既打印又保留；断言只检查因果关系，不猜兄弟任务的调度顺序。"""

    # append 原地修改调用方传入的同一个列表，随后打印同一条消息供人观察。
    events.append(message)
    print(f"事件 | {message}")


def make_three_tools(
    events: list[str],
    *,
    record_error: Exception | None = None,
) -> tuple[ControlledTool, ControlledTool, ControlledTool]:
    """每次实验创建全新的事件，避免一个实验的状态污染下一个实验。"""

    # 参数列表中的 * 表示它后面的 record_error 只能按名字传入，例如
    # make_three_tools(events, record_error=error)，不能把它作为第二个位置参数。
    # 返回值是固定三个元素的 tuple，调用方随后可解包为 search、record_call、status。
    return (
        # 每次 asyncio.Event() 都创建独立事件对象，初始未置位，wait() 会暂停。
        ControlledTool(
            "search_docs",
            "文档：退款期为 7 天",
            events,
            asyncio.Event(),
            asyncio.Event(),
        ),
        ControlledTool(
            "lookup_record",
            "记录：客户等级 gold",
            events,
            asyncio.Event(),
            asyncio.Event(),
            # 只把可选错误注入 lookup_record，便于实验精确指定唯一的首发失败项。
            record_error,
        ),
        ControlledTool(
            "fetch_status",
            "状态：服务正常",
            events,
            asyncio.Event(),
            asyncio.Event(),
        ),
    )


async def execute_controlled_tool(tool: ControlledTool) -> str:
    """模拟“获取资源 -> 等待 I/O -> 返回/失败 -> finally 清理”的完整调用。"""

    # 调用 async def 只会先得到协程对象；当它被 Task 调度并实际执行到这里时，才记录开始。
    record(tool.events, f"{tool.name}: 开始并获取资源")

    # set() 不会阻塞，它把事件置为已触发并唤醒等待 started.wait() 的父协程；在调用 clear()
    # 之前，后续 wait() 也会立即返回。本例从不 clear，因此每个 started 只承担一次就绪信号。
    tool.started.set()

    try:
        # release 初始未置位，所以 await 暂停当前 Task，把事件循环执行权交给其他任务。
        # 这代表可取消的真实 I/O 等待点，而不是阻塞线程的同步等待。
        await tool.release.wait()

        if tool.error is not None:
            # type(error).__name__ 取异常对象所属类的名字，只用于生成稳定的观察日志。
            record(
                tool.events,
                f"{tool.name}: 抛出 {type(tool.error).__name__}",
            )
            raise tool.error

        # 只有 release 已放行且没有注入错误才返回固定值；return 会把 str 保存为 Task 的结果。
        record(tool.events, f"{tool.name}: 返回成功")
        return tool.value
    except asyncio.CancelledError:
        # cancel() 只是发出请求；本任务在上面的 await 等可取消点收到 CancelledError 后到达这里。
        # 只记录再裸 raise。裸 raise 会重新抛出当前异常，让取消状态继续走出工具调用。
        # 若像普通 Exception 那样吞掉它，TaskGroup/timeout 就可能误判任务已经正常完成。
        record(tool.events, f"{tool.name}: 收到取消")
        raise
    finally:
        # finally 在成功、业务/程序异常和取消三条路径上都会执行。真实代码会在这里归还连接、
        # 关闭流或回滚事务；TaskGroup 必须等这一段结束后才能完成退出。
        record(tool.events, f"{tool.name}: 资源清理完成")


# 三个名字模拟不同业务工具，但目前都委托给同一个受控执行器。这里保留独立函数，是为了让
# Task 的名称、调用关系和真实 Agent 中三个不同客户端调用一致。
async def search_docs(tool: ControlledTool) -> str:
    # await 不只是“取结果”，它会暂停当前包装协程，直到下层协程成功返回或抛出异常。
    return await execute_controlled_tool(tool)


async def lookup_record(tool: ControlledTool) -> str:
    return await execute_controlled_tool(tool)


async def fetch_status(tool: ControlledTool) -> str:
    return await execute_controlled_tool(tool)


async def wait_until_all_started(*tools: ControlledTool) -> None:
    """等待事实，而不是等待一个猜测的秒数。"""

    # 函数签名里的 *tools 把任意个位置参数收集成 tuple。循环逐个 await 看似串行，但已 set 的
    # Event.wait() 会立即返回；最终效果是直到每个工具都报告 started 才结束。
    for tool in tools:
        await tool.started.wait()


def release_all(*tools: ControlledTool) -> None:
    # set() 是同步操作；循环一次性解除所有工具的等待，之后具体谁先完成由事件循环调度决定。
    for tool in tools:
        tool.release.set()


def assert_cleaned_before(
    events: list[str],
    marker: str,
    *tool_names: str,
) -> None:
    # list.index 返回某消息第一次出现的位置；若消息缺失会直接抛 ValueError，使实验失败。
    marker_index: int = events.index(marker)
    for name in tool_names:
        # 这里只要求每个工具的清理都早于父边界，不要求兄弟之间以某个固定顺序清理。
        assert events.index(f"{name}: 资源清理完成") < marker_index


async def demonstrate_normal_task_group() -> None:
    # 第一个实验走纯成功路径：三个工具先全部停在 Event.wait()，父协程确认 Task 尚未完成，
    # 再统一放行。它要证明结果应在成功退出 TaskGroup 后读取。
    print_section("1. 最小正确用法：退出 TaskGroup 后再读结果")
    events: list[str] = []

    # tuple 按位置解包：三个新 ControlledTool 分别绑定到三个局部变量。
    search, record_call, status = make_three_tools(events)

    # async with 会调用 TaskGroup 的异步进入/退出协议。进入后组变为 active；到达块末尾时，
    # __aexit__ 会隐式等待所有登记任务结束，因此它相当于一个有明确所有权的并发作用域。
    async with asyncio.TaskGroup() as group:
        # create_task 接收协程对象，创建并登记 asyncio.Task，返回值不是最终字符串而是 Task 句柄。
        # name 便于调试；Python 3.14 的 eager_start=False 明确让任务先进入常规调度队列，
        # 不在 create_task 调用栈里立刻推进协程。后面的 await 才给事件循环调度它们的机会。
        search_task: Task[str] = group.create_task(
            search_docs(search),
            name="search_docs",
            eager_start=False,
        )
        record_task: Task[str] = group.create_task(
            lookup_record(record_call),
            name="lookup_record",
            eager_start=False,
        )
        status_task: Task[str] = group.create_task(
            fetch_status(status),
            name="fetch_status",
            eager_start=False,
        )

        # 等三个 started 全部置位后，可以确定任务已启动且停在各自的 release.wait()。
        await wait_until_all_started(search, record_call, status)
        print(
            "组内尚未放行，三个 Task 都未完成：",
            not any(
                task.done()
                for task in (search_task, record_task, status_task)
            ),
        )

        # 此处若调用 task.result() 会抛 InvalidStateError，因为 result() 不会像 Java Future.get()
        # 那样阻塞等待。先解除 I/O，再让作用域退出并隐式等待全部任务。
        release_all(search, record_call, status)

    # 能执行到这里，说明 TaskGroup 已成功退出，三个任务及其 finally 都已完成。
    record(events, "父请求：已经退出 TaskGroup")

    # 列表按固定的 Task 引用顺序取结果；完成的 Task.result() 才安全地返回各自字符串。
    results: list[str] = [
        search_task.result(),
        record_task.result(),
        status_task.result(),
    ]
    print("安全读取结果：", results)

    # 前两条断言验证值与 Task 状态，最后一条验证所有资源清理都发生在作用域退出标记之前。
    assert results == [search.value, record_call.value, status.value]
    assert all(
        task.done() for task in (search_task, record_task, status_task)
    )
    assert_cleaned_before(
        events,
        "父请求：已经退出 TaskGroup",
        search.name,
        record_call.name,
        status.name,
    )


async def demonstrate_all_required_failure() -> None:
    # “全部必需”表示缺少任一工具结果，本次请求就没有继续价值。这里让 lookup_record
    # 发生程序错误，观察 TaskGroup 如何取消两个尚未完成的兄弟并在清理后聚合异常。
    print_section("2. 全部必需：一项失败，取消兄弟并等清理")
    events: list[str] = []
    search, record_call, status = make_three_tools(
        events,
        # 只有 record_call 持有错误；search 和 status 不放行，稳定保持在可取消点。
        record_error=ToolProgramError("记录响应无法解析"),
    )
    # except* 收到异常组后会把叶子数量写入这里，供作用域外断言。
    caught_count: int = 0

    try:
        async with asyncio.TaskGroup() as group:
            # 三个 Task 都登记到同一个 group，因而共享同一失败和清理边界。
            search_task: Task[str] = group.create_task(
                search_docs(search), name="search_docs"
            )
            record_task: Task[str] = group.create_task(
                lookup_record(record_call), name="lookup_record"
            )
            status_task: Task[str] = group.create_task(
                fetch_status(status), name="fetch_status"
            )

            await wait_until_all_started(search, record_call, status)

            # 只放行失败项；另外两项确定仍停在可取消的 Event.wait() 中。record_call 恢复后
            # 抛 ToolProgramError，TaskGroup 开始 fail-fast 关闭并取消 search/status。
            record_call.release.set()

            try:
                # 新 Event 永远不会自行 set，这个 await 故意让父 Task 留在 with 块里。
                await asyncio.Event().wait()
            finally:
                # 子任务失败会用一次内部取消唤醒仍在 with 块中的父 Task，所以父 Task 的
                # finally 得以执行。这个内部取消用于驱动 __aexit__，最终不会以 CancelledError
                # 穿过 TaskGroup；组外收到的是由成员失败构成的 ExceptionGroup。
                record(events, "父请求：with 块中的等待被中断")
    # except* 不是捕获外层容器后手工遍历，它会从异常树中选出所有匹配叶子，并保持组结构
    # 绑定给 error_group。没有匹配的叶子才会继续传播给外层。
    except* ToolProgramError as error_group:
        caught_count = len(error_group.exceptions)
        record(events, "父请求：收到 ToolProgramError 异常组")

    # cancelled() 区分因取消结束的兄弟；失败任务不是 cancelled，而是保存了原异常。
    # exception() 在 Task 已完成后返回其异常对象，本例用 isinstance 验证类型没有被吞掉。
    print("失败叶子数：", caught_count)
    print(
        "两个兄弟 Task 均已取消：",
        search_task.cancelled() and status_task.cancelled(),
    )
    assert caught_count == 1
    assert search_task.cancelled()
    assert status_task.cancelled()
    assert isinstance(record_task.exception(), ToolProgramError)
    assert_cleaned_before(
        events,
        "父请求：收到 ToolProgramError 异常组",
        search.name,
        record_call.name,
        status.name,
    )


async def primary_group_failure(
    started: asyncio.Event,
    release: asyncio.Event,
    events: list[str],
) -> None:
    # 这个任务先报告“我已进入可控阶段”，再等待父协程统一触发真正的主失败。
    record(events, "主失败任务：已就绪")
    started.set()
    await release.wait()
    record(events, "主失败任务：抛出 ToolBusinessError")
    raise ToolBusinessError("文档索引中没有该条目")


async def fail_while_cancelling(
    name: str,
    started: asyncio.Event,
    events: list[str],
    error: Exception,
) -> None:
    """异常组实验：模拟任务收到取消后，自己的清理又发生异常。"""

    record(events, f"{name}: 已进入等待")
    started.set()
    try:
        # 事件对象没有保存在别处，因此不会被 set；正常情况下这个任务会一直等待。
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        # 主任务失败后，TaskGroup 取消本任务。本例故意让取消清理再抛一个指定异常，
        # 从而确定性地产生多个并发错误；真实代码只应在清理确实失败时出现这种结果。
        record(
            events,
            f"{name}: 取消清理时抛出 {type(error).__name__}",
        )
        # raise error from None 抛出传入的异常，并隐藏“处理 CancelledError 时又抛异常”的
        # 隐式上下文显示，使演示聚焦最终异常组；它与裸 raise 的重新抛取消语义不同。
        raise error from None


async def demonstrate_exception_groups() -> None:
    # 本实验稳定制造三个叶子：主任务的 ToolBusinessError，以及两个兄弟响应取消时产生的
    # OSError、ValueError。随后用 except* 分型处理前两类，让第三类继续传播。
    print_section("3. ExceptionGroup 与 except* 分型：未处理部分继续传播")
    events: list[str] = []

    # 每个 started Event 分别确认一个任务已就绪，release_primary 只放行主失败任务。
    primary_started = asyncio.Event()
    os_cleanup_started = asyncio.Event()
    value_cleanup_started = asyncio.Event()
    release_primary = asyncio.Event()

    # 两个列表收集已被对应 except* 处理的叶子消息。
    handled_business: list[str] = []
    handled_os: list[str] = []

    # 尚未收到剩余子组时用 None 表示；外层 except 命中后再保存 ExceptionGroup。
    remaining_group: ExceptionGroup | None = None

    # Python 禁止在同一 try 上混用 except 与 except*，所以需要两层：内层按叶子类型切分，
    # 外层把切分后仍未匹配的异常组当作一个普通 ExceptionGroup 接住，以便继续观察。
    try:
        try:
            async with asyncio.TaskGroup() as group:
                # Task 句柄在此实验中无需单独读结果；TaskGroup 自己持有成员并负责等待。
                group.create_task(
                    primary_group_failure(
                        primary_started, release_primary, events
                    ),
                    name="primary-failure",
                )
                group.create_task(
                    fail_while_cancelling(
                        "日志清理任务",
                        os_cleanup_started,
                        events,
                        OSError("写入清理日志失败"),
                    ),
                    name="os-cleanup-failure",
                )
                group.create_task(
                    fail_while_cancelling(
                        "状态清理任务",
                        value_cleanup_started,
                        events,
                        ValueError("清理状态格式错误"),
                    ),
                    name="value-cleanup-failure",
                )

                # 三个 started 事件保证：主失败发生前，两个清理任务都已在 await 中可被取消。
                # 这种事件握手比 sleep 后“猜任务大概启动了”更确定，不依赖机器速度。
                await primary_started.wait()
                await os_cleanup_started.wait()
                await value_cleanup_started.wait()
                release_primary.set()
        # except* 会保留匹配部分的异常树形状，所以即使只有一个叶子，变量仍是异常组。
        except* ToolBusinessError as business_group:
            # 本例没有嵌套子组，所以 exceptions 中直接就是匹配叶子；生成器表达式惰性地产生
            # 每条错误文字，extend 再逐项加入现有列表。
            handled_business.extend(
                str(error) for error in business_group.exceptions
            )
            print("except* ToolBusinessError 处理：", handled_business)
        except* OSError as os_group:
            handled_os.extend(str(error) for error in os_group.exceptions)
            print("except* OSError 处理：", handled_os)
    except ExceptionGroup as unhandled:
        # 两个 except* 都未匹配 ValueError，因此它保持异常组结构来到外层普通 except。
        remaining_group = unhandled
        print(
            "外层 except 收到未处理子组：",
            [type(error).__name__ for error in unhandled.exceptions],
        )

    # 断言既验证已处理叶子没有丢失，也验证未处理 ValueError 没有被前两个分支误吞。
    assert handled_business == ["文档索引中没有该条目"]
    assert handled_os == ["写入清理日志失败"]
    assert remaining_group is not None
    assert len(remaining_group.exceptions) == 1
    assert isinstance(remaining_group.exceptions[0], ValueError)


async def as_best_effort_result(
    name: str,
    operation: Awaitable[str],
) -> ToolResult:
    """只把已知业务失败变成数据；取消和未知缺陷都自然传播。"""

    # operation 是已经创建的可等待对象；await 成功后得到 str，再包装为成功 ToolResult。
    # 包装发生在每个子 Task 内部，所以 TaskGroup 看到“可恢复业务失败”时，成员仍是正常返回。
    try:
        return ToolResult(name=name, value=await operation, error=None)
    # 捕获范围刻意只包含已定义恢复语义的 ToolBusinessError。CancelledError 直接继承
    # BaseException 而不是 Exception；ToolProgramError 也不是该业务类型，两者都会自然传播。
    except ToolBusinessError as error:
        # str(error) 保存适合展示的业务消息；value=None 明确表示没有工具值。
        return ToolResult(name=name, value=None, error=str(error))


async def demonstrate_best_effort() -> None:
    # 先验证“允许部分业务失败”：客户记录不存在被转成数据，另外两项仍成功。
    # 再注入程序缺陷，证明 best-effort 并不等于捕获所有异常。
    print_section("4. 相互独立：收集部分业务失败，程序缺陷仍失败")
    events: list[str] = []
    search, record_call, status = make_three_tools(
        events,
        record_error=ToolBusinessError("客户记录不存在"),
    )

    async with asyncio.TaskGroup() as group:
        # 每个 Task 运行的最外层协程都是 as_best_effort_result；它在内部 await 真实工具调用，
        # 所以业务异常会在离开成员 Task 前被转换，TaskGroup 不会因此取消其他成员。
        search_task: Task[ToolResult] = group.create_task(
            as_best_effort_result(search.name, search_docs(search)),
            name="best-effort-search",
        )
        record_task: Task[ToolResult] = group.create_task(
            as_best_effort_result(
                record_call.name,
                lookup_record(record_call),
            ),
            name="best-effort-record",
        )
        status_task: Task[ToolResult] = group.create_task(
            as_best_effort_result(status.name, fetch_status(status)),
            name="best-effort-status",
        )
        await wait_until_all_started(search, record_call, status)

        # 三项全部放行：search/status 返回值，record_call 抛可恢复的业务异常。
        release_all(search, record_call, status)

    # 成功越过 TaskGroup 后，三个 Task 都以 ToolResult 正常完成，可以统一取结果。
    results: list[ToolResult] = [
        search_task.result(),
        record_task.result(),
        status_task.result(),
    ]
    print("部分成功结果：", results)

    # 列表推导式逐个读取 @property ok，预期为“成功、业务失败、成功”。
    assert [result.ok for result in results] == [True, False, True]
    assert results[1].error == "客户记录不存在"

    # 第二轮复用相同结构，但把 record 错误换成 ToolProgramError。该类型不会被包装器捕获。
    bug_events: list[str] = []
    bug_search, bug_record, bug_status = make_three_tools(
        bug_events,
        record_error=ToolProgramError("响应结构违反协议"),
    )
    bug_count: int = 0

    try:
        async with asyncio.TaskGroup() as group:
            bug_search_task: Task[ToolResult] = group.create_task(
                as_best_effort_result(
                    bug_search.name,
                    search_docs(bug_search),
                )
            )
            bug_record_task: Task[ToolResult] = group.create_task(
                as_best_effort_result(
                    bug_record.name,
                    lookup_record(bug_record),
                )
            )
            bug_status_task: Task[ToolResult] = group.create_task(
                as_best_effort_result(
                    bug_status.name,
                    fetch_status(bug_status),
                )
            )
            await wait_until_all_started(
                bug_search,
                bug_record,
                bug_status,
            )

            # 只放行错误项，让另外两个确定保持未完成，便于验证 fail-fast 对兄弟的取消。
            bug_record.release.set()

            # 父 Task 留在组内等待，直到成员失败触发 TaskGroup 的内部取消。
            await asyncio.Event().wait()
    except* ToolProgramError as error_group:
        bug_count = len(error_group.exceptions)
        record(bug_events, "父请求：未知程序缺陷继续传播")

    # 错误任务保存 ToolProgramError，未放行的两项则以 cancelled 状态结束。
    print("未知缺陷数：", bug_count)
    assert bug_count == 1
    assert bug_search_task.cancelled()
    assert isinstance(bug_record_task.exception(), ToolProgramError)
    assert bug_status_task.cancelled()


async def self_cancelling_child(
    started: asyncio.Event,
    events: list[str],
) -> None:
    # 这个成员主动取消自己，用来区分“单个成员取消”和“父请求被外部取消”。
    record(events, "自取消子任务：开始")
    started.set()

    # current_task() 返回当前事件循环正在执行此协程的 Task；理论上也可能是 None，
    # 因此先用断言收窄状态，再调用 Task.cancel() 给自己登记取消请求。
    current: Task[Any] | None = asyncio.current_task()
    assert current is not None
    current.cancel()
    try:
        # cancel() 不是同步抛异常；sleep(0) 明确让出一次执行权，取消才在这个 await 处注入。
        await asyncio.sleep(0)
    finally:
        # 即使 CancelledError 未在本函数捕获，finally 仍先执行，然后取消继续离开成员 Task。
        record(events, "自取消子任务：清理完成")


async def demonstrate_child_cancellation() -> None:
    # TaskGroup 把成员的 CancelledError 视作取消状态，而不是触发整个组 fail-fast 的普通失败，
    # 因此另一个兄弟仍可被放行并正常返回。
    print_section("5A. 单个子任务取消：不等于组失败")
    events: list[str] = []
    child_started = asyncio.Event()
    sibling = ControlledTool(
        "仍可完成的兄弟",
        "兄弟结果",
        events,
        asyncio.Event(),
        asyncio.Event(),
    )

    async with asyncio.TaskGroup() as group:
        cancelled_task: Task[None] = group.create_task(
            self_cancelling_child(child_started, events)
        )
        sibling_task: Task[str] = group.create_task(
            execute_controlled_tool(sibling)
        )

        # 先等两项都启动，避免在自取消任务尚未执行时就提前解释结果。
        await child_started.wait()
        await sibling.started.wait()
        sibling.release.set()

    # 组能够正常退出，但 cancelled_task.result() 仍会重新抛 CancelledError，所以这里只检查
    # cancelled()；正常兄弟则可以安全调用 result()。
    print("自取消状态：", cancelled_task.cancelled())
    print("兄弟结果：", sibling_task.result())
    assert cancelled_task.cancelled()
    assert sibling_task.result() == "兄弟结果"


async def waiting_best_effort_request(
    search: ControlledTool,
    record_call: ControlledTool,
    status: ControlledTool,
) -> list[ToolResult]:
    """外部取消实验的父请求：正常情况下三个工具会一直等待放行。"""

    async with asyncio.TaskGroup() as group:
        # 列表保存三个 Task 句柄。它们都用了 best-effort 包装，但该包装只处理业务异常，
        # 不会把上游取消转换成 ToolResult。
        tasks: list[Task[ToolResult]] = [
            group.create_task(
                as_best_effort_result(search.name, search_docs(search))
            ),
            group.create_task(
                as_best_effort_result(
                    record_call.name,
                    lookup_record(record_call),
                )
            ),
            group.create_task(
                as_best_effort_result(status.name, fetch_status(status))
            ),
        ]
        await wait_until_all_started(search, record_call, status)

        # 正常路径会一直停在这里，外部稍后取消承载本函数的父 Task。
        await asyncio.Event().wait()

    # 列表推导式只有在 TaskGroup 正常退出时才逐个取值。外部取消路径绝不应抵达这里；
    # 若到达，才说明三个工具都已有完整结果。
    return [task.result() for task in tasks]


async def demonstrate_external_cancellation() -> None:
    # 本实验从“请求调用方”视角创建并取消父 Task。取消父请求会进入其 TaskGroup，
    # 组再取消所有未完成成员、等待 finally，之后才把原 CancelledError 交还调用方。
    print_section("5B. 外部取消父请求：清理后仍向调用方传播取消")
    events: list[str] = []
    search, record_call, status = make_three_tools(events)

    # 这里故意用裸 asyncio.create_task，因为 parent_task 的所有者就是当前调用方；调用方会
    # 保存句柄并在下方 await 回收它。eager_start=False 同样把首次执行留给后续事件循环调度。
    parent_task: Task[list[ToolResult]] = asyncio.create_task(
        waiting_best_effort_request(search, record_call, status),
        name="agent-request",
        eager_start=False,
    )

    # 先确认三个孩子都在工作，再取消父请求，排除“孩子其实从未启动”的歧义。
    await wait_until_all_started(search, record_call, status)

    # cancel() 只发取消请求；真正的清理与状态转换发生在 await parent_task 推进期间。
    parent_task.cancel()
    try:
        await parent_task
    except asyncio.CancelledError:
        # 到达这里时，父 Task 内部的 TaskGroup 已等待三个工具完成 finally。
        record(events, "调用方：仍收到 CancelledError")
    else:
        # 如果包装器错误地吞掉取消，await 会正常返回并走入这个显式失败分支。
        raise AssertionError("best-effort 包装不应把外部取消变成成功结果")

    # cancelled() 验证父请求最终保持取消语义；顺序断言验证清理早于调用方收到取消。
    assert parent_task.cancelled()
    assert_cleaned_before(
        events,
        "调用方：仍收到 CancelledError",
        search.name,
        record_call.name,
        status.name,
    )


async def owned_worker(
    name: str,
    started: asyncio.Event,
    release: asyncio.Event,
    events: list[str],
) -> None:
    # 这个最小 worker 只关心生命周期：started 表示已经归属并启动，release 表示允许完成，
    # events 则留下取消和清理证据。返回 None 是因为实验观察的是状态而不是业务结果。
    record(events, f"{name}: 开始")
    started.set()
    try:
        await release.wait()
    except asyncio.CancelledError:
        # 与工具执行器一样，记录后重新抛出，维持 Task 的 cancelled 状态。
        record(events, f"{name}: 收到取消")
        raise
    finally:
        record(events, f"{name}: 清理完成")


async def wrongly_spawn_detached_child(
    started: asyncio.Event,
    release: asyncio.Event,
    events: list[str],
) -> asyncio.Task[None]:
    """教学反例：裸 create_task 创建的任务不属于调用方现有的 TaskGroup。"""

    # asyncio.create_task 只把任务登记到当前事件循环，不会扫描调用栈并自动加入调用方的
    # TaskGroup。即使本函数自身由某个 group 管理，detached 仍是另一个未被该组拥有的 Task。
    detached: Task[None] = asyncio.create_task(
        owned_worker("游离子任务", started, release, events),
        name="detached-child",
        eager_start=False,
    )

    # 等孩子真正开始后才返回句柄，确保外层演示拿到的不是一个尚未运行的模糊状态。
    await started.wait()
    return detached


async def spawn_child_in_given_group(
    group: asyncio.TaskGroup,
    started: asyncio.Event,
    release: asyncio.Event,
    events: list[str],
) -> asyncio.Task[None]:
    """显式接收所有者，把嵌套创建的任务登记进同一个组。"""

    # group 作为显式参数传入，函数签名就能说明“新任务归调用方给定的作用域所有”。
    # 返回 Task 句柄便于调用方稍后观察，但生命周期等待由 group 负责。
    return group.create_task(
        owned_worker("受组管理的子任务", started, release, events),
        name="owned-child",
    )


async def demonstrate_nested_ownership() -> None:
    # 先演示错误所有权：组内 launcher 裸创建的孩子会越过外层组。
    # 再演示正确写法：把现有 group 传入下层，由 group.create_task 登记孩子。
    print_section("6. 嵌套所有权：裸 create_task 不会自动入组")

    wrong_events: list[str] = []
    detached_started = asyncio.Event()
    detached_release = asyncio.Event()

    # 先用 None 表示句柄尚未取得；finally 据此判断是否有任务需要隔离回收。
    detached: asyncio.Task[None] | None = None
    try:
        async with asyncio.TaskGroup() as group:
            # group 只直接拥有 launcher。launcher 返回一个 Task 对象，并不会把这个返回值代表的
            # detached Task 自动登记为成员，这与 Java 方法返回 Future 后谁负责 join 类似。
            launcher: Task[Task[None]] = group.create_task(
                wrongly_spawn_detached_child(
                    detached_started,
                    detached_release,
                    wrong_events,
                )
            )

        # 外层组只等 launcher 返回，因此现在可从 launcher.result() 取得游离 Task 句柄；
        # detached 自己仍停在 release.wait()，这正是生命周期漏洞。
        detached = launcher.result()
        record(wrong_events, "外层 TaskGroup：已经退出")
        print(
            "反例中组退出时，游离任务仍未结束：",
            not detached.done(),
        )
        assert not detached.done()
    finally:
        # 反例必须隔离。即使前面的断言失败，也显式取消并 await 游离任务。只调用 cancel()
        # 而不 await 仍不足以回收，因为任务的 finally 需要事件循环继续推进。
        if detached is not None and not detached.done():
            detached.cancel()
            try:
                await detached
            except asyncio.CancelledError:
                # 这是演示隔离层主动发起且已经等待完成的取消，所以在本边界消费它。
                record(wrong_events, "隔离层：游离任务已回收")

    # 时间线证明外层组退出发生在游离孩子清理之前，也就是它确实越过了错误的父作用域。
    assert wrong_events.index("外层 TaskGroup：已经退出") < wrong_events.index(
        "游离子任务: 清理完成"
    )

    # 正确轮使用全新的事件与时间线，避免前一轮已 set 的状态影响结论。
    correct_events: list[str] = []
    owned_started = asyncio.Event()
    owned_release = asyncio.Event()

    async with asyncio.TaskGroup() as group:
        # launcher 本身和它通过同一个 group 新增的 owned-child 都是成员。TaskGroup 在退出期间
        # 仍会等待后加入的成员，直到组内不再有未完成任务。
        launcher: Task[Task[None]] = group.create_task(
            spawn_child_in_given_group(
                group,
                owned_started,
                owned_release,
                correct_events,
            )
        )
        await owned_started.wait()
        owned_release.set()

    # launcher 的返回值仍是子 Task 句柄；区别在于该 Task 已登记到 group，所以成功退出组时
    # 它必定完成。下面同时检查 done() 和清理标记的先后。
    owned_task: Task[None] = launcher.result()
    record(correct_events, "正确示例：外层 TaskGroup 已退出")
    print(
        "正确示例中组退出时，嵌套子任务已结束：",
        owned_task.done(),
    )
    assert owned_task.done()
    assert correct_events.index(
        "受组管理的子任务: 清理完成"
    ) < correct_events.index("正确示例：外层 TaskGroup 已退出")


async def demonstrate_whole_request_timeout() -> None:
    # 共同时间预算应包住整个 TaskGroup。期限触发时 timeout 先取消当前父 Task，父 Task 退出
    # TaskGroup 又会取消孩子并等待清理，最后 timeout 才把自己发起的取消转换为 TimeoutError。
    print_section("7. 整体 timeout 包围 TaskGroup：回收后再抛 TimeoutError")
    events: list[str] = []
    search, record_call, status = make_three_tools(events)

    # 使用事件循环自己的单调时钟来设置期限，避免系统墙钟调整影响超时判断。
    loop: AbstractEventLoop = asyncio.get_running_loop()
    try:
        # 先不设期限，三个工具都开始后再把截止点设为“现在”。
        # 这仍是真实 timeout 取消路径，但不依赖机器速度或固定 sleep 时长。
        # asyncio.timeout(None) 先创建没有截止时间的异步上下文；as 绑定的对象可稍后 reschedule。
        async with asyncio.timeout(None) as timeout_scope:
            async with asyncio.TaskGroup() as group:
                # 三个 Task 都不保存句柄，因为本实验只验证统一取消和清理时间线。
                group.create_task(search_docs(search))
                group.create_task(lookup_record(record_call))
                group.create_task(fetch_status(status))
                await wait_until_all_started(search, record_call, status)

                # loop.time() 是“现在”；reschedule 后事件循环会尽快取消当前父 Task。
                timeout_scope.reschedule(loop.time())

                # 留在可取消点，让刚设定的期限有机会生效并进入 TaskGroup 的退出协议。
                await asyncio.Event().wait()
    # TimeoutError 的转换发生在离开 timeout 上下文时，所以必须在上下文外捕获。
    except TimeoutError:
        record(events, "父请求：收到 TimeoutError")

    # 只有三个工具的 finally 都先完成，这个超时边界断言才会通过。超时不是硬杀任务。
    assert_cleaned_before(
        events,
        "父请求：收到 TimeoutError",
        search.name,
        record_call.name,
        status.name,
    )


async def demonstrate_gather_difference() -> None:
    # gather 也能并发等待多个 awaitable，但默认情况下一个成员抛普通异常时，它会把首个异常
    # 立即交给等待者，不会像 TaskGroup 那样自动取消并等待未完成兄弟。
    print_section("8. gather 对照：首个异常不会自动取消兄弟")
    events: list[str] = []
    failing = ControlledTool(
        "gather-失败项",
        "不会返回",
        events,
        asyncio.Event(),
        asyncio.Event(),
        ToolProgramError("gather 中的工具失败"),
    )
    sibling = ControlledTool(
        "gather-兄弟项",
        "兄弟继续完成",
        events,
        asyncio.Event(),
        asyncio.Event(),
    )

    # 这里显式保存两个裸 Task 的句柄，因为 gather 异常返回后，所有权和回收责任仍在调用方。
    failing_task: Task[str] = asyncio.create_task(
        execute_controlled_tool(failing)
    )
    sibling_task: Task[str] = asyncio.create_task(
        execute_controlled_tool(sibling)
    )

    try:
        # 先确保两项都已启动，再只放行失败项；兄弟仍确定停在自己的 release.wait()。
        await wait_until_all_started(failing, sibling)
        failing.release.set()

        try:
            # 默认 return_exceptions=False；failing_task 的 ToolProgramError 直接从 await 抛出。
            await asyncio.gather(failing_task, sibling_task)
        except ToolProgramError as error:
            print("gather 立即传播：", error)

        # 捕获 gather 抛出的异常后，sibling_task 仍由事件循环运行，未被自动取消。
        print("捕获异常后，兄弟仍在运行：", not sibling_task.done())
        assert not sibling_task.done()

        # 调用方仍持有句柄，因此可以放行并 await，明确完成剩余任务的回收。
        sibling.release.set()
        print("调用方主动回收兄弟结果：", await sibling_task)
    finally:
        # 若教学断言意外失败，也不能把 sibling 留给 asyncio.run 收尾。finally 覆盖正常、
        # 异常和断言失败路径；只有 Task 尚未完成时才发送取消并等待它真正结束。
        if not sibling_task.done():
            sibling_task.cancel()
            try:
                await sibling_task
            except asyncio.CancelledError:
                # 清理兜底路径预期由本函数发起取消，此处消费它后即可继续原来的退出过程。
                pass

    # 失败 Task 已结束但不是 cancelled；exception() 应返回最初的 ToolProgramError。
    assert failing_task.done()
    assert isinstance(failing_task.exception(), ToolProgramError)


async def run_examples() -> None:
    # 每个 await 都等当前实验彻底结束后才进入下一项，避免各实验的事件与任务交错。
    # 这份顺序只用于教学展示；每个实验内部的 Task 仍按各自并发规则调度。
    await demonstrate_normal_task_group()
    await demonstrate_all_required_failure()
    await demonstrate_exception_groups()
    await demonstrate_best_effort()
    await demonstrate_child_cancellation()
    await demonstrate_external_cancellation()
    await demonstrate_nested_ownership()
    await demonstrate_whole_request_timeout()
    await demonstrate_gather_difference()
    print("\n所有第 22 章示例与运行时断言均已完成。")


def main() -> None:
    """同步入口只创建一次事件循环；所有并发任务都在受控作用域内回收。"""

    # 调用 run_examples() 先产生顶层协程对象；asyncio.run 创建事件循环、运行它到结束，
    # 最后关闭循环。它只能在当前线程没有正在运行的事件循环时调用。
    asyncio.run(run_examples())


# 模块被直接作为脚本执行时 __name__ 等于 "__main__"，才调用同步入口；若被其他模块 import，
# 上面的类与函数只会完成定义，不会自动运行九个实验。这与 Java 的固定 main 方法查找不同。
if __name__ == "__main__":
    main()
