"""第 18 章：上下文管理器与资源生命周期。

示例使用可控的 FakeToolSession 记录获取、使用和清理事件，不访问网络，
也不要求 API Key。每一节都用断言校验关键生命周期，而不依赖垃圾回收时机。
"""

# 建议按“基础模型与协议实现 -> 辅助管理器/生成器 -> 六组 demonstrate_* 实验 -> main”
# 的顺序阅读。所有实验把生命周期事件写入 list：代码一边打印过程，一边用同一份事件数据
# 做断言，由此验证正常返回、异常和提前停止等路径是否都经过了确定的清理边界。
#
# collections.abc 中的类型用于描述生成器和可迭代参数；contextlib 提供把生成器包装为
# 上下文管理器、动态管理多个资源以及替只有 close() 的对象补上 with 协议的工具。
# Enum/auto 用来建立可读的会话状态机，TracebackType 则用于精确标注 __exit__ 的第三个参数。
from collections.abc import Generator, Iterable
from contextlib import ExitStack, closing, contextmanager
from enum import Enum, auto
from types import TracebackType


# Enum 的成员是三个唯一的单例对象，适合表达互斥状态。auto() 让 Python 自动生成成员值；
# 本章只关心 NEW/OPEN/CLOSED 这些名字，不依赖底层值是几。
class SessionState(Enum):
    """会话只保留本章观察生命周期所需的三个状态。"""

    NEW = auto()
    OPEN = auto()
    CLOSED = auto()


# 三种异常分别标记“进入失败”“使用失败”和“生命周期结束后误用”。
# 像 Java 中定义专用 RuntimeException 子类一样，即使类体无需额外方法，独立类型也能让
# except 只捕获它真正准备处理的错误；这里继承 RuntimeError，所以都属于运行时异常。
class SessionInitializationError(RuntimeError):
    """模拟握手或鉴权阶段失败。"""


class ToolCallError(RuntimeError):
    """模拟 with 块内的业务调用失败。"""


class SessionClosedError(RuntimeError):
    """关闭后继续使用会话时给出明确错误。"""


def print_section(title: str) -> None:
    # f-string 会先计算花括号内的 title，再拼进输出；开头的 \n 是换行转义，
    # 让每个实验从空行开始。
    print(f"\n=== {title} ===")


def record(events: list[str], message: str) -> None:
    """事件既打印又保留，便于肉眼观察和运行时断言。"""
    # list.append 会原地修改调用方传入的同一个列表，不会创建副本。因此调用方稍后能从
    # events 中检查事件顺序；str 类型提示只供读者和静态检查器使用，不负责运行时校验。
    events.append(message)
    print(f"  事件 | {message}")


# ToolHandle 代表“可使用但不拥有”的句柄。管理器会在 __enter__ 成功后构造它，with 块和
# 借用者只能通过它查询/使用会话；真正的关闭入口仍留在 FakeToolSession 上。
class ToolHandle:
    """__enter__ 交给 with 块的借用句柄，本身不拥有关闭职责。"""

    def __init__(self, owner: FakeToolSession) -> None:
        # Python 3.14 会延迟求值注解，所以此处可以引用稍后才定义的 FakeToolSession。
        # 前导下划线表示内部属性的命名约定，不等于 Java private，运行时仍可访问。
        self._owner: FakeToolSession = owner

    # @property 把无参数方法暴露成只读属性：调用方写 handle.is_open，而不是 is_open()。
    # 它在每次访问时重新查询拥有者，没有复制状态，因此能看见 owner 后续的关闭动作。
    @property
    def is_open(self) -> bool:
        # Enum 成员是单例；is 比较对象身份，正适合判断当前是否就是 OPEN 成员。
        # 不要把 is 泛化为 Java 的 equals：普通值比较通常应使用 ==。
        return self._owner.state is SessionState.OPEN

    def use(self, command: str) -> str:
        # 句柄只转发操作，由 owner 统一执行状态检查、记录事件和生成结果。
        return self._owner.use(command)


