"""第 24 章：异步生成器、流式消费与异步资源管理。

全部实验只使用标准库、内存事件和可控闸门，不访问真实 LLM 或网络。
事件轨迹与断言关注协议、所有权和清理顺序，不依赖精确耗时或偶然调度顺序。
"""

# 建议按入口中的 1～9 段顺序阅读：先展开异步迭代协议，再建立事件流和资源所有权，
# 最后比较“直接拉取”与“Queue 解耦”两种生产/消费结构。数据主线都是：
# FAKE_EVENTS -> 异步生成器 -> 消费者；events 列表则在旁边保存生命周期轨迹供断言检查。
# 导入按用途理解：asyncio 提供事件循环、Task、Event、Queue 与 TaskGroup，inspect 用来辨认
# 调用后得到的对象，json 只把完整 JSON 文本解析成 Python 对象，不负责处理传输分片。
# collections.abc 提供迭代协议类型；contextlib 提供异步资源管理工具；dataclasses 与 Enum
# 用来声明事件和状态。TracebackType、Self 则让上下文协议方法的类型提示更准确。
import asyncio
import inspect
import json
from collections.abc import AsyncGenerator, AsyncIterator, Iterable, Iterator
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum, auto
from types import TracebackType
from typing import Self


# 这四个 dataclass 是应用层事件，不是网络 chunk 或 JSON 字典。
# frozen=True 禁止字段重新赋值，slots=True 不再为每个实例创建可任意扩展的 __dict__；
# 两者都不负责运行时类型校验，传入值是否符合注解仍由调用方保证。
@dataclass(frozen=True, slots=True)
class TokenEvent:
    # 一次可展示的文本增量，例如“正在”；它不承诺对应一个网络分块。
    text: str


@dataclass(frozen=True, slots=True)
class ToolStartEvent:
    # name 标识工具，call_id 把开始事件与稍后的结果事件关联起来。
    name: str
    call_id: str


@dataclass(frozen=True, slots=True)
class ToolResultEvent:
    name: str
    call_id: str
    # 为简化实验，工具输出直接保存为字符串；真实协议可能还要建模结构化结果和错误。
    output: str


@dataclass(frozen=True, slots=True)
class DoneEvent:
    # done 是应用协议中的完成通知，不等于 iterator 已耗尽，也不等于连接已经关闭。
    reason: str


# Python 3.12+ 的 type 语句给联合类型命名。LLMEvent 表示四类对象之一；
# 类型检查器可据此检查分支是否覆盖事件，但运行时不会自动验证赋值。
type LLMEvent = TokenEvent | ToolStartEvent | ToolResultEvent | DoneEvent


# tuple[LLMEvent, ...] 表示元素都是 LLMEvent、长度不限的元组。元组本身不可增删，
# 配合不可变事件对象，让九组实验安全复用同一份固定输入。
FAKE_EVENTS: tuple[LLMEvent, ...] = (
    TokenEvent("正在"),
    TokenEvent("查询"),
    ToolStartEvent("weather", "call-1"),
    ToolResultEvent("weather", "call-1", "晴，25°C"),
    TokenEvent("。"),
    DoneEvent("stop"),
)


# 普通 def 足够完成纯同步输出；async 函数也可以调用它，不必把所有辅助函数都写成 async def。
def print_section(title: str) -> None:
    # f-string 会先求值花括号内的 title，再拼进字符串；开头的 \n 让章节之间空一行。
    print(f"\n=== {title} ===")


def record(events: list[str], message: str) -> None:
    """事件既打印又保存，便于人眼观察并用断言验证因果关系。"""
    # append 原地修改调用方传入的同一个 list；没有复制，因此稍后的断言能看到完整轨迹。
    events.append(message)
    print(f"  事件 | {message}")


def event_label(event: LLMEvent) -> str:
    # match/case 做结构模式匹配：类模式既检查实例类型，也按 dataclass 字段顺序解构属性。
    # 例如 TokenEvent("正在") 会匹配第一支，并把 text 绑定为“正在”。
    match event:
        case TokenEvent(text):
            # !r 使用 repr 格式，文本边界和转义字符会比普通插值更容易观察。
            return f"token({text!r})"
        case ToolStartEvent(name, call_id):
            return f"tool-start({name}, {call_id})"
        case ToolResultEvent(name, call_id, output):
            return f"tool-result({name}, {call_id}, {output!r})"
        case DoneEvent(reason):
            return f"done({reason})"


# 继承 AsyncIterator[int] 主要表达类型契约：每次异步取值最终得到 int。
# 真正让 async for 工作的是下面两个特殊方法，而不是继承动作本身。
class ManualAsyncRange(AsyncIterator[int]):
    """最小异步迭代器：每次 __anext__ 都返回一个需要 await 的对象。"""

    def __init__(self, stop: int, events: list[str]) -> None:
        # iterator 必须保存当前位置；_ 前缀只是“不公开使用”的命名约定，并非访问权限。
        self._next_value = 0
        self._stop: int = stop
        # 保存外部列表的引用，让协议方法与演示函数把记录写到同一条轨迹中。
        self._events: list[str] = events

    def __aiter__(self) -> Self:
        # __aiter__ 同步返回异步 iterator 本身，不能返回等待中的 coroutine。
        record(self._events, "协议：__aiter__ 返回异步迭代器自己")
        return self

    async def __anext__(self) -> int:
        # async def 调用后先产生可等待对象；async for 会 await 它，得到一项或耗尽信号。
        record(self._events, f"协议：请求下一项，当前位置={self._next_value}")
        # 真实实现可能在这里等待 socket 或数据库；sleep(0) 只提供协作检查点。
        await asyncio.sleep(0)
        if self._next_value >= self._stop:
            # StopAsyncIteration 是协议的正常结束信号，async for 会捕获它并结束循环。
            record(self._events, "协议：抛出 StopAsyncIteration")
            raise StopAsyncIteration

        # 先保存本次返回值再推进游标，确保依次得到 0、1，而非提前加一后的值。
        value: int = self._next_value
        self._next_value += 1
        record(self._events, f"协议：返回 {value}")
        return value


