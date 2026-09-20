"""第 27 章：离线观察 pytest 将要验证的异步行为。

从仓库根目录运行：
    .venv/bin/python -B java_to_python/chapters/27_pytest_async/example.py

这个入口拥有自己的事件循环，所以只在最外层调用一次 asyncio.run()；pytest 异步
测试的事件循环则由 pytest-asyncio 拥有，测试函数内部不会再次调用 asyncio.run()。
"""

# 阅读顺序：先看同步参数校验，再依次观察取消、TaskGroup、并发上限、流与
# ContextVar、I/O 边界和受控缺陷。这个文件负责把现象打印出来；真正可重复的
# 断言位于 tests/。外部边界都被限制在进程内或临时资源中，因此运行示例不会访问公网。
#
# asyncio 提供事件循环、Task 和 Event/Barrier 等同步原语；tempfile 与 Path
# 用于创建会自动删除的临时数据库目录。dataclass/field 则用少量代码声明后面的
# 有状态 Fake，作用类似 Java 测试里的小型 POJO，但 Python 不会据此做静态类型强制。
import asyncio
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# httpx 在第 6 段构造进程内 HTTP 客户端。chapter27.agent_runtime 是本章真正的
# 被测业务模块：入口只准备输入、控制执行阶段并展示它的公开结果。
import httpx
from chapter27.agent_runtime import (
    SearchArgs,
    StreamState,
    ToolBusinessError,
    ToolLifecycle,
    ToolProgramError,
    buggy_read_first_token,
    fetch_model_reply,
    flatten_exception_group,
    initialize_notes,
    observe_request_context,
    read_first_token,
    read_note,
    request_id_var,
    run_optional_tools,
    run_required_tools,
    run_with_limit,
    wait_for_tool,
)

# Pydantic 在数据不满足 SearchArgs 约束时抛出这个异常；它包含结构化错误列表，
# 不必依赖整段、可能随版本调整的错误文本来判断哪个字段失败。
from pydantic import ValidationError


# title: str 与 -> None 是类型提示，分别表示调用方应传字符串、函数不返回业务值；
# 它们主要服务阅读和静态检查，不像 Java 方法签名那样由解释器强制执行。
def print_section(title: str) -> None:
    # f-string 会计算花括号中的表达式再嵌入文本。这里先把 "=" 重复 72 次，
    # 再在同一次 print 中用 \n 排出空行、分隔线、标题和第二条分隔线。
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


# 先用同步例子建立最小测试合同：同一个模型对合法字典返回规范化对象，
# 对不可信的错误字典则抛 ValidationError。这里尚未创建事件循环。
def demonstrate_sync_validation() -> None:
    print_section("1. 同步起点：模型接受好参数、拒绝坏参数")

    # model_validate 在运行时读取这个普通 dict。SearchArgs 的校验器会去掉 query
    # 两端空白，所以 args.query 是 "asyncio"；args 是模型实例，不再是原始字典。
    # model_dump() 又产生适合打印/序列化的新字典，可观察 query 的规范化结果。
    args = SearchArgs.model_validate({"query": "  asyncio  ", "limit": 2})
    print("规范化后的参数:", args.model_dump())

    # try 只包住预期可能失败的调用，避免把准备或打印阶段的同类异常误当成功案例。
    # 这份输入同时包含三个问题：query 只有空白、严格整数 limit 不接受字符串 "2"、
    # 模型配置禁止 unknown 额外字段，因此一次校验可以收集多个字段错误。
    try:
        SearchArgs.model_validate({"query": " ", "limit": "2", "unknown": True})
    except ValidationError as error:
        # error.errors() 返回“每项描述一个校验错误”的字典列表。列表推导式逐项
        # 取出 loc 元组，得到失败位置，而不是把整段错误消息当成不稳定的断言依据。
        locations = [item["loc"] for item in error.errors()]
        print("坏参数的错误位置:", locations)


