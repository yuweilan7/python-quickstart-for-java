# 第 19 章：asyncio、coroutine 与事件循环

**一句话先讲透：asyncio 不是“自动多线程”，而是让一条任务在等待 I/O 时主动暂停，由事件循环去推进其他已经就绪的任务。**

Agent 服务经常需要等待 LLM、MCP tool、HTTP 接口或数据库返回。等待期间，当前请求没有可继续执行的 Python 代码。如果把这些等待写成真正的异步操作，一个线程就能交错推进许多请求，而不必让每个请求各占一个线程干等。

本章要建立的核心心智模型是：

```text
async def 定义可暂停的函数
          ↓ 调用
coroutine 对象保存一次调用的参数和执行状态
          ↓ 被 await、Task 或 asyncio.run 驱动
函数体开始执行
          ↓ 遇到尚未完成的异步等待
当前 Task 暂停，事件循环运行其他就绪工作
          ↓ 等待条件完成
Task 从暂停处恢复，return 值成为 await 表达式的结果
```

学完后，你应当能解释：

- 协程函数、协程对象和最终返回值为什么是三个不同的东西；
- `await` 暂停的是谁，为什么它不等于“新建线程”；
- 为什么连续写两个 `await` 仍然是顺序执行；
- 为什么 `time.sleep()`、同步 HTTP 和长时间 CPU 计算会卡住事件循环；
- coroutine、Task 和 Future 有什么职责差异；
- 脚本为何通常只在最外层调用一次 `asyncio.run()`。

Task 的完整生命周期、取消、异常汇总和结构化并发会在第 20 章展开。本章先把协程与事件循环的基础机制讲清楚。

## 19.1 Java 工程师应该怎样建立第一版映射

Python 没有与 Java `async`/`await` 完全一一对应的语言模型。可以借用 `CompletableFuture`、线程池和 Netty EventLoop 帮助理解，但必须同时记住边界。

| Java 中熟悉的概念 | Python asyncio 中可类比的概念 | 关键差异 |
| --- | --- | --- |
| `ExecutorService.submit(callable)` | `asyncio.create_task(coro)` | `submit` 把工作交给线程池；Task 通常由事件循环在其线程中协作推进 |
| `Future.get()` / `join()` | `await awaitable` | `get/join` 通常阻塞调用线程；等待未完成对象时，`await` 暂停当前 Task，把线程还给事件循环 |
| `CompletableFuture<T>` | `asyncio.Future[T]` 或 Task | 都保存未来的结果或异常；asyncio Future 通常绑定事件循环，也不是通用的线程间同步对象 |
| 单线程 Netty EventLoop | 单线程 asyncio 事件循环 | 都怕阻塞回调；asyncio coroutine 还会通过 `await` 显式暴露协作暂停点 |
| 一次可恢复的状态机 | coroutine 对象 | coroutine 保存局部变量和暂停位置，但必须由 `await`、Task 或 runner 驱动 |

最容易误判的一点是“调用即启动”：

```java
CompletableFuture<String> future =
    CompletableFuture.supplyAsync(this::loadAnswer); // 通常已经提交执行
```

```python
pending = load_answer()  # 只创建 coroutine 对象，函数体还没有执行
```

Python 的 coroutine 对象更像“一次尚未被驱动的可恢复调用”，而不是已经在线程池后台运行的 `CompletableFuture`。

## 19.2 asyncio 解决的是等待重叠，不是计算并行

先分清两组概念。

**I/O 等待**是请求已经发出，但结果还没准备好。例如网络响应尚未返回、socket 暂时没有字节可读、数据库仍在查询。此时当前工作没有有用的 Python 指令可执行。

**CPU 执行**是解释器正在解析 JSON、拼接 prompt、过滤数据或运行计算循环。只要代码持续占用事件循环线程，其他 Task 就无法在同一线程上运行。

| 概念 | 关心的问题 | 单线程 asyncio 能否做到 |
| --- | --- | --- |
| 并发（concurrency） | 多项工作能否在同一时间段交错推进 | 能；A 等 I/O 时可以推进 B |
| 并行（parallelism） | 多项计算能否在同一时刻真正执行 | 单个事件循环不能自动做到 |

