"""第 20 章：Task 生命周期与并发结果收集。

示例使用 asyncio.Event 控制三个本地工具替身的开始与完成条件，不访问真实网络。
断言检查事件和对象状态，不把精确耗时或偶然的调度先后当作结论。
"""

# 推荐按文件从上往下读：先认识工具调用的数据模型和 Event 控制器，再看九组实验。
# 每组实验都会先创建工具计划与控制信号，随后启动 Task，由控制器按确定顺序打开 release，
# 最后用结果、Task 状态和事件列表做断言。这样观察到的是 asyncio 的契约，而非机器快慢。
# asyncio 提供事件循环、Task 和并发收集工具；dataclass 用来定义轻量且可比较的数据对象；
# pairwise() 则在第一个实验中成对检查“前一个完成后，后一个才开始”。
import asyncio
from asyncio import AbstractEventLoop, Future, Task
from collections.abc import Coroutine as CoroutineType
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any


# 这是纯同步的展示辅助函数。title: str 和 -> None 是类型提示，便于阅读器和静态检查器理解
# 参数与返回值；Python 运行时并不会仅凭这些注解自动拒绝其他类型。
def print_section(title: str) -> None:
    # f-string 会先计算花括号里的 title，再把结果嵌入字符串；开头的 \n 先输出一个空行。
    print(f"\n=== {title} ===")


# events 是由调用方持有的可变 list。append() 会原地修改同一个列表，所以实验结束后仍可
# 从调用方看到完整轨迹；这类似把同一个 Java List 引用传入方法，而不是复制一份列表。
def record(events: list[str], message: str) -> None:
    """同时记录并打印事件，让调度过程既可观察又可断言。"""
    events.append(message)
    print(f"  事件 | {message}")


# 自定义异常继承 RuntimeError，让失败工具走一条可识别的业务异常路径；这里定义类并不会抛异常。
class ToolCallError(RuntimeError):
    """本地工具替身的可预期失败。"""


# @dataclass 会根据字段生成 __init__、__repr__ 和按值比较等样板方法，可类比精简的 Java
# record/POJO；但 Python 类型提示依然不是编译期强制约束。frozen=True 禁止正常的字段再赋值，
# 让一份调用计划在多个 Task 间共享时保持不变。
@dataclass(frozen=True)
class ToolPlan:
    # name 是日志与映射键，value 是成功返回的数据；should_fail 控制是否走演示失败分支。
    name: str
    value: str
    # 默认值只在调用方未传此参数时使用，因此前两个计划会成功，显式传 True 的计划会失败。
    should_fail: bool = False


# 工具成功后不直接返回裸字符串，而是把“哪个工具”和“返回值”一起保存，便于后续验证身份。
@dataclass(frozen=True)
class ToolResult:
    tool_name: str
    value: str


# 这组 dataclass 是每次工具调用的控制面。它没有 frozen=True，因为实验会改变三个 Event
# 内部的开关状态；字段本身仍始终引用各自的 Event 对象。
@dataclass
class ToolControl:
    """started/release/finished 分别控制并观察一个工具的生命周期。"""

    # default_factory 接收的是可调用对象 asyncio.Event，而不是已经创建好的 Event。
    # 每次构造 ToolControl 都会分别调用它三次，因此不同字段、不同工具不会误共享同一信号。
    # Event 初始为关闭状态；wait() 会暂停，set() 会打开并唤醒等待者，且 set() 本身不需 await。
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)


# 圆括号里的逗号创建 tuple。tuple 本身不可变，适合把三份固定计划作为所有实验的共同输入；
# 元素 ToolPlan 也因 frozen=True 不允许普通字段赋值。
TOOLS: tuple[ToolPlan, ToolPlan, ToolPlan] = (
    ToolPlan("资料工具", "用户等级=gold"),
    ToolPlan("搜索工具", "找到 2 条文档"),
    ToolPlan("策略工具", "允许生成摘要"),
)


# 输入是若干 ToolPlan 组成的 tuple；返回字典把每个唯一工具名映射到一套全新的控制信号。
def make_controls(plans: tuple[ToolPlan, ...]) -> dict[str, ToolControl]:
    # 这是字典推导式：for 每取出一个 plan，就计算键 plan.name 和值 ToolControl()。
    # `...` 表示 tuple 可包含任意数量的 ToolPlan，不是函数体中的省略占位符。
    return {plan.name: ToolControl() for plan in plans}


# async def 定义 coroutine 函数；调用 controlled_tool(...) 只创建 coroutine 对象，函数体要到
# 被直接 await 或包装进 Task 后才会推进。返回注解表示正常路径最终产生 ToolResult。
async def controlled_tool(
    plan: ToolPlan,
    control: ToolControl,
    events: list[str],
    completion_order: list[str],
) -> ToolResult:
    """等待 release 事件后成功或失败，用于替代不可控的真实 I/O。"""
    # current_task() 查询当前正在推进这段 coroutine 的 Task。直接 await 时它就是上层调用者
    # Task；create_task 后则是新建的业务 Task。条件表达式处理理论上的 None，避免直接取名报错。
    current: Task[Any] | None = asyncio.current_task()
    task_name: str = current.get_name() if current is not None else "无 Task"
    record(events, f"{plan.name}: 开始（运行于 {task_name}）")
    # 先打开 started，控制器才知道本工具已经真正进入函数体，而不只是创建了 coroutine/Task。
    control.started.set()

    try:
        # Event.wait() 返回一个 awaitable。release 尚未 set 时，await 在此交还事件循环控制权；
        # 控制器随后可推进并决定何时放行。这代表“外部结果尚未就绪”，却不依赖真实等待时长。
        await control.release.wait()
        # 放行后根据计划选择失败或成功。失败也先写 completion_order，记录本工具结束工作的顺序。
        if plan.should_fail:
            completion_order.append(plan.name)
            record(events, f"{plan.name}: 失败")
            raise ToolCallError(f"{plan.name} 调用失败")

        # 成功路径构造新的不可变结果对象，记录完成顺序和可读事件，再由 return 交给等待者。
        result = ToolResult(plan.name, plan.value)
        completion_order.append(plan.name)
        record(events, f"{plan.name}: 完成 -> {plan.value}")
        return result
    except asyncio.CancelledError:
        # cancel() 的请求在下一个可取消的 await 处以 CancelledError 送进 coroutine。
        # 这里只观察取消已经到达；bare raise 会重新抛当前异常，让 Task 正确进入 cancelled 状态，
        # 而不是把取消消耗成一次正常返回。完整的取消传播与清理协议留到第 21 章。
        completion_order.append(plan.name)
        record(events, f"{plan.name}: 收到取消")
        raise
    finally:
        # finally 在成功、普通异常、取消三条路径都会执行。finished 因而表示“已离开工具调用”，
        # 控制器可安全推进下一个步骤；它不表示一定成功。
        control.finished.set()


