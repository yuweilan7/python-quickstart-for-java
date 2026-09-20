# 第 24 章：异步生成器、流式消费与异步资源管理

本章最重要的一句话是：`async for` 解决“怎样等待下一项”，`async with` 解决“谁负责关闭资源”；可靠的流式程序必须同时把这两个问题写清楚。

LLM 回答、HTTP 响应、数据库游标和消息订阅都可能持续返回数据。消费者既可能自然读完，也可能在拿到足够信息后 `break`，还可能处理中途报错或被用户取消。流背后的连接、会话和后台 `Task` 不能靠垃圾回收碰运气，而要在所属作用域内确定性结束。

可以先把完整生命周期记成一条主线：

```text
获取异步会话
    ↓
创建流对象（异步生成器此时还没有执行函数体）
    ↓
逐项等待并消费应用事件
    ↓
自然耗尽 / break / 消费异常 / Task 取消
    ↓
关闭流 → 关闭会话 → 离开请求边界
```

学完本章，你应该能够：

- 展开 `async for` 背后的 `__aiter__()`、`__anext__()` 和 `StopAsyncIteration`；
- 区分 coroutine、async generator 及其正确消费方式；
- 解释 `async for` 为什么不会自动并行，以及直接拉取为什么自带背压；
- 区分网络分块、文本记录、JSON 对象、应用事件和结束信号；
- 使用 `async with`、`asynccontextmanager`、`aclosing` 和 `AsyncExitStack` 管理异步资源；
- 为自然结束、`break`、异常和取消分别指出清理责任人；
- 判断何时直接消费已经足够，何时才需要有界 `Queue` 和独立 producer `Task`。

本章只使用标准库和内存替身来讲语言协议与所有权。真实 HTTP 流、断线和连接池在第 25 章继续讨论，对外提供 FastAPI 流式接口则留到第 28 章。

## 24.1 先建立 Java → Python 心智模型

下面的对照只用于定位概念，不能把两边机械等同：

| Java 经验 | Python 对应入口 | 相似点 | 不能等同的地方 |
| --- | --- | --- | --- |
| `Iterator<T>` / 增强 `for` | `AsyncIterator[T]` / `async for` | 都是逐项请求，耗尽后结束 | Java `next()` 同步返回；下一项可能等待时 Python 要 `await` |
| `CompletableFuture<T>` | coroutine + `await` | 都表示未来的一次结果 | coroutine 调用后默认尚未交给 `Task`，并不会自动并发运行 |
| Java Stream API | async generator | 都能形成惰性数据管道 | Java Stream 主要是同步集合操作；async generator 允许每次 I/O |
| Reactive Streams `Publisher<T>` | async iterator | 都处理随时间到达的多项数据 | Reactive Streams 有订阅 / demand 协议；async iterator 是消费者主动拉取下一项 |
| `try-with-resources` | `async with` | 都把资源生命周期绑定到词法作用域 | `AutoCloseable.close()` 同步执行；`__aexit__()` 可以 `await` |
| 动态登记多个 `AutoCloseable` | `AsyncExitStack` | 都要逆序清理已成功获取的资源 | `AsyncExitStack` 能混合同步与异步退出回调 |
| 有界 `BlockingQueue` + executor | `asyncio.Queue` + `TaskGroup` | 都能解耦生产者与消费者并限制缓冲 | `asyncio.Queue` 服务于同一事件循环，不是线程安全队列 |

尤其不要被单词 stream 误导。Java Stream API、网络字节流、LLM 应用事件流和异步生成器是不同层次的概念。本章讨论的是“怎样异步逐项取得数据，以及怎样关闭承载这条数据流的资源”。

## 24.2 `async for` 到底怎样取得下一项

### 异步迭代协议

同步 iterator 的 `next()` 必须当场返回值；异步 iterator 的下一项可能还在网络上，因此 `__anext__()` 返回 awaitable，等待完成后才得到值。

| 操作 | 同步迭代 | 异步迭代 |
| --- | --- | --- |
| 获得 iterator | `iter(source)` | `aiter(source)` |
| iterable 协议 | `__iter__()` | `__aiter__()` |
| 取得下一项 | `next(iterator)` | `await anext(iterator)` |
| 下一项方法 | `__next__()` | `__anext__()` |
| 正常耗尽 | `StopIteration` | `StopAsyncIteration` |
| 循环语法 | `for` | `async for` |