# 这个 async def 不含 yield，所以它是 coroutine 函数：一次 await 最终取得一个 str。
async def coroutine_answer(events: list[str]) -> str:
    record(events, "coroutine：函数体运行")
    return "最终值"


# 函数体含 yield，因此它是异步生成器函数。调用时只创建 AsyncGenerator，
# 第一次 await anext(...) 或进入 async for 时才真正执行函数体。
async def tiny_async_generator(events: list[str]) -> AsyncGenerator[str]:
    try:
        record(events, "async generator：函数体运行")
        # yield 交出一项并暂停在这里，同时保留局部状态，等待消费者请求下一项或关闭。
        yield "流中的一项"
    finally:
        # 自然耗尽、显式 aclose 或进入生成器的异常/取消都可能让这个清理块执行。
        record(events, "async generator：finally 清理")


async def demonstrate_protocol_and_object_kinds() -> None:
    print_section("1. AsyncIterator 协议与 coroutine / async generator")
    events: list[str] = []
    source = ManualAsyncRange(2, events)
    # aiter(source) 调用 source.__aiter__()；本类返回自身，所以两个名字指向同一对象。
    iterator: ManualAsyncRange = aiter(source)
    # is 比较对象身份，不是内容是否相等。
    assert iterator is source

    # anext(iterator) 返回 awaitable，await 后才拿到 int。第三次传入默认值后，
    # 内部 StopAsyncIteration 会被 anext 转换为默认字符串，不向外抛出。
    first: int = await anext(iterator)
    second: int = await anext(iterator)
    exhausted: int | str = await anext(iterator, "<已耗尽>")
    print("  手动 aiter/anext:", first, second, exhausted)

    # async for 可理解为：先 aiter 一次，再反复 await anext；只有取值步骤能够暂停，
    # 循环体仍是一项处理完以后才会请求下一项，并不会自动为各项创建 Task。
    async_for_values: list[int] = []
    async for value in ManualAsyncRange(2, events):
        async_for_values.append(value)
        record(events, f"async for 循环体：消费 {value}")
    print("  async for 结果:", async_for_values)
    assert async_for_values == [0, 1]

    # 下面两次调用都只创建对象，尚未执行函数体，因此 kind_events 仍为空。
    kind_events: list[str] = []
    coroutine_object = coroutine_answer(kind_events)
    generator_object = tiny_async_generator(kind_events)
    print("  调用两个函数后，函数体事件数:", len(kind_events))
    print("  coroutine 对象可 await:", inspect.isawaitable(coroutine_object))
    print("  async generator 对象可 await:", inspect.isawaitable(generator_object))
    print("  async generator 对象是 AsyncIterator:", isinstance(generator_object, AsyncIterator))
    # inspect 同时从“函数种类”和“调用结果能否 await”两个角度验证区别。
    assert inspect.iscoroutinefunction(coroutine_answer)
    assert inspect.isasyncgenfunction(tiny_async_generator)
    assert inspect.isawaitable(coroutine_object)
    assert not inspect.isawaitable(generator_object)

    # coroutine 本身可 await 一次取得最终值；异步生成器不能直接 await，
    # 要 await 它的 anext/aclose 等协议操作。aclose 会使暂停处进入 finally。
    answer: str = await coroutine_object
    item: str = await anext(generator_object)
    await generator_object.aclose()
    print("  await coroutine 得到:", answer)
    print("  anext(async generator) 得到:", item)
    assert kind_events == [
        "coroutine：函数体运行",
        "async generator：函数体运行",
        "async generator：finally 清理",
    ]


def parse_event(raw: object) -> LLMEvent:
    """在 JSON 边界做最小校验，再建立明确的应用事件类型。"""
    # 参数写成 object，表示边界输入尚不可信；先缩小为 dict，才能安全调用 get。
    if not isinstance(raw, dict):
        raise TypeError("事件必须是 JSON object")

    # dict.get 在键不存在时返回 None，便于把缺字段与类型错误统一判为协议不合格。
    event_type = raw.get("type")
    if event_type == "token" and isinstance(raw.get("text"), str):
        return TokenEvent(raw["text"])
    if event_type == "tool-start":
        # 先把字段取到局部变量，再用 isinstance 做运行时检查；类型提示本身不会校验 JSON。
        name = raw.get("name")
        call_id = raw.get("call_id")
        if isinstance(name, str) and isinstance(call_id, str):
            return ToolStartEvent(name, call_id)
    if event_type == "tool-result":
        name = raw.get("name")
        call_id = raw.get("call_id")
        output = raw.get("output")
        if isinstance(name, str) and isinstance(call_id, str) and isinstance(output, str):
            return ToolResultEvent(name, call_id, output)
    if event_type == "done" and isinstance(raw.get("reason"), str):
        return DoneEvent(raw["reason"])
    # !r 保留原始对象的可辨认表示，便于定位具体是哪份边界数据不符合约定。
    raise ValueError(f"事件字段不符合约定：{raw!r}")


