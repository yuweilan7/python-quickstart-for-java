# 第 26 章：`contextvars`、日志与异步装饰器

## 先记住一句话

`ContextVar` 让同一个变量在不同异步执行上下文中拥有不同的绑定，适合保存 `request_id`、`run_id` 这类贯穿调用链的诊断信息；它不是普通全局变量，也不能简单等同于 Java 的 `ThreadLocal`。

一个 Agent 请求通常会同时调用 LLM、HTTP、数据库和多个 tool。多个请求又可能在同一个事件循环线程中交错运行。如果日志只写“tool 开始”和“tool 完成”，却不知道它们属于哪个请求，故障排查几乎只能靠猜。

本章要建立的是下面这条完整数据流：

```text
请求 Task 设置 request_id / run_id
        |
        +-- 创建 tool Task：复制创建点的 Context
        |
        +-- tool wrapper：临时设置 tool_name，并包住真正的 await
        |
        +-- logging.Filter：读取当前 Context，把字段写入 LogRecord

请求结束：finally 按相反顺序 reset，恢复进入请求前的 Context
```

学完后，你应该能解释：

- 为什么单线程事件循环中的普通全局 `request_id` 仍会串值；
- `ContextVar.get()`、`set()`、`Token`、`reset()` 各自负责什么；
- 子 Task 继承的是哪个时刻的 Context，父子后续修改为何互不回写；
- 为什么复制 Context 不等于深拷贝其中的可变对象；
- `asyncio.to_thread()` 在本章场景中何时取得 Context，为什么不能推广到所有线程入口；
- 什么适合放入隐式上下文，什么仍应通过参数或依赖对象明确传递；
- 怎样让日志和计时覆盖协程真正执行与等待 I/O 的全过程；
- 为什么异常和取消必须继续传播，async generator 又为什么需要单独的 wrapper。

## 1. Java → Python 心智模型

| Java 中熟悉的概念 | Python 中可以先想到 | 不能画等号的地方 |
|---|---|---|
| `static` 可变字段 | 模块级全局变量 | 两者都由并发调用共享，都不能隔离请求状态 |
| SLF4J MDC / `ThreadLocal` 中的关联 ID | `ContextVar` | MDC 常以物理线程为边界；`ContextVar` 的绑定属于当前 `Context`，`asyncio.Task` 原生支持复制与恢复 |
| 提交任务前复制 MDC | `asyncio.create_task()` 在创建点复制 Context | Python 只复制 Context 中的绑定映射，不深拷贝绑定的对象 |
| `try/finally` 恢复旧 MDC | `token = var.set(...)` 后在 `finally` 中 `var.reset(token)` | Token 记录精确的旧绑定，不能靠写死默认值代替 |
| 日志框架的 MDC 字段注入 | `logging.Filter` 为 `LogRecord` 补字段 | Python 标准库不自动规定字段、脱敏或跨进程传播，需要应用自己配置 |
| AOP `@Around` 拦截器 | 返回 wrapper 的函数装饰器 | Python 装饰器在定义时替换函数对象；堆叠顺序会改变注册表最终保存哪个对象 |
| `CompletableFuture` 的执行阶段计时 | `async def wrapper` 中 `await` 原调用 | 同步 wrapper 调用异步函数时只得到 coroutine，对业务函数体尚未开始执行 |
| 任务取消并向上报告 | `asyncio.CancelledError` 传播 | Python 取消是协作式的；捕获后通常应清理并重新抛出，不能当普通成功结果返回 |

最容易误判的是 `ThreadLocal` 类比。可以先把 `ContextVar` 理解成“异步代码可用的 MDC 数据来源”，但不能完全等同：

- `ThreadLocal` 绑定在工作线程上。线程池复用、响应式流切线程或 `CompletableFuture` 换 executor 时，通常需要框架或 wrapper 显式搬运 MDC。
- `ContextVar` 的值绑定在 `contextvars.Context` 上。`asyncio` 在创建 Task 时会复制当前 Context，因此多个 Task 即使运行在同一线程，也能读取各自的绑定。
- Python 的自动传播只覆盖 API 明确承诺的边界。它不会自动穿过任意线程池、子进程、消息队列或 HTTP 调用；跨服务仍要使用 trace header、消息元数据等显式协议。