一个最小异步 iterator 可以这样写：

```python
from collections.abc import AsyncIterator
from typing import Self


class AsyncCounter(AsyncIterator[int]):
    def __init__(self, stop: int) -> None:
        self.current = 0
        self.stop = stop

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> int:
        if self.current >= self.stop:
            raise StopAsyncIteration
        value = self.current
        self.current += 1
        return value
```

`__aiter__()` 直接返回异步 iterator，不能返回等待中的 coroutine；`__anext__()` 则必须返回 awaitable，所以通常写成 `async def`。真实实现会在 `__anext__()` 中等待 socket、数据库或消息队列，示例为了聚焦协议直接返回整数。

内置的 `aiter()` 和 `anext()` 可以手动探测这套协议：

```python
iterator = aiter(AsyncCounter(2))
first = await anext(iterator)                 # 0
second = await anext(iterator)                # 1
end = await anext(iterator, "<已结束>")       # 不抛异常，得到默认值
```

如果 `None` 本身也是合法事件，不要再用 `None` 表示结束。可以让 `StopAsyncIteration` 自然传播，或使用一个不会和业务值冲突的唯一哨兵。

### `async for` 的执行顺序

下面的循环：

```python
async for event in stream:
    await handle(event)
```

可以用这个核心过程理解：

```python
iterator = aiter(stream)
while True:
    try:
        event = await anext(iterator)
    except StopAsyncIteration:
        break
    else:
        await handle(event)
```

关键顺序是：先等待一项，完整执行一次循环体，再请求下一项。`async for` 中的 `async` 表示“取下一项允许等待”，不表示“每一项自动创建一个 `Task`”。因此它默认保持顺序，也不会自动并行处理多项。

下面这种写法既没有并发上限，也没有 `Task` 所有者：

```python
# 反例：快速拉完整条流，并遗留数量无界的后台 Task。
async for event in stream:
    asyncio.create_task(handle(event))
```

若事件必须有序，例如同一 `call_id` 的 `tool-result` 必须晚于 `tool-start`，直接 `await handle(event)` 最清楚。确实需要并发时，应先定义并发上限、结果顺序、失败策略和 `Task` 所有者，再使用 `TaskGroup` 等结构化并发工具。

## 24.3 coroutine 与 async generator 不是同一种对象

两者都用 `async def` 定义，但函数体中是否出现 `yield` 会改变函数种类：

```python
from collections.abc import AsyncIterator


async def get_answer() -> str:
    return "完整结果"


async def stream_answer() -> AsyncIterator[str]:
    yield "片段一"
    yield "片段二"
```

| 定义 | 调用结果 | 函数体何时开始 | 正确消费方式 |
| --- | --- | --- | --- |
| `async def`，没有 `yield` | coroutine 对象 | 被 `await`、创建为 `Task` 或交给 runner 时 | `answer = await get_answer()` |
| `async def`，包含 `yield` | async generator 对象 | 第一次 `anext()` 或进入 `async for` 时 | `async for part in stream_answer()` |

调用两种函数时都只是创建对象，并不会立刻执行函数体。coroutine 表示“一次最终结果”；async generator 表示“可以异步等待的多项结果”。因此不能直接 `await` 异步生成器：

```python
stream = stream_answer()
answer = await stream  # 类型错误：async generator 不是 awaitable
```

要等待的是“取得下一项”这个动作：

```python
stream = stream_answer()
first = await anext(stream)
await stream.aclose()
```

每次 `yield` 都会把一项交给当前消费者，同时保留局部变量和执行位置。下一次 `await anext(stream)` 才从 `yield` 后恢复。函数自然运行到底或执行空 `return` 后，下一次取值表现为 `StopAsyncIteration`。异步生成器不能用 `return value` 额外交付完成值，也不支持 `yield from`。

类型提示表达的是调用方能做什么：

