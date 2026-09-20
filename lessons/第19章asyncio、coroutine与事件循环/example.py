"""第 19 章：asyncio、coroutine、await 与事件循环。

示例只使用本地、可控的替身来暴露暂停与恢复顺序，不访问真实 LLM、数据库或网络。
每段都用事件列表做运行时断言，因此观察结果不依赖精确耗时或偶然的调度顺序。
"""

import asyncio
import inspect
import time

# asyncio 提供事件循环、协程调度工具以及 Event/Future/Task 等异步原语。
# inspect 在这里不参与调度，只用来辨认“协程函数 / 协程对象”并读取对象状态。
# time.sleep 则故意充当同步阻塞反例，用来和 asyncio 的协作等待形成对照。


# 这两个同步辅助函数只负责展示和收集事件。它们内部没有 await，调用时会立即执行完毕；
# `title: str`、`-> None` 等类型提示主要服务读者和静态检查器，不会像 Java 类型那样在运行时强制校验。
def print_section(title: str) -> None:
    # f-string 会先计算花括号里的 title，再嵌入字符串；开头的 \n 让各实验分段显示。
    print(f"\n=== {title} ===")


def record(events: list[str], message: str) -> None:
    """同时保存和打印事件，让控制流既可观察又可断言。"""
    # events 是调用方传入的同一个可变 list。append 会原地修改它，因此调用方随后能看到新事件；
    # 这不同于重新给局部变量赋一个新列表。保留事件后再打印，既方便人读，也方便后面精确断言顺序。
    events.append(message)
    print(f"  事件 | {message}")


# `async def` 定义的是协程函数。调用它只创建 coroutine 对象，不会立即执行下面的函数体；
# 当对象被 await、被包装成 Task，或交给 asyncio.run 后，函数体才会被推进。
# 返回注解 `-> str` 描述协程最终完成时交出的普通字符串，而不是调用瞬间的返回对象类型。
async def lifecycle_coroutine(events: list[str]) -> str:
    """用一个明确会暂停的检查点展示协程体何时启动、恢复和返回。"""
    record(events, "函数体开始")
    # sleep(0) 不模拟真实延迟，只提供一个保证会暂停当前 Task 的协作检查点。
    # `await` 保存当前协程的执行位置并把执行权交还事件循环；以后再次获得运行机会时，
    # 从下一行继续，而不是从函数开头重新执行。
    await asyncio.sleep(0)
    record(events, "恢复")
    record(events, "返回")
    return "生命周期结果"


# 第一组实验把“函数”“一次调用产生的对象”“对象完成后的结果”明确分开。
async def demonstrate_coroutine_lifecycle() -> None:
    print_section("1. async def、协程函数、协程对象与最终结果")
    # `list[str]` 表示期望元素都是字符串。运行时它仍是普通 list，类型参数不会自动检查 append 的值。
    events: list[str] = []

    # 这次调用把参数 events 保存进新 coroutine 对象 pending，但 lifecycle_coroutine 的函数体尚未运行。
    # 因而“创建前”和“创建后”会相邻出现，列表中暂时不会有“函数体开始”。
    record(events, "创建前")
    pending: CoroutineType[Any, Any, str] = lifecycle_coroutine(events)
    record(events, "创建后")

    # 第一个检查针对函数本身，第二个检查针对调用得到的 pending 对象，二者不是同一个概念。
    # getcoroutinestate 返回枚举式状态字符串；尚未被推进的原生协程处于 CORO_CREATED。
    print("  lifecycle_coroutine 是协程函数:", inspect.iscoroutinefunction(lifecycle_coroutine))
    print("  调用所得对象是 coroutine:", inspect.iscoroutine(pending))
    print("  刚创建时状态:", inspect.getcoroutinestate(pending))
    print("  函数体是否已经开始:", "函数体开始" in events)
    # `assert` 在条件为假时抛 AssertionError。本例用它把教学结论变成可执行的验证，
    # 而不只是依赖肉眼查看打印文本。
    assert events == ["创建前", "创建后"]
    assert inspect.getcoroutinestate(pending) == inspect.CORO_CREATED

    # await 开始推进 pending。它在内部 sleep(0) 处暂停一次，恢复后执行到 return；
    # 整个 await 表达式的值就是 return 的字符串，并被绑定到 result。
    result: str = await pending

    # 已执行到 return 的原生 coroutine 会关闭，不能把同一个对象倒带后再运行一次。
    # 第 6 组实验会专门演示重复 await 时的 RuntimeError。
    print("  await 表达式得到最终结果:", result)
    print("  完成后的对象状态:", inspect.getcoroutinestate(pending))
    assert events == ["创建前", "创建后", "函数体开始", "恢复", "返回"]
    assert result == "生命周期结果"
    assert inspect.getcoroutinestate(pending) == inspect.CORO_CLOSED