真正要记住的不是“`ContextVar` 就是 Python 版 `ThreadLocal`”，而是：先确认当前代码跨越的是调用栈、Task、线程、进程还是网络，再确认那个边界的传播规则。

## 2. 为什么普通全局变量一定会串请求

事件循环线程在同一瞬间通常只执行一个 Task 的 Python 代码，但 Task 会在 `await` 处暂停，让其他 Task 继续运行：

```text
请求 A：GLOBAL_REQUEST_ID = "REQ-A" -- await I/O ------------> 再读取
请求 B：                         GLOBAL_REQUEST_ID = "REQ-B"
```

模块中只有一个 `GLOBAL_REQUEST_ID` 绑定。请求 A 暂停时，请求 B 把它改成了 `REQ-B`；A 恢复后读取的仍是模块当前值，不会神奇地“回到 A 自己的副本”。

这不是只有多线程才会出现的数据竞争，而是多个并发控制流共享可变状态产生的逻辑串扰：

```python
import asyncio

CURRENT_REQUEST = "-"


async def handle(request_id: str) -> str:
    global CURRENT_REQUEST
    CURRENT_REQUEST = request_id
    await asyncio.sleep(0)  # 当前 Task 可能在这里让出执行权
    return CURRENT_REQUEST
```

并发调用 `handle("A")` 和 `handle("B")` 时，两个结果可能都读到 B，也可能随调度顺序变化。单次测试“碰巧正确”不能证明设计安全。

用 Lock 保护一次赋值也解决不了问题：锁释放后，别的请求仍能在本请求下次读取前改值。若把整个请求生命周期都锁住，虽然不串值，却把所有请求强制串行，失去了异步并发的意义。

问题需要的不是“更小心地共享同一个值”，而是“让不同执行上下文拥有不同绑定”。

## 3. `ContextVar` 的生命周期：设置、使用、精确恢复

### 3.1 在模块顶层声明

```python
from contextvars import ContextVar

REQUEST_ID = ContextVar[str]("request_id", default="-")
RUN_ID = ContextVar[str]("run_id", default="-")
TOOL_NAME = ContextVar[str]("tool_name", default="-")
```

- `ContextVar[str]` 告诉静态检查器这里约定保存字符串；
- `"request_id"` 是变量的诊断名称；
- `default="-"` 表示当前 Context 没有显式绑定时，`get()` 返回 `"-"`；
- `REQUEST_ID` 变量对象可以全局共享，随 Context 变化的是“该对象当前绑定了什么值”。

官方建议在模块顶层创建 `ContextVar`。Context 会持有变量的强引用，不应为每次请求在闭包中反复创建新变量。

没有声明默认值的变量，在当前 Context 也没有绑定时，`get()` 会抛 `LookupError`。两种策略都合理：日志字段常使用安全占位符；必须由入口设置的安全上下文，则可以故意不提供默认值，让遗漏尽早暴露。

### 3.2 `set()` 返回的 Token 是恢复凭证

```python
token = REQUEST_ID.set("REQ-100")
try:
    await handle_request()
finally:
    REQUEST_ID.reset(token)
```

`set()` 不是修改一个进程级字段，而是在当前 Context 中建立新绑定，并返回一个 Token。Token 记住本次设置之前的精确状态，`reset(token)` 因而能恢复外层。

不要用下面的代码代替：

```python
# 反例：外层也许不是默认值，而是另一个真实请求或调用上下文。
finally:
    REQUEST_ID.set("-")
```

假设外层已经绑定 `REQ-OUTER`，内层临时绑定 `REQ-INNER`。写死 `"-"` 会破坏外层；用内层 Token reset 才会恢复 `REQ-OUTER`。这与 Java 代码在 `finally` 中恢复进入方法前的 MDC 值属于同一类生命周期问题，但 Token 已替你保存了“旧值”和“此前根本没有显式绑定”的差别。

### 3.3 嵌套绑定按栈的反方向退出