# 这个 Fake 不是在模拟真实网络细节，而是把“分配传输层 -> 握手 -> 使用 -> 释放”的状态
# 变成可以断言的数据。它既是资源所有者，也是实现 __enter__/__exit__ 协议的管理器。
class FakeToolSession:
    """可观察的同步工具会话替身。

    open() 分两步模拟传输层分配与握手。第二步失败时，open() 自己回收已经
    分配的传输层，因为 __enter__ 一旦抛异常，本次 with 不会调用 __exit__。
    """

    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        fail_on_open: bool = False,
        suppress_block_error: bool = False,
    ) -> None:
        # 参数列表中的 * 表示其后的两个开关只能按关键字传入，例如 fail_on_open=True，
        # 可避免多个 bool 位置实参难以辨认。None 表示构造器只初始化实例，不返回业务值。
        self.name: str = name
        # 这里保留的是调用方列表的引用；所有 record(...) 都会写进同一个事件时间线。
        self.events: list[str] = events
        # 两个开关只用于可控地触发进入失败和异常误吞，正常会话都保持 False。
        self.fail_on_open: bool = fail_on_open
        self.suppress_block_error: bool = suppress_block_error
        # state 表示逻辑生命周期，transport_allocated 表示更底层的资源是否已分配。
        # 两者分开，才能观察“底层已分配但握手还未完成”的部分初始化状态。
        self.state: Literal[SessionState.NEW] = SessionState.NEW
        self.transport_allocated = False
        # 这两个计数器区分协议退出次数与底层资源实际释放次数，防止重复清理被掩盖。
        self.exit_calls = 0
        self.cleanup_count = 0

    def _release_transport(self) -> None:
        # 只有确实分配过传输层才执行释放和计数；重复调用不会把 cleanup_count 再加一。
        # 这种幂等性让失败回滚和常规 close 即使相遇，也不会产生第二次释放。
        if self.transport_allocated:
            self.transport_allocated = False
            # += 1 会把计算后的整数重新绑定给属性；int 不像 list 那样原地共享修改。
            self.cleanup_count += 1
            record(self.events, f"{self.name}: transport released")

    def open(self) -> ToolHandle:
        # 一个实例只能从 NEW 进入；is not 判断枚举身份，能同时拒绝重复打开和关闭后重开。
        if self.state is not SessionState.NEW:
            raise RuntimeError(f"{self.name}: 只能从 NEW 状态打开")

        # 先记录开始，再模拟已经拿到底层传输层。此刻尚未握手成功，所以不能提前设为 OPEN。
        record(self.events, f"{self.name}: open started")
        self.transport_allocated = True
        record(self.events, f"{self.name}: transport allocated")

        # try 覆盖握手及句柄构造：其中任一步失败，都要撤销上面已经完成的部分初始化。
        try:
            if self.fail_on_open:
                record(self.events, f"{self.name}: handshake failed")
                raise SessionInitializationError(f"{self.name}: 初始化失败")
            # 只有握手通过后才发布 OPEN 状态，并返回一个借用句柄给 with 的 as 目标。
            # 返回 ToolHandle(self) 也证明管理器对象和块内拿到的值不必是同一个对象。
            self.state: Literal[SessionState.OPEN] = SessionState.OPEN
            record(self.events, f"{self.name}: open completed")
            return ToolHandle(self)
        except BaseException:
            # __enter__ 尚未成功，with 不会替我们调用 __exit__；部分初始化必须自行清理。
            # 捕获 BaseException 是为了连 KeyboardInterrupt 等退出信号也先完成资源回滚；
            # 末尾的裸 raise 会原样重抛当前异常，并保留原 traceback，不会把失败转成成功。
            record(self.events, f"{self.name}: open rolls back partial state")
            self._release_transport()
            self.state: Literal[SessionState.CLOSED] = SessionState.CLOSED
            raise

    def use(self, command: str) -> str:
        # 每次使用都先检查生命周期，使关闭后误用立即暴露，而不是悄悄访问失效资源。
        if self.state is not SessionState.OPEN:
            raise SessionClosedError(f"{self.name}: 会话未打开或已关闭")

        record(self.events, f"{self.name}: use {command}")
        # 特殊命令只为稳定触发 with 块内部的业务异常，供后面的异常路径实验观察。
        if command == "business-error":
            raise ToolCallError(f"{self.name}: 工具调用失败")
        # 普通命令返回字符串；资源状态保持 OPEN，可以在同一生命周期中继续复用。
        return f"{self.name} -> {command} 完成"

    def close(self) -> None:
        """清理设计为幂等，避免重复关闭造成第二个故障。"""
        # 已关闭时只记录并返回。没有显式返回值的 Python 函数会返回 None。
        if self.state is SessionState.CLOSED:
            record(self.events, f"{self.name}: close skipped (already closed)")
            return

        # 先记录高层 close，再释放底层传输层，最后把逻辑状态推进到 CLOSED。
        record(self.events, f"{self.name}: close")
        self._release_transport()
        self.state: Literal[SessionState.CLOSED] = SessionState.CLOSED

    # with manager 会先调用 __enter__；这里复用 open 的状态检查和失败回滚，并把它返回的
    # ToolHandle 继续交给 as 目标。Java try-with-resources 通常暴露资源本身，Python 则允许
    # __enter__ 返回另一个受限视图。
    def __enter__(self) -> ToolHandle:
        return self.open()

    # __enter__ 成功后，无论块体正常结束、return 还是抛异常，with 都会调用一次 __exit__。
    # `T | None` 表示参数可以是 T 或 None：正常离开时三个参数全是 None，异常离开时分别是
    # 异常类、异常实例和 traceback。类型提示不改变运行时协议。
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        # 先把调用次数加一，再把异常对象转换为稳定、可读的事件文本。
        self.exit_calls += 1
        # `A if 条件 else B` 是条件表达式；这里避免对 None 读取 __name__ 或调用 str 之外
        # 的属性。异常类本身提供 __name__，异常实例则由 str(...) 给出消息。
        exception_name: str = "None" if exc_type is None else exc_type.__name__
        exception_text: str = "None" if exc_value is None else str(exc_value)
        has_traceback: bool = traceback is not None
        # 相邻的两个 f-string 会在语法层面自动拼接为一个字符串，用于控制源码行宽；
        # record 最终收到的仍是一条完整事件。
        record(
            self.events,
            f"{self.name}: __exit__(type={exception_name}, "
            f"value={exception_text}, traceback={has_traceback})",
        )
        # 无论块内是否失败都先关闭资源，然后才通过返回值决定原异常是否继续传播。
        self.close()

        # 正常资源管理器通常返回 False（或 None），让块内异常继续传播。
        # 这个开关只为受控演示“真值会抑制异常”，不应成为会话的默认行为。
        return self.suppress_block_error