因此，asyncio 最适合 I/O 密集型场景。它提升的通常是大量等待可以重叠时的吞吐量和响应性，不保证单个操作更快，也不会自动加速 CPU 密集型计算。

## 19.3 `async def`：调用函数时为什么没有执行函数体

普通 `def` 函数被调用后会立即进入函数体。`async def` 不同：它定义的是**协程函数**，调用后先得到一个**协程对象**。

```python
import asyncio


async def fetch_answer() -> str:
    print("函数体开始")
    await asyncio.sleep(0)
    return "answer"


async def demo() -> None:
    pending = fetch_answer()  # 此时不会打印
    answer = await pending  # 从这里开始驱动函数体
    print(answer)  # answer


asyncio.run(demo())
```

这里必须分清三层：

| 名称 | 例子 | 含义 |
| --- | --- | --- |
| 协程函数 | `fetch_answer` | 由 `async def` 定义的可调用对象 |
| 协程对象 | `fetch_answer()` 的返回对象 | 保存这一次调用的参数、局部状态和执行位置 |
| 最终结果 | `await pending` 得到的 `"answer"` | 协程执行到 `return` 后交给调用方的普通值 |

`fetch_answer() -> str` 中的 `str` 描述的是协程完成后的结果类型。从运行时看，直接调用 `fetch_answer()` 得到的不是字符串，而是 coroutine 对象。

即使一个 `async def` 函数体里完全没有 `await`，调用它时仍然只会创建 coroutine 对象。区别只是：该对象一旦被驱动，函数体会连续执行到 `return`，中间没有主动暂停的机会。

> `async def` 中如果出现 `yield`，定义的是异步生成器，而不是本章讨论的普通 coroutine。异步生成器与 `async for` 会在后续章节学习。

## 19.4 `await`：暂停、恢复和返回值如何沿调用链传递

`await expression` 会先计算 `expression`，计算结果必须是 awaitable（可等待对象）。除支持顶层 `await` 的交互环境外，`await` 只能写在 `async def` 函数体内。asyncio 中最常见的 awaitable 有三类：

```text
awaitable
├── coroutine：一次 async def 调用
├── Task：被事件循环调度的 coroutine，并保存其最终状态
└── Future：稍后才会有结果或异常的占位对象
```

业务代码最常直接创建 coroutine，Task 用于让工作获得独立调度机会，Future 则更常由框架、协议实现或底层 asyncio API 提供。

### 直接 await 子协程不会创建新线程或新 Task

```python
import asyncio


async def load_number() -> int:
    await asyncio.sleep(0)
    return 20


async def calculate() -> int:
    value = await load_number()
    return value + 1


print(asyncio.run(calculate()))  # 21
```

执行过程是：

```text
当前 Task 进入 calculate
└── await load_number()
    ├── 遇到尚未完成的异步等待
    │   当前 Task 连同整条调用链暂停
    │   事件循环获得控制权
    └── 等待完成后从原位置恢复，return 20
└── await 表达式得到 20，calculate 继续执行并 return 21
```

局部变量和执行位置会保存在 coroutine 状态中，恢复时不会从函数开头重跑。子协程抛出的异常也会从对应的 `await` 位置向外传播，可以像同步调用链一样用 `try`/`except`/`finally` 处理；Task 间的异常管理留到下一章。

### `await` 不保证每次都发生调度切换

如果 awaitable 已经完成，`await` 可以立即取得结果，当前 Task 不必暂停：

```python
import asyncio


async def read_completed() -> None:
    loop = asyncio.get_running_loop()
    completed = loop.create_future()
    completed.set_result("缓存命中")

    print("之前")
    result = await completed
    print("之后", result)


asyncio.run(read_completed())
```

这里“之前”和“之后”可以连续出现。类似地，如果被直接 await 的 coroutine 内部没有遇到尚未完成的等待，它也可以一路执行到返回。

因此，准确规则不是“见到 `await` 就切换任务”，而是：