```python
outer = REQUEST_ID.set("REQ-OUTER")
try:
    inner = REQUEST_ID.set("REQ-INNER")
    try:
        assert REQUEST_ID.get() == "REQ-INNER"
    finally:
        REQUEST_ID.reset(inner)

    assert REQUEST_ID.get() == "REQ-OUTER"
finally:
    REQUEST_ID.reset(outer)
```

后 `set()` 的绑定先 `reset()`。一个 Token 只属于创建它的变量、Context 和那一次设置，不能重复 reset，也不能拿去恢复另一个 `ContextVar`。

`finally` 覆盖正常返回、普通异常和取消，所以请求入口、middleware、tool wrapper 都应该把恢复动作放在这里。

Python 3.14 起，Token 也可以作为同步上下文管理器：

```python
with REQUEST_ID.set("REQ-100"):
    await handle_request()
```

离开 `with` 时会自动恢复。它只是 `set/try/finally/reset` 的简写，不是另一套隔离机制；阅读旧版本代码和排查嵌套问题时，仍需理解完整生命周期。

## 4. Task 复制 Context 的时机

`asyncio.create_task(coro)` 默认会在调用 `create_task` 的时刻复制当前 Context，让新 Task 在副本中运行。不是等子协程第一次真正执行时才读取父 Task 的最新值。

```python
REQUEST_ID.set("REQ-BEFORE-CREATE")
child = asyncio.create_task(child_work())
REQUEST_ID.set("REQ-AFTER-CREATE")
```

结果是：

- 子 Task 开始时读取 `REQ-BEFORE-CREATE`；
- 父 Task 后来的 `set()` 只改变父 Task 当前 Context；
- 子 Task 自己再 `set()`，也不会反向更新父 Task；
- 父子若要交换业务结果，应使用返回值、Task 结果、Queue 或明确共享状态，不能期待 `ContextVar` 回写。

这和在线程池提交前拍一份 MDC 快照有相似之处，但 asyncio 的复制是 Task API 的原生语义。

请求入口的正确顺序通常是：

```python
request_token = REQUEST_ID.set(incoming_request_id)
try:
    llm_task = asyncio.create_task(call_llm())
    db_task = asyncio.create_task(call_database())
    return await asyncio.gather(llm_task, db_task)
finally:
    REQUEST_ID.reset(request_token)
```

若先创建 Task、再设置请求 Context，已创建的 Task 不会追溯继承新值。`TaskGroup.create_task()` 也应遵守同样的创建点思维。高级代码可以通过 `context=` 显式指定 Context，但普通请求处理通常不需要这样做。

### Context 副本不是对象深拷贝

复制的是 `ContextVar -> value` 这张绑定映射，不是递归复制 value 指向的 Python 对象：

```python
EVENTS = ContextVar[dict[str, list[str]]]("events")
bag = {"items": []}
EVENTS.set(bag)

first = asyncio.create_task(child_a())
second = asyncio.create_task(child_b())
```

两个子 Task 的 Context 都可能把 `EVENTS` 绑定到同一个 `bag`。如果一个子 Task 执行：

```python
EVENTS.get()["items"].append("changed")
```

它原地修改的是共享 dict 中的共享 list，并没有改变 `ContextVar` 绑定。父 Task 和兄弟 Task 都可能看到变化。`copy_context()` 也不会替你做 `deepcopy`。

因此 `ContextVar` 最适合保存短小、不可变的值，例如 request ID、run ID、租户诊断标签。若必须更新复合值，应构造新对象后重新 `set()`；若本来就需要共享可变状态，则应明确设计所有权、同步和消息传递，不要借 `ContextVar` 把它隐藏起来。

## 5. 跨线程时先看具体 API 的合同

### 5.1 `asyncio.to_thread()` 会传播当前 Context

`asyncio.to_thread(func, *args)` 会在线程中执行同步 callable，并传播当前 `contextvars.Context`。但要同时记住：调用 `to_thread()` 只创建 coroutine，它的函数体要在 coroutine 真正开始运行时才执行。