def return_from_with(manager: FakeToolSession) -> str:
    """return 先记录待返回值，但调用方要等 __exit__ 完成后才能收到它。"""
    # return 出现在 with 块内并不会跳过协议退出。Python 先准备好“返回成功”，调用
    # manager.__exit__(None, None, None) 完成清理后，调用方才能真正拿到该字符串。
    with manager as handle:
        handle.use("prepare-return")
        record(manager.events, "函数：执行 return")
        return "返回成功"

    # 只有 return 之前出现异常且 __exit__ 错误地将它抑制，才可能落到这里。
    raise AssertionError("return 前的异常被上下文管理器意外抑制")


def use_with_try_finally(events: list[str], *, fail: bool) -> FakeToolSession:
    """先展示不依赖协议的获取/使用/清理骨架。"""
    # 资源在进入 try 之前已成功取得；若 open 自己失败，它会自行回滚，也不会执行这里的
    # finally。成功后 manager 是所有者，handle 只是供 try 块使用的借用句柄。
    manager = FakeToolSession("manual", events)
    handle: ToolHandle = manager.open()
    try:
        handle.use("manual-call")
        # fail=True 用来证明异常离开 try 时仍会先进入 finally；fail=False 则会准备返回
        # manager，但同样要等 finally 的 close 完成后才真正返回。
        if fail:
            raise ToolCallError("manual: try 块失败")
        return manager
    finally:
        # finally 不负责决定是否吞掉异常，只保证离开使用区间时清理。
        manager.close()