def decode_json_lines(text_fragments: Iterable[str], events: list[str]) -> Iterator[LLMEvent]:
    """把已解码的任意文本分片重组为 JSONL；分片本身不是应用事件。"""
    # buffer 保存尚未组成完整行的尾部。该函数从 str 开始，不涉及 bytes 或 UTF-8 增量解码。
    buffer = ""
    # enumerate(..., start=1) 同时给出从 1 开始的展示编号和当前文本分片。
    for fragment_number, fragment in enumerate(text_fragments, start=1):
        record(events, f"传输替身：收到文本分片 {fragment_number}，长度={len(fragment)}")
        # 一个 JSON 行可以横跨分片，所以必须先累计，不能对每个 fragment 直接 json.loads。
        buffer += fragment

        # 同一分片也可能含多行；只要 buffer 还有换行符，就继续拆出最前面的完整记录。
        while "\n" in buffer:
            # maxsplit=1 只切第一处：line 交给解析器，其余完整行和残片继续留在 buffer。
            line, buffer = buffer.split("\n", maxsplit=1)
            if not line:
                # 空行不代表应用事件，本例选择直接跳过。
                continue
            # json.loads 返回的运行时类型未知，所以先标作 object，再交给 parse_event 校验。
            raw: object = json.loads(line)
            event = parse_event(raw)
            record(events, f"解析器：组成完整应用事件 {event_label(event)}")
            # decode_json_lines 是同步生成器：list(...) 或 for 每请求一次，才交出一个完整事件。
            yield event

    # 输入用尽却仍有残片，说明传输结束在一条 JSON 记录中间；不能静默丢失。
    if buffer:
        raise ValueError("流结束时仍有不完整 JSON 文本")


def demonstrate_transport_boundaries() -> None:
    print_section("2. 文本分片、JSON 行与应用事件不是一一对应")
    events: list[str] = []
    # 这三个 tuple 元素故意不按事件边界切分：第一个在 tool-start JSON 中间结束，
    # 第三个则一次带来 tool-result 与 done 两行。相邻字符串字面量会在解析时自动拼接。
    fragments = (
        '{"type":"token","text":"你"}\n{"type":"tool',
        '-start","name":"weather","call_id":"c1"}\n',
        (
            '{"type":"tool-result","name":"weather","call_id":"c1","output":"晴"}\n'
            '{"type":"done","reason":"stop"}\n'
        ),
    )
    # decode_json_lines 惰性地产生事件；list 会把它消费到结束并收集全部结果。
    decoded: list[LLMEvent] = list(decode_json_lines(fragments, events))
    print("  3 个文本分片最终组成的应用事件数:", len(decoded))
    print("  应用事件:", [event_label(event) for event in decoded])
    assert len(fragments) == 3
    assert len(decoded) == 4
    assert isinstance(decoded[0], TokenEvent)
    assert isinstance(decoded[-1], DoneEvent)


# Enum 把会话状态限制为三个有名字的成员；auto() 自动生成此处无需关心的成员值。
# 后面用 is 比较枚举成员身份，避免拿裸字符串在各处拼写状态。
class SessionState(Enum):
    NEW = auto()
    OPEN = auto()
    CLOSED = auto()


class SessionOpenError(RuntimeError):
    """模拟异步握手失败。"""


class ConsumerError(RuntimeError):
    """模拟消费者处理某个事件时失败。"""


# 这个对象集中保存资源是否关闭、关闭次数和产出数量，供不同 Task 共享并供实验断言。
@dataclass(slots=True)
class StreamLifecycle:
    # default_factory 传入“创建函数”而不是一个 Event 实例；每次 StreamLifecycle() 都会
    # 各自调用 asyncio.Event()，因此不同实验不会意外共享同一个闸门。
    stream_closed: asyncio.Event = field(default_factory=asyncio.Event)
    session_closed: asyncio.Event = field(default_factory=asyncio.Event)
    stream_close_count: int = 0
    session_close_count: int = 0
    produced_count: int = 0


class FakeAsyncSession:
    """只模拟可等待的会话获取与关闭，不代表任何真实客户端。"""

    def __init__(
        self,
        name: str,
        events: list[str],
        lifecycle: StreamLifecycle,
        *,
        fail_on_enter: bool = False,
    ) -> None:
        # 参数列表中的 * 使 fail_on_enter 成为仅限关键字参数，调用处必须写出参数名。
        self.name: str = name
        self.events: list[str] = events
        self.lifecycle: StreamLifecycle = lifecycle
        self.fail_on_enter: bool = fail_on_enter
        self.state = SessionState.NEW
        self.exit_calls = 0

    async def __aenter__(self) -> Self:
        # async with 会 await __aenter__，并把成功返回的 self 绑定给 as 后面的变量。
        record(self.events, f"{self.name}: __aenter__ 开始获取会话")
        # 真实客户端可能等待连接、TLS 或鉴权；本地替身只让出一次执行权。
        await asyncio.sleep(0)
        if self.fail_on_enter:
            # __aenter__ 失败时，本对象的 __aexit__ 不会被调用，所以进入方法必须自己回滚
            # 已取得的部分状态。本例同时设置 Event 和计数，表示回滚已完成。
            self.state = SessionState.CLOSED
            self.lifecycle.session_close_count += 1
            self.lifecycle.session_closed.set()
            record(self.events, f"{self.name}: 获取失败并回滚部分状态")
            raise SessionOpenError(f"{self.name}: 握手失败")

        self.state = SessionState.OPEN
        record(self.events, f"{self.name}: 会话已获取")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        # 块正常结束时三个异常参数都是 None；块内失败或取消时会收到对应异常信息。
        # 本例只观察 exc_type，exc_value/traceback 留在签名中以完整实现异步上下文协议。
        self.exit_calls += 1
        exception_name: str = "None" if exc_type is None else exc_type.__name__
        record(self.events, f"{self.name}: __aexit__ 收到 {exception_name}，开始关闭")
        # 关闭网络会话也可能需要发送协议帧或等待连接池；因此退出方法可 await。
        await asyncio.sleep(0)
        self.state = SessionState.CLOSED
        self.lifecycle.session_close_count += 1
        self.lifecycle.session_closed.set()
        record(self.events, f"{self.name}: 会话已关闭")
        # 返回 False 表示“不抑制异常”，原异常会在清理完成后继续向调用方传播。
        return False

    def assert_open(self) -> None:
        # is 用于比较唯一的枚举成员；流每次产出前检查，防止在会话关闭后继续使用它。
        if self.state is not SessionState.OPEN:
            raise RuntimeError(f"{self.name}: 流只能使用已打开会话")