- 参数只要求“能提供异步 iterator”时，使用 `AsyncIterable[Event]`；
- 返回值只承诺“可以逐项读取”时，使用 `AsyncIterator[Event]`；
- 当前代码拥有具体生成器、需要调用 `aclose()` 时，保留 `AsyncGenerator[Event]`。

## 24.4 网络分块不是应用事件

真实 LLM 流不会天然按 Python 对象边界到达。数据通常经过这些层次：

```text
任意大小的网络 bytes chunk
    ↓ 增量解码；一个 UTF-8 字符可能横跨两个 chunk
任意大小的文本片段
    ↓ 按 SSE、JSONL 等 framing 规则累计
完整文本记录
    ↓ JSON 解析和字段校验
JSON 值
    ↓ 按供应商协议转换
TokenEvent / ToolStartEvent / ToolResultEvent / DoneEvent
```

所以不能假设“一次网络读取恰好得到一个 UTF-8 字符串，也恰好得到一个 JSON 对象”：

```python
# 反例：chunk 可能切断 UTF-8 字符或 JSON，也可能同时包含多条记录。
event = json.loads(chunk.decode("utf-8"))
```

本章示例从“已经正确解码的文本片段”开始，演示 3 个文本片段最终组成 4 条 JSONL 事件。其中一条 JSON 横跨两个片段，另一个片段又包含两条 JSON。字节级增量解码和 HTTP framing 属于下一章。

还要分清四种“结束”：

| 信号 | 所属层次 | 它真正表示什么 |
| --- | --- | --- |
| `DoneEvent(reason="stop")` | LLM 应用协议 | 模型声明本次生成完成 |
| `StopAsyncIteration` | Python 迭代协议 | 这个 iterator 没有下一项 |
| `QueueEnd` | 进程内 producer/consumer 协议 | worker 不必再等待 `QueueItem` |
| 连接或会话关闭 | 资源生命周期 | 底层资源已经释放或归还 |

收到 `DoneEvent` 不等于连接已经关闭；连接断开时也可能根本收不到 `DoneEvent`。应用完成、流耗尽、worker 退出和资源关闭必须分别建模。

## 24.5 `async with` 管理“关闭也需要等待”的资源

同步上下文管理器通过 `__enter__()` / `__exit__()` 进入和退出。异步资源可能要等待建连、鉴权、发送关闭帧、回滚事务或归还异步连接池，因此使用 `__aenter__()` / `__aexit__()`：

```python
class AsyncSession:
    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()
        return False


async with AsyncSession() as session:
    await session.call()
```

`return False` 表示退出方法不吞掉块内异常。无论块正常结束、抛异常还是收到取消，Python 都会先等待 `__aexit__()`，然后再离开资源边界。

有一个容易漏掉的边界：如果 `__aenter__()` 自己失败，当前对象的 `__aexit__()` 不会被调用，因为它从未成功进入上下文。因此 `__aenter__()` 必须自行回滚已经完成的部分初始化。

选择 `with` 还是 `async with`，依据是资源协议，不是外围函数有没有 `async`：

- 获取和释放都能立即完成，使用 `with`；
- 获取或释放需要等待异步 I/O，使用 `async with`；
- 同步 SDK 内部仍会阻塞时，仅套一层 `async def` 或 `async with` 不会把它变成非阻塞 API。

### 用 `asynccontextmanager` 表达线性生命周期

如果资源生命周期就是“获取 → 借用 → 关闭”，不必专门写一个类：

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


@asynccontextmanager
async def open_session() -> AsyncIterator[Session]:
    session = Session()
    await session.open()
    try:
        yield session
    finally:
        await session.close()
```

装饰后的函数是异步上下文管理器工厂。进入 `async with` 时执行到 `yield`，把资源借给调用方；退出时从 `yield` 处恢复，并在离开边界前完成 `finally`。

一次上下文必须恰好 `yield` 一次。块内异常会被重新抛到暂停的 `yield` 位置；如果为了记录日志而捕获它，通常必须原样重新抛出：

```python
try:
    yield session
except BaseException as error:
    log(error)
    raise
finally:
    await session.close()