本章在 CPython 3.14.7 中验证了两种不同时间线。

只创建 coroutine，改值后再直接 await：

```python
REQUEST_ID.set("REQ-WHEN-CALLED")
deferred = asyncio.to_thread(read_request_id)

REQUEST_ID.set("REQ-WHEN-AWAITED")
result = await deferred
```

`to_thread` 在直接 await 时才开始运行并复制 Context，所以线程读到 `REQ-WHEN-AWAITED`。

先把 `to_thread` coroutine 创建成 Task：

```python
REQUEST_ID.set("REQ-WHEN-TASK-CREATED")
task = asyncio.create_task(asyncio.to_thread(read_request_id))

REQUEST_ID.set("REQ-PARENT-CHANGED-LATER")
result = await task
```

这里先发生 Task 创建点的 Context 复制。`to_thread` 以后在子 Task 的 Context 中开始，再把那份 Context 传播给工作线程，所以线程读到 `REQ-WHEN-TASK-CREATED`。

```text
父 Task 当前 Context
    | create_task 时复制
    v
承载 to_thread 的子 Task Context
    | to_thread 开始运行时复制，并在线程中 Context.run
    v
工作线程中的同步 callable
```

公开语义见 `asyncio.to_thread()`；具体复制位置可参考 CPython 3.14 `Lib/asyncio/threads.py`。

### 5.2 `run_in_executor()` 不会自动提供同样保证

不要把 `to_thread` 的规则推广成“Python 线程池都会继承 Context”。本章的新单线程池实验中：

```text
run_in_executor 直接提交，线程看到：-
显式 Context.run 提交，线程看到：REQ-EXECUTOR-CALLER
```

需要显式传播时，可以在提交前复制：

```python
context = contextvars.copy_context()
result = await loop.run_in_executor(executor, context.run, sync_function)
```

这很像 Java executor 中显式包装 Runnable/Callable 来复制和恢复 MDC。每次并发提交都应获得自己的 Context 副本；不要让同一个已进入的 Context 同时在多个线程运行。

`to_thread` 也只负责适配同步调用和传播 Context。取消等待它的 Task，并不会强行终止已经运行的线程。底层 timeout、副作用、线程安全和 executor 关闭责任仍要单独设计。

## 6. 哪些信息适合隐式传递

`ContextVar` 能避免把横切元数据穿过每一层函数签名，但“可以隐式传”不等于“都应该藏起来”。

| 信息 | 推荐方式 | 判断依据 |
|---|---|---|
| `request_id`、`run_id`、trace/span 关联 ID | `ContextVar` | 用于诊断关联，不决定核心业务结果 |
| 当前租户的日志标签 | 谨慎使用 `ContextVar` | 可辅助诊断；真正授权判断仍应经过清晰、可测试的安全边界 |
| 用户问题、tool 输入、SQL 参数 | 显式参数 | 它们决定函数做什么，应该从签名和调用点可见 |
| HTTP client、数据库 session、repository | 显式参数或有生命周期的对象字段 | 它们有容量、事务、关闭与测试替换责任 |
| API Key、访问令牌 | 依赖边界显式管理，禁止写日志 | 放进 Context 不会让凭据自动安全，反而可能扩大可见范围 |
| 可变步骤结果、消息列表 | 返回值、Queue 或有所有者的状态 | 需要明确数据流；Context 的浅复制不能提供隔离 |

一个实用问题是：去掉这个值后，函数只是少了诊断关联，还是连业务行为都无法决定？前者可能适合 ContextVar，后者通常应该显式传参。

这也解释了为什么 ContextVar 不是依赖注入容器。把 client、session 和业务输入全塞进去，虽然减少了参数，却隐藏了资源所有权和函数真正依赖，测试会比 Java 中滥用静态 Service Locator 同样困难。

## 7. 让每条日志在输出边界取得当前上下文

Python 标准库 `logging` 可以通过 Filter 在 `LogRecord` 进入 Handler 前补字段：