# 等待所有工具都报告 started。函数本身不关心这些工具最终按什么顺序完成。
async def wait_until_all_started(controls: dict[str, ToolControl]) -> None:
    """不猜首次调度顺序，明确等到所有工具都已进入等待状态。"""
    # values() 取出各 ToolControl；生成器表达式逐个创建 Event.wait() coroutine。
    # 前缀 * 把它们展开为 gather 的多个位置参数。gather 并发等待，全部完成后这一行才返回。
    await asyncio.gather(*(control.started.wait() for control in controls.values()))


# 这个控制器配合“每次只启动一个工具”的实验：看到当前工具开始，就放行并等它完全结束，
# 然后 for 才进入下一轮。plans 的排列因此直接决定完成顺序。
async def drive_releases_after_each_start(
    plans: tuple[ToolPlan, ...],
    controls: dict[str, ToolControl],
    events: list[str],
) -> None:
    """逐个放行顺序调用；这个 Task 模拟三个彼此独立的外部服务。"""
    for plan in plans:
        # 用 plan.name 找到与本轮工具配套的三个 Event；字典下标在缺键时会抛 KeyError。
        control: ToolControl = controls[plan.name]
        # 即使控制器比工具先运行也没关系：它会先停在 started.wait()，不会过早放行。
        await control.started.wait()
        record(events, f"控制器: 放行 {plan.name}")
        control.release.set()
        # 等到工具的 finally 打开 finished，才允许处理下一个计划。
        await control.finished.wait()


# 这个控制器用于并发实验：先建立“全部已开始”的屏障，再按 order 指定的业务名逐个放行。
async def drive_release_order(
    controls: dict[str, ToolControl],
    order: tuple[str, ...],
    events: list[str],
) -> None:
    """先确认全部工具开始，再按指定次序制造确定的完成顺序。"""
    await wait_until_all_started(controls)
    record(events, "控制器: 已观察到全部工具开始")
    for name in order:
        record(events, f"控制器: 放行 {name}")
        controls[name].release.set()
        # 每次等待对应 finished 后再放行下一项，使 completion_order 与 order 稳定一致。
        await controls[name].finished.wait()


# 把“为每份计划创建有名字的 Task，并保存所有权”集中到一个同步辅助函数中。
# 参数列表中的单独 * 表示其后的 label 只能按关键字传入，调用点必须写 label="..."。
def create_tool_tasks(
    plans: tuple[ToolPlan, ...],
    controls: dict[str, ToolControl],
    events: list[str],
    completion_order: list[str],
    *,
    label: str,
) -> dict[str, asyncio.Task[ToolResult]]:
    """创建并返回有名字的 Task；调用方必须继续持有并最终回收它们。"""
    # 类型注解说明：键是工具名，值是最终产出 ToolResult 的 Task。局部字典会通过 return
    # 转交给调用方；调用方继续保存它，也就继续持有所有 Task 的强引用与业务身份。
    tasks: dict[str, asyncio.Task[ToolResult]] = {}
    for plan in plans:
        record(events, f"创建 Task: {plan.name}")
        # controlled_tool(...) 先产生尚未执行的 coroutine，再由 create_task 包装并安排运行。
        # name 只用于诊断和日志，不影响调度优先级。基础实验不传 eager_start，并在入口断言
        # 当前使用默认任务工厂，所以这次同步调用返回时 coroutine 函数体尚未开始。
        tasks[plan.name] = asyncio.create_task(
            controlled_tool(plan, controls[plan.name], events, completion_order),
            name=f"{label}:{plan.name}",
        )
    return tasks