# StreamPause 是可控取消实验的双向握手：生成器用 waiting 通知“已经停在目标位置”，
# 测试方可在此时取消任务，或通过 release 明确放行，而不依赖碰运气的 sleep 时长。
@dataclass(slots=True)
class StreamPause:
    before_index: int
    waiting: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


async def fake_llm_stream(
    session: FakeAsyncSession,
    events: list[str],
    lifecycle: StreamLifecycle,
    *,
    source_events: tuple[LLMEvent, ...] = FAKE_EVENTS,
    pause: StreamPause | None = None,
) -> AsyncGenerator[LLMEvent]:
    """逐项产生 LLM 风格应用事件，并在任何退出路径记录流关闭。"""
    # 虽然这是函数体第一句，但调用 fake_llm_stream(...) 时不会立刻执行；第一次请求下一项时
    # 才检查会话并记录“流：开始”。这正是异步生成器的惰性执行特征。
    session.assert_open()
    record(events, "流：开始")
    try:
        # enumerate 同时提供从 0 开始的位置和事件；位置用来精确选择暂停点。
        for index, event in enumerate(source_events):
            if pause is not None and index == pause.before_index:
                record(events, f"流：产出第 {index + 1} 项前等待外部条件")
                # 先 set 告知外部“已经到达”，再等待 release。Event 被 set 后的 wait 会立即通过。
                pause.waiting.set()
                await pause.release.wait()

            # 只模拟“准备下一事件需要异步等待”，不模拟真实 token 到达间隔。
            await asyncio.sleep(0)
            session.assert_open()
            lifecycle.produced_count += 1
            record(events, f"流：产出 {event_label(event)}")
            # yield 把当前 event 交回 anext/async for，并暂停整个生成器；消费者处理期间，
            # 下面的“从 yield 后恢复”和下一轮生产都不会发生，这就形成直接拉取的自然背压。
            yield event
            record(events, "流：消费者请求下一项，生成器从 yield 后恢复")

        record(events, "流：自然结束")
    finally:
        # finally 统一覆盖自然耗尽、aclose、生成器内异常，以及发生在当前 __anext__ 中的取消。
        lifecycle.stream_close_count += 1
        lifecycle.stream_closed.set()
        record(events, "流：已关闭")


# 装饰器把“恰好 yield 一次”的异步生成器函数转换为异步上下文管理器工厂。
# yield 之前负责获取资源，yield 的值借给 async-with 块，yield 之后负责退出和清理。
@asynccontextmanager
async def owned_llm_stream(
    name: str,
    events: list[str],
    lifecycle: StreamLifecycle,
    *,
    source_events: tuple[LLMEvent, ...] = FAKE_EVENTS,
    pause: StreamPause | None = None,
) -> AsyncIterator[AsyncGenerator[LLMEvent]]:
    """拥有会话和流：离开一次 async-with 就确定性关闭二者。"""
    # 外层 session 先进入、后退出；内层 aclosing 因为后进入，所以会先关闭 stream。
    async with FakeAsyncSession(name, events, lifecycle) as session:
        # 此时只创建异步生成器对象，函数体尚未执行；所有者保留具体对象以便调用 aclose。
        stream: AsyncGenerator[LLMEvent, None] = fake_llm_stream(
            session,
            events,
            lifecycle,
            source_events=source_events,
            pause=pause,
        )
        # aclosing 的 __aexit__ 会 await stream.aclose()。因此消费者 break、抛异常或被取消时，
        # 流都在 session 仍为 OPEN 的范围内确定性关闭，而不是等垃圾回收或 runner 兜底。
        async with aclosing(stream):
            record(events, "所有者：把流借给消费者")
            try:
                # yield 把 stream 借给调用方的 as 变量，并暂停管理器在这里等待块结束。
                yield stream
            except BaseException as error:
                # asynccontextmanager 会把块内异常送回 yield；这里只记录后原样传播。
                # 这里捕获 BaseException 是为了也观察 CancelledError；裸 raise 保留原异常对象。
                record(events, f"所有者：yield 收到 {type(error).__name__}")
                raise
            finally:
                # 这条记录发生后才退出 aclosing，随后退出 session，轨迹可验证嵌套清理顺序。
                record(events, "所有者：离开流的借用区间")