```python
import asyncio
import logging
import threading


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = REQUEST_ID.get()  # type: ignore[attr-defined]
        record.run_id = RUN_ID.get()  # type: ignore[attr-defined]
        record.tool_name = TOOL_NAME.get()  # type: ignore[attr-defined]
        try:
            task = asyncio.current_task()
        except RuntimeError:  # 当前工作线程没有事件循环
            task = None
        record.task_name = (  # type: ignore[attr-defined]
            task.get_name() if task else f"thread:{threading.current_thread().name}"
        )
        return True
```

Formatter 随后可以使用这些字段：

```python
logging.Formatter(
    "%(levelname)s request=%(request_id)s run=%(run_id)s "
    "task=%(task_name)s tool=%(tool_name)s | %(message)s"
)
```

职责关系类似“日志框架在输出时从 MDC 读取字段”，但这里的 MDC 数据源是当前 Context。Filter 不负责建立请求上下文；请求入口仍要先 `set()`，结束时仍要 `reset()`。

本章完整示例还给 Task 显式命名，并通过 wrapper 临时设置 `tool_name`。一条工具日志的字段来源是：

| 字段 | 产生位置 | 隔离原因 |
|---|---|---|
| `request_id` | 请求入口 | 每个请求 Task 有自己的 Context，工具 Task 在创建点复制 |
| `run_id` | 请求入口 | 与 `request_id` 使用相同生命周期 |
| `task_name` | `create_task(..., name=...)` | Filter 从当前 Task 读取，不依赖全局可变字段 |
| `tool_name` | 异步 wrapper 入口 | 每次工具调用在自己的 Context 中临时 set，并在 finally 恢复 |
| `elapsed_ms` | wrapper 的局部变量 | 每次调用有独立栈帧，计时包住完整 await |

输出类似：

```text
INFO request=REQ-100 run=RUN-ALPHA task=context:REQ-100:lookup:1 \
tool=lookup_documents | 工具调用成功 status=success elapsed_ms=0.134
```

日志要能关联，也要克制。通常适合记录：

- 经过审查的关联 ID、工具名、模型名和依赖名；
- `started`、`success`、`error`、`cancelled`、`retry` 等有限状态；
- 状态码、有界计数、耗时和异常类型；
- 按环境与安全要求审查后的 traceback。

通常不要直接记录：

- 完整 prompt、用户文档、HTTP 请求或响应正文；
- Authorization、Cookie、API Key、数据库密码；
- 未脱敏的个人信息；
- 任意 `args`、`kwargs` 的 `repr()`；
- 未审查的异常文本，因为 SDK 异常可能带 URL、正文或凭据。

日志平台、采样、trace 导出与脱敏管线不在本章范围内。这里解决的是字段从哪里来、并发时间归属是否正确，以及成功、失败、取消有没有被如实记录。

## 8. 异步装饰器必须包住真正的 `await`

### 8.1 同步 wrapper 只测到 coroutine 创建

下面的代码看起来包住了调用，实际并没有包住异步执行：

```python
def wrong(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        started = perf_counter_ns()
        coroutine = function(*args, **kwargs)
        report(perf_counter_ns() - started)
        return coroutine

    return wrapper
```

调用 `async def` 函数只创建 coroutine 对象。原函数体第一行还没有执行，网络、数据库和其他 I/O 更没有开始。这段 wrapper 测到的只是对象创建开销。

对应到 Java，可以类比为只测量“创建一个尚未启动的异步描述对象”，却没有测量 future 真正完成。异步耗时的终点必须和异步完成信号绑定。

### 8.2 wrapper 自己也要是 `async def`

本章使用 Python 3.14 支持的类型参数语法，保留原函数的参数关系和最终结果类型：

```python
def observe_async[**P, R](
    function: Callable[P, Awaitable[R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    @wraps(function)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        token = TOOL_NAME.set(function.__name__)
        started = perf_counter_ns()
        try:
            result = await function(*args, **kwargs)
        except asyncio.CancelledError:
            log_cancelled(perf_counter_ns() - started)
            raise
        except Exception:
            log_error(perf_counter_ns() - started)
            raise
        else:
            log_success(perf_counter_ns() - started)
            return result
        finally:
            TOOL_NAME.reset(token)

    return wrapper
```