# async def 调用后先产生 coroutine 对象，只有被 await 或包装为 Task 才真正向前执行。
# 本段展示稳定的取消顺序：确认任务已进入等待点，再请求取消，最后 await 到清理完成。
async def demonstrate_cancellation_cleanup() -> None:
    print_section("2. 取消：先确认已启动，再取消、等待并检查 finally")

    # ToolLifecycle 内的 Event 是阶段信号，events 列表则保存可读的先后记录。
    # create_task 把 coroutine 安排到当前事件循环并立即返回 Task；保留 task 引用意味着
    # 当前函数仍负责等待它的最终结果。name 只用于调试，不影响调度。
    lifecycle = ToolLifecycle()
    task = asyncio.create_task(wait_for_tool(lifecycle), name="demo-cancellable-tool")

    # wait_for_tool 会先记录 "started"、设置 started，再阻塞于 may_finish。
    # 等 Event 比 sleep 若干毫秒更可靠：恢复到这里时，可以确定目标已进入可取消区域。
    await lifecycle.started.wait()
    print("取消前事件:", lifecycle.events)

    # cancel() 只是向 Task 提出取消请求，并把消息附到随后注入的 CancelledError；
    # 它不会像强行终止 Java 线程那样同步杀死代码。真正的取消在 Task 下次运行时传播。
    task.cancel("演示用户停止")
    try:
        # await 不只是“拿结果”，还把调用方与子任务的结束建立先后关系：
        # wait_for_tool 的 finally 已运行完，控制流才会进入下面的 except。
        await task
    except asyncio.CancelledError:
        # CancelledError 代表协作式取消的正常传播，这里只为展示而确认它；
        # 业务代码通常完成本层清理后还应继续向上抛，不能用宽泛 except 吞掉取消。
        print("调用方收到了 CancelledError")

    # is_set() 读取 Event 当前状态；cancelled() 只存在 Task 最终以取消结束后才为 True。
    # 最后的 assert 是脚本内的安全检查，正式 pytest 测试会把这些结论拆得更易诊断。
    print("取消后事件:", lifecycle.events)
    print("清理完成 / Task 已取消:", lifecycle.cleaned.is_set(), task.cancelled())
    assert lifecycle.events == ["started", "cleaned"]


# 这是用来驱动并发路径的可控 Fake，而不是生产工具。
# @dataclass 自动生成初始化方法等样板代码；slots=True 不再为每个实例创建普通 __dict__，
# 字段集合因而固定。它可类比精简的 Java 测试 POJO，但类型提示仍不是运行时类型检查。
@dataclass(slots=True)
class DemoTool:
    # value 是成功时返回的文本；error 为 None 表示成功，否则放行后抛出指定异常。
    # Exception | None 是联合类型提示，可读作“Exception 或空值 None”。
    value: str
    error: Exception | None = None

    # default_factory 会为每个 DemoTool 实例分别调用 asyncio.Event，避免多个实例错误地
    # 共享同一个可变 Event。三个信号依次表示：已启动、允许继续、已完成 finally 清理。
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    cleaned: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False

    # 定义 __call__ 后，实例本身就能像函数一样写成 await tool()；run_*_tools 正是以
    # 这种统一调用接口接收不同 Fake。-> str 描述 await 完成后得到的值，而非 coroutine 本身。
    async def __call__(self) -> str:
        # 先发 started，再等待测试/演示方打开 release 闸门，使外部能精确控制交错顺序。
        self.started.set()
        try:
            await self.release.wait()
            # is not None 检查是否确实配置了异常对象；不能写简单的 if self.error 来猜真值。
            if self.error is not None:
                raise self.error
            return self.value
        except asyncio.CancelledError:
            # 记录“确实观察到取消”，随后裸 raise 原异常。若这里不重新抛出，TaskGroup
            # 可能把已取消的工具误认为正常完成。
            self.cancelled = True
            raise
        finally:
            # 正常返回、配置异常和取消三条路径都会执行 finally，因此 cleaned 表示
            # 生命周期已经收尾；真实实现会在这里释放连接、流或锁。
            self.cleaned.set()


