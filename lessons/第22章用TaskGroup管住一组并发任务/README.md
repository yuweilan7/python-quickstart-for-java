# 第 22 章：用 TaskGroup 管住一组并发任务

一句话先记住：`asyncio.TaskGroup` 不只是“同时启动几个协程”，它还把这些任务的创建、等待、失败传播和清理限制在一个明确的代码作用域里。

这类能力常见于一次请求需要并发调用多个下游的场景。例如，一个 Agent 同时搜索知识库、查询客户记录和获取服务状态。并发本身并不难，真正的工程问题是：一个调用失败后，其余调用由谁停止；用户取消请求后，连接由谁关闭；父请求返回前，怎样保证没有遗漏仍在后台运行的任务。

学完本章，你应该能够：

- 用任务树说明父任务、`TaskGroup` 和子任务之间的所有权；
- 在 `TaskGroup` 成功退出后安全收集每个任务的结果；
- 解释一个子任务失败后，兄弟任务取消、资源清理和异常抛出的先后关系；
- 用 `ExceptionGroup` 和 `except*` 处理多个并发错误；
- 区分子任务失败、单个子任务取消、父请求取消和整体超时；
- 在“全部成功才有意义”和“允许部分业务失败”之间选择合适策略。

本章示例只使用 Python 标准库。`TaskGroup`、`ExceptionGroup` 和 `except*` 从 Python 3.11 开始提供；仓库要求 Python 3.14，完整示例也使用了 Python 3.14 支持的 `eager_start` 参数。

## 22.1 为什么裸 `create_task()` 容易留下生命周期漏洞

下面的代码确实启动了后台任务，但父请求立即返回了：

```python
async def handle_request() -> str:
    asyncio.create_task(call_tool())
    return "请求已结束"
```

问题不是 `call_tool()` 能不能运行，而是代码没有回答三个问题：

- 谁保存这个 Task 的引用并等待它结束？
- 父请求被取消时，谁取消这个 Task？
- Task 稍后失败时，谁接收它的异常？

如果没有明确答案，这个 Task 就可能比创建它的请求活得更久。它可能继续占用连接，也可能产生无人接收的异常。

`TaskGroup` 把任务组织成一棵有边界的树：

```text
handle_request：承载本次请求的父 Task
└── async with asyncio.TaskGroup() as group
    ├── group.create_task(search_docs())
    ├── group.create_task(load_customer())
    └── group.create_task(fetch_status())
```

父 Task 负责进入和退出 `TaskGroup`。只有通过 `group.create_task()` 登记的 Task 才是组成员。父 Task 成功越过 `async with` 时，所有组成员都已经结束。

因此，结构化并发解决的是所有权、生命周期和错误传播。它不会创建新线程，不保证任务完成顺序，也不限制同时运行的任务数量。

## 22.2 Java 工程师可以怎样建立心智模型

可以先把 `TaskGroup` 类比为 Java 的结构化并发作用域，但不能完全等同。两者都希望让子任务的生命周期服从词法作用域；具体调度模型、失败策略和 API 并不相同。

| Java 心智模型 | Python 对应概念 | 不能直接等同的地方 |
| --- | --- | --- |
| 结构化并发作用域 | `async with TaskGroup()` | `TaskGroup` 对非取消异常采用固定的兄弟取消与异常聚合规则 |
| 在作用域中派生子任务 | `group.create_task(coro)` | 创建的是事件循环中的 Task，不是平台线程或虚拟线程 |
| `Future<T>` / `CompletableFuture<T>` | `asyncio.Task[T]` | `task.result()` 不会阻塞等待；未完成时会抛 `InvalidStateError` |
| `join` 后读取结果 | 成功退出 `TaskGroup` 后读取各 Task 结果 | 等待动作由 `async with` 的退出协议完成，没有单独的 `join()` |
| 多个失败原因 | `ExceptionGroup` | Python 保留一棵异常树，并用 `except*` 按叶子类型切分 |
| `try-finally` / `try-with-resources` | 协程中的 `try-finally` / `async with` | 取消通过 `CancelledError` 在可取消点进入协程，不是硬中断线程 |

`CompletableFuture.allOf(...)` 也能组合多个异步结果，但它本身不表达“一个请求拥有这些任务，离开作用域前必须完成统一取消和清理”。`TaskGroup` 的重点不是少写几个 API 调用，而是让任务树直接体现在代码结构中。