计时起点在调用原函数之前，终点在 `await` 正常完成、抛异常或收到取消之后，所以测量的是这次调用的墙钟耗时，包含协程实际运行和等待 I/O 的时间。它不是 CPU 时间，也不应测试某次运行必须精确等于多少毫秒。

终态处理有三条硬规则：

1. success 放在 `try` 的 `else`，只有 `await` 真正返回才记录成功；
2. 普通异常可以记录，但随后裸 `raise`，调用方仍收到原异常；
3. `CancelledError` 单独记录为 cancelled，完成清理后也必须继续抛出。

`asyncio.CancelledError` 直接继承 `BaseException`。吞掉取消会让 timeout、TaskGroup 和请求中止误以为下游已经正常结束。

### 8.3 `wraps` 和参数规格解决的是两件事

`@wraps(function)` 保留函数名、docstring、注解和 `__wrapped__` 链，`inspect.signature()` 通常因此仍展示原始签名。它解决运行时元信息问题。

`P` 是参数规格，记录位置参数、仅位置参数、关键字参数、仅关键字参数和默认值之间的关系；`P.args`、`P.kwargs` 把这份关系传给 wrapper。它解决静态检查问题。

两者不能互相替代。可以把 `wraps` 类比成保留 Java 反射可见的方法元数据，把参数规格类比成泛型地保持函数式接口的输入输出关系，但 Python 装饰器仍是一个新的 callable 对象。

还要区分两种“返回类型”：

```python
async def lookup(query: str) -> Document: ...
```

这里的 `-> Document` 表示 coroutine 完成后交付的结果。调用 `lookup("python")` 当下得到的是 coroutine，不是 `Document`。

从装饰器接收“可调用对象”的角度描述，调用结果应写成：

```python
Callable[P, Awaitable[R]]
```

本章 wrapper 本身由 `async def` 定义，调用后具体得到 `Coroutine[Any, Any, R]`。不要把原异步函数定义误写成 `async def lookup(...) -> Awaitable[Document]`；那表示 await 第一层后还会得到一个 Awaitable，多出了一层协议。

## 9. 装饰器顺序决定注册表保存谁

Python 装饰器从下往上应用：

```python
@register_tool("lookup")
@observe_async
async def lookup_documents(...):
    ...
```

等价于：

```python
lookup_documents = register_tool("lookup")(observe_async(lookup_documents))
```

先创建观测 wrapper，再把 wrapper 注册。因此直接调用公开名字和从注册表动态调用，都会经过日志与计时层。

如果顺序反过来，注册器会先保存原函数，之后公开名字才被包装。从注册表取出的对象会绕过观测层。这与 Java 注解顺序本身不完全等价，更接近“哪个代理对象最终被容器登记”。判断时不要只看源码上有几个 `@`，要按从下到上的函数变换展开。

异构工具注册表是动态类型边界。字符串运行时才决定具体工具，普通 dict 无法让静态检查器从任意字符串恢复每个工具的精确签名。真实 Agent 还需要 tool schema、输入校验和结果校验；`ContextVar` 与装饰器不会替代这些业务边界。

## 10. async generator 不是“一次 await 得到最终结果”

本章 wrapper 的合同假定：调用被包装对象得到 Awaitable，`await` 一次得到最终 `R`。

```python
async def token_stream() -> AsyncGenerator[str]:
    yield "token-1"
    yield "token-2"
```

调用 `token_stream()` 得到 async generator 对象，它不可直接 await；消费者要使用 `async for`、`anext()`，并在提前停止时处理 `aclose()`。

如果把它交给普通 coroutine wrapper，wrapper 中的 `await function(...)` 会在运行时报错。真正的流式观测层必须定义：

- 耗时结束于第一项、自然耗尽还是 `aclose()` 完成；
- 消费者 break、异常、取消时谁关闭上游；
- 是否记录每一项，如何避免高基数和 token 内容泄露；
- 是否完整代理 `asend()`、`athrow()` 等协议。

这不是加一个 `inspect.isasyncgenfunction()` 分支就能自动正确。同步函数、一次返回的 coroutine 和逐项返回的 async generator，应分别设计范围明确的 wrapper。

