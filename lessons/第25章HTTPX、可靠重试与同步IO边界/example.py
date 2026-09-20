"""第 25 章：HTTPX、可靠请求与同步 I/O 边界。

从仓库根目录运行：
    .venv/bin/python -B java_to_python/chapters/25_async_http_io/example.py all

也可把最后的 all 换成 http、retry、sync 或 combined，逐节观察。
"""

# 建议按文件中的定义顺序阅读：先认识 HTTP/模型合同和重试策略，再看三个独立实验，
# 最后看 combined 如何把 HTTP、协程与同步数据库查询放进同一个请求预算。
# 数据主线是：HTTP 响应 -> Python JSON 值 -> Pydantic Document；失败则转换成稳定的应用异常。
# 本章所有公网 URL 都由 MockTransport 接管，真实 socket 只访问配套 fixture 的 127.0.0.1。

# 标准库分别提供：命令行入口、asyncio 编排、JSON/SQLite 同步处理、线程协调和临时目录。
# inspect 只用于在运行时展示“返回协程的方法”和“普通方法”的区别。
import argparse
import asyncio
import inspect
import json
import random
import sqlite3
import tempfile
import threading
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from pathlib import Path

# HTTPX 是原生异步 HTTP 客户端；Requests 刻意作为同步客户端反例。
# local_http_fixture 是同目录模块，负责启动可控的本地 HTTP/1.1 服务。
# Pydantic 在 JSON 解码后执行运行时模型校验；它和静态类型提示承担不同职责。
import httpx
import requests
from local_http_fixture import FixtureState, LocalHttpFixture
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# HTTPX 的 timeout 不是一个笼统总时限，而是连接、等待下一块响应数据、写入和等待连接池
# 四个阶段各自允许空等多久。真正覆盖“多次尝试 + 退避”的总预算稍后由 deadline 表示。
HTTP_TIMEOUT = httpx.Timeout(connect=0.20, read=0.08, write=0.20, pool=0.03)

# 池最多只有一条连接并保留一条空闲 keep-alive 连接，便于实验稳定展示连接复用和池等待超时。
# 这限制的是 HTTP 连接数量，不等于 asyncio Task 数量，也不是每秒请求数限制。
HTTP_LIMITS = httpx.Limits(max_connections=1, max_keepalive_connections=1)

# frozenset 是不可变集合：成员测试仍是 `code in RETRYABLE_STATUS_CODES`，但策略不会被意外增删。
# 这里只把通常可能短暂恢复的限流/网关错误加入白名单；400 等参数错误不应盲目重发。
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 502, 503, 504})

# 圆括号构造一个异常类元组，稍后可直接写 `except RETRYABLE_TRANSPORT_ERRORS as error`。
# 有意不包含 PoolTimeout：池耗尽首先是本地容量/背压问题，立即重试往往只会重新排队。
RETRYABLE_TRANSPORT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.WriteTimeout,
    httpx.RemoteProtocolError,
)

# BaseModel 类似带运行时校验能力的 DTO。HTTP JSON 先是 dict/list 等普通 Python 值，
# 只有通过 `Document.model_validate(payload)` 后，调用方才拿到字段合同稳定的 Document 实例。
class Document(BaseModel):
    """进入 Agent 内部前的稳定数据形状。"""

    # strict=True 禁止把字符串 "25" 悄悄转成整数 25；extra="forbid" 拒绝未声明字段；
    # frozen=True 禁止正常方式重新给模型字段赋值。它们是 Pydantic 的运行时规则，
    # 不能简单等同于 Java 编译期类型检查，也不意味着对象内部任意嵌套值都会深度不可变。
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    id: int
    title: str
    # Field 给 score 追加闭区间约束；类型正确但小于 0 或大于 1 也会校验失败。
    score: float = Field(ge=0.0, le=1.0)


# 下面先建立应用层 I/O 异常树。调用方可以统一捕获 ToolIOError，也可以按阶段精细恢复；
# 原始 HTTPX、JSON 或 Pydantic 异常会通过 `raise ... from error` 保存在 `__cause__` 中。
class ToolIOError(RuntimeError):
    """本章应用层 I/O 异常的共同基类。"""


class TransportFailure(ToolIOError):
    """连接、读写或协议传输失败。"""


class HttpStatusFailure(ToolIOError):
    """收到了响应，但 HTTP 状态不成功。"""


class ResponseJsonFailure(ToolIOError):
    """成功 HTTP 响应的正文不是合法 JSON。"""


class ResponseValidationFailure(ToolIOError):
    """JSON 合法，但不符合模型合同。"""


class RetryExhausted(ToolIOError):
    """可重试失败用完最大尝试次数。"""


# 总预算耗尽单独继承内置 TimeoutError，而不伪装成某次 HTTP I/O 失败；
# 上层若对全局超时有统一清理策略，可以和其他 TimeoutError 一起捕获。
class RetryBudgetExceeded(TimeoutError):
    """重试调用共享的绝对截止时间已经耗尽。"""


# `@dataclass` 会根据字段生成初始化、比较和展示方法，可类比减少 Java record/POJO 样板代码，
# 但字段注解默认仍不会像 Java 编译器那样阻止错误类型传入。
# frozen=True 禁止字段重新赋值，slots=True 则为实例声明固定槽位，不再给每个实例创建 __dict__。
@dataclass(frozen=True, slots=True)
class RetryPolicy:
    # max_attempts 包含第一次请求：默认总共最多发送 3 次，并非“首次 + 3 次重试”。
    max_attempts: int = 3
    # base_delay 是首次退避基数，随后按 2 的幂增长；max_delay 是单次退避的本地上限。
    base_delay: float = 0.01
    max_delay: float = 0.10
    # jitter_ratio 控制在指数退避上最多额外增加多大比例的正向随机抖动。
    jitter_ratio: float = 0.20

    # dataclass 生成的 __init__ 完成字段赋值后会自动调用 __post_init__。
    # frozen 只限制赋值，不会自动校验数值范围，所以不变量仍需在这里显式检查。
    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts 必须至少为 1")
        if self.base_delay < 0 or self.max_delay < 0:
            raise ValueError("退避时间不能为负数")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio 必须位于 0 到 1")