# 第一组实验不为业务工具新建 Task，而是在入口 Task 的调用链中依次推进三个 coroutine。
async def demonstrate_direct_await() -> None:
    print_section("1. 直接 await: 三个工具严格顺序调用")
    # 这两个列表分别保留细粒度事件和完成工具名。空列表字面量每执行一次都会创建新对象，
    # 各实验不会共享记录；list[str] 仍是静态类型提示。
    events: list[str] = []
    completion_order: list[str] = []
    controls: dict[str, ToolControl] = make_controls(TOOLS)
    # 控制器必须作为独立 Task 运行，否则当前调用链停在工具的 release.wait() 后，无人放行。
    controller: Task[None] = asyncio.create_task(
        drive_releases_after_each_start(TOOLS, controls, events),
        name="direct-await:service-controller",
    )

    results: list[ToolResult] = []
    for plan in TOOLS:
        # 没有为工具创建 Task：controlled_tool 在当前入口 Task 的调用链里运行到 return。
        # 当前循环要等 result 到手后才会进入下一轮，所以后一个 coroutine 此刻甚至尚未创建。
        result: ToolResult = await controlled_tool(
            plan,
            controls[plan.name],
            events,
            completion_order,
        )
        results.append(result)
    # 三个工具返回后，控制器通常也已走完；仍显式 await，确保它的最终状态与异常被回收。
    await controller

    # 列表推导式保持 TOOLS 的迭代顺序，得到预期名列表；下一行再用相同写法读取实际结果。
    expected_names: list[str] = [plan.name for plan in TOOLS]
    assert [result.tool_name for result in results] == expected_names
    assert completion_order == expected_names
    # pairwise(TOOLS) 依次产出相邻二元组：(资料，搜索)、(搜索，策略)。
    for previous, following in pairwise(TOOLS):
        # index() 找前一个工具“完成”事件的位置；next() 从生成器取第一个匹配的“开始”位置。
        # enumerate(events) 每轮给出（下标，消息），for 中的逗号把二元组解包到两个名字。
        previous_finished: int = events.index(
            f"{previous.name}: 完成 -> {previous.value}"
        )
        following_started: int = next(
            index
            for index, message in enumerate(events)
            if message.startswith(f"{following.name}: 开始")
        )
        # 比较下标而非耗时：每一对都满足完成事件在开始事件之前，便证明业务调用严格顺序。
        assert previous_finished < following_started
    print("  结论: 后一个工具只在前一个工具返回后才开始")


# 第二组把两种都使用 create_task 的写法放在一起，证明“是否并发”取决于创建与暂停的时机，
# 不能只看代码里有没有 create_task，也不能只看后面是否逐个 await。
async def demonstrate_creation_patterns() -> None:
    print_section("2. create_task 后立刻 await，与先全部创建再 await")

    # 第一小组仍使用顺序控制器。每创建一个业务 Task，就立刻停下来等待它完成。
    immediate_events: list[str] = []
    immediate_completion: list[str] = []
    immediate_controls: dict[str, ToolControl] = make_controls(TOOLS)
    immediate_controller: Task[None] = asyncio.create_task(
        drive_releases_after_each_start(TOOLS, immediate_controls, immediate_events),
        name="immediate-await:service-controller",
    )

    # 字典既保留每个 Task 的强引用，也保留工具名到 Task 的身份关系。
    immediate_tasks: dict[str, asyncio.Task[ToolResult]] = {}
    for plan in TOOLS:
        # 这里确实新建了业务 Task，它会由事件循环独立推进，而非沿当前调用链直接推进。
        task: Task[ToolResult] = asyncio.create_task(
            controlled_tool(
                plan,
                immediate_controls[plan.name],
                immediate_events,
                immediate_completion,
            ),
            name=f"immediate-await:{plan.name}",
        )
        immediate_tasks[plan.name] = task
        # 当前入口 Task 在此暂停并等待新 Task 的最终状态；这一步完成前，for 不会开始下一轮，
        # 因此下一个业务 Task 尚未创建，三个工具的工作仍然没有重叠。
        await task
    # 同样明确回收控制器，而不是因“看起来早已完成”就丢弃引用。
    await immediate_controller

    expected_names: list[str] = [plan.name for plan in TOOLS]
    assert immediate_completion == expected_names
    # str.join(iterable) 用箭头连接所有名称，不会改变原列表。
    print("  创建一个就立刻 await:", " -> ".join(immediate_completion))

    # 第二小组先在同步函数 create_tool_tasks 内完成三次创建，中间没有任何 await。
    concurrent_events: list[str] = []
    concurrent_completion: list[str] = []
    concurrent_controls: dict[str, ToolControl] = make_controls(TOOLS)
    concurrent_tasks: dict[str, Task[ToolResult]] = create_tool_tasks(
        TOOLS,
        concurrent_controls,
        concurrent_events,
        concurrent_completion,
        label="create-all",
    )
    # any(...) 只要找到一个真值就返回 True。默认任务工厂不会在 create_task 调用内部立刻推进
    # coroutine，所以此刻记录里只有“创建 Task”，不应出现工具函数体写下的“开始”。
    assert not any(": 开始" in message for message in concurrent_events)

    # 该 tuple 与 TOOLS 的输入顺序故意不同，用来区分创建、完成和结果处理顺序。
    forced_completion = ("策略工具", "资料工具", "搜索工具")
    # create_task 返回后控制器与三个业务 Task 都处于可调度状态；入口在稍后的 await 才真正让出控制。
    concurrent_controller: Task[None] = asyncio.create_task(
        drive_release_order(
            concurrent_controls,
            forced_completion,
            concurrent_events,
        ),
        name="create-all:service-controller",
    )

    awaited_results: list[str] = []
    # 虽然这里按 TOOLS 顺序逐个 await，但三个 Task 早已全部安排运行，所以这不是顺序启动。
    # 入口先等“资料工具”时，控制器仍能让策略工具先完成；完成的 Task 会保存结果，等未来读取。
    for plan in TOOLS:
        result: ToolResult = await concurrent_tasks[plan.name]
        awaited_results.append(result.tool_name)
    await concurrent_controller

    assert concurrent_completion == list(forced_completion)
    # awaited_results 记录的是 for 选择等待/读取的顺序，因此仍与输入 expected_names 相同。
    assert awaited_results == expected_names
    # 先定位控制器写入的“全部开始”屏障，再寻找任意第一个成功完成事件。
    all_started_marker: int = concurrent_events.index("控制器: 已观察到全部工具开始")
    first_completion: int = next(
        index
        for index, message in enumerate(concurrent_events)
        if ": 完成 ->" in message
    )
    # 这个断言排除了“某工具还没开始，另一个就已完成”的情况，不依赖三者内部的开始先后。
    assert all_started_marker < first_completion

    print("  实际完成顺序:", " -> ".join(concurrent_completion))
    print("  逐个 await 得到结果的顺序:", " -> ".join(awaited_results))
    print("  结论: await task1 再 await task2，不会撤销它们已经获得的并发性")