# 接下来的三个协程组成一条嵌套调用链。直接 await 子协程不会为每一层创建新线程或新 Task；
# 当前 Task 会依次进入外层、中层和内层，直到最深处遇到真正会暂停的 awaitable。
async def load_leaf(events: list[str]) -> int:
    record(events, "内层: 开始")
    # 整条调用链在这里暂停；事件循环获得控制权，之后再从本行 await 的后面恢复。
    await asyncio.sleep(0)
    record(events, "内层: 等待结束后恢复")
    return 20


async def transform_middle(events: list[str]) -> int:
    record(events, "中层: 开始并 await 内层")
    # 调用 load_leaf 创建内层 coroutine，await 推进它并等待其 return；value 最终得到整数 20。
    value: int = await load_leaf(events)
    record(events, f"中层: 拿到 {value} 后恢复")
    return value + 1


async def serve_outer(events: list[str]) -> int:
    record(events, "外层: 开始并 await 中层")
    # 中层完成后把 21 作为 await 表达式的值传回这里，外层才会继续计算。
    value: int = await transform_middle(events)
    record(events, f"外层: 拿到 {value} 后恢复")
    return value * 2


# 第二组实验从调用链最外层进入，用一份完整事件序列验证“由外向内进入、由内向外返回”。
async def demonstrate_await_chain() -> None:
    print_section("2. await 调用链怎样暂停与恢复")
    events: list[str] = []
    # result 要等 serve_outer 连同全部子协程完成后才会得到最终整数 42。
    result: int = await serve_outer(events)

    # expected 是预先写下的精确顺序。把实际列表与它整体比较，可以捕捉缺失、重复或次序错误。
    expected: list[str] = [
        "外层: 开始并 await 中层",
        "中层: 开始并 await 内层",
        "内层: 开始",
        "内层: 等待结束后恢复",
        "中层: 拿到 20 后恢复",
        "外层: 拿到 21 后恢复",
    ]
    print("  最终结果:", result)
    assert events == expected
    assert result == 42


# 这个协程没有任何 await：它一旦被 Task 真正推进，就会连续执行到隐式返回 None。
# “没有 await”并不改变其协程函数身份，只说明函数体内部没有主动暂停点。
async def peer_coroutine(events: list[str]) -> None:
    record(events, "旁路 Task: 得到运行机会")


# 第三组实验反驳“代码里出现 await，就一定先切换到其他 Task”这一误解。
async def demonstrate_immediate_await() -> None:
    print_section("3. 写了 await 不等于一定切换调度")
    events: list[str] = []

    # 调用 peer_coroutine 先产生 coroutine 对象，create_task 再把它交给事件循环管理。
    # 这个 Task 只是观察器；name 便于调试，eager_start=False 明确要求它先排队而不立刻启动。
    # 完整 Task 生命周期留到下一章，本例只用它判断旁路工作是否获得过运行机会。
    peer_task: Task[None] = asyncio.create_task(
        peer_coroutine(events),
        name="immediate-await-observer",
        eager_start=False,
    )
    # 当前代码已在 asyncio.run 建立的事件循环中，所以 get_running_loop 能取得正在运行的 loop。
    # Future 可理解为“将来结果的占位对象”；泛型参数 str 是对其最终结果类型的静态说明。
    loop: AbstractEventLoop = asyncio.get_running_loop()
    completed: asyncio.Future[str] = loop.create_future()
    # set_result 立即把 Future 标记为已完成，并保存字符串结果；这里没有真实 I/O 要等待。
    completed.set_result("缓存命中")

    record(events, "await 已完成 Future: 之前")
    # completed 已经就绪，await 可同步取出其结果，因此当前 Task 无需暂停，旁路 Task 还不能插进来。
    result: str = await completed
    record(events, "await 已完成 Future: 之后")

    # Future 已经有结果，await 直接取得它；当前代码没有为旁路 Task 让出运行机会。
    assert events == ["await 已完成 Future: 之前", "await 已完成 Future: 之后"]
    # peer_task 尚未完成，await 它时当前协程需要等待；事件循环于是推进旁路协程，直到其结束。
    await peer_task

    # `events[-1]` 用负索引读取列表最后一个元素，验证旁路事件是在前两条事件之后才出现。
    print("  已完成 Future 的结果:", result)
    print("  旁路 Task 只在后续真正暂停时才运行")
    assert events[-1] == "旁路 Task: 得到运行机会"