# 该实例不可变，因此可安全地作为默认策略共享；若 dataclass 可变，就应避免共享默认对象。
DEFAULT_RETRY_POLICY = RetryPolicy()


# 数据库线程返回与 cursor/connection 脱钩的普通值对象。事件循环拿到它后无需再访问 SQLite 资源。
@dataclass(frozen=True, slots=True)
class DatabaseDocument:
    id: int
    title: str
    score: float


def print_section(title: str) -> None:
    # f-string 中的表达式先求值：`'=' * 72` 生成分隔线，再与标题一起插入最终字符串。
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


# 协程不能直接调用阻塞式 Event.wait()，否则服务线程没发信号时，整个事件循环也无法调度。
async def wait_for_thread_event(event: threading.Event, description: str) -> None:
    """threading.Event.wait 是同步方法，因此在线程中等，并保留有限清理上限。"""
    # to_thread 的第一个参数是尚未调用的普通函数，后面的 1.0 才是传给 event.wait 的实参。
    # await 等待工作线程返回布尔值期间，事件循环仍能运行其他 Task。
    reached: bool = await asyncio.to_thread(event.wait, 1.0)
    if not reached:
        raise RuntimeError(f"等待超时：{description}")


# 该函数只处理已经拿到的 Response，内部没有网络等待点，所以是普通 def 而不是 async def。
# 返回路径严格经过 HTTP 状态、JSON 语法、Document 合同三关，便于定位失败属于哪一层。
def document_from_response(response: httpx.Response) -> Document:
    """按 HTTP -> JSON -> 模型三层检查；这些步骤本身都不是协程。"""
    # raise_for_status 只检查内存中的状态码；4xx/5xx 时抛 HTTPStatusError。
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        # `from error` 建立显式异常链：外层接口稳定，日志又不会丢失底层失败证据。
        raise HttpStatusFailure(f"HTTP 状态错误：{response.status_code}") from error

    # 普通 client.get 已把正文读入 Response；json() 在当前线程把 bytes 解码成 dict 等 Python 值。
    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        raise ResponseJsonFailure("响应正文不是合法 JSON") from error

    # 类型提示不会自动校验来自网络的 payload；Pydantic 在此真正执行字段、严格类型与范围规则。
    try:
        return Document.model_validate(payload)
    except ValidationError as error:
        raise ResponseValidationFailure("JSON 不符合 Document 模型") from error


# client 和 path/query 都由调用者提供：函数借用连接池发一次 GET，但不拥有也不关闭 client。
# 签名中的 `*` 表示它后面的 path 只能按名字传入，例如 path="/model"，不能作为第四个位置值。
async def fetch_document(
    client: httpx.AsyncClient,
    query: str,
    *,
    path: str = "/document",
) -> Document:
    """AsyncClient 由调用者拥有；工具函数只使用，不擅自关闭。"""
    # 类型提示 `query: str` 主要服务类型检查器，运行时仍需手动拒绝非字符串与纯空白字符串。
    # `or` 有短路行为：query 不是 str 时不会继续调用不存在的 strip()。
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 必须是非空字符串")
    try:
        # client.get 返回协程；只有 await 才真正等待连接、发送请求并读取非流式响应。
        # 相对 path 会与 client 的 base_url 合并，params 字典编码为 `?query=...`。
        response = await client.get(path, params={"query": query})
    except httpx.TransportError as error:
        # 这里只转换“尚未获得 HTTP 响应”的传输层异常；状态/JSON/模型错误留给下一函数分层。
        raise TransportFailure(f"HTTP 传输失败：{type(error).__name__}") from error
    return document_from_response(response)


# `str | None` 表示请求头可能缺失；返回值也可能是秒数或 None。
# now 是仅限关键字的可选参数，测试可传固定时刻，生产调用默认读取当前 UTC 时间。
def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    """解析 Retry-After 的 delay-seconds 或 HTTP-date 两种标准形态。"""
    if value is None:
        return None
    # 先去掉头值两端空白。纯十进制数字走 delay-seconds 快速路径，例如 "2" -> 2.0。
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        # 非纯数字时按 HTTP-date 解析；合法结果是 datetime，错误输入则返回 None 表示忽略该提示。
        retry_at = parsedate_to_datetime(value)
    # Python 3.14 允许多个异常类直接以逗号分隔，不再强制写成 `(TypeError, ValueError, ...)`；
    # 语义仍与 Java 的 multi-catch 相近，`as error` 只有需要读取异常对象时才必需。
    except TypeError, ValueError, OverflowError:
        return None
    # 少数旧日期可能没有时区信息；这里只补成 UTC，确保后续能和带时区的 current 相减。
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    # 过去日期不能形成负 sleep，max 把它收敛成“无需额外等待”的 0 秒。
    return max(0.0, (retry_at - current).total_seconds())


# 根据“第几次失败”计算下一轮等待。response 为 None 表示传输阶段失败、根本没有响应头可读。
def next_backoff(
    policy: RetryPolicy,
    failed_attempt: int,
    response: httpx.Response | None,
    randomizer: random.Random,
) -> float:
    """指数退避加正向 jitter，同时考虑服务端 Retry-After 提示。"""
    # 第 1、2、3 次失败对应 base_delay * 1、2、4；`**` 是乘方运算符。
    exponential = policy.base_delay * (2 ** (failed_attempt - 1))
    # random() 返回 [0.0, 1.0)；这里只加正向随机抖动，避免大量客户端同时重试。
    jitter = exponential * policy.jitter_ratio * randomizer.random()
    client_delay = exponential + jitter
    # 条件表达式只在确实拿到 Response 时读取 Retry-After，否则 server_delay 为 None。
    server_delay = retry_after_seconds(response.headers.get("Retry-After")) if response else None
    # server_delay or 0.0 把 None 和合法的 0 秒都转为 0；先尊重更长的一方，再受本地上限约束。
    requested_delay = max(client_delay, server_delay or 0.0)
    return min(requested_delay, policy.max_delay)