# 装饰器在函数定义完成时执行，效果近似
# managed_tool_session = contextmanager(managed_tool_session)。装饰后的调用返回上下文管理器，
# 不是直接返回 ToolHandle；Generator[ToolHandle] 描述这段生成器会 yield 的值类型。
@contextmanager
def managed_tool_session(name: str, events: list[str]) -> Generator[ToolHandle]:
    """用恰好一次 yield 表达同一个获取/使用/清理骨架。"""
    # with 进入装饰器生成的管理器时，函数体才开始运行：先创建并打开真实管理对象。
    manager = FakeToolSession(name, events)
    handle: ToolHandle = manager.open()
    record(events, f"{name}: contextmanager yield 前")
    try:
        # yield 暂停当前函数，并把 handle 交给 with 的 as 目标；暂停期间执行的是 with 块。
        yield handle
    except BaseException as error:
        # with 块内的异常会被注入到暂停的 yield 位置；这里只记录，所以必须重抛。
        # type(error).__name__ 先取异常实例的类，再取类名；裸 raise 保留原异常和 traceback。
        record(events, f"{name}: yield 收到 {type(error).__name__}")
        raise
    finally:
        # 正常退出和异常退出都会执行 finally，这一段对应类管理器的 __exit__ 清理责任。
        record(events, f"{name}: contextmanager finally")
        manager.close()


# 这个装饰器用法与上面相同，但函数体故意漏掉重抛，用来展示一个容易被 Java 经验忽略的
# 差异：块内异常会回到 yield 处，若生成器随后正常结束，contextmanager 会把它视为已处理。
@contextmanager
def swallowing_tool_session(name: str, events: list[str]) -> Generator[ToolHandle]:
    """教学反例：记录异常后忘记 raise，会把故障伪装成成功。"""
    manager = FakeToolSession(name, events)
    handle: ToolHandle = manager.open()
    try:
        yield handle
    except ToolCallError as error:
        # as error 把当前 ToolCallError 实例绑定为局部变量，f-string 会读取它的错误消息。
        record(events, f"{name}: 只记录但误吞 {error}")
        # 教学反例：这里缺少 raise。生成器正常结束后，异常会被抑制。
    finally:
        manager.close()


# @contextmanager 要求每次生命周期恰好到达一次 yield。下面把 yield 放进永假的分支，
# Python 仍把该函数编译为生成器函数，但 with 进入时它会直接耗尽，因此触发 RuntimeError。
@contextmanager
def context_without_yield() -> Generator[None]:
    """教学反例：保留生成器形态，但进入阶段没有实际 yield。"""
    if False:
        yield


# 第一次 yield 可以正常完成进入并把 "first" 绑定给 as；退出阶段生成器恢复后若又 yield，
# contextmanager 发现生成器没有按约定结束，也会抛 RuntimeError。
@contextmanager
def context_with_two_yields(events: list[str]) -> Generator[str]:
    """教学反例：with 块结束后又 yield，违反恰好一次的约定。"""
    yield "first"
    record(events, "two-yields: 准备错误地执行第二次 yield")
    yield "second"


def borrower_run_tool(handle: ToolHandle, command: str, events: list[str]) -> str:
    """借用者只使用传入句柄，不关闭不属于自己的会话。"""
    # 该函数没有创建 handle，也没有接收所有权，因此只负责一次 use 并返回结果。
    # 这类似方法临时接收由外层 try-with-resources 管理的 Java 资源：能调用不等于应关闭。
    record(events, f"borrower: 开始 {command}")
    result: str = handle.use(command)
    record(events, f"borrower: 完成 {command}，不关闭共享会话")
    return result


def stream_tool_results(
    manager: FakeToolSession,
    commands: Iterable[str],
    events: list[str],
) -> Generator[str]:
    """资源型生成器：首次消费时打开，结束或 close() 时退出内部 with。"""
    # 调用生成器函数只创建 generator 对象，不立刻执行这里。第一次 next()（通常由 for
    # 隐式调用）才记录开始并进入 manager，因此创建流之后会话仍保持 NEW。
    record(events, "stream: 生成器函数体开始")
    with manager as handle:
        # Iterable[str] 允许 tuple、list 或其他逐项产生字符串的对象。每轮 command 绑定
        # 下一条命令，use 的返回字符串经 yield 交给消费者，然后生成器暂停在这一行。
        for command in commands:
            record(events, f"stream: 准备产出 {command}")
            yield handle.use(command)
    # 只有 commands 自然耗尽时才会运行到这里；若外部调用 close()，内部 with 的清理仍会
    # 执行，但 GeneratorExit 会让控制流离开生成器，不会记录“自然耗尽”。
    record(events, "stream: 自然耗尽")