# 下面两个函数是确定性的本地替身。它们保留“发起工作 -> 等待 -> 结果就绪”的形状，
# 只用于讲解控制流，不代表真实 LLM 或数据库客户端的网络、超时、重试等行为。
async def simulated_llm(prompt: str, events: list[str]) -> str:
    """可控替身：只模拟一次能协作暂停的等待，不模拟真实 LLM 协议。"""
    # f-string 中的 `!r` 使用 repr(prompt)，所以输出会带引号，便于看清字符串边界。
    record(events, f"LLM: 开始处理 {prompt!r}")
    await asyncio.sleep(0)
    record(events, "LLM: 响应就绪")
    return "需要查询订单 42"


async def simulated_database(query: str, events: list[str]) -> list[str]:
    """可控替身：不建立连接，只展示数据库步骤何时才开始。"""
    record(events, f"数据库: 开始执行 {query!r}")
    await asyncio.sleep(0)
    record(events, "数据库: 结果就绪")
    # 返回单元素 list，模拟数据库的多行结果；调用方稍后用 rows[0] 读取第一条记录。
    return ["订单 42: 已发货"]


# 这个 Agent 流程有真实的数据依赖：必须先拿到模型判断，才进入后续数据库步骤。
# 连续写两个 await 只允许同一调用链在等待时让出事件循环，并不会让两个操作并发启动。
async def handle_agent_request(events: list[str]) -> str:
    record(events, "Agent: 收到请求")
    # 右侧 coroutine 完成前不会执行下一条语句；intent 接收 simulated_llm 的最终字符串结果。
    intent: str = await simulated_llm("订单到哪里了?", events)
    record(events, f"Agent: 拿到模型结果 {intent!r}")

    # 只有第一段 await 完成后，Python 才调用 simulated_database 创建并推进第二个 coroutine。
    rows: list[str] = await simulated_database("select order 42", events)
    # len(rows) 读取结果条数；rows[0] 则取第一条记录作为整个 Agent 协程的最终返回值。
    record(events, f"Agent: 拿到 {len(rows)} 条数据库记录")
    return rows[0]


# 第四组实验不用计时，而是比较事件在列表中的位置，证明两个替身严格顺序执行。
async def demonstrate_sequential_agent_io() -> None:
    print_section("4. Agent 场景: 连续 await 仍是顺序执行")
    events: list[str] = []
    answer: str = await handle_agent_request(events)

    # list.index 返回完全匹配元素第一次出现的下标。
    llm_finished: int = events.index("LLM: 响应就绪")
    # 生成器表达式逐个产生符合条件的下标；next 只取第一个。
    # enumerate(events) 每轮给出 `(下标, 消息)` 二元组，再解包给 index 和 message。
    database_started: int = next(
        index for index, message in enumerate(events) if message.startswith("数据库: 开始执行")
    )
    print("  最终回答:", answer)
    print("  可观察结论: 数据库开始位置晚于 LLM 完成位置")
    assert llm_finished < database_started
    assert answer == "订单 42: 已发货"