# 这是本章的核心重试状态机。client、limiter 和绝对 deadline 都由更外层所有者注入，
# 可选的 policy/randomizer 控制策略与可重复性，trace/Event 只暴露观察点而不改变成功结果。
# `*` 后的所有参数都必须写出名字，调用点不易把多个 float/策略对象按位置传错。
async def fetch_read_only_with_retry(
    client: httpx.AsyncClient,
    query: str,
    *,
    limiter: asyncio.Semaphore,
    deadline: float,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    randomizer: random.Random | None = None,
    trace: list[str] | None = None,
    backoff_started: asyncio.Event | None = None,
) -> Document:
    """在共同 deadline 内有限重试一个按合同只读的 GET。

    每次网络尝试才持有 Semaphore；退避前已经退出 async with，避免所有许可都被
    睡眠中的重试占住。模型/参数错误不会进入重试分支。
    """

    # 重试不会让永久性的参数错误自行恢复，因此在进入循环、消耗并发许可前先拒绝它。
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 必须是非空字符串，参数错误不会重试")

    # loop.time() 是单调时钟，不受系统墙上时间校准影响；传入的 deadline 必须来自同一时钟。
    loop = asyncio.get_running_loop()
    # 调用者可注入固定随机源让实验可重复；未传时才创建独立 Random 实例。
    rng = randomizer or random.Random()
    # None 表示调用者不关心轨迹，此时创建新列表。不能写 trace or []，因为传入的空列表
    # 本来就是要由函数原地追加并让调用者观察的对象，按真值选择会错误地换掉它。
    events = trace if trace is not None else []
    last_failure = "尚未尝试"

    # range 的停止值不包含在结果内，所以这里生成 1..max_attempts，attempt 可直接用于日志。
    for attempt in range(1, policy.max_attempts + 1):
        # deadline 是一次逻辑调用共享的绝对时刻；每轮只重算剩余量，不重新发一份完整预算。
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise RetryBudgetExceeded("发送下一次尝试前，整体预算已耗尽")

        # 每轮先假定还没拿到响应。若 client.get 抛传输错误，后续退避不能读取响应头。
        response: httpx.Response | None = None
        retryable_failure = False
        try:
            # timeout_at 超时会先取消当前 Task；离开上下文时将这次内部取消转换成 TimeoutError。
            # limiter 只包一次在途传输，退出 async with 时即使异常/取消也会自动归还许可。
            async with asyncio.timeout_at(deadline):
                async with limiter:
                    events.append(f"尝试 {attempt}：获取许可，剩余预算 {remaining:.3f}s")
                    response = await client.get("/document", params={"query": query})
        except TimeoutError as error:
            # 这里只捕获本层 timeout_at 转出的 TimeoutError；外部 Task.cancel() 产生的
            # CancelledError 不属于该分支，会原样向上传播。
            raise RetryBudgetExceeded("一次传输等待越过了整体截止时间") from error
        except RETRYABLE_TRANSPORT_ERRORS as error:
            # 白名单中的异常说明没有可用响应，但只要次数和预算允许，仍可退避后再尝试。
            retryable_failure = True
            last_failure = type(error).__name__
            events.append(f"尝试 {attempt}：可重试传输失败 {last_failure}")
        else:
            # `else` 只在 try 没抛异常时执行，此时 response 已由 client.get 绑定。
            if response.status_code in RETRYABLE_STATUS_CODES:
                retryable_failure = True
                last_failure = f"HTTP {response.status_code}"
                events.append(f"尝试 {attempt}：可重试状态 {response.status_code}")
            else:
                # 400、JSON 错误、模型错误都在这里立即抛出，不会被重试循环吞掉。
                # document_from_response 若返回，得到的已是校验后的 Document；可立即结束整个函数。
                return document_from_response(response)

        # 当前实现所有正常落到此处的路径都已经标记“可重试”；断言用于尽早暴露状态机维护错误。
        if not retryable_failure:
            raise AssertionError("重试状态机进入了不可能的分支")
        # 最后一次失败不再计算/等待退避，直接把最后失败类别写入稳定异常。
        if attempt == policy.max_attempts:
            raise RetryExhausted(f"{policy.max_attempts} 次尝试后仍失败；最后失败={last_failure}")

        # 把这次 Response 交给退避函数后，429/503 的 Retry-After 才可能影响等待时间。
        delay = next_backoff(policy, attempt, response, rng)
        remaining = deadline - loop.time()
        # `>=` 也拒绝：若 sleep 恰好完结时刻，已经没有预算发送下一次请求。
        if delay >= remaining:
            raise RetryBudgetExceeded(
                f"剩余预算 {max(remaining, 0.0):.3f}s 不足以完成 {delay:.3f}s 退避"
            )

        # 此处已经离开 limiter 上下文。sleep 本身是可取消的等待点。
        events.append(f"尝试 {attempt}：已释放许可，退避 {delay:.3f}s")
        # 可选 Event 只用于测试精确知道任务已进入退避，不靠固定 sleep 猜调度时机。
        if backoff_started is not None:
            backoff_started.set()
        try:
            # 退避也受同一个 deadline 约束；await sleep 会把控制权交还事件循环，且可被取消。
            async with asyncio.timeout_at(deadline):
                await asyncio.sleep(delay)
        except TimeoutError as error:
            raise RetryBudgetExceeded("退避期间整体预算耗尽") from error
    # 上面的 return/raise 覆盖有限循环的所有出口；保留断言可防止以后改状态机时静默返回 None。
    raise AssertionError("有限 for 循环不应从末尾落出")