# 这个极小 coroutine 专门探测“函数体何时开始”。label 既作为日志内容，也作为最终返回值。
async def eager_probe(label: str, events: list[str]) -> str:
    record(events, f"{label}: 函数体开始")
    # sleep(0) 不等待真实时长，却仍是协作检查点：当前 Task 主动暂停，让事件循环推进其他工作。
    # eager 模式只会同步推进到这个首次阻塞点；实验没有基于耗时做断言。
    await asyncio.sleep(0)
    record(events, f"{label}: 恢复并完成")
    return label


# 第三组是 Python 3.14 专题：只改变 create_task 的 eager_start 参数，比较返回瞬间的事件列表。
async def demonstrate_python314_eager_start() -> None:
    print_section("3. Python 3.14 补充: eager_start 会改变开始时机")
    # get_running_loop() 只能在正在运行的事件循环中调用。任务工厂为 None 表示使用默认工厂，
    # 避免外部安装的 eager 工厂改变基础实验的开始语义。
    loop: AbstractEventLoop = asyncio.get_running_loop()
    assert loop.get_task_factory() is None

    normal_events: list[str] = []
    # 默认工厂下，普通 create_task 只安排稍后运行；同步调用返回时 eager_probe 尚未写事件。
    normal: Task[str] = asyncio.create_task(
        eager_probe("普通 Task", normal_events), name="normal-probe"
    )
    print("  普通 create_task 返回时的事件:", normal_events)
    assert normal_events == []
    # await 让出控制并回收 Task，之后普通探针会完整执行。
    await normal

    eager_events: list[str] = []
    # eager_start=True 允许 coroutine 在 create_task 调用内部立即运行，直到首次阻塞。
    eager: Task[str] = asyncio.create_task(
        eager_probe("eager Task", eager_events),
        name="eager-probe",
        eager_start=True,
    )
    # 因此“函数体开始”已经发生，但 sleep(0) 后的恢复尚未发生，Task 也还没有最终状态。
    print("  eager create_task 返回时的事件:", eager_events)
    assert eager_events == ["eager Task: 函数体开始"]
    assert not eager.done()
    await eager

    print("  结论: 创建顺序不能自动等同于开始顺序；eager 模式尤其会改变时机")


# 调用此 async 函数会得到一次性的原生 coroutine 对象，第四组用它和可复用最终状态的 Task 对照。
async def one_shot_coroutine() -> str:
    return "原生 coroutine 的结果"


# 第四组依次制造 pending、成功、失败和取消状态，并在各状态下调用 Task 查询方法。
async def demonstrate_task_state() -> None:
    print_section("4. Task 状态、结果、异常与重复 await")
    events: list[str] = []
    completion_order: list[str] = []
    plan = ToolPlan("状态工具", "ready")
    control = ToolControl()
    task: Task[ToolResult] = asyncio.create_task(
        controlled_tool(plan, control, events, completion_order),
        name="state:success",
    )
    # 等到 started 后，工具确定已经运行并阻塞在仍关闭的 release 上，因此 Task 稳定处于 pending。
    await control.started.wait()

    print("  未完成: done=", task.done(), "cancelled=", task.cancelled())
    # 每个二元组保存“展示名称”和一个已绑定方法。for 会将二元组解包到 method_name/accessor；
    # 直接调用 accessor() 等价于分别调用 task.result()、task.exception()，不会等待 Task。
    for method_name, accessor in (
        ("result", task.result),
        ("exception", task.exception),
    ):
        try:
            accessor()
        except asyncio.InvalidStateError as error:
            # pending Task 没有最终值或异常，两种读取器都会立刻抛 InvalidStateError。
            print(f"  未完成时调用 {method_name}():", type(error).__name__)
        else:
            # else 只在 try 中没有异常时执行；到达这里说明 asyncio 行为与实验预期不符。
            raise AssertionError(f"未完成 Task 的 {method_name}() 应抛 InvalidStateError")

    # 打开 release 后，await 真正等待最终状态并取得 coroutine 的 return 值。
    control.release.set()
    first: ToolResult = await task
    # Task 保存了最终状态，因此可再次 await；它不会重新执行 controlled_tool，也不会重复事件。
    second: ToolResult = await task
    print("  完成: done=", task.done(), "cancelled=", task.cancelled())
    print("  两次 await 同一 Task:", first, "|", second)
    print("  result():", task.result(), "exception():", task.exception())
    # is 比较对象身份而不是值相等：两次 await 返回的正是同一个 ToolResult 实例。
    assert first is second
    assert task.exception() is None

    # 再建一份 should_fail=True 的计划，隔离演示 Task 如何保存普通异常。
    failed_plan = ToolPlan("失败状态工具", "不会返回", should_fail=True)
    failed_control = ToolControl()
    failed: Task[ToolResult] = asyncio.create_task(
        controlled_tool(failed_plan, failed_control, events, completion_order),
        name="state:failure",
    )
    await failed_control.started.wait()
    failed_control.release.set()
    try:
        # await 失败 Task 会在等待点把 ToolCallError 重新抛给当前调用方。
        await failed
    except ToolCallError as error:
        print("  await 失败 Task:", type(error).__name__, str(error))
    else:
        raise AssertionError("失败 Task 应向等待者重新抛出工具异常")

    # Task 已完成且未取消，此时 exception() 不再抛 InvalidStateError，而是返回保存的异常对象。
    stored_error: BaseException | None = failed.exception()
    assert isinstance(stored_error, ToolCallError)
    try:
        # result() 只在成功状态返回值；失败状态会重新抛出 Task 保存的原异常。
        failed.result()
    except ToolCallError:
        print("  失败后 exception() 返回异常对象，result() 重新抛出它")
    else:
        raise AssertionError("失败 Task 的 result() 应重新抛出原异常")

    # 这里没有 create_task：one_shot 直接引用一次 coroutine 调用产生的执行对象。
    one_shot: CoroutineType[Any, Any, str] = one_shot_coroutine()
    await one_shot
    try:
        # 原生 coroutine 不是最终状态容器；一次执行结束后不能倒带或再次推进。
        await one_shot
    except RuntimeError as error:
        print("  原生 coroutine 第二次 await:", type(error).__name__, str(error))
    else:
        raise AssertionError("已完成的原生 coroutine 不可复用")

    # 最后一种状态只作取消预告：Task 已开始且正停在可取消的 Event.wait() 上。
    cancel_plan = ToolPlan("取消预告工具", "不会返回")
    cancel_control = ToolControl()
    cancelled_task: Task[ToolResult] = asyncio.create_task(
        controlled_tool(cancel_plan, cancel_control, events, completion_order),
        name="state:cancel-preview",
    )
    await cancel_control.started.wait()
    # cancel(message) 发送取消请求并返回请求是否被接受；它不是同步把 cancelled 标志设为 True。
    accepted: bool = cancelled_task.cancel("第 21 章再讲完整协议")
    print("  cancel() 请求已接受:", accepted, "立即 cancelled=", cancelled_task.cancelled())
    try:
        # 必须 await，才能让 CancelledError 送入 coroutine，执行其 except/finally，并回收最终状态。
        await cancelled_task
    except asyncio.CancelledError:
        print("  取消送达并回收后: cancelled=", cancelled_task.cancelled())
    else:
        raise AssertionError("本例中的 Task 应进入取消完成状态")
    # and 具有短路语义；这里只在 done() 为真后再检查 cancelled()，验证最终确为取消完成。
    assert cancelled_task.done() and cancelled_task.cancelled()