# 两类合同不能混在一起：可选工具允许把已知业务错误保存在结果中；
# 必需工具遇到程序缺陷时，整个结构化并发作用域都必须失败并回收兄弟任务。
async def demonstrate_partial_and_total_failure() -> None:
    print_section("3. 部分失败与整体失败是两种合同")

    # 两个独立实例分别代表成功工具和预期内的业务失败。"unused" 不会成为结果，
    # 因为 expected_failure 在 release 后会先抛 ToolBusinessError。
    ok = DemoTool("文档结果")
    expected_failure = DemoTool("unused", ToolBusinessError("客户记录不存在"))

    # 字典把工具名映射到可异步调用的 DemoTool；create_task 让整组工作在后台推进，
    # 当前函数才能同时等待两个 started，并决定何时放行它们。
    optional_task = asyncio.create_task(
        run_optional_tools({"search": ok, "record": expected_failure})
    )
    await ok.started.wait()
    await expected_failure.started.wait()

    # Event.set() 是同步方法：信号一旦置位，所有当前和以后等待者都可继续，无需 await。
    ok.release.set()
    expected_failure.release.set()

    # run_optional_tools 只把 ToolBusinessError 转成 error 字段；成功项保留 value，
    # 因而 await 返回的是包含两种 outcome 的列表，而不是抛出整个请求错误。
    outcomes = await optional_task
    print("可选工具结果:", outcomes)

    # 第二组中 waiting 永远不会由本函数主动 release；defect 被放行并抛程序错误。
    # 这刻意制造“一个成员失败、另一个兄弟仍在等待”的 TaskGroup 场景。
    waiting = DemoTool("不会返回")
    defect = DemoTool("不会返回", ToolProgramError("响应协议损坏"))
    required_task = asyncio.create_task(
        run_required_tools({"waiting": waiting, "defect": defect})
    )
    await waiting.started.wait()
    await defect.started.wait()
    defect.release.set()
    try:
        # run_required_tools 内的 TaskGroup 看到 defect 失败后，会取消 waiting，并等待两者
        # 的 finally 完成；退出 TaskGroup 时再把未处理错误放入 ExceptionGroup 抛给这里。
        await required_task
    except ExceptionGroup as group:
        # ExceptionGroup 是异常树的外壳。递归展开叶子后才能确认真正原因确实是
        # ToolProgramError，而不是只满足“某个组失败了”这种过宽结论。
        leaves = flatten_exception_group(group)
        print("整体失败的异常叶子:", [type(error).__name__ for error in leaves])

    # 能运行到此处时 required_task 已结束；waiting.cancelled 证明它收到取消，
    # 两个 cleaned 状态则证明 TaskGroup 没在兄弟还占用资源时提前返回。
    print("等待中的兄弟被取消 / 两者都清理:", waiting.cancelled, waiting.cleaned.is_set())
    assert waiting.cancelled and waiting.cleaned.is_set() and defect.cleaned.is_set()


# 这个探针把“并发是否超过 2”变成可直接读取的状态，避免用总耗时反推并发度。
@dataclass(slots=True)
class DemoConcurrencyProbe:
    expected_peak: int
    # active 是当前正在 operation 内的数量，max_active 保存运行期间见过的最大值。
    active: int = 0
    max_active: int = 0

    # reached_peak 通知观察者已经占满预期槽位；release 在观察完成后一次性放行任务。
    reached_peak: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def call(self, item: int) -> int:
        # asyncio 在单线程事件循环中只会于 await 等让出点切换 Task；这三步之间没有
        # await，所以更新计数并检查峰值时不会被另一个 coroutine 插入执行。
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.active == self.expected_peak:
            self.reached_peak.set()
        try:
            # 所有已取得 Semaphore 槽位的调用停在同一个闸门，便于中途读取真实在途数。
            await self.release.wait()
            return item * 10
        finally:
            # 即便等待期间被取消，也归还探针自己的 active 状态；Semaphore 的槽位则由
            # run_with_limit 内的 async with 负责归还。
            self.active -= 1