# 第一节使用真实回环服务验证客户端生命周期、连接复用、两类 timeout 和流关闭，
# 再用进程内 MockTransport 精确制造四层错误。fixture 由更外层 run_examples 创建并拥有。
async def demonstrate_httpx(fixture: LocalHttpFixture) -> None:
    print_section("第一节：AsyncClient、连接池、分层错误与 JSONL 流")
    # state 是共享状态对象的另一个名字，并没有复制；客户端协程和 fixture 服务线程用其中的
    # Event 协调先后，用请求记录观察服务端真正收到的连接。
    state = fixture.state

    # AsyncClient 是异步上下文管理器：进入后可复用内部连接池，离开时自动 await 关闭资源。
    # base_url 让下方只写相对路径；trust_env=False 避免本机代理环境变量干扰回环实验。
    async with httpx.AsyncClient(
        base_url=fixture.base_url,
        timeout=HTTP_TIMEOUT,
        limits=HTTP_LIMITS,
        trust_env=False,
    ) as client:
        # fixture 可能已经服务过其他小节。先记当前长度，之后切片只分析本节新增请求。
        record_start = len(state.records_snapshot())

        # 第一条请求拆开写，便于后面展示 Response 的同步解析；第二条走可复用工具函数。
        response = await client.get("/document", params={"query": "HTTPX"})
        first = document_from_response(response)
        second = await fetch_document(client, "MCP")

        # inspect 检查函数实现是否声明为协程函数：client.get 需要 await，response.json 不需要。
        # 最后一行读取的是两个通过 Pydantic 校验的 Document.title 字段。
        print("1) client.get 是协程方法:", inspect.iscoroutinefunction(client.get))
        print("   response.json 是普通方法:", not inspect.iscoroutinefunction(response.json))
        print("   两次模型结果:", first.title, "|", second.title)

        # 切片 [record_start:] 取起点到末尾的新列表；列表推导式再只保留 /document 请求。
        # 推导式可类比一段紧凑的 Java Stream filter，但过长时普通 for 往往更清楚。
        document_records = [
            record
            for record in state.records_snapshot()[record_start:]
            if record.path == "/document"
        ]
        # 每条服务端记录保存客户端源端口；两次端口相同是本次受控实验复用 TCP 连接的证据。
        ports = [record.client_port for record in document_records]
        print("   服务端观察到的客户端端口:", ports)
        print("   本次本地实验复用了连接:", len(ports) == 2 and ports[0] == ports[1])
        # assert 记录教学实验必须满足的不变量；若连接没有按预期复用，立即停止而非只打印 False。
        assert len(ports) == 2 and ports[0] == ports[1]

        # 打印配置是为了把阶段 timeout 与池容量放在一起观察；它们都不是整个操作的 deadline。
        print("2) timeout:", HTTP_TIMEOUT)
        print("   limits: max_connections=1, max_keepalive_connections=1")

        # create_task 立即把慢请求注册进事件循环并返回 Task；此处先不 await 它，
        # 才能同时等待服务端 Event，确定请求已到达后再观察 read timeout。
        slow_task = asyncio.create_task(client.get("/slow"), name="read-timeout-demo")
        await wait_for_thread_event(state.slow_request_started, "服务端收到慢请求")
        try:
            await slow_task
        except httpx.ReadTimeout as error:
            # ReadTimeout 表示在 read 阶段等待下一块数据过久，不代表总 deadline 或连接池等待。
            print("   慢响应结果:", type(error).__name__, "(read 阶段无新数据)")
        else:
            # try 没抛异常才走 else；慢端点若意外成功，实验条件已失效，因此主动报错。
            raise AssertionError("慢响应应触发 ReadTimeout")
        finally:
            # finally 无论成功、预期异常还是断言都执行，放行服务线程，避免它永久卡在 Event.wait。
            state.release_slow_response.set()
        # 客户端超时只结束客户端等待；服务线程是独立执行单元，还需确认其清理完成。
        await wait_for_thread_event(state.slow_request_finished, "慢请求服务线程清理")

        # 上一个慢请求已结束后才能重置三个 Event，给池耗尽实验建立全新的同步点。
        state.reset_slow_exchange()
        holder_timeout = httpx.Timeout(connect=0.2, read=1.0, write=0.2, pool=0.03)
        holder = asyncio.create_task(
            client.get("/slow", timeout=holder_timeout),
            name="pool-holder",
        )
        await wait_for_thread_event(state.slow_request_started, "首个请求占用唯一连接")
        try:
            # HTTP_LIMITS 只有一条连接且已被 holder 占用；第二个请求最多等待 pool=0.03 秒。
            await client.get("/document", params={"query": "等待连接池"})
        except httpx.PoolTimeout as error:
            print("   连接池耗尽结果:", type(error).__name__, "(等待池中连接超时)")
        else:
            raise AssertionError("唯一连接被占用时，第二个请求应触发 PoolTimeout")
        finally:
            state.release_slow_response.set()
        holder_response = await holder
        assert holder_response.status_code == HTTPStatus.OK

        # 先在外层保留 Response 引用，才能对比 stream 上下文内外的 is_closed 状态。
        # `T | None` 说明进入上下文前还没有响应对象，也是静态检查器要求下方先判空的原因。
        streamed_response: httpx.Response | None = None
        # client.stream 不预先读完整正文；上下文拥有这次 Response，并在退出时异步关闭它。
        async with client.stream("GET", "/events") as stream_response:
            streamed_response = stream_response
            stream_response.raise_for_status()
            # aiter_lines() 以协议行作为消费边界。async for 每次可能等待网络数据，
            # 但仍是逐行串行消费，不会因为写了 async 自动并行处理多行。
            async for line in stream_response.aiter_lines():
                # line 已是不含换行符的 str；json.loads 每次把一条完整 JSONL 记录解析成 dict。
                event = json.loads(line)
                print("3) 收到一条完整 JSONL 事件:", event)
                # 刻意只消费第一行，验证提前停止时外层 stream 上下文仍负责关闭响应。
                break
            assert state.first_stream_line_sent.is_set()
            print("   stream 上下文内 response.is_closed:", stream_response.is_closed)
        # 离开 async with 后 Response 应已关闭。and 会短路，None 时不会访问 .is_closed。
        assert streamed_response is not None and streamed_response.is_closed
        print("   提前退出后 response.is_closed:", streamed_response.is_closed)

        # 客户端已经关闭响应流，现在才允许服务端尝试发送尾部，并等待其处理预期断连后退出。
        state.release_stream_tail.set()
        await wait_for_thread_event(state.stream_request_finished, "流服务线程清理")

    # 这里已经离开 AsyncClient 的 async with 但变量仍可引用对象；is_closed 用于验证资源状态。
    print("4) 客户端作用域退出后 client.is_closed:", client.is_closed)

    # MockTransport handler 是普通同步函数：每次请求按 path 返回指定 Response 或抛指定异常。
    # 它不进行 DNS/TCP，也不能用来证明连接池和真实网络 timeout 行为。
    def layered_handler(request: httpx.Request) -> httpx.Response:
        # match/case 是结构化模式匹配；这里对字符串值分支，作用近似 Java 的 switch expression。
        match request.url.path:
            case "/transport":
                raise httpx.ConnectError("受控连接失败", request=request)
            case "/status":
                return httpx.Response(404, json={"detail": "missing"})
            case "/json":
                return httpx.Response(200, content=b"not-json")
            case "/model":
                return httpx.Response(
                    200,
                    json={"id": "25", "title": "错误类型", "score": 0.95},
                )
            case _:
                # `_` 是兜底通配模式，任何未列出的路径都会来到这里。
                raise AssertionError(f"未预期的 mock 路径：{request.url.path}")

    print("5) MockTransport 分层错误（不经过 DNS/TCP/连接池）:")
    # transport 注入进程内 handler；base_url 仍是必需的，因为 fetch_document 发送相对路径。
    async with httpx.AsyncClient(
        base_url="https://mock.invalid",
        transport=httpx.MockTransport(layered_handler),
    ) as mock_client:
        # 每个元素都是（请求路径，预期应用异常类）二元组。
        # Sequence[...] 是只读使用方式的类型合同，内层 type[Exception] 表示保存的是异常类本身，
        # 不是某个异常实例；外层实际值选 tuple，使测试用例在运行中不会被追加或删除。
        cases: Sequence[tuple[str, type[Exception]]] = (
            ("/transport", TransportFailure),
            ("/status", HttpStatusFailure),
            ("/json", ResponseJsonFailure),
            ("/model", ResponseValidationFailure),
        )
        # for 将每个二元组按位置解包为 path 和 expected_type，无需手写 case[0]/case[1]。
        for path, expected_type in cases:
            try:
                await fetch_document(mock_client, "demo", path=path)
            # except 后也可以放运行时变量，只要它的值是异常类或异常类元组。
            except expected_type as error:
                # 前面用 raise ... from ... 保留了底层异常；此处读取 __cause__ 验证异常分层未丢证据。
                cause_name = type(error.__cause__).__name__
                print(f"   {path:<10} -> {type(error).__name__}, cause={cause_name}")
            else:
                raise AssertionError(f"{path} 应抛 {expected_type.__name__}")