# 第五组让完成顺序故意偏离输入顺序，观察 gather 按哪一种顺序排列返回列表。
async def demonstrate_gather_order() -> None:
    print_section("5. gather: 并发等待，结果保持输入顺序")
    events: list[str] = []
    completion_order: list[str] = []
    controls: dict[str, ToolControl] = make_controls(TOOLS)
    tasks: dict[str, Task[ToolResult]] = create_tool_tasks(
        TOOLS,
        controls,
        events,
        completion_order,
        label="gather-order",
    )
    # 列表推导式按 TOOLS 的固定顺序，从字典中取出三个原 Task；这就是稍后 gather 的输入顺序。
    ordered_tasks: list[Task[ToolResult]] = [tasks[plan.name] for plan in TOOLS]
    # 完成顺序被明确固定为“搜索、策略、资料”，与 ordered_tasks 的“资料、搜索、策略”不同。
    forced_completion = ("搜索工具", "策略工具", "资料工具")
    controller: Task[None] = asyncio.create_task(
        drive_release_order(controls, forced_completion, events),
        name="gather-order:service-controller",
    )

    # * 在调用位置把列表拆成三个位置参数。因输入已经是 Task，gather 会观察这些原对象，
    # 并返回一个独立的聚合 Future-like 对象；此刻尚未 await，所以先有机会检查它的状态。
    aggregate: Future[list[ToolResult]] = asyncio.gather(*ordered_tasks)
    waiter: Task[Any] | None = asyncio.current_task()
    # current_task() 理论上可返回 None，条件表达式在取 get_name() 前先保护这一情况。
    print("  等待者 Task:", waiter.get_name() if waiter else None)
    # `in` 按相等性检查容器成员；Task 使用对象身份语义，这里证明聚合对象不是任一输入 Task。
    print("  gather 聚合对象是独立 Future-like:", aggregate not in ordered_tasks)
    print("  await 前 aggregate.done():", aggregate.done())
    # await aggregate 会等待所有输入成功结束，并给出按输入位置排列的 ToolResult 列表。
    results: list[ToolResult] = await aggregate
    await controller

    input_order: list[str] = [plan.name for plan in TOOLS]
    result_order: list[str] = [result.tool_name for result in results]
    # 第一条断言验证真实完成顺序，第二条验证 gather 的结果顺序；两者故意不同。
    assert completion_order == list(forced_completion)
    assert result_order == input_order
    print("  完成顺序:", " -> ".join(completion_order))
    print("  gather 结果顺序:", " -> ".join(result_order))


# 第六组的控制器先只放行失败项，然后等待调用方确认“已经观察兄弟状态”，再放行其余项。
# caught_by_waiter 是双方的同步信号，使 pending 结论不受事件循环碰巧多调度几步的影响。
async def drive_gather_failure(
    plans: tuple[ToolPlan, ...],
    controls: dict[str, ToolControl],
    caught_by_waiter: asyncio.Event,
    events: list[str],
) -> None:
    """先让失败发生；确认等待者已捕获后，才放行两个兄弟 Task。"""
    await wait_until_all_started(controls)
    # next() 取得生成器产生的第一个失败计划名；本实验数据确保恰好存在一个失败计划。
    failed_name: str = next(plan.name for plan in plans if plan.should_fail)
    controls[failed_name].release.set()
    # finished 在工具的 finally 中打开，所以返回后失败异常已经写入对应 Task 的最终状态。
    await controls[failed_name].finished.wait()

    # 这道门确保“捕获异常时兄弟仍 pending”不是调度速度造成的偶然现象：在调用方显式
    # set() 之前，控制器不可能打开兄弟 Task 的 release。
    await caught_by_waiter.wait()
    for plan in plans:
        # continue 立即跳到下一轮，失败项不会被重复放行；其余两个才进入下面的清理步骤。
        if plan.name == failed_name:
            continue
        controls[plan.name].release.set()
        await controls[plan.name].finished.wait()