def demonstrate_with_protocol() -> None:
    # 第一组实验先走最普通的成功路径，观察管理器、as 绑定值和协议退出。
    print_section("1. with 协议、as 绑定值与 return")
    # `list[str]` 是类型提示；空列表仍是普通可变对象。manager 保存对它的引用，后续所有
    # open/use/close 事件都会按发生顺序追加进来。
    events: list[str] = []
    manager = FakeToolSession("normal", events)

    # 执行顺序是 manager.__enter__() -> 把返回的 ToolHandle 绑定给 handle -> 块体 ->
    # manager.__exit__(None, None, None)。只有 __enter__ 成功，退出责任才正式建立。
    with manager as handle:
        # `is not` 比较对象身份，结果为 True，证明 as 绑定的是借用句柄而非 manager。
        print("  as 绑定 ToolHandle，而不是管理器本身:", handle is not manager)
        print("  调用结果:", handle.use("search"))

    # 正常退出的最后三条事件依次是 __exit__、close、transport released，所以 events[-3]
    # 用负索引从列表末尾倒数第三项取出退出事件。assert 失败才抛 AssertionError；通过时无输出。
    print("  正常退出后三个异常参数均为空:", "type=None" in events[-3])
    print("  最终状态:", manager.state.name)
    assert manager.state is SessionState.CLOSED
    assert manager.exit_calls == 1

    # 第二条成功路径把 return 放进另一个函数的 with 块。分别记录“开始返回”“close”和
    # “调用方收到结果”，再比较它们在列表中的位置，验证清理先于结果交付。
    return_events: list[str] = []
    return_manager = FakeToolSession("return", return_events)
    result: str = return_from_with(return_manager)
    record(return_events, f"调用方：收到 {result}")
    # list.index 返回指定事件第一次出现的零基下标；close 下标更小就表示它发生得更早。
    close_index: int = return_events.index("return: close")
    receive_index: int = return_events.index("调用方：收到 返回成功")
    print("  return 的清理先于调用方收到结果:", close_index < receive_index)
    assert close_index < receive_index


def demonstrate_exception_paths() -> None:
    print_section("2. 块内异常、初始化失败与误吞异常")
    # 场景一：__enter__ 已成功，错误来自 with 块。__exit__ 应先收到异常三元组并关闭，
    # 因为默认返回 False，原 ToolCallError 随后继续传播到外层 except。
    block_events: list[str] = []
    block_manager = FakeToolSession("block-error", block_events)
    try:
        with block_manager as handle:
            handle.use("business-error")
    except ToolCallError as error:
        print("  外层仍收到块内异常:", error)

    # 生成器表达式逐条筛选包含 "__exit__(" 的事件，next(...) 只取第一个匹配项。
    # 本场景确定存在该事件；若不存在，next 会抛 StopIteration，也会让实验失败。
    exit_event: str = next(event for event in block_events if "__exit__(" in event)
    print("  __exit__ 收到异常三元组:", exit_event)
    print("  异常路径仍已关闭:", block_manager.state is SessionState.CLOSED)
    assert "type=ToolCallError" in exit_event
    assert "traceback=True" in exit_event
    assert block_manager.state is SessionState.CLOSED

    # 场景二：让 open 在分配 transport 后、返回句柄前失败。body_started 是哨兵变量：
    # 只有 with 块真的执行过才会变成 True，据此证明 __enter__ 失败会阻止块体开始。
    init_events: list[str] = []
    init_manager = FakeToolSession("init-error", init_events, fail_on_open=True)
    body_started = False
    try:
        with init_manager:
            body_started = True
    except SessionInitializationError as error:
        print("  捕获初始化错误:", error)

    # 此时协议尚未建立退出责任，所以 exit_calls 必须为 0；cleanup_count 为 1 则证明
    # open 自己回滚了已经分配的 transport。两条规则必须同时成立才没有资源泄漏。
    print("  __enter__ 失败后块未运行:", not body_started)
    print("  with 没有调用 __exit__:", init_manager.exit_calls == 0)
    print("  open 自行回收部分资源:", init_manager.cleanup_count == 1)
    assert not body_started
    assert init_manager.exit_calls == 0
    assert init_manager.cleanup_count == 1
    assert init_manager.state is SessionState.CLOSED

    # 场景三是故意保留的反例：管理器会清理资源，但 __exit__ 返回 True，使块内异常被吞掉。
    # 若异常正常传播，赋值 reached_after_with=True 根本不会执行；它最终为 True 就是故障证据。
    suppression_events: list[str] = []
    bad_manager = FakeToolSession(
        "suppress-error",
        suppression_events,
        suppress_block_error=True,
    )
    reached_after_with = False
    with bad_manager as handle:
        handle.use("business-error")
    reached_after_with = True
    print("  教学反例：返回真值后故障被隐藏:", reached_after_with)
    assert reached_after_with
    assert bad_manager.state is SessionState.CLOSED