# 第二节全部使用 MockTransport，让“第几次发生哪种失败”完全确定；它验证的是重试状态机，
# 不是实际网络阶段。五组场景依次覆盖：暂时失败后成功、次数耗尽、预算不足、退避中取消，
# 以及不可重试状态立即失败。
async def demonstrate_retries() -> None:
    print_section("第二节：有限重试、共同预算与取消")

    # 整数不可变；内层 handler 若要重新绑定外层函数的局部变量，必须用 nonlocal 声明。
    transient_attempts = 0

    def transient_handler(request: httpx.Request) -> httpx.Response:
        nonlocal transient_attempts
        transient_attempts += 1
        # 同一个 handler 按调用次数制造“连接失败 -> 503 -> 200”，使成功必然发生在第三次。
        if transient_attempts == 1:
            raise httpx.ConnectError("第一次连接失败", request=request)
        if transient_attempts == 2:
            return httpx.Response(503, headers={"Retry-After": "0"})
        return httpx.Response(
            200,
            json={"id": 25, "title": "第三次成功", "score": 0.99},
        )

    # trace 是可变列表，会被重试函数原地追加；调用结束后本函数仍可看到同一个列表中的事件。
    trace: list[str] = []
    # 容量 1 代表同一时刻最多一次网络尝试；每次退避时许可会释放。
    limiter = asyncio.Semaphore(1)
    async with httpx.AsyncClient(
        base_url="https://mock.invalid",
        transport=httpx.MockTransport(transient_handler),
    ) as client:
        # deadline 只在逻辑调用入口计算一次。固定种子 Random 让 jitter 序列可重复，
        # 但示例只依赖次数和上限，不把某个随机小数当作业务合同。
        document = await fetch_read_only_with_retry(
            client,
            "read-only",
            limiter=limiter,
            deadline=asyncio.get_running_loop().time() + 1.0,
            policy=RetryPolicy(base_delay=0.001, max_delay=0.005, jitter_ratio=0.25),
            randomizer=random.Random(25),
            trace=trace,
        )
    # client 已关闭，Document 和 trace 是与连接资源脱钩的普通 Python 对象，仍可安全读取。
    print("1) 暂时失败后成功:", document.title, f"attempts={transient_attempts}")
    # 每个 event 是一条 str；print 的多个参数默认用空格分隔。
    for event in trace:
        print("   ", event)
    assert transient_attempts == 3

    # 第二个 handler 永远返回白名单状态 503，用计数器验证 max_attempts 指总尝试数。
    always_attempts = 0

    def always_fails(request: httpx.Request) -> httpx.Response:
        nonlocal always_attempts
        always_attempts += 1
        return httpx.Response(503, headers={"Retry-After": "0"}, request=request)

    async with httpx.AsyncClient(
        base_url="https://mock.invalid",
        transport=httpx.MockTransport(always_fails),
    ) as client:
        try:
            # 退避参数全为 0，让实验无需真实等待；有限次数规则仍照常执行。
            await fetch_read_only_with_retry(
                client,
                "always-fail",
                limiter=asyncio.Semaphore(1),
                deadline=asyncio.get_running_loop().time() + 1.0,
                policy=RetryPolicy(base_delay=0, max_delay=0, jitter_ratio=0),
            )
        except RetryExhausted as error:
            # f-string 只负责把实际计数嵌入输出；下一条 assert 才正式验证次数。
            print("2) 一直失败:", error, f"actual_attempts={always_attempts}")
        else:
            raise AssertionError("一直返回 503 应耗尽重试")
    assert always_attempts == 3

    # 第三个 handler 要求等待 2 秒，但调用总预算只有约 0.05 秒。
    # 预期在第一次响应后立刻拒绝退避，不发送第二次请求。
    budget_attempts = 0

    def asks_for_long_wait(request: httpx.Request) -> httpx.Response:
        nonlocal budget_attempts
        budget_attempts += 1
        return httpx.Response(503, headers={"Retry-After": "2"}, request=request)

    async with httpx.AsyncClient(
        base_url="https://mock.invalid",
        transport=httpx.MockTransport(asks_for_long_wait),
    ) as client:
        try:
            await fetch_read_only_with_retry(
                client,
                "budget",
                limiter=asyncio.Semaphore(1),
                deadline=asyncio.get_running_loop().time() + 0.05,
                policy=RetryPolicy(base_delay=0.01, max_delay=2.0, jitter_ratio=0),
            )
        except RetryBudgetExceeded as error:
            print("3) 剩余预算不足:", error, f"attempts={budget_attempts}")
        else:
            raise AssertionError("Retry-After 大于剩余预算时不应再睡眠重试")
    assert budget_attempts == 1

    # 第四个 handler 要求长退避，给外层留下稳定窗口：先确认许可已释放，再主动取消 Task。
    cancelled_attempts = 0

    def waits_in_backoff(request: httpx.Request) -> httpx.Response:
        nonlocal cancelled_attempts
        cancelled_attempts += 1
        return httpx.Response(503, headers={"Retry-After": "10"}, request=request)

    # asyncio.Event 只在同一事件循环内协调协程；这里无需跨线程，所以不用 threading.Event。
    backoff_started = asyncio.Event()
    cancellation_limiter = asyncio.Semaphore(1)
    async with httpx.AsyncClient(
        base_url="https://mock.invalid",
        transport=httpx.MockTransport(waits_in_backoff),
    ) as client:
        # create_task 让重试协程与当前演示协程并发推进；name 只用于调试和任务观测。
        retry_task = asyncio.create_task(
            fetch_read_only_with_retry(
                client,
                "cancel-me",
                limiter=cancellation_limiter,
                deadline=asyncio.get_running_loop().time() + 20.0,
                policy=RetryPolicy(base_delay=1, max_delay=10, jitter_ratio=0),
                backoff_started=backoff_started,
            ),
            name="cancel-during-backoff",
        )
        # handler 返回 503 后，重试函数释放 Semaphore、设置 Event 并进入 sleep，才会唤醒这里。
        await backoff_started.wait()

        # 若退避仍占着唯一许可，这里会超时。成功获取是释放时机的运行证据。
        async with asyncio.timeout(0.2):
            await cancellation_limiter.acquire()
        # 手动 acquire 必须配对 release；Semaphore 不是线程锁，没有 with 块替我们自动释放这一次。
        cancellation_limiter.release()
        permit_was_free = True

        # cancel() 只是提出取消请求；必须继续 await Task，才能观察它最终传播的 CancelledError。
        retry_task.cancel()
        try:
            await retry_task
        except asyncio.CancelledError:
            # 重试函数没有把外部取消改写成预算或重试失败；这也是协作取消必须保留的语义。
            print(
                "4) 退避中整体取消：CancelledError",
                f"attempts={cancelled_attempts}, 退避未占许可={permit_was_free}",
            )
        else:
            raise AssertionError("取消退避任务后应继续传播 CancelledError")

    # 最后一组用 400 证明不可重试 HTTP 状态会直接经过分层校验，只发一次请求。
    non_retry_attempts = 0

    def bad_request(request: httpx.Request) -> httpx.Response:
        nonlocal non_retry_attempts
        non_retry_attempts += 1
        return httpx.Response(400, json={"detail": "bad query"}, request=request)

    async with httpx.AsyncClient(
        base_url="https://mock.invalid",
        transport=httpx.MockTransport(bad_request),
    ) as client:
        try:
            await fetch_read_only_with_retry(
                client,
                "invalid",
                limiter=asyncio.Semaphore(1),
                deadline=asyncio.get_running_loop().time() + 1.0,
            )
        except HttpStatusFailure as error:
            print("5) 参数/HTTP 400 不重试:", error, f"attempts={non_retry_attempts}")
        else:
            raise AssertionError("HTTP 400 应立即失败")
    assert non_retry_attempts == 1