## 11. Java 工程师最容易踩的迁移坑

| 写法或假设 | 失败表现 | 正确方向 |
|---|---|---|
| 模块全局保存当前 request ID | 并发 Task 的日志串请求 | 请求入口设置 ContextVar |
| 把 ContextVar 当作普通 ThreadLocal | 错判 Task、线程、进程和网络边界 | 针对具体 API 核对传播合同 |
| `finally: REQUEST_ID.set("-")` | 嵌套调用退出后丢失真实外层值 | 保存 Token 并 `reset(token)` |
| 先 `create_task`，后设置 request ID | 子 Task 已经复制旧 Context | 先 set，再创建子 Task |
| ContextVar 中放共享 dict 并原地修改 | 父子 Task 仍观察到彼此修改 | 优先放不可变值，或复制后重新 set |
| 假设所有 executor 自动传播 Context | 工作线程读取默认值或旧值 | 用 `to_thread` 的合同，或显式 `copy_context().run` |
| 把 client/session 藏进 ContextVar | 生命周期、事务和测试依赖不可见 | 明确传参或交给有所有权的组件 |
| 同步 wrapper 计时 async 函数 | 只测到 coroutine 创建 | `async def wrapper` 并 await 原调用 |
| success 写在无条件 `finally` | 失败和取消也被记成成功 | success 写在 `else`，finally 只做清理 |
| `except BaseException: return None` | 取消被吞，结构化并发状态失真 | 普通异常与取消分别记录并继续传播 |
| 日志打印全部 args/请求体/token | 敏感数据泄露、字段基数失控 | 只记录审查过的 ID、状态和有界计数 |
| 注册器先于 wrapper 保存原函数 | 动态调用绕过日志和计时 | 先包装，再让外层注册 wrapper |
| 用普通 async wrapper 包 async generator | generator 不可 await，关闭语义缺失 | 为流协议单独设计观测层 |

## 12. 运行本章完整实验

### 环境和依赖

原仓库根目录的 `pyproject.toml` 要求 Python `>=3.14`，Ruff 与 Pyright 也以 Python 3.14 为目标。本章已核对的本地解释器是 CPython 3.14.7。

本章只使用标准库，不需要新增依赖、API Key、外网服务或日志平台。Python 3.14 特有的正文写法主要有 `with CONTEXT_VAR.set(...)`；核心生命周期仍可用显式 `set/reset` 理解。

本章目录包含：

| 文件 | 职责 |
|---|---|
| `README.md` | Context 传播模型、Java 对照、日志与 wrapper 设计 |
| `example.py` | 10 组使用 Event 控制顺序的可运行实验 |

在当前独立课程目录执行：

```bash
python3.14 -B example.py
```

如果已安装开发工具，可以继续执行：

```bash
ruff check .
ruff format --check .
pyright .
```

完整示例会依次验证：

1. 普通全局变量让 REQ-A 的两个工具稳定误读 REQ-B；
2. Token 在嵌套、异常安全的 `finally` 和 Python 3.14 `with` 写法中恢复正确外层；
3. 子 Task 读取创建点 Context，父子后续 set/reset 互不回写；
4. Context 副本仍可能指向同一个可变 dict；
5. `to_thread` 的直接 await、Task 调度和 `run_in_executor` 入口表现不同；
6. 同步 wrapper 返回时原协程尚未启动，异步 wrapper 保留签名并覆盖完整 await；
7. 两个请求各派生两个 tool Task，日志逐条匹配所属请求；
8. 普通失败记录 error、不记录 success，并传播原异常；
9. 取消在工具层和请求层记录 cancelled、不记录 success，并到达最外层；
10. async generator 不可直接 await，提前停止时需要显式关闭。

实验使用 `asyncio.Event` 建立明确先后关系，不靠固定 `sleep` 猜调度顺序。耗时数值、线程名称和不同请求日志的打印顺序不是稳定合同。