还要注意，常见的 asyncio 程序在一个事件循环线程中协作调度。协程执行到 `await` 才把控制权交还事件循环。它适合并发等待 I/O；CPU 密集型同步代码如果长时间不让出控制权，仍会阻塞整个事件循环。

## 22.3 正常路径：创建任务，退出作用域，再取结果

下面是一个可以独立运行的最小示例：

```python
import asyncio


async def call_tool(name: str) -> str:
    await asyncio.sleep(0.1)
    return f"{name}: ok"


async def handle_request() -> list[str]:
    async with asyncio.TaskGroup() as group:
        search = group.create_task(call_tool("search"))
        status = group.create_task(call_tool("status"))

    return [search.result(), status.result()]


print(asyncio.run(handle_request()))
```

执行过程可以拆成四步：

1. 调用 `call_tool(...)` 得到协程对象。调用 `async def` 不会立即执行函数体。
2. `group.create_task(...)` 把协程包装成 Task，登记到组内，并交给事件循环调度。
3. 到达 `async with` 末尾时，`TaskGroup` 的退出协议等待所有成员结束。
4. 成功退出后，两个 Task 都已完成，`result()` 可以直接返回各自结果。

这里保存的 `search` 和 `status` 是 `Task[str]`，不是最终字符串。Python 的 `with` 块不会像 Java 的花括号那样创建局部变量作用域，所以退出块后仍能访问这两个名字。

不要在任务尚未完成时调用 `task.result()`。这个方法不会像 Java 的 `Future.get()` 那样阻塞，而是立即抛出 `asyncio.InvalidStateError`。如果组内逻辑确实依赖某个结果，可以写 `value = await task`；但这样会引入依赖顺序，可能把原本可并发的工作变成分阶段执行。

“退出后可以取结果”还隐含了一个前提：`TaskGroup` 是成功退出的。如果组内有任务失败，`async with` 会直接抛异常组，后面的结果收集语句不会执行。失败任务保留原异常，被取消的任务则处于 cancelled 状态，对它们调用 `result()` 都不会得到正常值。

## 22.4 一个任务失败后，TaskGroup 会做什么

只要有成员抛出 `CancelledError` 之外的异常，`TaskGroup` 就开始 fail-fast 关闭。假设 `load_customer` 首先失败，因果顺序是：

```text
load_customer 抛出非取消异常
        ↓
TaskGroup 进入关闭状态
        ├── 向仍未完成的 search_docs 请求取消
        ├── 向仍未完成的 fetch_status 请求取消
        └── 若父 Task 仍在组内 await，则用内部取消唤醒它
        ↓
子任务在可取消点收到 CancelledError，并执行 finally
        ↓
TaskGroup 等到所有成员真正结束
        ↓
把非取消异常组成 ExceptionGroup，再抛到作用域外
```

最重要的边界是：先等所有成员清理结束，再把异常交给组外代码。兄弟任务具体按什么顺序收到取消、按什么顺序完成清理，都由调度决定，不应成为业务逻辑的依赖。

取消也不是强制终止。`task.cancel()` 只是提出取消请求。任务通常在下一次 `await` 时收到 `CancelledError`。如果协程正在执行长时间不让出控制权的同步代码，取消就不能及时生效。

资源清理应放在 `finally` 中：

```python
async def read_remote(client) -> bytes:
    stream = await client.open_stream()
    try:
        return await stream.read()
    finally:
        await stream.aclose()
```

成功、普通异常和取消都会经过 `finally`。如果确实要捕获 `CancelledError` 来记录日志或补充清理，完成后通常要再次 `raise`，否则 `TaskGroup`、`timeout` 以及调用方可能误以为任务正常结束。

如果异常来自 `async with` 块本身，而不是某个子任务，`TaskGroup` 也会取消并等待所有成员。这个块异常会和子任务产生的非取消异常一起参与聚合。`KeyboardInterrupt` 和 `SystemExit` 是特殊情况：组仍会先取消并等待其他成员，但最终重新抛出原异常，而不是把它当作普通失败包装。

## 22.5 为什么并发错误需要 ExceptionGroup

第一个失败触发取消，不代表最终只有一个错误。另一个任务可能已经同时失败，也可能在关闭资源时又遇到错误。为了不丢失这些信息，`TaskGroup` 会用 `ExceptionGroup` 保存异常树：

```text
ExceptionGroup: unhandled errors in a TaskGroup
├── RecordNotFound: 客户记录不存在
├── OSError: 关闭响应流失败
└── ValueError: 响应结构不符合约定
```