async def demonstrate_concurrency_limit() -> None:
    print_section("4. 并发：断言可观察的最大在途数，不比较毫秒")
    probe = DemoConcurrencyProbe(expected_peak=2)

    # range(5) 按需提供 0～4；run_with_limit 会为各项安排工作，但 Semaphore 保证同一时刻
    # 最多两个 call 进入受保护区。包装为 Task 后，当前函数可在请求完成前检查中间状态。
    request = asyncio.create_task(run_with_limit(range(5), max_in_flight=2, operation=probe.call))

    # reached_peak 置位说明两个槽位都已被占用。release 尚未打开，所以此刻稳定地看到
    # active/max_active 都是 2，而不是依赖某台机器“应该在几毫秒内跑到这里”。
    await probe.reached_peak.wait()
    print("闸门关闭时 active / max_active:", probe.active, probe.max_active)

    # Event 保持置位；首批任务结束后，后续取得槽位的调用也会直接通过同一闸门。
    probe.release.set()
    results = await request

    # TaskGroup 最终按输入创建顺序读取各 Task 结果，所以列表为 0、10、20、30、40；
    # active 回到 0 则证明所有 call 的 finally 都执行完毕。
    print("结果 / 最终 active:", results, probe.active)
    assert probe.max_active == 2 and probe.active == 0


# 把两个独立概念放在一段展示：资源型异步流由创建者显式关闭；ContextVar 则让
# 同一事件循环中交错执行的请求各自看到自己的上下文值。
async def demonstrate_stream_and_context() -> None:
    print_section("5. 提前退出流与 ContextVar 请求隔离")

    # read_first_token 只消费第一个 "agent" 就 return，但其内部 aclosing 会先 await
    # 异步生成器的 aclose()。因此函数返回到这里时，finally 已设置 closed，而非稍后碰运气回收。
    stream_state = StreamState()
    first: str = await read_first_token(stream_state)
    print("只读取:", first, "; 返回前流已关闭:", stream_state.closed.is_set())

    # Barrier(2) 要求两个参与者都到达 wait 才一起继续，用确定的阶段关系制造交错。
    # gather 接收两个 coroutine，把它们并发调度，并按传入顺序收集返回的二元组；
    # 返回顺序不表示 req-A 一定先运行。
    barrier = asyncio.Barrier(2)
    contexts = await asyncio.gather(
        observe_request_context("req-A", barrier),
        observe_request_context("req-B", barrier),
    )

    # 每个子 Task 在屏障前后都读到自己的 request id；它们的 finally 用 token 恢复上下文。
    # 父 Task 从未设置该 ContextVar，所以 get() 仍得到 agent_runtime 中声明的默认值 "-"。
    print("两个并发请求在屏障前后的值:", contexts)
    print("父上下文恢复为默认值:", request_id_var.get())
    assert contexts == [("req-A", "req-A"), ("req-B", "req-B")]