def demonstrate_finally_and_contextmanager() -> None:
    print_section("3. 从 try/finally 到 @contextmanager")
    # 先用最基础的 try/finally 骨架。fail=True 让 try 抛错，finally 仍先关闭资源；
    # finally 没有 return 或新异常，所以原 ToolCallError 保持不变地交给外层。
    manual_events: list[str] = []
    try:
        use_with_try_finally(manual_events, fail=True)
    except ToolCallError as error:
        print("  try/finally 保留原异常:", error)
    print("  try/finally 已执行 close:", "manual: close" in manual_events)
    assert "manual: close" in manual_events

    # 再验证生成器管理器的对应路径：块内错误会在 yield 暂停点重新出现，except 记录它，
    # 裸 raise 重抛，finally 关闭，最后同一个异常才到达这里的外层 except。
    generator_events: list[str] = []
    try:
        with managed_tool_session("generated", generator_events) as handle:
            handle.use("business-error")
    except ToolCallError as error:
        print("  yield 处记录后重抛，外层仍收到:", error)

    # 两次 index 得到关键事件的位置，用 `<` 验证异常先回到 yield，随后才进入 finally。
    received_index: int = generator_events.index("generated: yield 收到 ToolCallError")
    finally_index: int = generator_events.index("generated: contextmanager finally")
    print("  异常先回到 yield，再执行 finally:", received_index < finally_index)
    assert received_index < finally_index
    assert "generated: close" in generator_events

    # 对照反例没有裸 raise：contextmanager 看到生成器处理异常后正常结束，就抑制块内错误。
    # 因而 with 后的赋值可以执行，这不是成功，而是错误被错误地隐藏了。
    swallowed_events: list[str] = []
    continued = False
    with swallowing_tool_session("forgot-raise", swallowed_events) as handle:
        handle.use("business-error")
    continued = True
    print("  教学反例：contextmanager 漏写 raise 后继续运行:", continued)
    assert continued

    # 零次 yield 会在进入阶段被包装器拒绝，块内的 pass 不会执行任何操作。
    try:
        with context_without_yield():
            pass
    except RuntimeError as error:
        print("  零次 yield 被拒绝:", error)

    # 两次 yield 的第一次值能绑定给 value；离开块时包装器要求生成器结束，却遇到第二次
    # yield，于是抛 RuntimeError。事件列表精确证明恢复后确实走到了第二次 yield 之前。
    two_yield_events: list[str] = []
    try:
        with context_with_two_yields(two_yield_events) as value:
            print("  第一次 yield 绑定值:", value)
    except RuntimeError as error:
        print("  两次 yield 被拒绝:", error)
    assert two_yield_events == ["two-yields: 准备错误地执行第二次 yield"]