# 这是一个极小协程：Task 一旦得到事件循环执行机会，就设置跨线程可见的 heartbeat。
# 它没有 await，因此开始运行后会一直执行到函数返回。
async def set_heartbeat(state: FixtureState) -> None:
    state.event_loop_heartbeat.set()


# create_database 是交给 to_thread 的同步工作单元：连接在调用它的工作线程中创建、使用并关闭。
def create_database(database_path: Path) -> None:
    """初始化也在工作线程完成；连接不会泄漏到事件循环线程。"""
    # sqlite3.Connection 自身的 with 只管理事务，不负责 close；closing 明确关闭连接。
    # 一个 with 可同时进入多个上下文：先由 closing 保证最终 close，再由 connection 管理事务。
    # 正常退出时事务提交；异常退出时回滚；最后无论哪条路径都会关闭连接。
    with closing(sqlite3.connect(database_path, timeout=0.5)) as connection, connection:
        # execute 创建表；数据库路径来自 TemporaryDirectory，不会覆盖固定业务数据库。
        connection.execute(
            "CREATE TABLE documents (id INTEGER PRIMARY KEY, title TEXT, score REAL)"
        )
        # executemany 对三个参数元组重复执行同一条参数化 SQL。`?` 由 sqlite3 绑定，
        # 不要用 f-string 拼接外部值，否则会混淆 SQL 与数据并引入注入风险。
        connection.executemany(
            "INSERT INTO documents VALUES (?, ?, ?)",
            (
                (1, "asyncio 取消", 0.92),
                (2, "HTTPX 连接池", 0.97),
                (3, "普通同步代码", 0.61),
            ),
        )