# 把 Task 的内部组合状态转换成适合打印的标签。检查顺序很重要：取消也属于 done，
# 所以先排除 cancelled，再判断 pending，最后才可安全调用 exception()。
def state_label(task: asyncio.Task[ToolResult]) -> str:
    if task.cancelled():
        return "cancelled"
    if not task.done():
        return "pending"
    return "failed" if task.exception() is not None else "succeeded"


# 默认 gather 会把第一个普通异常传播给等待者，但不会因此取消其他输入 Task。
async def demonstrate_gather_failure() -> None:
    print_section("6. gather 异常: 传播一个异常，不自动取消兄弟 Task")
    # 只有“搜索工具”启用失败开关；三份 ToolControl 仍各自独立。
    plans: tuple[ToolPlan, ToolPlan, ToolPlan] = (
        ToolPlan("资料工具", "用户等级=gold"),
        ToolPlan("搜索工具", "不会返回", should_fail=True),
        ToolPlan("策略工具", "允许生成摘要"),
    )
    events: list[str] = []
    completion_order: list[str] = []
    controls: dict[str, ToolControl] = make_controls(plans)
    tasks: dict[str, Task[ToolResult]] = create_tool_tasks(
        plans,
        controls,
        events,
        completion_order,
        label="gather-failure",
    )
    # 保存输入顺序和原 Task，既便于 gather 聚合，也便于异常传播后继续管理兄弟 Task。
    ordered_tasks: list[Task[ToolResult]] = [tasks[plan.name] for plan in plans]
    caught_by_waiter = asyncio.Event()
    controller: Task[None] = asyncio.create_task(
        drive_gather_failure(plans, controls, caught_by_waiter, events),
        name="gather-failure:service-controller",
    )

    # 没传 return_exceptions，默认值 False 表示普通异常通过 await aggregate 直接抛给等待者。
    aggregate: Future[list[ToolResult]] = asyncio.gather(*ordered_tasks)
    try:
        await aggregate
    except ToolCallError as error:
        # 控制器尚停在 caught_by_waiter：搜索 Task 已失败，两个兄弟还停在各自 release.wait()。
        print("  gather 向等待者传播:", type(error).__name__, str(error))
        print("  aggregate.done():", aggregate.done())
        for plan in plans:
            print(f"  捕获时 {plan.name}: {state_label(tasks[plan.name])}")
        # 这些断言把“聚合 Future 已因异常完成”与“兄弟 Task 仍未完成且未取消”区分开。
        assert tasks["搜索工具"].done()
        assert not tasks["资料工具"].done()
        assert not tasks["策略工具"].done()
        assert not tasks["资料工具"].cancelled()
        assert not tasks["策略工具"].cancelled()
        # 状态观察完才通知控制器放行兄弟，确保上面的证据稳定可复现。
        caught_by_waiter.set()
    else:
        raise AssertionError("默认 gather 应传播第一个工具异常")

    # 仍在同一个事件循环中，把相同的原 Task 再交给 gather。完成 Task 可以重复等待；
    # return_exceptions=True 让 ToolCallError 作为对应位置的列表元素返回，而不是再次中断等待，
    # 因而两个兄弟有机会继续完成，所有 Task 的结果/异常也得到统一回收。
    collected: list[ToolResult | BaseException] = await asyncio.gather(
        *ordered_tasks, return_exceptions=True
    )
    await controller
    # 索引 1 对应 ordered_tasks 的第二项“搜索工具”；gather 的结果按输入位置而非完成顺序排列。
    assert isinstance(collected[1], ToolCallError)
    # all(...) 要求每个 Task 都完成；any(...) 只要发现一个已取消就为真，外层 not 要求一个也没有。
    assert all(task.done() for task in ordered_tasks)
    assert not any(task.cancelled() for task in ordered_tasks)
    # 字典推导式保留“工具名 -> 最终标签”，items() 每轮把键和值解包给 name/task。
    print("  再收集后的状态:", {name: state_label(task) for name, task in tasks.items()})
    print("  return_exceptions=True 的输入位置 2:", repr(collected[1]))
    print("  完成顺序:", " -> ".join(completion_order))


# 第七组不要求固定字段顺序，而是哪个 Task 先完成就立即处理哪个，同时仍要找回其业务名称。
async def demonstrate_as_completed() -> None:
    print_section("7. as_completed: 按完成顺序处理，并保留 Task 身份")
    events: list[str] = []
    completion_order: list[str] = []
    controls: dict[str, ToolControl] = make_controls(TOOLS)
    tasks: dict[str, Task[ToolResult]] = create_tool_tasks(
        TOOLS,
        controls,
        events,
        completion_order,
        label="as-completed",
    )
    # tasks 原来是“名称 -> Task”；这里用字典推导式反转成“Task -> 名称”。Task 对象可哈希，
    # 所以能作为 dict 键；后面无需依赖输入下标或解析 Task 的诊断名称来识别业务身份。
    task_names: dict[Task[ToolResult], str] = {
        task: name for name, task in tasks.items()
    }
    forced_completion = ("策略工具", "搜索工具", "资料工具")
    controller: Task[None] = asyncio.create_task(
        drive_release_order(controls, forced_completion, events),
        name="as-completed:service-controller",
    )

    processed: list[str] = []
    # as_completed 返回可异步迭代对象；async for 会在每个下一项完成前协作等待，而不会阻塞线程。
    # Python 3.14 使用这种异步迭代形式时，传入的原 Task 会按完成顺序原样产出，适合身份映射。
    # 这不同于兼容旧版的普通 `for ... in as_completed(...)`：后者产出包装 coroutine。
    async for finished_task in asyncio.as_completed(tasks.values()):
        assert isinstance(finished_task, asyncio.Task), "3.14 异步迭代应产出原 Task"
        # 只有原对象身份得到保留，finished_task 才能直接命中反向字典。
        tool_name: str = task_names[finished_task]
        # 产出的 Task 已完成，await 会立刻交付它保存结果；若它失败，则会在这里抛出异常。
        result: ToolResult = await finished_task
        assert result.tool_name == tool_name
        processed.append(tool_name)
        print(f"  立即处理 {tool_name}: {result.value}")
    await controller

    # 控制器逐个放行并等待 finished，所以异步迭代的处理顺序应与确定的完成顺序完全一致。
    assert processed == list(forced_completion)
    assert completion_order == processed
    print("  处理顺序:", " -> ".join(processed))