def demonstrate_multiple_resources() -> None:
    print_section("4. 多资源逆序退出与 ExitStack")
    # 三个管理器共享同一事件列表，便于从一条时间线观察固定数量资源的进入与退出顺序。
    static_events: list[str] = []
    first = FakeToolSession("A", static_events)
    second = FakeToolSession("B", static_events)
    third = FakeToolSession("C", static_events)

    # 逗号分隔的多项 with 等价于三层嵌套：按 A、B、C 进入，按 C、B、A 退出。
    # 后进入的资源可能依赖先进入的资源，因此像栈一样后进先出。
    with first as first_handle, second as second_handle, third as third_handle:
        first_handle.use("one")
        second_handle.use("two")
        third_handle.use("three")

    # 列表推导式从 static_events 只保留以 ": close" 结尾的事件。split(..., maxsplit=1)
    # 最多切一次，再取 [0]，所以 "C: close" 会提取出资源名 "C"。
    close_order: list[str] = [
        event.split(":", maxsplit=1)[0]
        for event in static_events
        if event.endswith(": close")
    ]
    # 字典推导式逐个读取三个 manager，产生 {名称: 最终状态名}，便于一次检查全体资源。
    final_states: dict[str, str] = {
        manager.name: manager.state.name for manager in (first, second, third)
    }
    print("  声明顺序: A -> B -> C")
    print("  退出顺序:", " -> ".join(close_order))
    print("  所有最终状态:", final_states)
    assert close_order == ["C", "B", "A"]
    # dict.values() 取得状态值视图，set(...) 去重；结果只有 {"CLOSED"} 表示三者全已关闭。
    assert set(final_states.values()) == {"CLOSED"}

    # 当资源数量来自运行时列表，源码无法预先写死多项 with。ExitStack 让循环每成功进入一个
    # manager 就登记一个 __exit__，离开 stack 的 with 时再统一逆序调用。
    runtime_names: list[str] = ["runtime-1", "runtime-2", "runtime-3"]
    stack_events: list[str] = []
    runtime_managers: list[FakeToolSession] = []
    with ExitStack() as stack:
        for name in runtime_names:
            # 每轮创建的 manager 都写同一事件列表；append 保存对象供退出后检查状态。
            manager = FakeToolSession(name, stack_events)
            runtime_managers.append(manager)
            # enter_context 会立即调用 manager.__enter__，将成功进入后的退出方法压栈，
            # 并把 __enter__ 的返回值交给 handle，效果相当于动态增加一层 with。
            handle: ToolHandle = stack.enter_context(manager)
            handle.use("dynamic-call")

    # reversed 返回按相反方向遍历 runtime_names 的迭代器；list(...) 将它收集成列表，
    # 与实际 close_order 比较，验证动态栈同样后进先出。
    runtime_close_order: list[str] = [
        event.split(":", maxsplit=1)[0]
        for event in stack_events
        if event.endswith(": close")
    ]
    print("  ExitStack 动态退出顺序:", " -> ".join(runtime_close_order))
    assert runtime_close_order == list(reversed(runtime_names))

    # 最后一组让第三个资源在 __enter__ 中失败。attempted 包含所有已经尝试构造的管理器，
    # 因而可以同时检查前两个成功资源和第三个半初始化资源分别由谁清理。
    failure_events: list[str] = []
    attempted: list[FakeToolSession] = []
    try:
        with ExitStack() as stack:
            for name in ("ready-1", "ready-2", "fails-3"):
                # 比较表达式只在 name 为 "fails-3" 时得到 True，作为关键字参数触发失败。
                manager = FakeToolSession(
                    name,
                    failure_events,
                    fail_on_open=name == "fails-3",
                )
                attempted.append(manager)
                # 第三个 enter_context 抛错后，循环立即中止。ExitStack 离开时清理已经入栈的
                # ready-2、ready-1；失败对象没入栈，依靠自己的 open 回滚。
                stack.enter_context(manager)
    except SessionInitializationError as error:
        print("  后续获取失败:", error)

    # [-1] 取得最后尝试的 fails-3。列表推导式随后验证三个 exit_calls 恰为 [1, 1, 0]：
    # 两个成功进入者各由栈退出一次，进入失败者不调用 __exit__。
    failed_manager: FakeToolSession = attempted[-1]
    failure_states: dict[str, str] = {
        manager.name: manager.state.name for manager in attempted
    }
    print("  已成功进入的资源由 ExitStack 回收:", failure_states)
    print("  失败资源的 __exit__ 调用次数:", failed_manager.exit_calls)
    assert set(failure_states.values()) == {"CLOSED"}
    assert [manager.exit_calls for manager in attempted] == [1, 1, 0]
    assert failed_manager.cleanup_count == 1