1. `await` 检查并推进 awaitable；
2. 若结果暂时无法取得，当前 Task 暂停；
3. 若结果已经就绪，`await` 可以立即返回；
4. 当前 Task 暂停后，事件循环才有机会选择其他就绪工作。

`await asyncio.sleep(0)` 有明确的协作暂停语义，适合教学实验或极少数主动让出执行权的场景。正常业务代码应等待真实的异步操作，而不是到处插入 `sleep(0)` 修补阻塞设计。

## 19.5 为什么连续两个 `await` 仍然是顺序执行

下面的完整小例子不会同时启动两项操作：

```python
import asyncio


async def operation(name: str) -> str:
    await asyncio.sleep(0)
    return name


async def sequential() -> None:
    llm_result = await operation("LLM")
    rows = await operation("数据库")
    print(llm_result, rows)


asyncio.run(sequential())
```

执行顺序是：

```text
创建并推进 call_llm 的 coroutine
  → 等待 LLM 时，当前 Task 可以暂停
  → LLM 完成，第一条语句得到结果
然后才调用 query_database
  → 创建并推进数据库 coroutine
  → 数据库完成，第二条语句得到结果
```

第二条语句必须等待第一条语句完成后才能执行，所以数据库 coroutine 甚至不会提前创建。

这并不一定是问题。如果数据库查询参数依赖 LLM 的输出，顺序正是业务约束。只有两项工作彼此独立时，才应该考虑并发调度：

```python
async def concurrent() -> None:
    llm_task = asyncio.create_task(operation("LLM"))
    database_task = asyncio.create_task(operation("数据库"))

    llm_result = await llm_task
    rows = await database_task
    print(llm_result, rows)
```

`create_task()` 会把 coroutine 安排给事件循环，使两项工作都能在后续调度中向前推进。但 Task 必须有人保存引用、取得结果并处理异常；生产代码如何用 `TaskGroup` 管理这些责任是第 20 章的主题。

## 19.6 事件循环为什么最怕同步阻塞

在本章的单线程模型中，事件循环一次只推进一个 Task。Task 运行到尚未完成的异步等待时主动暂停，事件循环才能处理 I/O 完成事件、定时器和其他 Task。这叫**协作调度**。

把函数声明成 `async def`，不会改变函数体中普通调用的执行方式：

```python
import time


async def wrong() -> None:
    time.sleep(1)  # 阻塞事件循环所在的 OS 线程
    sum(i * i for i in range(100_000_000))  # 长计算持续占用事件循环线程
```

这与在 Netty EventLoop 中执行阻塞 JDBC 调用很相似：并不是只有当前请求变慢，而是同一循环负责的心跳、超时、I/O 回调和其他请求都会被推迟。

常见工作应这样选择：

| 工作类型 | 通常的处理方式 | 原因 |
| --- | --- | --- |
| 原生异步 HTTP、数据库、MCP 客户端 | 直接 `await` 异步 API | 等待期间能交还事件循环 |
| 无法替换的同步阻塞 I/O | `await asyncio.to_thread(func, ...)` | 把阻塞调用移出事件循环线程 |
| 很短的 CPU 处理 | 直接执行 | 调度和传输也有成本 |
| 长时间 CPU 计算 | 进程、可并行的原生实现或外部计算服务 | 单事件循环不提供计算并行 |

`asyncio.to_thread()` 主要用于兼容阻塞 I/O。它不会把任意 CPU 代码自动变快；受 GIL、原生扩展是否释放 GIL、数据传输和调度成本影响，CPU 并行需要单独设计。

## 19.7 coroutine、Task 和 Future 的生命周期不能混用

三者都可以被 `await`，但不是同一种对象。

| 对象 | 谁推进它 | 完成后能否再次 await | 典型用途 |
| --- | --- | --- | --- |
| coroutine | 直接 await 它的调用链，或交给 Task/runner | 不能；同一个对象是一次性的 | 表示一次 `async def` 调用 |
| Task | 事件循环 | 可以观察同一个最终结果或异常 | 调度 coroutine，并管理其完成状态 |
| Future | 由生产结果的一方调用 `set_result` / `set_exception` | 可以观察同一个最终状态 | 框架和底层异步协议传递未来结果 |