# heartbeat 是并发观察器：每次事件循环调度到它，就把共享计数加一并发出一次 Event 信号。
async def heartbeat(
    stop: asyncio.Event,
    next_pulse: asyncio.Event,
    pulse_count: list[int],
    events: list[str],
) -> None:
    """最小观察器：每次得到事件循环执行机会就发出一个心跳。"""
    # try/finally 保证无论正常退出还是外部取消，都能记录“已停止”；对应 Java 的 finally 清理语义。
    try:
        # Event 是电平式标志，不是自动累加消息的队列。只要 stop 尚未被 set，就继续产生心跳。
        while not stop.is_set():
            # int 本身不可变，因此用单元素 list 作为共享可变容器；原地改索引后，调用方能看到新计数。
            pulse_count[0] += 1
            record(events, f"heartbeat: 第 {pulse_count[0]} 次")
            # set 令所有等待 next_pulse 的协程具备恢复条件；它不会在这里阻塞 heartbeat。
            next_pulse.set()
            # 主动让出一次执行权，否则这个无 I/O 的 while 循环会一直占住事件循环线程。
            await asyncio.sleep(0)
    finally:
        record(events, "heartbeat: 已停止")


# 当前协程通过等待“下一次心跳”来证明：自己暂停期间，旁路 Task 的确向前推进了。
async def cooperative_wait(next_pulse: asyncio.Event, events: list[str]) -> None:
    """等待明确的下一次心跳，证明当前协程确实把执行权交回了事件循环。"""
    record(events, "协作等待: 开始")
    # 先 clear 丢弃旧的已触发状态，确保下面 wait 等到的是本次实验期间产生的新心跳。
    next_pulse.clear()
    # 标志尚未 set 时，wait 会暂停当前 Task；heartbeat 随后获得运行机会并调用 set。
    await next_pulse.wait()
    record(events, "协作等待: 看到等待期间的新心跳后恢复")


# 反例虽然写成 async def，函数体中的普通同步调用仍按同步方式执行。
async def blocking_wait(events: list[str]) -> None:
    """教学反例：async def 不会改变 time.sleep 的阻塞性质。"""
    record(events, "阻塞等待: 开始")
    # 时长只用于让现象便于人眼感受；断言只检查事件顺序，不比较耗时。
    # time.sleep 不返回 awaitable，也不会把控制权还给事件循环；同一线程上的 heartbeat 因此无法运行。
    time.sleep(0.05)  # noqa: ASYNC251 - 本函数专门演示应被检查器阻止的写法。
    record(events, "阻塞等待: 结束")


# 第五组实验用共享计数和 Event 构造确定性证据，避免用脆弱的耗时大小猜测是否发生调度。
async def demonstrate_blocking_event_loop() -> None:
    print_section("5. heartbeat: 协作等待与阻塞事件循环")
    events: list[str] = []
    # 用 list 包装计数，heartbeat 对 pulse_count[0] 的原地修改可被本协程直接观察。
    pulse_count: list[int] = [0]
    # 两个 Event 分工不同：stop 通知观察器退出，next_pulse 通知当前协程“新心跳已发生”。
    stop = asyncio.Event()
    next_pulse = asyncio.Event()
    # 保存 Task 引用，才能在 finally 中负责它的完整退出；后台工作不应被创建后就遗忘。
    heartbeat_task: Task[None] = asyncio.create_task(
        heartbeat(stop, next_pulse, pulse_count, events),
        name="heartbeat-observer",
        eager_start=False,
    )

    try:
        # 先等到观察器确实启动，后面的结论不依赖谁先被调度。
        await next_pulse.wait()

        # 记录协作等待前后的快照。cooperative_wait 只有看到一次新 pulse 才返回，所以计数必须增加。
        before_cooperative: int = pulse_count[0]
        await cooperative_wait(next_pulse, events)
        after_cooperative: int = pulse_count[0]

        # 清除上一步留下的触发状态，再记录同步阻塞前后的快照。
        # blocking_wait 内没有真正的异步暂停点，因此其执行期间计数必须保持不变。
        next_pulse.clear()
        before_blocking: int = pulse_count[0]
        await blocking_wait(events)
        after_blocking: int = pulse_count[0]

        # 差值只关心“是否推进”，不要求事件循环恰好产生几次心跳，也不依赖机器速度。
        print("  协作等待期间新增心跳:", after_cooperative - before_cooperative)
        print("  阻塞等待期间新增心跳:", after_blocking - before_blocking)
        assert after_cooperative > before_cooperative
        assert after_blocking == before_blocking
        assert not next_pulse.is_set()
    finally:
        # 不遗留后台 Task；即使断言失败，也通知观察器退出并等待清理完成。
        # set 只发退出信号，await heartbeat_task 才真正等到观察器运行 finally 并结束。
        stop.set()
        await heartbeat_task