本地替身只证明当前 Python 运行时中的 Task Context、线程适配、异常与取消语义。它没有证明真实 HTTP SDK、数据库驱动、跨进程队列、远端 trace header 或日志平台会自动传播同一上下文。

## 13. 递进练习

### 练习 1：增加租户诊断字段

新增模块级 `TENANT_LABEL: ContextVar[str]`，让日志 Filter 输出 `tenant=<value>`。两个并发请求分别使用 `team-blue` 和 `team-green`。

验收标准：

- 每条工具日志的 tenant 与所属请求一致；
- 请求结束后恢复默认值；
- 不修改工具签名来重复传 tenant；
- 授权逻辑仍不依赖这个诊断标签。

### 练习 2：修复可变上下文共享

保留共享 dict 反例，再新增一个正例。父 Context 初始值为 `{"events": ["parent"]}`，两个子 Task 分别构造新的 dict 和 list，再追加 `child-A`、`child-B` 并临时 set。

验收标准：

- 父对象最后没有变化；
- A 只看到 `parent, child-A`，B 只看到 `parent, child-B`；
- 用 `is` 或 `id()` 证明三个 dict、三个 list 不是同一对象；
- 每个子 Task 都在 `finally` 中 reset，顺序验证不依赖 `sleep`。

### 练习 3：封装显式线程传播

实现 `submit_with_current_context(executor, function, *args)`：每次调用都执行一次 `copy_context()`，再通过 `run_in_executor` 提交。用两个请求 Task 各提交两个同步读取函数。

验收标准：

- 四个线程结果都带正确请求 ID；
- 同一个 executor 可以复用，并由明确所有者关闭；
- 参数与返回类型关系使用参数规格和类型变量表达；
- 不缓存同一个 Context 给多个并发线程共用。

### 练习 4：给异步观测器增加 operation

让 `observe_async` 接收 `operation` 参数，并分别记录 `started`、`success`、`error`、`cancelled`。准备一个成功工具、一个抛 `ValueError` 的工具和一个停在 Event 上供外部取消的工具。

验收标准：

- 每次调用只有一个正确终态；
- 失败方仍收到同一个 `ValueError`，取消方仍收到 `CancelledError`；
- 终态日志有非负 `elapsed_ms`，但测试不限制精确毫秒数；
- `inspect.signature()` 与原签名一致，敏感参数没有进入日志。

### 练习 5：先定义 async generator 的观测合同

先写清自然耗尽、消费者提前关闭、上游异常和外部取消四条路径的结束点，再为一个两项异步生成器实现专用 wrapper。

验收标准：

- 四条路径都能有限结束，上游 `finally` 恰好执行一次；
- 异常和取消保持原协议，不转成普通结果；
- 不记录每个 token 的原文；
- 明确是否支持 `asend()` / `athrow()`，不要把有限实现描述成万能 wrapper。

## 14. 本章小结

可以把整章压缩成六条规则：

1. 普通全局变量按模块共享，单线程 asyncio 也会因为 `await` 交错而串请求。
2. ContextVar 按当前 Context 绑定；请求入口先 set，所有出口都用原 Token reset。
3. 子 Task 在创建点复制 Context，后续绑定互不回写，但可变 value 仍可能共享身份。
4. Task、线程、进程和网络是不同传播边界；只相信具体 API 的明确合同。
5. 日志关联字段适合隐式上下文，业务输入、资源和安全决策仍应显式建模。
6. 异步 wrapper 必须 await 原调用，并区分 success、error、cancelled；流式协议另行设计。

最后尝试不看正文回答：

1. 一条 tool 日志里的 request ID 在哪里设置、何时复制、最后由谁读取？
2. 为什么 `reset(token)` 比 `set(default)` 更能保护嵌套调用？
3. 为什么父子 Task 的 request ID 不串，ContextVar 里的 dict 却仍可能相互影响？
4. `to_thread(...)` 只创建 coroutine 后再改 Context，与先创建 Task 后再改，有什么不同？
5. 为什么同步 wrapper 的耗时不包含 I/O，成功日志也不能写在无条件 `finally` 中？
6. 为什么 async generator 不能直接复用“一次 await 得到 R”的 wrapper？