普通 `except ExceptionGroup` 抓到的是整个容器。`except* SomeError` 则递归匹配异常树中的叶子，把匹配部分保留为一个子组交给当前处理器，未匹配部分继续交给后续处理器或向外传播。

```python
try:
    async with asyncio.TaskGroup() as group:
        group.create_task(search_docs())
        group.create_task(load_customer())
        group.create_task(fetch_status())
except* RecordNotFound as expected_group:
    log_expected_failures(expected_group)
except* OSError as io_group:
    log_resource_failures(io_group)
```

`expected_group` 和 `io_group` 都是异常组，不是单个异常。即使只匹配到一个叶子，处理器拿到的仍然是组。这样可以保留原异常树的层次和 traceback。

使用 `except*` 时还要记住这些语法边界：

- 同一个 `try` 只能使用 `except` 或 `except*`，不能混用；需要两种处理时嵌套两层 `try`；
- `except*` 后必须写异常类型，不存在裸 `except*:`；
- `return`、`break` 和 `continue` 不能出现在 `except*` 子句中；
- 没有被任何 `except*` 处理的部分不会消失，而会保持异常组结构继续传播。

不要为了遍历方便就把异常组压平。嵌套 `TaskGroup` 会自然形成嵌套异常组，保留结构有助于判断故障来自哪一层并发作用域。

## 22.6 不要混淆失败、取消和超时

这四种退出原因会经过相似的清理路径，但最终语义不同：

| 情况 | TaskGroup 怎样处理兄弟任务 | 组外通常看到什么 |
| --- | --- | --- |
| 一个成员抛非取消异常 | 取消未完成兄弟并等待清理 | `ExceptionGroup` 或 `BaseExceptionGroup` |
| 一个成员被取消或自行取消 | 不因为这一项取消而自动取消其他成员 | 若无其他失败，组可以正常退出 |
| 承载 TaskGroup 的父 Task 被取消 | 取消未完成成员并等待清理 | 原来的 `CancelledError` 继续传播 |
| 外层 `asyncio.timeout()` 到期 | 先取消父 Task，再由组取消并等待成员 | timeout 作用域外收到 `TimeoutError` |

单个成员被取消并不等于它返回了一个空值。这个 Task 的 `cancelled()` 为 `True`，调用 `result()` 会重新抛出 `CancelledError`；只是 `TaskGroup` 不把这一次取消当作触发整组失败的普通异常。

`asyncio.CancelledError` 直接继承 `BaseException`，而不是 `Exception`。这能降低它被普通业务异常处理误吞的概率，但不代表可以随意写 `except BaseException`。下面的包装会把用户停止请求也伪装成一次普通失败：

```python
async def wrong(operation):
    try:
        return await operation
    except BaseException as error:
        return {"ok": False, "error": str(error)}
```

正确原则是：取消属于控制流，清理后继续传播；只有业务已经定义为“允许恢复”的异常，才转换成普通结果。

## 22.7 两种常见业务策略

`TaskGroup` 提供统一的生命周期规则，但不会替你决定业务上允许失败到什么程度。关键问题是：缺少一个子任务结果时，本次请求是否仍然有意义且安全？

### 全部必需：让异常自然离开子任务

如果生成合规答复必须同时拿到政策、客户记录和实时状态，缺一项就应终止请求：

```python
async with asyncio.TaskGroup() as group:
    policy = group.create_task(load_policy())
    customer = group.create_task(load_customer())
    status = group.create_task(fetch_status())
```

不要在每个工具调用中用宽泛的 `except Exception` 把错误变成 `None`。那会使 `TaskGroup` 误以为任务全部正常完成，也会让调用方拿着不完整数据继续执行。

### 允许部分业务失败：只转换明确可恢复的异常

如果“没有找到客户记录”本来就是可展示的业务结果，可以在子任务内部把这一种异常转换成显式数据：

```python
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


class RecordNotFound(Exception):
    pass


@dataclass(frozen=True)
class ToolResult:
    name: str
    value: str | None
    error: str | None


async def capture_business_failure(
    name: str,
    operation: Callable[[], Awaitable[str]],
) -> ToolResult:
    try:
        return ToolResult(name, await operation(), None)
    except RecordNotFound as error:
        return ToolResult(name, None, str(error))
```

使用时，每个包装器本身成为 `TaskGroup` 的成员：