async def demonstrate_natural_consumption() -> None:
    print_section("3. 直接 async for：生产与消费交替，自然结束")
    events: list[str] = []
    lifecycle = StreamLifecycle()
    consumed: list[LLMEvent] = []

    # 消费者只借用 stream；owned_llm_stream 才是创建并关闭 stream/session 的所有者。
    async with owned_llm_stream("natural-session", events, lifecycle) as stream:
        async for event in stream:
            record(events, f"消费者：收到 {event_label(event)}")
            consumed.append(event)

    # 能执行到这里说明管理器已完整退出；现在检查数据内容和两个资源各关闭一次。
    print("  消费事件类型:", [type(event).__name__ for event in consumed])
    print("  流/会话关闭次数:", lifecycle.stream_close_count, lifecycle.session_close_count)
    assert consumed == list(FAKE_EVENTS)
    assert lifecycle.stream_closed.is_set()
    assert lifecycle.session_closed.is_set()
    assert lifecycle.stream_close_count == lifecycle.session_close_count == 1

    # 同一个 async for 的循环体运行完，才会请求下一项；它没有并行执行循环体。
    # range(len(consumed) - 1) 给出每对相邻事件中前一项的下标。
    for index in range(len(consumed) - 1):
        consumed_marker: str = f"消费者：收到 {event_label(consumed[index])}"
        next_produced_marker: str = f"流：产出 {event_label(consumed[index + 1])}"
        assert events.index(consumed_marker) < events.index(next_produced_marker)


async def demonstrate_break_and_consumer_error() -> None:
    print_section("4. break 与消费者异常：清理责任必须写进边界")

    break_events: list[str] = []
    break_lifecycle = StreamLifecycle()
    # 第一部分故意不用 owned_llm_stream/aclosing，以便单独观察 async for 的 break 行为。
    async with FakeAsyncSession("break-session", break_events, break_lifecycle) as session:
        stream: AsyncGenerator[LLMEvent, None] = fake_llm_stream(
            session, break_events, break_lifecycle
        )
        async for event in stream:
            record(break_events, f"消费者：只取 {event_label(event)} 后 break")
            # break 只退出循环，不是异步 iterator 协议中的“关闭”命令。
            break

        # stream 仍被局部变量引用并暂停在 yield；其 finally 尚未执行。
        print("  break 后、aclose 前，流已关闭:", break_lifecycle.stream_closed.is_set())
        assert not break_lifecycle.stream_closed.is_set()
        # aclose 返回 awaitable；必须 await，才会向暂停的生成器注入关闭信号并跑完 finally。
        await stream.aclose()
        print("  await stream.aclose() 后，流已关闭:", break_lifecycle.stream_closed.is_set())
        assert break_lifecycle.stream_closed.is_set()

    assert break_lifecycle.session_closed.is_set()
    assert break_events.index("流：已关闭") < break_events.index("break-session: 会话已关闭")

    # 第二部分改用推荐的 owning context。消费者循环体失败后，异常会穿过 yield 回到管理器；
    # aclosing 先关闭流、session 再关闭，最后外层 except 才能观察原 ConsumerError。
    error_events: list[str] = []
    error_lifecycle = StreamLifecycle()
    try:
        async with owned_llm_stream("error-session", error_events, error_lifecycle) as stream:
            async for event in stream:
                record(error_events, f"消费者：处理 {event_label(event)} 时失败")
                raise ConsumerError("渲染事件失败")
    except ConsumerError as error:
        # as error 把捕获到的异常对象绑定到局部变量，用于记录其消息。
        record(error_events, f"调用方：捕获 {error}")

    assert error_lifecycle.stream_closed.is_set()
    assert error_lifecycle.session_closed.is_set()
    assert "所有者：yield 收到 ConsumerError" in error_events
    assert error_events.index("流：已关闭") < error_events.index("error-session: 会话已关闭")
    assert error_events.index("error-session: 会话已关闭") < error_events.index(
        "调用方：捕获 渲染事件失败"
    )


async def consume_until_cancelled(
    events: list[str],
    lifecycle: StreamLifecycle,
    pause: StreamPause,
) -> None:
    # 这个函数运行在独立 Task 中。取消可能从内部某个 await 抛出 CancelledError，
    # 但退出 owned_llm_stream 前会先走完流与会话的异步清理。
    async with owned_llm_stream(
        "cancel-session",
        events,
        lifecycle,
        pause=pause,
    ) as stream:
        async for event in stream:
            record(events, f"消费者 Task：收到 {event_label(event)}")


async def demonstrate_external_cancellation() -> None:
    print_section("5. 外部取消：先关闭流和会话，再向调用方传播")
    events: list[str] = []
    lifecycle = StreamLifecycle()
    # before_index=1 表示第一项已完成消费，生成器准备第二项前停住。
    pause = StreamPause(before_index=1)
    # create_task 立即把 coroutine 交给事件循环调度；保留 Task 引用才能取消并等待其收尾。
    consumer_task = asyncio.create_task(
        consume_until_cancelled(events, lifecycle, pause),
        name="cancelled-stream-consumer",
    )

    # 这道事件证明任务已经消费第一项，并在请求第二项时进入可取消等待。
    await pause.waiting.wait()
    # cancel 只是请求在 Task 的下一处可取消点抛 CancelledError，不代表此刻已经清理完成。
    consumer_task.cancel("用户停止生成")
    try:
        # 所有者必须继续 await 被取消的 Task；返回到这里时，内部 finally/__aexit__ 才已跑完。
        await consumer_task
    except asyncio.CancelledError:
        record(events, "调用方：收到 CancelledError")
    else:
        raise AssertionError("外部取消不应被流所有者吞掉")

    print("  最终 Task.cancelled():", consumer_task.cancelled())
    print(
        "  流关闭 / 会话关闭:", lifecycle.stream_closed.is_set(), lifecycle.session_closed.is_set()
    )
    assert consumer_task.cancelled()
    assert lifecycle.stream_closed.is_set()
    assert lifecycle.session_closed.is_set()
    assert lifecycle.produced_count == 1
    # index 返回事件首次出现的位置；严格小于关系证明清理先于调用方观察取消。
    assert events.index("流：已关闭") < events.index("cancel-session: 会话已关闭")
    assert events.index("cancel-session: 会话已关闭") < events.index("调用方：收到 CancelledError")