### 同一个 coroutine 对象只能执行一次

```python
pending = fetch_answer()
first = await pending
second = await pending  # RuntimeError: cannot reuse already awaited coroutine
```

coroutine 对象保存的是一次调用的执行状态。完成后这条执行路径已经关闭，不能倒带。要再次执行，就重新调用协程函数：

```python
first = await fetch_answer()
second = await fetch_answer()  # 新对象、新的一次调用
```

### 忘记 await，整个函数体可能一次都没运行

```python
async def save_message() -> None:
    print("真的保存")


async def wrong() -> None:
    save_message()  # 只创建对象；既没有保存，也没有后台执行
```

未被驱动的 coroutine 最终被回收时，Python 通常会报告 `RuntimeWarning: coroutine ... was never awaited`。这个警告只是问题线索，不能替代正确的生命周期管理。正确做法是按业务语义直接 `await`，或把 coroutine 交给受管理的 Task。

示例代码为了演示错误，会对故意遗弃且尚未启动的 coroutine 调用 `close()`，避免留下警告。`close()` 表示放弃这次调用，不是日常业务中的补救方案。

## 19.8 谁负责创建和关闭事件循环

普通 `.py` 脚本从同步世界启动，推荐在最外层建立一次清晰边界：

```python
import asyncio


async def do_work() -> None:
    await asyncio.sleep(0)


async def run_application() -> None:
    await do_work()


def main() -> None:
    asyncio.run(run_application())


if __name__ == "__main__":
    main()
```

`asyncio.run()` 会创建事件循环，驱动入口 awaitable，完成必要清理，然后关闭循环。业务库通常只暴露 `async def` API，把“怎样运行事件循环”的决定留给应用入口。

不要在 coroutine 内再次调用 `asyncio.run()`：

```python
async def wrong() -> None:
    asyncio.run(other_coroutine())  # RuntimeError：当前线程已有运行中的循环


async def correct() -> None:
    await other_coroutine()
```

Notebook、IPython、Web 框架和异步测试框架通常已经拥有事件循环。在支持顶层 `await` 的交互环境中，应使用 `await run_application()`；在框架中则遵循框架提供的生命周期入口。

## 19.9 Java 工程师最容易踩的迁移坑

| 误判 | 失败表现 | 正确做法 |
| --- | --- | --- |
| 调用 `async def` 就会后台运行 | 函数体没有执行，出现 never-awaited 警告 | 直接 `await`，或创建并管理 Task |
| `await` 相当于 `Future.get()` | 错把协作暂停理解为阻塞线程 | 关注 awaitable 是否未完成，以及 Task 是否把控制权交回循环 |
| 每个 `await` 都切换任务 | 对事件顺序作出错误假设 | 已完成 awaitable 可以立即返回；不要依赖猜测的调度顺序 |
| 连续两个 `await` 会并发 | 第二项工作始终在第一项完成后才开始 | 有依赖就保持顺序；独立工作才显式创建 Task |
| `async def` 会包装同步阻塞调用 | 整个事件循环卡住 | 换异步客户端，或用线程适配确实无法替换的阻塞 I/O |
| 异步一定更快 | 单次请求没有变快，甚至多出调度成本 | 把 asyncio 用在大量可重叠等待上 |
| coroutine 和 Future 都能重复等待 | 第二次 await 同一 coroutine 抛 `RuntimeError` | 每次调用创建新 coroutine；不要混淆其生命周期 |
| 任意位置都能调用 `asyncio.run()` | 运行中循环内抛 `RuntimeError` | 只在同步程序最外层创建 runner |

阅读一个陌生异步 API 时，可以按以下顺序判断：

1. 调用后得到普通值、coroutine、Task，还是 Future？
2. 它内部使用真正的异步 I/O，还是偷偷调用同步阻塞库？
3. 下一步是否依赖当前结果？依赖就顺序 `await`，独立才考虑并发。
4. 当前 coroutine 中是否存在 `time.sleep()`、同步网络调用或长 CPU 循环？
5. 如果创建了 Task，谁负责保存引用、取得结果、处理异常和清理？

## 19.10 运行本章示例