```

这里使用 `BaseException` 是为了说明取消也会经过这条路径。捕获 `CancelledError` 或 `BaseException` 后正常返回，会把用户取消伪装成成功。只需要清理而不需要记录时，直接使用 `try/finally` 更清楚。

## 24.6 为什么 `break` 后仍要关闭流

资源型异步生成器通常把清理放在 `finally`：

```python
async def read_events(session):
    try:
        async for event in session.incoming_events():
            yield event
    finally:
        await session.finish_stream()
```

消费者提前退出后，生成器可能仍暂停在 `yield`：

```python
stream = read_events(session)
async for event in stream:
    if enough(event):
        break
```

`async for` 只定义怎样取下一项和怎样识别耗尽。它不会为任意 async iterator 猜测并调用 `aclose()`，因为并非所有异步 iterator 都有这个方法。只要 `stream` 仍被引用，它的 `finally` 就可能尚未执行。

具体的 async generator 提供 `aclose()`。等待它会在生成器暂停处注入关闭信号，让 `finally` 有机会完成：

```python
try:
    async for event in stream:
        if enough(event):
            break
finally:
    await stream.aclose()
```

`contextlib.aclosing()` 把这条规则直接写进作用域：

```python
from contextlib import aclosing

async with aclosing(read_events(session)) as stream:
    async for event in stream:
        if enough(event):
            break
# 此处已经 await stream.aclose()
```

它的重要价值不是少写两行代码，而是让迭代和退出代码在同一个 `Task`、同一份上下文中执行。`asyncio.run()` 关闭事件循环时也会最终处理尚未完成的异步生成器，但那只是入口级安全网，不能替代请求作用域内的资源所有权。

### 四种退出路径的责任分工

| 退出路径 | 生成器可能处于什么状态 | 谁保证确定性关闭 |
| --- | --- | --- |
| 自然耗尽 | 运行到结尾，`finally` 已执行 | 生成器自然结束，外层管理器随后关闭会话 |
| 消费者 `break` | 仍暂停在最后一次 `yield` | 拥有者显式 `aclose()`，或使用 `aclosing()` |
| 循环体抛异常 | 异常离开消费区间，未必进入生成器 | 拥有者先关闭流，再让会话退出并传播原异常 |
| `Task` 被取消 | 取消可能落在取下一项、循环体或清理中的 `await` | `Task` 所有者等待完整清理，并继续传播取消 |

如果取消恰好发生在生成器的 `__anext__()` 内部，它会直接进入生成器并触发 `finally`。但取消也可能发生在循环体中，所以不能依赖“取消刚好落在生成器内部”来代替明确所有权。

## 24.7 把流和会话交给同一个所有者

对于“一个请求创建一条流，并独占其会话”的场景，推荐从这个结构开始：

```python
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager


@asynccontextmanager
async def owned_stream() -> AsyncIterator[AsyncGenerator[Event]]:
    async with open_session() as session:
        stream = read_events(session)
        async with aclosing(stream):
            yield stream
```

嵌套顺序表达了关闭顺序：先进入的 session 最后退出，后进入的 stream 最先关闭。消费者只借用 iterator，不承担猜测底层资源结构的责任：

```python
async def consume_request() -> None:
    async with owned_stream() as events:
        async for event in events:
            await handle(event)
            if isinstance(event, DoneEvent):
                break
```

无论消费者自然结束、`break`、抛异常还是收到取消，离开 `owned_stream()` 时都会先 `await stream.aclose()`，再等待 session 关闭，最后才把异常或取消交还外层。

可以用一句工程规则记住它：谁创建并独占资源，谁负责在自己的作用域内关闭；只借用资源的函数不要擅自关闭调用方还要复用的对象。

若第三方 SDK 已经返回同时管理响应与连接的原生异步上下文管理器，应优先遵循它的官方协议，不要根据方法名猜一套额外关闭逻辑。锁定版本不同，SDK 返回值可能是 `AsyncIterator`、上下文管理器，或两者的组合。

### 取消不是“丢掉 Task 引用”

`task.cancel()` 只是请求在某个可取消点抛出 `CancelledError`，不表示资源已经关完。`Task` 的所有者还要等待它：

```python
task.cancel()
try:
    await task
except asyncio.CancelledError:
    pass