async def demonstrate_async_exit_stack() -> None:
    print_section("6. AsyncExitStack：动态资源在后续获取失败时逆序关闭")
    events: list[str] = []
    sessions: list[FakeAsyncSession] = []

    try:
        # AsyncExitStack 适合资源数量由运行时循环决定的场景。每次成功进入一个管理器，
        # 栈就登记其 __aexit__；离开时按“后进先出”顺序逐个 await 清理。
        async with AsyncExitStack() as stack:
            for name in ("stack-A", "stack-B", "stack-fails-C"):
                # 每个会话各有独立生命周期记录；这里只用 state/exit_calls 被收集后检查。
                lifecycle = StreamLifecycle()
                session = FakeAsyncSession(
                    name,
                    events,
                    lifecycle,
                    fail_on_enter=name == "stack-fails-C",
                )
                sessions.append(session)
                # C 的 __aenter__ 抛错时，C 尚未成功登记，故栈不会调用 C.__aexit__；
                # C 由自己的 __aenter__ 回滚，栈随后按 B、A 的顺序关闭已登记资源。
                await stack.enter_async_context(session)
                session.assert_open()
    except SessionOpenError as error:
        record(events, f"调用方：捕获动态获取失败 {error}")

    print("  三个资源最终状态:", [session.state.name for session in sessions])
    print("  三个 __aexit__ 调用次数:", [session.exit_calls for session in sessions])
    assert [session.state for session in sessions] == [
        SessionState.CLOSED,
        SessionState.CLOSED,
        SessionState.CLOSED,
    ]
    assert [session.exit_calls for session in sessions] == [1, 1, 0]
    assert events.index("stack-B: 会话已关闭") < events.index("stack-A: 会话已关闭")


# 这对 Event 把“消费者已开始第一项”与“允许第一项结束”分开，
# 从而能在两者之间稳定观察上游是否偷偷生产第二项。
@dataclass(slots=True)
class SlowConsumerControl:
    first_item_started: asyncio.Event = field(default_factory=asyncio.Event)
    first_item_may_finish: asyncio.Event = field(default_factory=asyncio.Event)


async def run_slow_consumer(
    events: list[str],
    lifecycle: StreamLifecycle,
    control: SlowConsumerControl,
) -> None:
    # 切片 [:3] 创建含前三个引用的新元组；事件对象本身不可变，可以安全复用。
    source_events = FAKE_EVENTS[:3]
    async with owned_llm_stream(
        "slow-session",
        events,
        lifecycle,
        source_events=source_events,
    ) as stream:
        position = 0
        async for event in stream:
            position += 1
            record(events, f"慢消费者：开始处理第 {position} 项 {event_label(event)}")
            if position == 1:
                # 通知控制器已经进入第一轮循环体，然后暂停本 Task。
                # 生成器与消费者处在同一个 Task 调用链中，因此生成器也无法恢复到下一轮。
                control.first_item_started.set()
                await control.first_item_may_finish.wait()
            record(events, f"慢消费者：完成第 {position} 项")


async def demonstrate_natural_backpressure() -> None:
    print_section("7. 无 Queue：慢消费者自然约束下一项生产")
    events: list[str] = []
    lifecycle = StreamLifecycle()
    control = SlowConsumerControl()
    # 单独建 Task 是为了让当前控制器能在消费者暂停时检查共享状态；
    # 它没有把每个流事件各自并行化，内部仍只有一个顺序 async for。
    task = asyncio.create_task(
        run_slow_consumer(events, lifecycle, control),
        name="slow-direct-consumer",
    )

    await control.first_item_started.wait()
    # 此刻不是依靠时间猜测，而是由 Event 证明第一项循环体已开始且仍未完成。
    print("  第一项处理未结束时，已经产出的数量:", lifecycle.produced_count)
    assert lifecycle.produced_count == 1
    assert f"流：产出 {event_label(FAKE_EVENTS[1])}" not in events

    # 放行后再 await Task，保证其消费完三项并退出资源上下文。
    control.first_item_may_finish.set()
    await task
    print("  消费者继续后，最终产出数量:", lifecycle.produced_count)
    assert lifecycle.produced_count == 3
    assert lifecycle.stream_closed.is_set() and lifecycle.session_closed.is_set()


# QueueEnd 只属于进程内 producer/consumer 协议，用来唤醒 consumer 并让其退出。
# 它和上游的 DoneEvent、异步 iterator 的 StopAsyncIteration 是三个不同层次的结束信号。
@dataclass(frozen=True, slots=True)
class QueueEnd:
    """内部管道结束哨兵；它不是 LLM 的 done 应用事件。"""


# 只创建一个不可变哨兵实例；producer 放入它，consumer 用 isinstance 识别它。
QUEUE_END = QueueEnd()
# Queue 中既能放业务事件，也能放内部哨兵，因此元素类型写成联合类型。
type QueueItem = LLMEvent | QueueEnd