# 查询也封装成完整同步工作单元。返回值是新建 dataclass 列表，不携带连接或 cursor。
def query_database(database_path: Path, minimum_score: float) -> list[DatabaseDocument]:
    """完整查询在线程内创建、使用并关闭同一条 sqlite3 连接。"""
    with closing(sqlite3.connect(database_path, timeout=0.5)) as connection:
        # fetchall 在关闭连接前把全部结果物化为行列表，不把惰性 cursor 带回事件循环线程。
        rows = connection.execute(
            "SELECT id, title, score FROM documents WHERE score >= ? ORDER BY score DESC",
            (minimum_score,),
        ).fetchall()
    # 列表推导式逐行构造与数据库资源脱钩的值对象，并显式归一化 SQLite 返回的基础类型。
    return [DatabaseDocument(int(row[0]), str(row[1]), float(row[2])) for row in rows]


# 这个普通函数模拟阻塞式 SDK：started/finished 暴露线程生命周期，may_finish 是合作式放行闸门，
# side_effects 则证明即使等待它的 asyncio Task 已取消，底层线程仍可能产生可见副作用。
def lingering_sync_work(
    started: threading.Event,
    may_finish: threading.Event,
    finished: threading.Event,
    side_effects: list[str],
) -> None:
    """模拟无法由 Task.cancel() 强停的同步 SDK 调用。"""
    # threading.Event.set() 可从工作线程唤醒其他线程中的 wait()。
    started.set()
    try:
        # Event.wait 是同步阻塞，最多等 2 秒作为清理保险；返回 True 表示确实被 may_finish 放行。
        if may_finish.wait(timeout=2.0):
            side_effects.append("线程中的底层工作仍然完成")
    finally:
        # 不论正常放行还是函数异常，所有者都能通过 finished 判断线程函数已经离开。
        finished.set()


# 第三节先故意在 async 函数中直调 Requests，随后用 to_thread 正确适配；
# 再把同一边界应用到 SQLite，并观察“取消 asyncio Task 不会强停线程”。
async def demonstrate_sync_io(fixture: LocalHttpFixture) -> None:
    print_section("第三节：同步库、sqlite3 与 asyncio.to_thread")

    state = fixture.state
    # f-string 把 fixture 动态选择的本机端口插入 URL；结果是普通 str。
    url = f"{fixture.base_url}/sync-wait"

    # clear 建立“尚未心跳”的初始状态；create_task 只是把协程排入事件循环，
    # 只有当前协程让出控制权后，heartbeat Task 才真正有机会运行。
    state.event_loop_heartbeat.clear()
    direct_heartbeat = asyncio.create_task(set_heartbeat(state))

    # 此处没有 await；requests.get 从调用到返回独占当前线程。元组 timeout 分别表示连接和读取等待。
    direct_response = requests.get(url, timeout=(0.2, 1.0))
    # Requests 已缓冲响应正文，json() 是当前线程中的普通解析；close 明确归还其底层连接资源。
    direct_payload = direct_response.json()
    direct_response.close()
    # 直到 requests.get 返回后，事件循环才终于能运行刚才计划的 heartbeat Task。
    await direct_heartbeat
    print("1) async 内直接 requests，服务端及时看到 heartbeat:", direct_payload["heartbeat_seen"])
    # 服务端只等 0.25 秒；期间事件循环被同步请求堵住，所以观察值稳定为 False。
    assert direct_payload == {"heartbeat_seen": False}

    # 重置相同实验。这次工作线程负责阻塞式 HTTP，事件循环可调度 heartbeat Task。
    state.event_loop_heartbeat.clear()
    threaded_heartbeat = asyncio.create_task(set_heartbeat(state))
    # to_thread 接受普通同步函数以及它的位置/关键字参数；不要把调用结果传进去。
    # requests.get 在这里是函数对象，url 是位置参数，timeout 是关键字参数；调用发生在线程池中。
    threaded_response = await asyncio.to_thread(
        requests.get,
        url,
        timeout=(0.2, 1.0),
    )
    # await to_thread 只把同步等待移出了事件循环；返回的仍是普通 Requests Response，照常解析和关闭。
    threaded_payload = threaded_response.json()
    threaded_response.close()
    await threaded_heartbeat
    print(
        "   requests 放入 to_thread，服务端及时看到 heartbeat:",
        threaded_payload["heartbeat_seen"],
    )
    assert threaded_payload == {"heartbeat_seen": True}

    # TemporaryDirectory 是同步上下文管理器：进入时创建唯一临时目录，退出时递归清理其中的数据库。
    with tempfile.TemporaryDirectory(prefix="chapter25-") as temp_directory:
        # `/` 被 Path 重载为路径拼接，生成 Path 对象而不是做数值除法。
        database_path = Path(temp_directory) / "documents.sqlite3"
        # 传入的是函数对象和参数；两次工作各在线程中打开并关闭自己的 sqlite3 连接。
        await asyncio.to_thread(create_database, database_path)
        rows = await asyncio.to_thread(query_database, database_path, 0.90)
        print("2) sqlite3 线程内完整查询:", rows)
        # 推导式取出排序后的 id，验证 SQL 的值过滤和 `ORDER BY score DESC`。
        assert [row.id for row in rows] == [2, 1]

    # 三个线程 Event 把“已开始、允许结束、确实结束”分开，避免依赖碰运气的 sleep。
    started = threading.Event()
    may_finish = threading.Event()
    finished = threading.Event()
    side_effects: list[str] = []
    # to_thread 返回 coroutine；再用 create_task 包装后，才能稍后单独对等待方调用 cancel()。
    thread_task = asyncio.create_task(
        asyncio.to_thread(lingering_sync_work, started, may_finish, finished, side_effects),
        name="to-thread-cancellation-demo",
    )
    # 先确认工作线程已进入函数，避免在尚未提交/启动前就取消而得出错误结论。
    await wait_for_thread_event(started, "同步线程开始工作")
    # 取消的是 asyncio Task 的等待关系，不会向正在运行的普通 Python 线程注入异常。
    thread_task.cancel()
    try:
        await thread_task
    except asyncio.CancelledError:
        print("3) 等待 to_thread 的 Task 已取消；工作线程已结束:", finished.is_set())
    else:
        raise AssertionError("等待 to_thread 的 Task 应进入取消状态")
    # Task 已传播取消时，底层函数仍堵在 may_finish.wait，因此 finished 必须尚未设置。
    assert not finished.is_set()

    # 当前示例仍拥有底层工作的释放事件，所以必须放行并确认线程结束。
    may_finish.set()
    await wait_for_thread_event(finished, "取消后仍运行的同步线程结束")
    # list 在两个线程间共享；此处只在 finished 发信号后读取，观察底层工作最终追加的副作用。
    print("   稍后观察到底层结果:", side_effects)
    assert side_effects == ["线程中的底层工作仍然完成"]
    print("4) CPU 密集工作需另选策略；to_thread 主要用于避免阻塞事件循环。")