```python
async with asyncio.TaskGroup() as group:
    search = group.create_task(
        capture_business_failure("search", search_docs)
    )
    customer = group.create_task(
        capture_business_failure("customer", load_customer)
    )

results = [search.result(), customer.result()]
```

这里故意传入函数，而不是提前调用函数得到协程。包装器开始执行后才创建并等待真正的协程，任务的创建与消费边界更清楚。

这个包装器不会捕获 `TypeError`、协议解析错误等未知缺陷，也不会吞掉 `CancelledError`。这些情况仍会触发 `TaskGroup` 的正常失败或取消流程。“best effort”不等于“忽略所有异常”。

| 业务含义 | 处理方式 |
| --- | --- |
| 少一项就不能正确回答或提交 | 让异常离开子任务，由 TaskGroup 终止整组 |
| 某种已知业务失败也是合法结果 | 只捕获该具体异常，转换成结构化结果 |
| 未知程序、协议或数据不变量错误 | 不降级为部分成功，继续传播 |
| 用户取消或上游断开 | 清理后继续传播 `CancelledError` |

## 22.8 嵌套调用不会自动继承任务所有权

`TaskGroup` 不会扫描调用栈，也不会收编当前上下文中所有由 `asyncio.create_task()` 创建的任务：

```python
async def launch_background() -> None:
    asyncio.create_task(background_work())  # 不属于调用方的 TaskGroup


async with asyncio.TaskGroup() as group:
    group.create_task(launch_background())
```

组只拥有 `launch_background()` 对应的 Task。这个 Task 返回后，`background_work()` 仍可能继续运行。正确设计通常有两种。

第一种是把现有组显式传给下层，让新任务属于同一个请求：

```python
def add_request_tasks(group: asyncio.TaskGroup) -> None:
    group.create_task(background_work())
```

第二种是让下层建立自己的嵌套作用域，并且退出自己的组以后再返回：

```python
async def run_nested_work() -> None:
    async with asyncio.TaskGroup() as nested_group:
        nested_group.create_task(background_work())
```

可以把 `TaskGroup` 作为参数，但不要把它藏在全局变量里。显式参数让函数签名直接说明新任务属于哪个生命周期。如果某项后台工作本来就应该跨越请求长期运行，它应当归属于应用级服务，并有独立的启动、停止和异常处理协议。

## 22.9 整体超时应该包住整个任务组

如果几个调用共享同一份请求预算，把 `asyncio.timeout()` 放在 `TaskGroup` 外层：

```python
try:
    async with asyncio.timeout(2.0):
        async with asyncio.TaskGroup() as group:
            group.create_task(search_docs())
            group.create_task(load_customer())
            group.create_task(fetch_status())
except TimeoutError:
    handle_timeout()
```

到期后的数据流是：

1. timeout 取消当前父 Task；
2. 父 Task 退出 `TaskGroup`，组取消仍未完成的成员；
3. `TaskGroup` 等待子任务执行清理；
4. 取消回到 timeout 作用域；
5. timeout 把自己触发的取消转换成 `TimeoutError`，在作用域外抛出。

因此，两秒预算不等于函数一定在墙钟时间 2.000 秒时强制返回。事件循环要有机会投递取消，资源清理也可能需要时间。超时能约束等待协议，但不能撤销远端已经产生的副作用。

`TimeoutError` 必须在 timeout 上下文外捕获，因为异常转换发生在退出上下文时。普通的上游取消也不会被随意转换成超时；timeout 只转换由它自己触发的取消。

## 22.10 TaskGroup 与 `gather()` 怎样选择

`asyncio.gather()` 仍适用于需要按输入顺序聚合结果的代码，也常见于旧项目。但它的默认失败行为与 `TaskGroup` 不同：

| 维度 | TaskGroup | `gather(..., return_exceptions=False)` |
| --- | --- | --- |
| 代码边界 | `async with` 明确表示任务作用域 | 调用方等待一个聚合 awaitable |
| 一个成员普通失败 | 取消未完成兄弟，等其清理后抛异常组 | 立即向等待者传播该异常，其他成员默认继续运行 |
| 多个失败 | 关闭完成后聚合为异常组 | 默认传播首先观察到的异常 |
| 结果收集 | 保存 Task，成功退出组后读取 | 返回列表，顺序与输入一致 |
| 适用重点 | 请求内的任务所有权、失败联动与清理 | 结果排序或必须兼容的既有聚合语义 |