def demonstrate_ownership() -> None:
    print_section("5. 生命周期的拥有者与借用者")
    # owner 创建并持有会话，with 划定唯一的打开区间；shared_handle 只在该区间借给函数。
    events: list[str] = []
    owner = FakeToolSession("shared", events)

    with owner as shared_handle:
        # 两个借用函数顺序复用同一句柄，都不 close；因此第一个返回后第二个仍能正常调用。
        first_result: str = borrower_run_tool(shared_handle, "tool-A", events)
        second_result: str = borrower_run_tool(shared_handle, "tool-B", events)
        print("  两个借用者之间会话仍打开:", shared_handle.is_open)
        print("  借用结果:", first_result, "/", second_result)

    # Python 的名字离开 with 后仍存在，shared_handle 仍指向原对象；with 管理的是资源
    # 生命周期，不是变量作用域。此时 owner 已 CLOSED，所以再次 use 会得到明确异常。
    print("  所有者离开 with 后关闭:", owner.state is SessionState.CLOSED)
    try:
        shared_handle.use("too-late")
    except SessionClosedError as error:
        print("  关闭后的句柄不能继续使用:", error)
    # 实际底层释放只有一次，证明借用者没有提前或重复接管关闭责任。
    assert owner.cleanup_count == 1


def demonstrate_generator_cleanup() -> None:
    print_section("6. 生成器提前停止需要显式清理")
    # 调用 stream_tool_results 只返回惰性的 generator，函数体尚未执行，manager 仍是 NEW。
    manual_events: list[str] = []
    manual_manager = FakeToolSession("manual-stream", manual_events)
    stream: Generator[str, None, None] = stream_tool_results(
        manual_manager,
        ("chunk-1", "chunk-2"),
        manual_events,
    )
    print("  创建生成器后会话状态:", manual_manager.state.name)

    # for 第一次取值时才进入生成器内部的 with 并打开会话。break 只退出 for，不会自动调用
    # 任意 iterator 的 close；变量 stream 仍引用暂停在 yield 处的生成器，所以资源仍 OPEN。
    for result in stream:
        print("  消费者只取一项:", result)
        break

    print("  break 后、显式 close 前仍打开:", manual_manager.state is SessionState.OPEN)
    # generator.close() 会在暂停的 yield 位置触发 GeneratorExit，使内部 with 执行 __exit__。
    # 这给提前停止建立了确定的清理时机，而不是等待垃圾回收。
    stream.close()
    print("  generator.close() 后已关闭:", manual_manager.state is SessionState.CLOSED)
    assert manual_manager.state is SessionState.CLOSED

    # closing(...) 把“有 close() 但自身不作为上下文管理器使用”的 generator 适配给 with。
    # 调用方拥有 managed_stream，因此适合在离开消费边界时由 closing 统一调用 close()。
    closing_events: list[str] = []
    closing_manager = FakeToolSession("closing-stream", closing_events)
    managed_stream: Generator[str, None, None] = stream_tool_results(
        closing_manager,
        ("chunk-1", "chunk-2"),
        closing_events,
    )
    with closing(managed_stream) as results:
        # closing.__enter__ 返回原对象本身，所以 results 和 managed_stream 指向同一生成器。
        for result in results:
            print("  closing 管理的消费者只取一项:", result)
            break

    # 即使循环只消费一项，离开 closing 的 with 也会调用 managed_stream.close()，继而退出
    # 生成器内部的 with 并关闭 closing_manager。
    print("  离开 closing 后已关闭:", closing_manager.state is SessionState.CLOSED)
    assert closing_manager.state is SessionState.CLOSED


def main() -> None:
    # 六组实验彼此创建独立 Fake 和事件列表，按课程顺序运行；任何 assert 失败都会立即指出
    # 对应生命周期假设不成立，全部通过后才打印最终完成消息。
    demonstrate_with_protocol()
    demonstrate_exception_paths()
    demonstrate_finally_and_contextmanager()
    demonstrate_multiple_resources()
    demonstrate_ownership()
    demonstrate_generator_cleanup()
    print("\n所有第 18 章示例与运行时断言均已完成。")


# 导入此文件时，Python 会定义上面的类和函数，但不会自动运行实验；只有把文件作为脚本
# 直接执行时，内置变量 __name__ 才等于 "__main__"。这相当于显式的程序入口保护，避免
# 其他模块为了复用 FakeToolSession 而 import 时产生打印和断言副作用。
if __name__ == "__main__":
    main()