# 组合示例把前面概念放进一个请求作用域：一个绝对 deadline 约束整组工作，
# HTTP 分支持有共享客户端/并发许可，SQLite 分支在线程中持有自己的连接。
async def demonstrate_combined(fixture: LocalHttpFixture) -> None:
    print_section("组合示例：一个 deadline 下共享客户端、并发许可与数据库线程")

    loop = asyncio.get_running_loop()
    # 绝对截止时刻只计算一次，并同时传给外层 timeout_at 和内部 HTTP 重试状态机。
    deadline = loop.time() + 1.0
    limiter = asyncio.Semaphore(2)
    trace: list[str] = []

    # 上下文嵌套顺序也表达所有权：退出时先关最内层 AsyncClient，再清理最外层临时目录。
    with tempfile.TemporaryDirectory(prefix="chapter25-combined-") as temp_directory:
        database_path = Path(temp_directory) / "documents.sqlite3"
        await asyncio.to_thread(create_database, database_path)

        # 本节单独给客户端两条连接容量；client 的生命周期覆盖整个 HTTP 子任务。
        async with httpx.AsyncClient(
            base_url=fixture.base_url,
            timeout=HTTP_TIMEOUT,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
            trust_env=False,
        ) as client:
            # timeout_at 约束 TaskGroup 的整体等待。TaskGroup 是结构化并发作用域：
            # 离开前会等待所有子 Task；若一个子任务失败，会取消仍运行的兄弟并汇总异常。
            async with asyncio.timeout_at(deadline):
                async with asyncio.TaskGroup() as group:
                    # group.create_task 返回有类型结果的 Task，成功退出组后可用 result() 同步取值。
                    http_task = group.create_task(
                        fetch_read_only_with_retry(
                            client,
                            "组合查询",
                            limiter=limiter,
                            deadline=deadline,
                            trace=trace,
                        ),
                        name="combined-http",
                    )
                    # 这里传给 to_thread 的仍是普通函数和实参；数据库连接会在该工作线程内新建。
                    database_task = group.create_task(
                        asyncio.to_thread(query_database, database_path, 0.90),
                        name="combined-database",
                    )

            # 能执行到这里说明 TaskGroup 已正常结束，两个 result() 都不会再阻塞。
            remaining = max(0.0, deadline - loop.time())
            print("HTTP 结果:", http_task.result().title)
            print("数据库结果数:", len(database_task.result()))
            print("HTTP 尝试记录:", trace)
            print(f"退出组合工作后剩余预算: {remaining:.3f}s")

    # 该输出位于两层资源上下文之外，表示客户端和临时目录清理都已执行；
    # SQLite 连接更早已在各自的同步函数返回前关闭。
    print("组合示例已关闭客户端、数据库连接和临时目录。")


# section 是命令行解析出的普通字符串。retry 完全基于 MockTransport，所以无需启动本地服务；
# 其他小节共享同一个 fixture，上下文退出时统一关闭监听 socket 和服务线程。
async def run_examples(section: str) -> None:
    if section == "retry":
        await demonstrate_retries()
        # 提前 return，避免为 retry 分支创建从未使用的本地 HTTP 服务。
        return

    # LocalHttpFixture 是同步上下文管理器，因为其启动/关闭接口本身不是协程；
    # 内部服务跑在线程中；当前 asyncio 事件循环负责客户端侧实验。
    with LocalHttpFixture() as fixture:
        # 集合成员测试把 all 与单节入口合并，避免重复写调用代码。
        if section in {"all", "http"}:
            await demonstrate_httpx(fixture)
        if section == "all":
            await demonstrate_retries()
        if section in {"all", "sync"}:
            await demonstrate_sync_io(fixture)
        if section in {"all", "combined"}:
            await demonstrate_combined(fixture)

    # with 已退出但 fixture 变量仍可引用对象，因此可以读取 closed 标志验证清理状态。
    print("\n本地 HTTP 服务已关闭:", fixture.closed)


# argparse 把命令行文本转换成 Namespace；这里只定义参数规则，不运行任何异步代码。
def parse_args() -> argparse.Namespace:
    # 模块 docstring 作为 `--help` 的说明文本；这是文件原本已有的真实 __doc__。
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "section",
        # nargs="?" 表示位置参数可出现 0 或 1 次；省略时使用 default="all"。
        nargs="?",
        # choices 在进入业务逻辑前拒绝其他字符串，所以 run_examples 只需处理这五种值。
        choices=("all", "http", "retry", "sync", "combined"),
        default="all",
        help="选择单节运行；默认 all",
    )
    return parser.parse_args()


# 同步 main 负责解析 CLI，并用 asyncio.run 创建事件循环、运行顶层协程、最后关闭循环。
# asyncio.run 应位于程序边界；已经运行在事件循环中的库代码通常应直接 await run_examples。
def main() -> None:
    arguments = parse_args()
    asyncio.run(run_examples(arguments.section))


# Python 导入该文件时只定义上面的类和函数，不执行示例；直接运行脚本时 __name__ 才是
# "__main__"，于是调用 main。作用近似 Java 的程序入口约定，但由模块运行方式决定。
if __name__ == "__main__":
    main()