`gather()` 自身被取消时，会把取消传播给尚未完成的成员；但这与“某个成员普通失败”不是一回事。使用 `return_exceptions=True` 可以把异常放进结果列表，却也更容易把程序缺陷与业务失败混在一起。

新写的请求级并发通常优先选择 `TaskGroup`。只有确实需要 `gather` 的返回值语义，并且能明确说明异常后谁继续拥有、等待和清理其他任务时，再使用 `gather`。

另外，`TaskGroup` 不负责限流。循环调用一千次 `group.create_task()`，就可能登记一千个任务。限制并发量要结合 `asyncio.Semaphore`、连接池或速率限制器，这是第 23 章的主题。

## 22.11 Java 工程师最容易踩的坑

| 误判或错误写法 | 实际表现 | 正确做法 |
| --- | --- | --- |
| `task.result()` 等同于 `Future.get()` | 未完成时立即抛 `InvalidStateError` | `await task`，或成功退出组后读取 |
| TaskGroup 会返回一张结果列表 | TaskGroup 管生命周期，不直接聚合结果 | 保存 `create_task()` 返回的 Task |
| 一个任务失败后所有代码立刻停止 | 取消需要到达可取消点，清理也需要时间 | 用 `finally` 清理，并等待作用域退出 |
| 捕获 `Exception` 就是 best effort | 未知程序缺陷也会被伪装成业务失败 | 只捕获已定义恢复语义的具体异常 |
| 捕获 `BaseException` 更保险 | 会吞掉取消和进程退出类信号 | 不宽捕获；捕获取消后通常重新抛出 |
| 普通 `except ValueError` 能匹配组内叶子 | 外层对象是异常组 | 使用 `except* ValueError` |
| 嵌套函数裸建 Task 会自动入组 | 新 Task 不属于当前组 | 传入现有组，或在下层自建嵌套组 |
| TaskGroup 会限制并发数量 | 它只管理所有权和失败传播 | 另加 `Semaphore`、连接池或速率限制 |
| 超时会强制终止工作 | 取消是协作式的，远端副作用也不会回滚 | 设计幂等、清理和服务端超时 |

## 22.12 运行本章示例

本章包含两个文件：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 建立 TaskGroup、取消、异常组和失败策略的心智模型 |
| `example.py` | 用 `asyncio.Event` 精确控制时序，并用断言验证各条路径 |

`example.py` 不访问网络，也没有第三方依赖。它依次验证：

- 正常退出后读取结果；
- 一个任务失败时取消兄弟并等待清理；
- 多个失败的聚合与 `except*` 分型；
- 部分业务失败与未知程序缺陷的不同处理；
- 单个成员取消与父请求取消的差别；
- 裸 `create_task()` 产生的游离任务；
- 整体 timeout 的清理顺序；
- `gather` 传播异常后兄弟任务仍继续运行。

从仓库根目录运行：

```bash
.venv/bin/python -B java_to_python/chapters/22_asyncio_taskgroup/example.py
```

在本次独立还原的课程目录中，可以运行：

```bash
python -B example.py
```

成功时最后会输出：

```text
所有第 22 章示例与运行时断言均已完成。
```

可继续执行静态检查：

```bash
.venv/bin/ruff check java_to_python/chapters/22_asyncio_taskgroup
.venv/bin/ruff format --check java_to_python/chapters/22_asyncio_taskgroup
.venv/bin/pyright java_to_python/chapters/22_asyncio_taskgroup/example.py
```

这些内存替身能验证 Python 层面的任务、取消、异常和清理语义，但不能代替真实 HTTP、数据库、LLM 或 MCP 客户端的集成测试。真实系统还要验证连接池、驱动取消、服务端断连、重试、幂等性以及远端副作用。

Python 3.14 官方文档可作为进一步参考：

- `asyncio.TaskGroup`
- `asyncio.gather`
- `asyncio.timeout`
- `ExceptionGroup`
- `except*`

## 22.13 本章小结

`TaskGroup` 最核心的价值，是让父任务在离开作用域前对所有子任务负责。正常路径会等待所有成员；一个成员发生非取消失败时，会取消仍在运行的兄弟，等它们清理后再聚合错误；父请求取消时，同样先清理孩子，再把取消继续交还调用方。

工程上要同时守住两条边界：用 `group.create_task()` 明确任务所有权；只把已知且可恢复的业务异常转换成普通结果。做到这两点，`TaskGroup` 才不只是并发语法，而是可靠的请求生命周期边界。