# 这里比较两种离线 I/O 边界：MockTransport 仍走 HTTPX 的请求/响应处理，
# 临时文件则真实执行 SQLite SQL；两者都不会访问生产服务或长期数据。
async def demonstrate_http_and_database() -> None:
    print_section("6. I/O 分层：MockTransport 与临时 SQLite 文件")

    # MockTransport 会把 AsyncClient 构造好的 Request 直接交给这个同步 handler，
    # 不经过 DNS、socket、TLS 或真实网络。params 中的 query 可从 request.url 读回，
    # 再组装成状态码 200、JSON 对象为响应体的内存 Response。
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"answer": f"离线回答：{request.url.params['query']}"},
        )

    # async with 表示本函数拥有 client：进入时取得资源，离开时无论成功或异常都异步关闭。
    # base_url 只负责把 fetch_model_reply 使用的相对路径 /model 补成完整 URL；
    # transport 已被显式替换，因此这个看似真实的域名不会产生网络连接。
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://model.test",
    ) as client:
        # 原始 dict 先被 SearchArgs 校验，再用于构造 query 参数；handler 的 JSON 响应
        # 随后又被校验成 ModelReply，reply 因而是模型实例，可通过属性访问 answer。
        reply = await fetch_model_reply(client, {"query": "pytest", "limit": 1})
    print("进程内 HTTP transport 返回:", reply.answer)

    # TemporaryDirectory 的 with 作用域拥有整个临时目录：directory 是目录路径字符串，
    # Path(directory) / "notes.sqlite3" 用 / 运算符拼出跨平台路径对象。
    with tempfile.TemporaryDirectory(prefix="chapter27-") as directory:
        database_path: Path = Path(directory) / "notes.sqlite3"
        # initialize_notes 和 read_note 会分别打开真实 SQLite 连接，执行建表、插入和参数化查询；
        # 它比伪造 Connection 更能发现 SQL/绑定错误，但不代表验证了生产数据库驱动。
        initialize_notes(database_path)
        print("临时数据库查询:", read_note(database_path, 2))

    # 离开 with 后 TemporaryDirectory 已递归删除目录，所以路径对象仍存在，文件却不存在。
    # database_path 在 Python 中没有块级作用域，因而这里仍可用它检查清理结果。
    print("临时目录退出后数据库仍存在:", database_path.exists())


# 这段故意调用错误实现，说明一条回归测试怎样观察资源泄漏；示例本身随后负责清理，
# 因此完整运行 example.py 不会把未关闭的异步生成器遗留给事件循环。
async def demonstrate_controlled_defect() -> None:
    print_section("7. 受控缺陷：测试应能看到提前返回时流仍未关闭")
    state = StreamState()

    # buggy_read_first_token 用 anext() 取得第一个值后直接返回二元组：first 是 "agent"，
    # leaked_stream 是仍暂停在 yield 处的异步生成器对象，其 finally 还没有执行。
    first, leaked_stream = await buggy_read_first_token(state)
    try:
        # 此刻 closed 必为 False；正式受控失败测试会断言它应为 True，并准确地变红。
        # 这里只打印预期失败点，让普通演示仍能继续运行到底。
        print("缺陷函数返回:", first, "; 此刻 closed:", state.closed.is_set())
        print("回归断言 state.closed.is_set() 会准确失败")
    finally:
        # 演示完立即清理，入口不会遗留未关闭的异步生成器。
        # aclose() 会让生成器执行其 finally；await 保证清理完成后才离开本函数。
        await leaked_stream.aclose()
    print("受控清理后 closed:", state.closed.is_set())


# 统一异步入口按顺序运行七段演示。每个 await 都等当前段完整结束后再进入下一段，
# 所以输出易于阅读；并发只发生在各演示明确创建 Task/gather 的内部。
async def run_examples() -> None:
    # 普通 def 可直接调用；它会在当前线程同步执行完再返回。
    demonstrate_sync_validation()
    await demonstrate_cancellation_cleanup()
    await demonstrate_partial_and_total_failure()
    await demonstrate_concurrency_limit()
    await demonstrate_stream_and_context()
    await demonstrate_http_and_database()
    await demonstrate_controlled_defect()


# main 是同步世界与异步世界的唯一边界。asyncio.run() 创建事件循环、运行 coroutine，
# 并在结束时关闭异步生成器和循环；已有运行中事件循环的 async 函数里不能再次调用它。
def main() -> None:
    asyncio.run(run_examples())


# Python 导入模块时 __name__ 是模块名，不执行 main；直接运行此文件时它才等于
# "__main__"。这与 Java 的固定 main 方法入口不同，是 Python 常见的可导入/可运行双用写法。
if __name__ == "__main__":
    main()