```

当 `await task` 返回或抛出取消时，`Task` 内部的 `finally` 和上下文退出才已经走完。内部代码可以记录取消，但通常不应吞掉它，否则上层会把“用户停止”误判为正常完成。

## 24.8 直接拉取已经具有自然背压

异步生成器和消费者在同一个 `Task` 中直接连接时，运行过程是：

```text
consumer: await anext(stream)
generator: 准备并 yield event-1
consumer: 完整处理 event-1
consumer: 再次 await anext(stream)
generator: 才从上次 yield 后恢复并准备 event-2
```

消费者没有请求下一项时，上游就不会恢复生产，这就是拉取模型的自然背压。顺序处理、无需预取、单项处理足够快时，直接 `async for` 往往已经是最可靠的方案：没有额外 `Task`，没有缓冲区，也不需要单独设计 producer/consumer 的失败联动。

### 什么时候才加入有界 Queue

只有确实需要有限预取、独立 producer/consumer 等待，或把来源分发给 worker 时，才值得增加 `Queue`：

```text
producer Task：唯一拥有并消费底层 stream
    ↓ await queue.put(event)
Queue(maxsize=2)：最多缓冲 2 项
    ↓ await queue.get()
consumer Task：处理应用事件

共同的 TaskGroup：拥有并等待双方
```

这里有四条硬规则：

1. 使用 `maxsize > 0`。`asyncio.Queue(maxsize=0)` 表示无界，不是零容量；队列满时 `await put()` 才会把压力传回 producer。
2. producer 和 consumer 必须有共同所有者。只靠正常路径放结束哨兵不够：producer 失败时，consumer 可能永远等不到哨兵；consumer 失败时，producer 可能永远卡在满队列上。
3. 每次成功 `get()` 都要配对一次 `task_done()`，包括内部结束哨兵。`await queue.join()` 只检查 Queue 记账归零，不代表业务一定成功，也不会自动停止 worker。
4. `DoneEvent` 和 `QueueEnd` 分开建模。前者属于 LLM 协议，后者只用于本地 worker 生命周期。

最小的消费记账形状是：

```python
item = await queue.get()
try:
    if isinstance(item, QueueEnd):
        return
    await handle(item)
finally:
    queue.task_done()
```

本章示例把 producer 与 consumer 放进同一个 `TaskGroup`。正常路径使用内部哨兵；外部取消时，`TaskGroup` 取消并等待双方，producer 自己关闭流和会话，管道所有者明确丢弃未消费项目并完成 Queue 记账。这是第 23 章中的有界 Queue 与结构化并发在资源型流上的具体应用。

## 24.9 `AsyncExitStack` 管理运行时数量的资源

固定数量的资源直接嵌套最容易读：

```python
async with open_http_client() as http, open_mcp_session() as mcp:
    await use(http, mcp)
```

资源数量由配置或运行结果决定时，使用 `AsyncExitStack`：

```python
from contextlib import AsyncExitStack

async with AsyncExitStack() as stack:
    sessions = []
    for config in runtime_configs:
        session = await stack.enter_async_context(open_session(config))
        sessions.append(session)
    await use(sessions)