# 该状态对象是两个子 Task 与管道所有者之间的协调面：Event 表示阶段事实，
# 普通字段保存最大容量和消费结果。它只在一个事件循环线程内协作使用，不是线程同步对象。
@dataclass(slots=True)
class QueueRunState:
    producer_backpressured: asyncio.Event = field(default_factory=asyncio.Event)
    start_consuming: asyncio.Event = field(default_factory=asyncio.Event)
    pipeline_ready: asyncio.Event = field(default_factory=asyncio.Event)
    producer_finished: asyncio.Event = field(default_factory=asyncio.Event)
    consumer_finished: asyncio.Event = field(default_factory=asyncio.Event)
    max_queue_size: int = 0
    # list 是可变对象，必须用 default_factory；否则多个 QueueRunState 会共享消费结果。
    consumed: list[LLMEvent] = field(default_factory=list)


async def queue_producer(
    queue: asyncio.Queue[QueueItem],
    events: list[str],
    lifecycle: StreamLifecycle,
    state: QueueRunState,
) -> None:
    """producer 拥有底层流；取消或失败时上下文仍会关闭流和会话。"""
    try:
        # producer 是底层 stream 的唯一消费者与所有者；它把应用事件转交到 Queue。
        async with owned_llm_stream("queue-session", events, lifecycle) as stream:
            async for event in stream:
                # full() 只是当前瞬间的观察。此实验只有一个 producer，且 consumer 尚未放行，
                # 所以 set() 可稳定证明接下来的 put 会因容量已满而等待。
                if queue.full():
                    record(events, f"Queue producer：队列已满({queue.qsize()})，等待 put")
                    state.producer_backpressured.set()
                # 有界 Queue 已满时 await put 会暂停 producer，直到 consumer 腾出一格。
                await queue.put(event)
                # qsize 是当前缓冲数量；max 保存整个实验实际观察到的容量峰值。
                state.max_queue_size = max(state.max_queue_size, queue.qsize())
                record(
                    events,
                    f"Queue producer：已放入 {event_label(event)}，qsize={queue.qsize()}",
                )

        # 只有流自然结束，才发送内部哨兵。若 producer 异常或取消，TaskGroup 会取消 consumer，
        # 不能假定任何失败路径都一定来得及把哨兵放进可能已满的 Queue。
        await queue.put(QUEUE_END)
        state.max_queue_size = max(state.max_queue_size, queue.qsize())
        record(events, "Queue producer：已放入内部结束哨兵")
    except asyncio.CancelledError:
        # 只记录后重新抛出，保留 Task 的取消状态，让 TaskGroup/调用方正确观察失败。
        record(events, "Queue producer：收到取消")
        raise
    finally:
        # 无论正常、异常或取消，都发布“producer 已结束”的可观察事实。
        state.producer_finished.set()
        record(events, "Queue producer：已结束")


async def queue_consumer(
    queue: asyncio.Queue[QueueItem],
    events: list[str],
    state: QueueRunState,
) -> None:
    """每次 get 都与一次 task_done 配对，包括内部结束哨兵。"""
    try:
        # 正常实验先让 producer 填满队列；取消实验则始终不打开这道闸门。
        await state.start_consuming.wait()
        while True:
            # Queue 为空时 get 会等待。尚未完成数在 put 时增加；get 只取走项目，
            # 要到下面的 task_done 才会把对应记账减一。
            item = await queue.get()
            try:
                if isinstance(item, QueueEnd):
                    record(events, "Queue consumer：收到内部结束哨兵")
                    # return 仍会先执行内层 finally，所以哨兵也能正确 task_done。
                    return
                # 经过 isinstance 排除 QueueEnd 后，类型检查器也能把 item 缩小为 LLMEvent。
                state.consumed.append(item)
                record(events, f"Queue consumer：消费 {event_label(item)}，qsize={queue.qsize()}")
            finally:
                # task_done 只表示这一次 get 的 Queue 记账结束，不等于业务一定成功。
                queue.task_done()

            # 给 producer 一个推进机会；正确性断言仍只依赖 Event 与队列状态。
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        record(events, "Queue consumer：收到取消")
        raise
    finally:
        state.consumer_finished.set()
        record(events, "Queue consumer：已结束")


async def demonstrate_bounded_queue() -> None:
    print_section("8. 有界 Queue：解耦生产消费，同时保留容量背压")
    events: list[str] = []
    lifecycle = StreamLifecycle()
    state = QueueRunState()
    # maxsize=2 表示最多缓冲两项；默认值 0 反而表示无界，不是零容量。
    queue: asyncio.Queue[QueueItem] = asyncio.Queue(maxsize=2)

    # TaskGroup 共同拥有 producer 与 consumer：退出上下文会等待二者；任一子任务以普通异常
    # 失败时，会取消同组其他任务并在收尾后向外传播异常组。
    async with asyncio.TaskGroup() as group:
        producer_task = group.create_task(
            queue_producer(queue, events, lifecycle, state),
            name="bounded-queue-producer",
        )
        consumer_task = group.create_task(
            queue_consumer(queue, events, state),
            name="bounded-queue-consumer",
        )

        # consumer 仍在闸门外；producer 填满两格后会确定地阻塞在第三次 put。
        await state.producer_backpressured.wait()
        print("  producer 等待时 qsize/maxsize:", queue.qsize(), queue.maxsize)
        assert queue.qsize() == queue.maxsize == 2
        assert not producer_task.done()
        assert not consumer_task.done()
        # set 后 consumer 的 wait 可通过，开始 get；腾出的容量让 producer 的 put 继续。
        state.start_consuming.set()

    # 越过 TaskGroup 时两个 Task 都已结束。join 再核对每次 put 都已有匹配的 task_done。
    await queue.join()
    print("  观察到的最大队列长度:", state.max_queue_size)
    print("  producer / consumer 已结束:", producer_task.done(), consumer_task.done())
    assert state.consumed == list(FAKE_EVENTS)
    assert state.max_queue_size == 2
    assert state.producer_finished.is_set() and state.consumer_finished.is_set()
    assert lifecycle.stream_closed.is_set() and lifecycle.session_closed.is_set()