本章只使用 Python 标准库，不需要安装额外运行依赖。仓库根目录的 `pyproject.toml` 要求 Python 3.14 或更高版本。

在仓库根目录运行：

```bash
.venv/bin/python -B java_to_python/chapters/19_asyncio_coroutines/example.py
```

如果没有使用仓库内的 `.venv`，可将解释器路径替换为本机的 Python 3.14+。`-B` 只是不生成 `.pyc` 文件，不影响示例语义。

`example.py` 包含六组可独立观察的实验。运行时重点看这些事实：

1. 创建 coroutine 对象后，函数体事件尚未出现；`await` 后才得到最终结果。
2. 三层直接 `await` 按“由外向内进入、由内向外返回”的顺序工作。
3. `await` 已完成 Future 时，旁路 Task 不会强行插入两条相邻事件之间。
4. Agent 流程中，数据库替身在 LLM 替身完成后才开始。
5. 协作等待期间 heartbeat 能推进，`time.sleep()` 期间 heartbeat 完全停住。
6. 重复 `await`、忘记 `await` 和嵌套 `asyncio.run()` 都会以受控方式展示并清理。

`example.py` 中的 `asyncio.sleep(0)` 只是可控暂停点，不模拟 DNS、TLS、网络超时、重试、连接池或真实服务延迟。它能证明控制流，不代表真实客户端一定是非阻塞实现。

当前 `java_to_python/run_all.py` 只包含前 10 章，因此第 19 章需要使用上面的命令单独运行。可选的静态检查命令是：

```bash
.venv/bin/ruff check java_to_python/chapters/19_asyncio_coroutines
.venv/bin/ruff format --check java_to_python/chapters/19_asyncio_coroutines
.venv/bin/pyright java_to_python/chapters/19_asyncio_coroutines/example.py
```

## 19.11 动手练习

### 练习 1：先预测，再验证 coroutine 生命周期

实现 `async def normalize_prompt(text: str) -> str`：先记录“开始”，执行一次 `await asyncio.sleep(0)`，再记录“恢复”，最后返回去除首尾空白的字符串。

验证调用后、`await` 前事件列表为空；`await` 后结果为 `"hello asyncio"`，事件顺序为“开始、恢复”。

### 练习 2：判断顺序还是并发

实现 `fetch_profile(user_id)` 和 `build_reply(profile)` 两个本地 coroutine。先让第二步依赖第一步的返回值，记录完整事件顺序；再设计两个彼此独立的 coroutine，预测顺序 `await` 与创建 Task 后的差异。

重点不是比较毫秒数，而是用事件记录证明“谁先创建、谁先开始、谁在何处恢复”。

### 练习 3：证明阻塞会饿死其他 Task

创建一个不断递增计数并 `await asyncio.sleep(0)` 的 heartbeat Task，分别执行一次协作等待和一次很短的 `time.sleep()`。用等待前后的计数断言：协作等待期间 heartbeat 前进，阻塞调用期间计数不变。

记得在 `finally` 中通知 heartbeat 退出并 `await` 它，避免遗留后台 Task。

## 19.12 本章小结

- `async def` 定义协程函数；调用只创建一次性的 coroutine 对象。
- coroutine 必须被 `await`、包装成 Task，或交给 `asyncio.run()`，函数体才会执行。
- `await` 等待未完成对象时暂停当前 Task，不会自动创建线程。
- 直接 `await` 子 coroutine 会沿同一 Task 的调用链进入和返回。
- `await` 遇到已完成对象时可以立即返回，不保证发生调度切换。
- 连续两条 `await` 语句默认顺序执行；并发需要显式调度独立工作。
- 同步阻塞 I/O 和长 CPU 代码会占住事件循环线程，`async def` 不会改变这一点。
- coroutine 是一次性执行状态；Task 和 Future 保存完成状态，复用规则不同。
- `asyncio.run()` 通常只出现在同步程序的最外层入口。

如果你能仅凭事件顺序解释“对象何时创建、函数体何时开始、哪条调用链暂停、由谁恢复、结果如何返回”，就已经掌握了阅读 asyncio 业务代码最重要的基础。