# 第八组使用 wait 取得 done/pending 两个集合。wait 只报告状态，所有权和后续处理仍归调用方。
async def demonstrate_wait() -> None:
    print_section("8. wait: done/pending、超时和返回条件")
    events: list[str] = []
    completion_order: list[str] = []
    controls: dict[str, ToolControl] = make_controls(TOOLS)
    tasks: dict[str, Task[ToolResult]] = create_tool_tasks(
        TOOLS,
        controls,
        events,
        completion_order,
        label="wait-first-completed",
    )
    # set(...) 保存无顺序、元素唯一的 Task 集合；wait 的返回值也是集合，不承诺输入位置语义。
    task_set: set[Task[ToolResult]] = set(tasks.values())
    task_names: dict[Task[ToolResult], str] = {
        task: name for name, task in tasks.items()
    }
    # 先确认所有业务 Task 已停在 release 上，后面的 timeout=0 才会稳定得到全 pending。
    await wait_until_all_started(controls)

    # timeout=0 表示立刻查看一次状态。wait 超时不会抛 TimeoutError，也不会取消未完成 Task。
    timeout_done: set[Task[ToolResult]]
    timeout_pending: set[Task[ToolResult]]
    timeout_done, timeout_pending = await asyncio.wait(task_set, timeout=0)
    print("  timeout=0: done=", len(timeout_done), "pending=", len(timeout_pending))
    # set() 创建空集合；不能写 {}，因为空的 {} 是 dict。
    assert timeout_done == set()
    assert timeout_pending == task_set
    assert not any(task.cancelled() for task in timeout_pending)

    # 只放行策略工具。FIRST_COMPLETED 会在任一 Task 成功、失败或取消完成时提前返回。
    controls["策略工具"].release.set()
    done: set[Task[ToolResult]]
    pending: set[Task[ToolResult]]
    done, pending = await asyncio.wait(
        task_set, return_when=asyncio.FIRST_COMPLETED
    )
    # 两个集合没有固定迭代顺序，转成名称集合后再与期望集合比较。
    done_names: set[str] = {task_names[task] for task in done}
    pending_names: set[str] = {task_names[task] for task in pending}
    print("  FIRST_COMPLETED done:", done_names)
    print("  FIRST_COMPLETED pending:", pending_names)
    assert done_names == {"策略工具"}
    assert pending_names == {"资料工具", "搜索工具"}
    assert not any(task.cancelled() for task in pending)

    # wait 只报告 pending；拥有者仍需决定如何结束并回收它们。这里按反向映射找到控制信号，
    # 打开剩余 release，随后 gather 等待原 task_set 中的全部 Task，避免实验留下游离工作。
    for task in pending:
        controls[task_names[task]].release.set()
    await asyncio.gather(*task_set)
    assert all(task.done() for task in task_set)

    # 第二小组专门演示 FIRST_EXCEPTION：只有搜索工具抛普通异常，两个兄弟仍保持 pending。
    failure_plans: tuple[ToolPlan, ToolPlan, ToolPlan] = (
        ToolPlan("资料工具", "用户等级=gold"),
        ToolPlan("搜索工具", "不会返回", should_fail=True),
        ToolPlan("策略工具", "允许生成摘要"),
    )
    failure_events: list[str] = []
    failure_order: list[str] = []
    failure_controls: dict[str, ToolControl] = make_controls(failure_plans)
    failure_tasks: dict[str, Task[ToolResult]] = create_tool_tasks(
        failure_plans,
        failure_controls,
        failure_events,
        failure_order,
        label="wait-first-exception",
    )
    failure_set: set[Task[ToolResult]] = set(failure_tasks.values())
    failure_names: dict[Task[ToolResult], str] = {
        task: name for name, task in failure_tasks.items()
    }
    await wait_until_all_started(failure_controls)
    # 只放行预定失败项，确保 FIRST_EXCEPTION 的唤醒原因明确。
    failure_controls["搜索工具"].release.set()

    failed_done: set[Task[ToolResult]]
    failed_pending: set[Task[ToolResult]]
    failed_done, failed_pending = await asyncio.wait(
        failure_set,
        return_when=asyncio.FIRST_EXCEPTION,
    )
    print("  FIRST_EXCEPTION done:", {failure_names[task] for task in failed_done})
    print("  FIRST_EXCEPTION pending:", {failure_names[task] for task in failed_pending})
    assert {failure_names[task] for task in failed_done} == {"搜索工具"}
    # 本例确定 done 集合只有一个元素。iter(...) 创建迭代器，next(...) 取出该失败 Task；
    # Task 已完成且未取消，所以现在调用 exception() 是安全的。
    assert isinstance(next(iter(failed_done)).exception(), ToolCallError)
    assert not any(task.cancelled() for task in failed_pending)

    # 提前返回没有替调用方处理 pending；这里明确让兄弟完成，再把失败异常作为值收集。
    for task in failed_pending:
        failure_controls[failure_names[task]].release.set()
    outcomes: list[ToolResult | BaseException] = await asyncio.gather(
        *failure_set, return_exceptions=True
    )
    # failure_set 是无序集合，因此不检查异常的固定下标，只要求所有结果中存在 ToolCallError。
    assert any(isinstance(outcome, ToolCallError) for outcome in outcomes)
    assert all(task.done() for task in failure_set)