# 第六组实验会先正常完成这个协程，再故意重复等待同一个对象。
async def returns_once(events: list[str]) -> str:
    record(events, "一次性协程: 函数体执行")
    return "完成"


# 该函数只作为嵌套 asyncio.run 的参数；若外层事件循环正确拒绝嵌套，它的函数体就不会执行。
async def nested_run_target() -> None:
    print("这行不应执行")


# 最后一组把三个常见错误放进受控的 try/except/finally 中，既观察异常，也清理故意创建的对象。
async def demonstrate_controlled_errors() -> None:
    print_section("6. 一次性对象、忘记 await 与嵌套 asyncio.run")

    reuse_events: list[str] = []
    # one_object 代表 returns_once 的这一次具体调用；第一次 await 会把它一直推进到 return。
    one_object: CoroutineType[Any, Any, str] = returns_once(reuse_events)
    first_result: str = await one_object
    print("  第一次 await 得到:", first_result)
    try:
        # 原生 coroutine 对象保存的是一次性执行状态，完成后已关闭；第二次 await 必须失败。
        await one_object
    except RuntimeError as error:
        # `as error` 把捕获到的异常对象绑定给局部变量；type(error).__name__ 读取异常类名。
        print("  第二次 await 同一对象:", type(error).__name__, str(error))
    else:
        # else 只在 try 没抛异常时执行；走到这里反而说明预期规则没有成立，因此主动令实验失败。
        raise AssertionError("已完成的原生协程对象不应允许第二次 await")
    assert reuse_events == ["一次性协程: 函数体执行"]

    forgotten_events: list[str] = []
    # 这里只调用而不 await：forgotten 被创建，但 returns_once 的函数体事件仍然是 0 条。
    forgotten: CoroutineType[Any, Any, str] = returns_once(forgotten_events)
    print("  只调用不 await 后，函数体事件数:", len(forgotten_events))
    print("  未启动对象状态:", inspect.getcoroutinestate(forgotten))
    # 教学中故意创建了错误对象，所以显式 close，避免正常入口留下 RuntimeWarning。
    # close 表示放弃这次调用，使对象直接进入 CORO_CLOSED；正常业务应 await 或受控调度它，
    # 而不是靠 close 补做原本期望发生的工作。
    forgotten.close()
    print("  显式 close 后状态:", inspect.getcoroutinestate(forgotten))
    assert forgotten_events == []
    assert inspect.getcoroutinestate(forgotten) == inspect.CORO_CLOSED

    # 先创建对象，是为了把它传给 asyncio.run；但当前函数本身已运行在外层事件循环中。
    nested: CoroutineType[Any, Any, None] = nested_run_target()
    try:
        # asyncio.run 负责创建并独占一个顶层事件循环，同一线程已有运行中循环时禁止再次调用。
        asyncio.run(nested)
    except RuntimeError as error:
        print("  运行中事件循环内再调用 asyncio.run:", type(error).__name__, str(error))
    else:
        raise AssertionError("同一线程已有事件循环时，asyncio.run 应拒绝嵌套调用")
    finally:
        # asyncio.run 在接管对象前就失败；调用方仍负责关闭刚创建的协程对象。
        nested.close()


# run_examples 是全章唯一的异步总入口。六个 await 按源码顺序逐组完成，并不让实验彼此并发。
async def run_examples() -> None:
    await demonstrate_coroutine_lifecycle()
    await demonstrate_await_chain()
    await demonstrate_immediate_await()
    await demonstrate_sequential_agent_io()
    await demonstrate_blocking_event_loop()
    await demonstrate_controlled_errors()
    print("\n所有第 19 章示例与运行时断言均已完成。")


# 普通脚本入口不能直接写顶层 await，因此用一次 asyncio.run 建立事件循环并推进顶层 coroutine。
def main() -> None:
    """同步脚本入口只调用一次 asyncio.run，由它创建并关闭事件循环。"""
    asyncio.run(run_examples())


# 导入本文件时，Python 只定义上面的函数，不自动运行实验；直接执行本文件时 __name__ 才是
# "__main__"，于是调用同步 main。实际顺序是 main -> asyncio.run -> run_examples -> 六组实验。
if __name__ == "__main__":
    main()