async def cancellable_queue_pipeline(
    events: list[str],
    lifecycle: StreamLifecycle,
    state: QueueRunState,
) -> None:
    """管道所有者在取消后回收双方，并明确丢弃尚未消费的队列项。"""
    queue: asyncio.Queue[QueueItem] = asyncio.Queue(maxsize=2)
    try:
        # 父管道拥有整个 TaskGroup。外部取消父 Task 时，取消会进入这个上下文，
        # TaskGroup 再取消并等待 producer/consumer，之后才允许执行外层 finally。
        async with asyncio.TaskGroup() as group:
            group.create_task(
                queue_producer(queue, events, lifecycle, state),
                name="cancel-queue-producer",
            )
            group.create_task(
                queue_consumer(queue, events, state),
                name="cancel-queue-consumer",
            )
            # 等 producer 准备向满队列继续 put，确保两个子任务都停在明确的可取消点。
            await state.producer_backpressured.wait()
            state.pipeline_ready.set()
            # 新建的 Event 永不 set，因此这里会一直等待，直到外部对父 Task 发出取消。
            await asyncio.Event().wait()
    finally:
        # 本例的取消策略是“丢弃尚未交付项”。每个已 put 项仍要完成 task_done
        # 记账；否则 queue.join() 会永久等待。本地队列随作用域一起销毁。
        discarded = 0
        while not queue.empty():
            # get_nowait 在已确认非空后同步取出一项；这里没有其他仍运行的 Queue 使用者竞争。
            queue.get_nowait()
            queue.task_done()
            discarded += 1
        # 所有残留项都补完记账后 join 应立即返回，也把“没有遗漏 task_done”写成可执行检查。
        await queue.join()
        record(events, f"Queue owner：丢弃 {discarded} 项并清空 join 记账")


async def demonstrate_queue_cancellation() -> None:
    print_section("9. Queue 管道取消：producer、consumer、流和会话一起回收")
    events: list[str] = []
    lifecycle = StreamLifecycle()
    state = QueueRunState()
    # 与单个消费者取消实验相同：必须保留父 Task，cancel 后还要 await 它完成级联清理。
    pipeline_task = asyncio.create_task(
        cancellable_queue_pipeline(events, lifecycle, state),
        name="cancellable-queue-pipeline",
    )

    await state.pipeline_ready.wait()
    # pipeline_ready 证明队列已满、producer 卡在 put、consumer 卡在启动闸门。
    pipeline_task.cancel("请求已断开")
    try:
        await pipeline_task
    except asyncio.CancelledError:
        record(events, "Queue 调用方：收到 CancelledError")
    else:
        raise AssertionError("管道所有者不应吞掉外部取消")

    print(
        "  producer / consumer finished:",
        state.producer_finished.is_set(),
        state.consumer_finished.is_set(),
    )
    print(
        "  stream / session closed:",
        lifecycle.stream_closed.is_set(),
        lifecycle.session_closed.is_set(),
    )
    assert pipeline_task.cancelled()
    assert state.producer_finished.is_set()
    assert state.consumer_finished.is_set()
    assert lifecycle.stream_closed.is_set()
    assert lifecycle.session_closed.is_set()
    # next(generator expression) 找到具体的 owner 清理记录；若没有该记录会直接失败。
    owner_cleanup: str = next(event for event in events if event.startswith("Queue owner：丢弃"))
    assert events.index("Queue producer：已结束") < events.index(owner_cleanup)
    assert events.index("Queue consumer：已结束") < events.index(owner_cleanup)
    assert events.index(owner_cleanup) < events.index("Queue 调用方：收到 CancelledError")


async def run_examples() -> None:
    # 前两段分别是异步与同步演示，所以只有 coroutine 调用需要 await。
    # 后续段落按“正常 -> 提前退出/异常 -> 取消 -> 动态资源 -> 背压 -> Queue”递进执行。
    await demonstrate_protocol_and_object_kinds()
    demonstrate_transport_boundaries()
    await demonstrate_natural_consumption()
    await demonstrate_break_and_consumer_error()
    await demonstrate_external_cancellation()
    await demonstrate_async_exit_stack()
    await demonstrate_natural_backpressure()
    await demonstrate_bounded_queue()
    await demonstrate_queue_cancellation()

    # asyncio.all_tasks() 返回当前事件循环中尚未结束的 Task 集合，其中包含当前 run_examples。
    # 集合推导式排除当前 Task 和已完成 Task；空集合证明示例没有遗留后台工作。
    unfinished = {
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    }
    print("\n入口结束前未回收的其他 Task 数:", len(unfinished))
    assert not unfinished
    print("所有第 24 章示例与运行时断言均已完成。")


def main() -> None:
    # asyncio.run 创建事件循环、运行顶层 coroutine，并在返回前完成 runner 级别的收尾。
    # 资源仍由各实验在自己的局部 async-with 中关闭，不能依赖入口退出才回收。
    asyncio.run(run_examples())


# 导入该模块时只定义类型与函数；直接执行本文件时，特殊变量 __name__ 才等于 "__main__"。
if __name__ == "__main__":
    main()