# 第九组模拟超出当前请求调用栈、但仍属于应用生命周期的后台任务；重点是所有权而非并发 API。
async def demonstrate_background_ownership() -> None:
    print_section("9. 后台 Task: 强引用、异常收集与退出回收")
    plans: tuple[ToolPlan, ToolPlan] = (
        ToolPlan("审计写入", "audit-ok"),
        ToolPlan("指标上报", "不会返回", should_fail=True),
    )
    events: list[str] = []
    completion_order: list[str] = []
    controls: dict[str, ToolControl] = make_controls(plans)

    # 这个 dict 是明确的拥有者：键对 Task 保存强引用，值保存业务身份。若只写
    # asyncio.create_task(...) 而丢掉返回值，就缺少后续等待、异常收集和退出清理的句柄。
    owned_tasks: dict[asyncio.Task[ToolResult], str] = {}
    for plan in plans:
        task: Task[ToolResult] = asyncio.create_task(
            controlled_tool(plan, controls[plan.name], events, completion_order),
            name=f"background:{plan.name}",
        )
        owned_tasks[task] = plan.name

    # 等全部后台 Task 确实开始后，字典仍保存两项；len() 观察的是拥有者中的引用数。
    await wait_until_all_started(controls)
    print("  运行中的已拥有 Task 数:", len(owned_tasks))

    # 模拟应用退出信号：values() 取出每个控制器并打开 release，让这些有限任务可以结束。
    for control in controls.values():
        control.release.set()
    # list(owned_tasks) 遍历 dict 时得到键，因此快照是 Task 列表。快照冻结本轮等待对象，
    # 后面处理结果期间即使拥有者发生变化，也不会破坏 Task 与 outcomes 的位置对应关系。
    task_snapshot: list[Task[ToolResult]] = list(owned_tasks)
    # return_exceptions=True 保证一个后台 Task 失败时，仍等待其他 Task 并把每项最终状态收齐。
    outcomes: list[ToolResult | BaseException] = await asyncio.gather(
        *task_snapshot, return_exceptions=True
    )

    # zip 按位置把 Task 与 gather 结果配对；strict=True 要求两个序列等长，长度不符就抛 ValueError，
    # 防止静默遗漏结果。for 再把每个二元组解包为 task 和 outcome。
    for task, outcome in zip(task_snapshot, outcomes, strict=True):
        owner_name: str = owned_tasks[task]
        # CancelledError 也属于 BaseException；用它分类可覆盖成功值之外的异常/取消结果。
        if isinstance(outcome, BaseException):
            print(f"  已收集 {owner_name} 的异常: {type(outcome).__name__}: {outcome}")
        else:
            print(f"  已收集 {owner_name} 的结果: {outcome.value}")

    # 只有结果和异常都处理完，才 clear() 原地清空字典，释放拥有者保存的强引用。
    owned_tasks.clear()
    # 空容器在布尔上下文中为 False，所以 not owned_tasks 验证拥有者已经清空。
    assert not owned_tasks


# 这是九组异步实验的总入口。它本身会被 asyncio.run 包装为顶层 Task，并按书写顺序逐组等待。
async def run_examples() -> None:
    current: Task[Any] | None = asyncio.current_task()
    # current 理论上可为 None；本函数实际由 asyncio.run 驱动，因此通常能把顶层 Task 改成可读名称。
    if current is not None:
        current.set_name("chapter20-main")

    # 基础实验依赖默认的非 eager 任务工厂，所以在入口尽早断言环境没有被外部代码改写。
    loop: AbstractEventLoop = asyncio.get_running_loop()
    print("Python 3.14 基础实验任务工厂:", loop.get_task_factory())
    assert loop.get_task_factory() is None

    # 每个 await 都等本组实验完整回收内部 Task 后才进入下一组，避免事件和状态相互干扰。
    await demonstrate_direct_await()
    await demonstrate_creation_patterns()
    await demonstrate_python314_eager_start()
    await demonstrate_task_state()
    await demonstrate_gather_order()
    await demonstrate_gather_failure()
    await demonstrate_as_completed()
    await demonstrate_wait()
    await demonstrate_background_ownership()

    # all_tasks() 返回当前事件循环中尚未完成的 Task 集合，其中也包含正在执行本行的入口 Task。
    # 集合推导式排除当前 Task 和已完成项；每个辅助 Task 都应在所属实验内被等待，最终应为空。
    unfinished: set[Task[Any]] = {
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    }
    print("\n入口结束前未回收的其他 Task 数:", len(unfinished))
    assert not unfinished
    print("所有第 20 章示例与运行时断言均已完成。")


# 普通同步 main 是程序与 asyncio 的边界。asyncio.run 会创建事件循环、运行 run_examples
# 到完成，并在退出时关闭循环；一个进程里通常只在最外层调用它，不能嵌套在已运行的循环中。
def main() -> None:
    asyncio.run(run_examples())


# Python 直接执行本文件时，模块名是 "__main__"，因此调用 main；若被其他模块 import，
# 解释器只完成上面的定义，不自动启动九组实验。这相当于显式声明脚本入口。
if __name__ == "__main__":
    main()