```

每次 `enter_async_context()` 成功，退出方法就被登记入栈。离开时按后进先出顺序关闭；后续某次获取失败时，已经成功进入的资源也会逆序清理。失败资源自己的 `__aenter__()` 尚未成功，所以仍要由它自行回滚部分初始化。

`AsyncExitStack` 还可以组合：

- `stack.enter_context(...)`：同步上下文管理器；
- `await stack.enter_async_context(...)`：异步上下文管理器；
- `stack.callback(...)`：同步清理回调；
- `stack.push_async_callback(...)`：异步清理回调。

异步栈没有可替代清理的同步 `close()`；要通过外层 `async with`，或显式 `await stack.aclose()` 驱动异步退出。

## 24.10 Java 工程师最容易踩的坑

| 误判或错误写法 | 实际表现 | 正确做法 |
| --- | --- | --- |
| `await stream_answer()` | async generator 不是 awaitable | `async for` 或 `await anext(...)` |
| `async for` 会并行处理每项 | 下一项要等当前循环体结束后才请求 | 需要并发时显式建立有上限、有所有者的 `Task` |
| `break` 会关闭任意 async iterator | 循环退出，但生成器可能仍暂停在 `yield` | 具体生成器使用 `aclose()` / `aclosing()` |
| 等 `asyncio.run()` 退出再清理 | 请求级资源可能被占用到进程入口结束 | 在创建流的局部作用域确定性关闭 |
| `asynccontextmanager` 记录异常后不 `raise` | 原异常或取消被误判为已经处理 | 只记录时原样重新抛出，清理放在 `finally` |
| 一个网络 chunk 直接 decode 再 `json.loads` | UTF-8、文本行或 JSON 都可能跨 chunk | 增量解码、累计 framing，再解析应用事件 |
| 把模型 done 当作 worker 哨兵 | 应用协议与本地生命周期耦合 | 单独定义 `QueueEnd` 或明确关闭策略 |
| `Queue()` 默认不能缓存 | `maxsize=0` 实际是无界 | 需要容量边界时使用正整数 |
| producer 正常结束时放哨兵就够了 | 任一方失败都可能让另一方永久等待 | 用 `TaskGroup` 联动失败、取消并等待收尾 |
| 调用 `task_done()` 表示业务成功 | 它只减少 Queue 的未完成项目计数 | 业务结果和异常要单独保存、传播 |
| `task.cancel()` 后立刻丢引用 | `finally` 可能仍在异步关闭资源 | cancel 后继续 `await Task` |
| `async with` 能修复同步阻塞 SDK | 语法不会改变内部调用方式 | 使用真正异步 API，或明确把同步工作移出事件循环 |

做方案选择时，可以从这张表开始：

| 需求 | 优先选择 |
| --- | --- |
| 下一项立即可得，不需要异步 I/O | `Iterator[T]` + `for` |
| 取得下一项可能等待 | `AsyncIterator[T]` + `async for` |
| 一次异步操作只返回一个最终值 | coroutine + `await` |
| 自己实现惰性的异步多项来源 | async generator |
| 具体 async generator 可能提前停止 | `aclosing()` |
| 获取或关闭资源需要等待 | `async with` |
| 线性的异步资源工厂 | `@asynccontextmanager` |
| 运行时数量的同步 / 异步资源 | `AsyncExitStack` |
| 顺序消费已经满足需求 | 直接 `async for` |
| 需要有限预取或独立生产 / 消费 `Task` | 有界 `Queue` + `TaskGroup` |

## 24.11 递进练习

建议把练习写在独立文件中，不要修改已经带断言的 `example.py`。

### 练习 1：手写协议并展开 `async for`

实现 `AsyncMessageSource`，依次产生 `"A"`、`"B"`，耗尽时抛 `StopAsyncIteration`。先手动调用 `aiter()` / `anext()`，再用 `async for` 消费，并写出两者之间的对应关系。

验收重点：`__aiter__()` 直接返回 iterator；第三次取值分别用异常和默认值两种方式验证；`async for` 得到相同结果。

### 练习 2：验证提前 `break` 的清理时机

写一个产出三个 token 的 async generator，把清理记录放在 `finally`。消费第二项后 `break`，分别观察不调用 `aclose()`、显式调用 `aclose()` 和使用 `aclosing()` 时的事件顺序。

验收重点：第三项没有生产；离开 `aclosing()` 前流仍在借用区间内，离开后 `finally` 已执行且只执行一次。

### 练习 3：覆盖四种退出路径

用 `@asynccontextmanager` 同时拥有一个 `FakeSession` 和异步生成器，分别运行自然结束、消费者 `break`、消费者抛异常、外部取消。

验收重点：四条路径都只关闭流一次、会话一次；流关闭早于会话；异常和 `CancelledError` 在清理完成后仍能到达外层。

### 练习 4：决定是否需要 Queue

先用直接 `async for` 证明慢消费者处理第一项时生成器不会生产第二项。再改成 `Queue(maxsize=2)` 和两个 `Task`，让 producer 在第三次 `put()` 上确定等待。

验收重点：producer 与 consumer 由同一个 `TaskGroup` 拥有；正常、消费者失败和外部取消都不遗留 `Task`；每次 `get()` 都有一次 `task_done()`；应用 `DoneEvent` 与内部结束哨兵不是同一对象。

## 24.12 本章小结与运行方式

只记住下面六条，本章的主线就不会丢：

1. coroutine 是一次异步结果，async generator 是可异步拉取的多项结果。
2. `async for` 反复执行 `await anext(...)`，默认顺序消费，不会自动并行。
3. 网络分块、应用事件、iterator 耗尽和资源关闭是不同层次的事实。
4. `break` 只退出循环；拥有具体 async generator 的代码要确定性 `aclose()`。
5. `async with` 管资源边界，`asynccontextmanager` 管线性生命周期，`AsyncExitStack` 管动态数量。
6. 直接拉取已有自然背压；增加 Queue 时，也必须增加容量、结束协议和 `Task` 所有权。

### 前置章节与环境

本章承接以下内容：

- 第 17 章：iterator、生成器与惰性生产；
- 第 18 章：`with`、`contextmanager` 与资源所有权；
- 第 19 章：coroutine、`await` 与事件循环；
- 第 20～22 章：`Task` 生命周期、取消与 `TaskGroup`；
- 第 23 章：Queue、背压与结构化并发。

根 `pyproject.toml` 要求 Python >= 3.14，Ruff 和 Pyright 也以 Python 3.14 为目标。本章只使用标准库，没有新增依赖。课件核对环境为：

| 工具 | 版本 | 用途 |
| --- | --- | --- |
| CPython | 3.14.7 | 运行完整示例与断言 |
| Ruff | 0.16.5 | 检查并格式化 Python 代码 |
| Pyright | 1.1.411 | basic 模式静态类型检查 |

`aiter()`、`anext()` 和 `aclosing()` 从 Python 3.10 可用，`TaskGroup` 从 3.11 可用；本章示例按项目的 3.14 约束编写，不承诺兼容更旧版本。

### 运行完整示例

本章文件只有两个：

| 文件 | 职责 |
| --- | --- |
| `README.md` | 概念、心智模型、所有权规则与练习 |
| `example.py` | 九组离线实验、中文生命周期轨迹与运行时断言 |

在仓库根目录运行：

```bash
.venv/bin/python -B java_to_python/chapters/24_async_streams_resources/example.py
```

`-B` 只是不生成 `.pyc`，不改变异步语义。`java_to_python/run_all.py` 目前只运行前 10 章，本章没有接入。

完整入口依次验证：

1. 手动 `aiter` / `anext`，以及 coroutine 与 async generator 的对象差异；
2. 3 个文本片段组成 4 个 JSONL 应用事件；
3. 直接 `async for` 的生产 / 消费交替与自然关闭；
4. `break`、显式 `aclose()` 和消费者异常的清理顺序；
5. 外部取消先关闭流和会话，再把 `CancelledError` 交还调用方；
6. `AsyncExitStack` 在后续获取失败时逆序清理已进入资源；
7. 慢消费者对直接拉取形成自然背压；
8. `Queue(maxsize=2)` 满时 producer 等待；
9. Queue 管道取消后 producer、consumer、stream、session 和 Queue 记账全部回收。

示例结束时应看到：

```text
入口结束前未回收的其他 Task 数：0
所有第 24 章示例与运行时断言均已完成。
```

项目中已配置的检查命令：

```bash
.venv/bin/ruff check java_to_python/chapters/24_async_streams_resources
.venv/bin/ruff format --check java_to_python/chapters/24_async_streams_resources
.venv/bin/pyright java_to_python/chapters/24_async_streams_resources/example.py
```

这些离线实验验证 Python 协议、清理顺序、取消传播、Queue 容量和 `Task` 所有权，不验证真实网络中的 DNS、TCP、TLS、HTTP framing、连接复用、SDK 事件 schema 或远端是否响应取消。接入真实客户端后，仍应根据锁定版本的官方文档和集成测试确认流边界与关闭行为。

继续核对语言细节时，可直接查阅：

- Python 3.14 异步迭代器数据模型；
- Python 3.14 `async for` 与 `async with`；
- `asynccontextmanager`、`aclosing` 与 `AsyncExitStack`；
- `asyncio.Queue`；
- `asyncio.TaskGroup`。
