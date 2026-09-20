# 第 21 章：取消、超时与截止时间

## 先记住一句话

`asyncio` 的取消不是“立刻杀死任务”，而是向 `Task` 发出请求，让 `CancelledError` 在后续的协作调度点进入协程；超时通常也是先触发这套取消协议，再由超时边界把结果解释为 `TimeoutError`。

Agent 收到“停止”后，真正需要的不是返回一个“已取消”字符串，而是让整条等待链停止继续产生结果，同时仍然释放连接、会话和锁。

这两个概念最容易被一句“cancel 就停了”掩盖。实际上至少要分开四件事：

```text
调用方发出 cancel 请求
        ↓
下一次事件循环推进目标 Task
        ↓
CancelledError 注入等待链
        ↓
协程执行 finally（可能还包含异步清理）
        ↓
Task 最终结束；等待者观察到取消、超时、结果或其他异常
```

完成本章后，你应该能够：

- 解释 `Task.cancel()` 为什么只是请求，不是同步终止命令；
- 区分取消请求、异常注入、清理中和 Task 最终取消四个阶段；
- 沿“入口 → 工具 → I/O”逐层说出 `CancelledError` 的传播方向；
- 正确选择 `asyncio.timeout()`、`timeout_at()` 和 `wait_for()`；
- 用同一个单调时钟截止时间约束单次尝试、单工具和整个 Agent 请求；
- 解释事件循环阻塞、慢清理和不可撤回的外部副作用为何会突破直觉中的“准点停止”；
- 识别吞掉取消和遗留子 Task 的生命周期缺陷。

`asyncio.shield()` 放在选学部分。它不是常规取消修复方式，只适用于调用方明确拥有、愿意在自己取消后继续管理的工作。

## Java → Python 心智模型

可以先把 Python Task 的取消类比为 Java 中“Future 生命周期 + 线程中断意图 + 异常传播”的组合，但不能完全等同。Task 通常不是一条独占线程，`Task.cancel()` 也不会调用 `Thread.interrupt()`。

| Java 中熟悉的概念 | Python 中可以先想到 | 最重要的不等价之处 |
|---|---|---|
| `Future.cancel(true)` | `task.cancel()` | Java 可尝试中断执行线程；Python 不做线程中断，而是安排 `CancelledError` |
| `InterruptedException` / `CancellationException` | `asyncio.CancelledError` | Python 取消在 Task 的协作调度点传播，不是线程中断标志 |
| `future.get()` | `await task` | 两者都观察最终结果；Python 的 `await` 还会形成取消传播边 |
| `Future.get(timeout, unit)` | `asyncio.wait({task}, timeout=...)` | 都不自动取消工作；Java 抛异常，Python 返回 `done` / `pending` 两个集合 |
| 带超时并取消目标 | `asyncio.wait_for(target, seconds)` | `wait_for()` 到期会取消目标，并等待目标完成取消 |
| 给一段调用链设置预算 | `async with asyncio.timeout(seconds)` | 它取消执行代码块的当前 Task，不会自动创建 worker Task |
| `System.nanoTime()` 计算剩余预算 | `loop.time()` + `timeout_at()` | 都适合经过时间；都不是日历时间或 Unix 时间戳 |
| `try/finally`、try-with-resources | `try/finally`、异步上下文管理器 | Python 的清理还能包含 `await`，取消后 Task 仍可能是 pending |
| 调用方保存并回收 Future | Task 所有者保存并 `await` Task | 只保存引用不等于已经等待清理完成 |

Java 的 `Future.cancel(...)` 也提醒调用方不要只靠返回值判断状态，而应查看 `isCancelled()`。Python 的差异更深一层：`task.cancel()` 只是安排异常进入协程，只有 coroutine 最终以 `CancelledError` 结束时，`task.cancelled()` 才会为 `True`。

异常层级也不能照搬。Java 的 `InterruptedException` 是受检 `Exception`，`CancellationException` 是运行时异常；Python 的 `asyncio.CancelledError` 则直接继承 `BaseException`，故意绕开普通的 `except Exception` 业务错误边界。

## 21.1 前置、环境与运行方式

本章承接第 18 章的 `try/finally` 与资源所有权，以及第 20 章的 Task 生命周期与结果收集。这里只复习理解本章所需的最小 Task 前置：

- coroutine 对象保存一次异步调用的执行状态；
- `asyncio.create_task(coroutine)` 把它包装成由事件循环调度的 Task；
- Task 保存最终结果、异常或取消状态，可以被 `await`；
- 创建 Task 的代码通常是它的所有者，应保存引用并最终收集它的结果或异常；
- 直接 `await coroutine` 沿当前 Task 的调用链执行；`create_task()` 才建立另一个 Task。

根 `pyproject.toml` 声明 Python `>=3.14`，Pyright 和 Ruff 目标也都是 Python 3.14。本次优化时核对的实际环境为：

| 项目 | 实际值 | 用途 |
|---|---:|---|
| CPython | 3.14.7 | 运行完整示例与运行时断言 |
| Ruff | 0.16.5 | 格式和常规代码检查 |
| Pyright | 1.1.411 | 基础静态类型检查 |

本章只使用 `asyncio`、`time` 等标准库，没有新增运行依赖或开发依赖。所有关键 API 都链接到 Python 3.14 官方文档。`asyncio.timeout()` / `timeout_at()` 从 Python 3.11 起提供；本项目只需按 3.14 语义学习，不需要兼容更旧解释器。

从仓库根目录独立运行：

```bash
.venv/bin/python -B java_to_python/chapters/21_asyncio_cancellation_timeouts/example.py
```

`-B` 只阻止生成 `.pyc`，不改变示例行为。现有 `java_to_python/run_all.py` 只支持前 10 章，本章没有接入它。

项目已配置且当前虚拟环境可用的检查命令是：

```bash
.venv/bin/ruff check java_to_python/chapters/21_asyncio_cancellation_timeouts
.venv/bin/ruff format --check java_to_python/chapters/21_asyncio_cancellation_timeouts
.venv/bin/pyright java_to_python/chapters/21_asyncio_cancellation_timeouts/example.py
```

## 21.2 `cancel()` 发出什么请求

先看一个可以独立运行的最小取消骨架：

```python
import asyncio


async def worker() -> None:
    try:
        await asyncio.Event().wait()
    finally:
        print("释放资源")


async def main() -> None:
    task = asyncio.create_task(worker())
    await asyncio.sleep(0)  # 让 worker 先运行到等待点
    accepted = task.cancel("用户停止")
    print(accepted, task.done(), task.cancelled())  # True False False
    try:
        await task
    except asyncio.CancelledError as error:
        print(error.args)  # ('用户停止',)


asyncio.run(main())
```

Python 3.14 的 `Task.cancel()` 定义是：安排一个 `CancelledError`，在事件循环下一次推进这个 Task 时抛入其包装的 coroutine。因此它有几个重要限制：

1. `cancel()` 不会在当前 Python 表达式中抢占另一个 Task；
2. 目标 Task 要先获得事件循环执行机会，取消才会被处理；
3. 若目标正在等待 Future，取消请求通常先传给这个被等待对象；
4. coroutine 可以在 `except` / `finally` 中执行清理，因而不会立刻结束；
5. coroutine 甚至可以错误地吞掉 `CancelledError`、返回值或抛出另一种异常，所以取消不保证最终状态一定是 cancelled。

`cancel()` 的布尔返回值只说明请求是否被接受。对一个已完成 Task 再调用通常返回 `False`；返回 `True` 仍不能替代 `await task`。`task.cancelling()` 可观察待处理的取消请求数量，也不表示清理已经结束。

这里不能照搬 Java `Future.cancel(true)` 的实现模型。Java 可以尝试中断执行任务的线程；Python Task 还必须获得事件循环执行机会，并让 `CancelledError` 通过 coroutine。协程可以运行清理，甚至错误地吞掉取消，因此一定要观察 Task 的最终状态。

### `CancelledError` 通常在哪里出现

如果目标 Task 正暂停在一个尚未完成的 `await`：

```python
await response_ready.wait()
```

事件循环再次推进它时，这个 `await` 会以 `CancelledError` 离开，异常再沿普通 Python 调用栈向外传播。它不是操作系统在任意机器指令处插入的硬中断。

如果 coroutine 正在连续执行 Python 代码、调用 `time.sleep()`、运行同步 HTTP 客户端或做长 CPU 循环，事件循环得不到执行权，取消注入就会推迟。`await` 也不是无条件切换点：等待一个已经完成的对象可能立即返回；决定响应性的根本仍是代码是否把执行机会交回事件循环。

这和 Java 线程收到 interrupt 后仍需要代码或阻塞 API 配合响应很像，但 Python Task 没有被强行停止的独占线程，也没有任意机器指令级抢占。

官方的总体清理建议见 Task cancellation。

## 21.3 四个阶段不能混成一句“任务取消了”

要准确观察这四个阶段，可以先用一个 Event 确认模拟 I/O 已经开始，再发出用户取消；异步清理再由另一个 Event 暂停。这样观察的是明确状态，而不是依靠 `sleep` 猜测调度时序：

| 阶段 | `done()` | `cancelled()` | 资源状态 | 能得出的结论 |
|---|---:|---:|---|---|
| `cancel()` 刚返回 | `False` | `False` | `OPEN` | 请求已登记，异常尚未注入 |
| I/O 收到 `CancelledError` | `False` | `False` | `OPEN` | 取消正在沿调用链传播 |
| `finally` 异步清理中 | `False` | `False` | `OPEN` | 不能开始依赖“资源已关”的下一步 |
| `await task` 抛取消后 | `True` | `True` | `RELEASED` | 任务以取消终止，清理已完成 |

这里的最终 `cancelled() is True` 有一个前提：包装的 coroutine 最终以 `CancelledError` 结束。如果它捕获后返回普通值，Task 会是“成功完成”，`cancelled()` 为 `False`，尽管调用方确实调用过 `cancel()`。

### 为什么必须 `await` 被取消的 Task

```python
task.cancel()
await task
```

第二行不是重复操作。两行职责不同：

- `cancel()` 发请求；
- `await task` 等到最终结果，观察取消或其他异常，并与 `finally` 清理完成建立顺序关系。

把 Task 存在列表、字典或对象字段里很重要，因为所有者需要强引用和后续管理入口；但“引用还在”只说明对象可找到，不说明它已完成。只保存引用、不在关闭边界真正等待，后续代码仍可能在资源清理期间继续运行，程序退出时也可能留下 pending Task。

## 21.4 三层调用链：记录以后要继续抛

本章的核心链路是：

```text
request_entry()                 入口层
  └─ await call_tool()          工具层
       └─ await simulated_io()  模拟 I/O 层
            └─ finally 释放资源
```

三个层次都可以为了日志、指标或局部清理观察取消，但不应把它改写成普通业务结果：

```python
async def call_tool() -> str:
    try:
        return await simulated_io()
    except asyncio.CancelledError:
        logger.info("工具调用被取消")
        raise  # 让取消继续到拥有这条调用链的边界
```

多数函数甚至不必捕获取消；把资源清理放在 `finally`，让异常自然传播更不容易出错：

```python
resource = open_resource()
try:
    return await use_resource(resource)
finally:
    resource.release()
```

如果清理本身包含 `await`，Task 在清理完成前仍是 pending。清理应尽可能有限、幂等，并能处理部分完成。另一次新的取消仍可能打断异步清理；复杂系统应由清楚的生命周期所有者设计关闭策略，而不是在每一层随意屏蔽取消。

## 21.5 `CancelledError` 为什么不属于普通业务异常

Python 3.8 起，`asyncio.CancelledError` 直接继承 `BaseException`，而不是 `Exception`。Python 3.14 的正式层级见 `asyncio.CancelledError`。

```text
BaseException
├─ SystemExit
├─ KeyboardInterrupt
├─ GeneratorExit
├─ asyncio.CancelledError
└─ Exception
   ├─ ValueError
   ├─ RuntimeError
   └─ 大多数业务与库异常
```

因此各种捕获写法的范围不同：

| 写法 | 会捕获 `CancelledError` 吗 | 本章建议 |
|---|---:|---|
| `except Exception:` | 不会 | 适合普通错误边界，但仍要避免不加分析地吞业务错误 |
| `except asyncio.CancelledError:` | 会 | 只在记录或取消专用清理时使用，通常裸 `raise` |
| `except BaseException:` | 会 | 只适合“清理后原样重抛”等很窄的基础设施边界 |
| 裸 `except:` | 会 | 范围同样过宽，还会捕获退出信号；通常不要写 |

这里尤其不能套用 Java 的 `catch (Exception e)` 直觉。Java 的 `InterruptedException` 会被它捕获，Python 的 `except Exception` 却不会捕获 `CancelledError`；只有更宽的 `except BaseException` 或裸 `except` 才会把取消一起拦下。

`except Exception` 不会误吞取消是有意的安全边界，但这不表示所有宽泛捕获都安全。下面两个反例都会破坏结构化取消：

```python
# 教学反例 1：把取消伪装成业务失败。
try:
    return await call_remote_tool()
except BaseException as error:
    return {"ok": False, "error": str(error)}

# 教学反例 2：记录后继续下一轮，用户已停止但工具仍会产生副作用。
try:
    await call_remote_tool()
except asyncio.CancelledError:
    logger.info("stopped")
    # 漏了 raise

await submit_side_effect()
```

一个受控反例可以在取消被吞后，由外部打开本地 Event 闸门让它有限结束。最终 Task 返回“伪成功”、`cancelled()` 为 `False`，甚至继续提交副作用。正确修复是清理后继续抛出取消，而不是在每个函数中 catch 后 continue。

## 21.6 直接 `await` 另一个 Task 时怎样传播

假设父 Task 正在直接等待子 Task：

```python
child = asyncio.create_task(call_tool())
result = await child
```

当父 Task 被取消时，它正在等待的对象也会收到取消。子 Task 是 Future-like 对象，因此取消沿等待边向内传播：

```text
cancel(parent)
        ↓
parent 正在 await child
        ↓
cancel(child)
        ↓
child 的 coroutine 收到 CancelledError 并执行 finally
        ↓
child 以取消结束
        ↓
parent 的 await 抛出 CancelledError，parent 再向外传播
```

即使子 Task 的引用一直保存在 `child_task` 变量里，清理闸门关闭时 `child_task.done()` 仍然为 `False`。只有打开闸门并真正等待父 Task 后，父子 Task 才都完成取消，资源也已经释放。

这段结论以子 coroutine 正常响应取消并重新抛出为前提。若子任务吞掉取消，它可能返回值或继续等待，父任务完成时间和最终异常也会受影响。若不希望父取消传播到子任务，那已进入 `shield()` 的特殊所有权场景，而不是默认行为。

## 21.7 `asyncio.timeout()`：给一段当前调用链划范围

正确捕获结构是：

```python
try:
    async with asyncio.timeout(2.0):
        await call_tool()
except TimeoutError:
    print("这段操作超过 2 秒")
```

`asyncio.timeout()` 是异步上下文管理器。进入时它记住当前 Task；到期时取消的也是当前 Task，不是自动创建的独立 worker。其运行过程是：

1. 当前 Task 进入 timeout 范围；
2. 截止时间到达，范围向当前 Task 发出取消；
3. 最深处尚未完成的 `await` 收到 `CancelledError`；
4. `CancelledError` 沿当前调用链向外传播，途中先完成 `finally`；
5. timeout 管理器在退出范围时识别自己引发的取消；
6. 范围外看到内置 `TimeoutError`。

它没有严格对应的单个 Java API。可以把它理解为“作用域级 deadline + 对当前执行单元发出取消请求 + 在边界转换异常”的组合，而不是一次带超时的 `Future.get(...)`。

所以由这个管理器产生的 `TimeoutError` 只能在 `async with` 外面捕获：

```python
# 教学反例：这里捕不到当前 timeout 管理器最终转换出的 TimeoutError。
async with asyncio.timeout(2.0):
    try:
        await call_tool()
    except TimeoutError:
        ...
```

范围里面在取消传播阶段看到的是 `CancelledError`。若为了日志捕获它，仍应 `raise`，让管理器有机会在范围边界正确转换。不要自己把所有 `CancelledError` 都写成 `TimeoutError`，因为取消也可能来自用户停止、服务关闭或父任务失败。

本章还验证了相反路径：进入一个尚有 60 秒的 timeout 范围后，由外部调用 `task.cancel()`。因为该范围并未到期，这次普通外部取消离开范围后仍是 `CancelledError`，没有被误报成超时。如果外部取消与截止到期恰好竞争，应把语义交给各自清楚的所有权边界，不依赖偶然调度顺序。

### `TimeoutError` 的版本名称

Python 3.11 起，`asyncio` 超时 API 抛内置 `TimeoutError`；`asyncio.TimeoutError` 是它的别名。3.14 新代码直接写：

```python
except TimeoutError:
    ...
```

## 21.8 `wait_for()`：给一个明确目标设置相对超时

```python
result = await asyncio.wait_for(one_attempt(), timeout=0.8)
```

`asyncio.wait_for()` 关注的是一个 awaitable：

- Python 3.14 中，向 `wait_for` 传入 coroutine 时，不会先把它包装成独立 Task，而是在当前等待路径中直接执行；这是 Python 3.12 起的变化；
- 若要保存任务身份、检查状态或让其他代码管理它，应先显式 `create_task()`，再把 Task 传入；
- 若按时完成，返回它的结果；
- 若等待超时，取消 awaitable，并通常向等待者抛 `TimeoutError`；
- 若 `wait_for()` 自己被取消，取消也传播给目标；
- 它会等待目标实际完成取消，所以总墙钟时间可能超过传入的 `timeout`；
- 若目标错误地吞掉取消并返回值，Python 3.14 的 `wait_for()` 会返回那个值，而不是保证抛 `TimeoutError`。

如果传入一个已有的 `target_task`，并故意让它的 `finally` 等待清理闸门，计时到期后可以观察到：

```text
target 已收到取消并进入 finally
target.done()   == False
wait_for.done() == False
资源             == OPEN
```

打开闸门后，目标释放资源并以取消结束；这时 `wait_for()` 才向调用方报告 `TimeoutError`。因此“0.8 秒超时”通常是“不再愿意等待正常结果的预算”，不是“函数保证在第 0.800 秒已经返回”。

这也是它与 Java `Future.get(800, MILLISECONDS)` 最重要的差异：Java 的限时 get 默认只是让当前等待者超时，Future 仍可继续运行；Python 的 `wait_for()` 会主动取消目标。如果只想停止等待并保留 pending Task，应使用 `asyncio.wait()`。它在超时时不抛 `TimeoutError`，而是返回 `done` / `pending` 两个集合；调用方仍要继续承担 pending Task 的生命周期管理。

### `timeout()` 与 `wait_for()` 怎样选

| 需求 | 优先选择 | 作用对象 |
|---|---|---|
| 限制一个明确 awaitable 或一次尝试 | `wait_for(aw, seconds)` | 目标 awaitable；超时会取消它 |
| 限制多条 `await`、循环和解析组成的范围 | `async with timeout(seconds)` | 执行该范围的当前 Task |
| 多层代码共享同一个截止时刻 | `async with timeout_at(deadline)` | 当前 Task，使用绝对单调时钟 |
| 只想等一段时间、超时后保留 pending | `asyncio.wait(..., timeout=...)` | 返回 `done` / `pending`，不自动取消 pending；属于 Task 收集主题 |

不要仅凭函数名选择。先画出“哪个 Task 执行范围、哪个 awaitable 是目标、超时后谁取消和谁回收”。

## 21.9 相对预算会偷偷膨胀，绝对截止时间不会

下面的错误设计让每层都重新获得完整 10 秒：

```python
# 教学反例：每次调用都从“现在”重新计算 10 秒。
async with asyncio.timeout(10):  # Agent 请求
    async with asyncio.timeout(10):  # 工具
        await asyncio.wait_for(attempt(), 10)  # 单次尝试
```

如果还有重试、退避、多个工具和清理，整个请求很容易远超调用方承诺的预算。正确模型与 Java 使用 `System.nanoTime()` 传递剩余预算相似：先在最外层计算一次绝对截止时间，再向内传递同一个数：

```python
loop = asyncio.get_running_loop()
request_deadline = loop.time() + 10.0

try:
    async with asyncio.timeout_at(request_deadline):
        answer = await run_tool(request_deadline=request_deadline)
except TimeoutError:
    ...
```

`loop.time()` 返回事件循环使用的单调时钟；它适合计算经过时间和 deadline，不受系统墙钟校准影响。它的起点没有日历意义，不能拿来与 `time.time()`、Unix 时间戳或 `datetime.now()` 直接比较。

`asyncio.timeout_at(deadline)` 接收同一时钟体系下的绝对时刻。内层预算可以更严格，但不能更宽：

```python
loop = asyncio.get_running_loop()
tool_deadline = min(request_deadline, loop.time() + 2.0)
attempt_deadline = min(tool_deadline, loop.time() + 0.8)
attempt_timeout = max(0.0, attempt_deadline - loop.time())
```

三层职责应保持清楚：

| 层次 | 示例限制 | 失败后通常做什么 |
|---|---|---|
| 单次尝试 | 最多 0.8 秒，且不越过上层 deadline | 目标取消并清理；剩余预算足够时才考虑下一次尝试 |
| 单工具预算 | 搜索工具总共最多 2 秒 | 停止该工具的所有尝试，向工具调用边界报告预算耗尽 |
| Agent 请求 deadline | 整个请求最多 10 秒 | 停止继续编排其他工具，向请求边界报告整体超时 |

每次重试前都要从原 deadline 重新计算剩余时间：

```python
remaining = tool_deadline - loop.time()
if remaining <= 0:
    raise TimeoutError("工具总预算已耗尽")

attempt_timeout = min(0.8, remaining)
result = await asyncio.wait_for(one_attempt(), timeout=attempt_timeout)
```

这不是让重试“获得剩余时间之外的新预算”。第一次尝试和清理已经消耗的时间不会被忘掉。

### 只让更严格的内层创建自己的超时范围

若工具的局部 deadline 与请求 deadline 完全相同，在相同时刻嵌套两个超时范围没有额外保护，还会让“是谁的预算先耗尽”难以命名。本章示例采用：

- 工具 deadline 更早：工具创建 `timeout_at(tool_deadline)`，并可在范围外转换成 `ToolBudgetExceeded`；
- 工具 deadline 不更早：工具不再复制范围，直接沿用外层请求 deadline；
- 无论哪条路径，外部 `CancelledError` 都不由工具伪装成超时。

嵌套 timeout 管理器本身受官方支持，但业务层仍要决定每个边界代表什么。不要依赖两个完全相同到期时刻的偶然回调顺序来分类错误。

## 21.10 超时为何不承诺精确到点停止

取消是协作协议，超时又建立在取消上，所以它不是硬中断。三个边界尤其重要。

### 1. 事件循环被阻塞

```python
async with asyncio.timeout(0.01):
    time.sleep(1)  # 错误：事件循环无法执行超时回调
    await something()
```

计时器可能已经到期，但唯一的事件循环线程还卡在 `time.sleep()`。用一个有限的同步阻塞即可观察到“同步阻塞先完成，之后才出现 `TimeoutError`”；可靠的测试应该断言事件顺序，而不是某个精确毫秒数。

### 2. 清理阶段需要时间

取消进入 coroutine 后，`finally` 会先运行。关闭协议、刷新缓冲、结束事务或等待子 Task 都可能耗时。`wait_for()` 明确会等待目标实际完成取消，`timeout()` 也要等当前调用链离开它的范围后才能在外侧抛 `TimeoutError`。

预算设计应预留清理余量，并在监控中区分：deadline 到期时刻、开始取消时刻、清理结束时刻和调用返回时刻。不要把一个 timeout 数字当成端到端延迟的硬上界。

### 3. 底层工作可能不可取消或副作用不可撤回

取消 Python Task 只改变本地等待与协程控制流，不自动提供以下保证：

- 已在线程中开始的同步函数会立刻停止；
- 已发出的 HTTP 请求一定从服务器队列撤回；
- 数据库已经提交的事务自动回滚；
- MCP tool 已执行的外部动作被逆转；
- 第三方客户端一定以与本地替身相同的方式关闭连接。

原生异步客户端收到取消后可能关闭 socket、发送协议级取消，或只是停止本地等待；具体行为取决于库版本、传输协议和远端实现。需要幂等键、事务、补偿操作或服务端 deadline 时，应把它们当作独立的系统设计，而不是期待 `Task.cancel()` 撤销现实世界。

这和 Java 中断线程不能自动回滚已提交事务是同一个工程边界。两边都需要单独设计幂等与补偿，不能把本地控制流已经停止当作远端副作用已经撤回。

本章按课程边界不运行真实 `asyncio.to_thread()` 或 HTTP 实验；这些放到第 25 章。本地 `FakeIOResource` 只验证 Python 层的传播和清理顺序，没有覆盖线程停止、DNS、TLS、socket、连接池、服务器状态或远程副作用。

## 21.11 选学：`shield()` 保护谁

默认直接等待会传播取消。有时调用方取消了，但某项已经开始的工作必须完成，例如一个由当前组件负责持久化、且中断比完成更危险的短操作。此时可以考虑：

```python
child = asyncio.create_task(critical_work())
try:
    result = await asyncio.shield(child)
except asyncio.CancelledError:
    # 当前调用方仍然被取消；child 没有因这次等待者取消而被取消。
    result = await child  # 当前函数若是所有者，就要决定并收集最终状态
    raise
```

`asyncio.shield()` 保护的是传入的 awaitable，不是外面的调用方：

- 调用方仍会在 `await shield(...)` 处收到 `CancelledError`；
- 这一次调用方取消不会向内取消 child；
- child 若被其他代码取消，`shield()` 仍会反映那个取消；
- 必须保存 child 的强引用；事件循环只保留弱引用；
- 创建 child 的所有者仍要决定让它完成还是主动取消，并最终 `await` 以收集结果或异常。

如果 owner 取消后仍收集 child，再重新抛出自己的取消，就会同时得到“child 正常完成”和“owner 最终保持 cancelled”两个结果。代价是 owner 的最终退出要等待 child，因此这必须是明确的所有权决定。

不要把整个工具调用链随手套进 `shield()`。那会让用户已经停止时工具仍在运行，而系统又没有清楚的回收者。若说不清谁保存引用、谁等待结束、程序关闭时怎么办，就不应使用它。

## 21.12 示例运行导览

`example.py` 的完整入口按以下顺序运行：

1. **正常完成**：三层链路得到结果，I/O `finally` 释放一次资源；
2. **用户主动停止**：观察 cancel 返回、异常注入、清理中和最终 cancelled 四阶段；
3. **直接 await 子 Task**：父取消传播给子，父等待子清理后再完成；
4. **异常层级与吞取消反例**：验证 `except Exception` 不捕获取消，以及宽捕获后继续的后果；
5. **`timeout()`**：范围内是 `CancelledError`、范围外是 `TimeoutError`，外部取消不被改写；
6. **`wait_for()`**：单次尝试超时取消目标，并等待人为延迟的清理；
7. **绝对 deadline**：分别触发更短的工具预算和整个请求截止时间；
8. **非硬中断**：同步阻塞完成后，事件循环才处理已经到期的 timeout；
9. **选学 `shield()`**：调用方取消，child 继续，由所有者保存并最终收集。

所有并发顺序断言都先用 Event 确认状态，不假设 Task 创建后已经开始，也不依赖多个 Task 的固定调度顺序。短时间值只用于触发超时或展示有限阻塞，没有断言精确耗时。反例最终都会释放资源并收集 Task，不会挂住入口或污染后续段落。

## 21.13 常见错误与修复方向

| 错误 | 可观察后果 | 修复方向 |
|---|---|---|
| `task.cancel()` 后立即关闭共享资源 | Task 的 `finally` 可能仍在使用或清理资源 | 由所有者 `await task` 后再越过生命周期边界 |
| 把 `cancel()` 返回 `True` 当作已经停止 | `done()` / `cancelled()` 仍为 `False` | 区分请求接受与最终状态 |
| `except BaseException` 后返回错误字典 | 用户取消被伪装成业务失败 | 只为清理或记录时裸 `raise` |
| `except CancelledError` 后继续下一工具 | 用户停止后仍产生远程副作用 | 清理后传播到编排边界 |
| 在 timeout 范围内部捕获其 `TimeoutError` | 捕获位置看不到最终转换 | 在 `async with` 外捕获 |
| 把任意 `CancelledError` 改成 `TimeoutError` | 用户停止被误报成系统超时 | 让 timeout 管理器只转换自己发起的取消 |
| 每层都重新 `timeout(10)` | 重试和多工具令总时长膨胀 | 外层计算一次 `loop.time()` deadline 并向内传递 |
| 认为 `wait_for(..., 1)` 一秒后必返回 | 目标清理或吞取消会延后甚至改变结果 | 把 1 秒理解为发起取消的等待预算 |
| 只保存后台 Task 引用，从不 await | 无法确认清理，也可能漏掉异常 | 在所有者关闭边界取消并收集 |
| 用 `shield()` 让所有工作“不受取消” | 调用方退出后工作仍游离运行 | 只屏蔽明确对象，并指定最终回收者 |

## 21.14 递进练习

以下练习不给完整答案。建议先写出预测事件，再运行并用断言验证。

### 练习 1：实现可观察的取消四阶段

要求：实现 `download_model()`，内部打开一个本地 `FakeConnection`，等待 `data_ready`，并在 `finally` 等待 `cleanup_allowed` 后关闭。调用方先等 `started`，再取消 Task。

输入：任务名固定为 `"model-download"`，取消消息固定为 `"request closed"`。

验收标准：

- `cancel()` 刚返回时 `done()` 和 `cancelled()` 都为 `False`；
- `cleanup_started` 后、打开清理闸门前，连接仍打开且 Task 未完成；
- `await task` 得到 `CancelledError`，消息可观察；
- 最终连接只关闭一次，Task 的 `done()` 和 `cancelled()` 都为 `True`。

### 练习 2：修复“用户已停止但工具仍继续”

要求：给出一个捕获 `BaseException` 后继续执行 `write_result()` 的错误工具链，先复现问题，再把它改为只记录并传播取消。两个版本都必须有限结束并清理资源。

输入：`tool_started`、`write_allowed` 两个 Event；写入内容固定为 `"answer-42"`。

验收标准：

- 错误版能观察到取消后仍写入，并最终错误地 `cancelled() is False`；
- 修复版不调用 `write_result()`，调用方收到 `CancelledError`；
- 两版资源最终都关闭，不遗留 Task；
- 能解释为什么改成 `except Exception` 与专门捕获后裸 `raise` 的适用边界不同。

### 练习 3：三层预算与重试

要求：整个请求 deadline 为 2 秒，某工具总预算为 1.2 秒，每次尝试预算为 0.4 秒，最多三次。用 `loop.time()` 只计算一次请求 deadline，每次尝试前从工具 deadline 重新计算剩余时间。

输入：本地替身前两次等待到单次尝试超时，第三次由 `success_allowed` Event 决定是否成功；不要访问真实网络。

验收标准：

- 内层收到的是同一个绝对请求 deadline，而不是新的 2 秒；
- 每次尝试的相对 timeout 不超过工具和请求的剩余预算；
- 工具预算耗尽后不再开始新尝试；
- 取消的尝试都完成 `finally`，断言不依赖恰好在某毫秒返回。

### 练习 4：比较范围超时与目标超时

要求：分别用 `asyncio.timeout()` 包住“加载 + 解析”两步，以及用 `wait_for()` 只限制加载 Task。让加载 Task 的清理等待一个 Event。

输入：加载永不正常完成；解析函数只记录是否被调用；超时值使用很短但有限的常量。

验收标准：

- `timeout()` 内部取消路径观察到 `CancelledError`，范围外捕获内置 `TimeoutError`；
- `wait_for()` 到期后，在清理 Event 放行前仍未完成；
- 两种路径都不调用解析函数；
- 外部主动取消长 timeout 范围时仍观察到 `CancelledError`。

### 练习 5（选学）：给 `shield()` 指定真正的所有者

要求：创建一个必须写完本地审计记录的 child Task，owner 用 `shield()` 等待。取消 owner 后，child 由 Event 放行并正常结束，owner 收集 child 后继续传播自己的取消。

输入：审计记录固定为 `"request-aborted"`。

验收标准：

- owner 取消后立刻观察到 `CancelledError`，child 没有因这次取消而变为 cancelled；
- owner 始终保存 child 强引用；
- child 的结果或异常被真正 `await` 和收集；
- owner 最终仍是 cancelled，不把 `shield()` 当成吞取消工具。

## 21.15 本章验收、验证与边界

本章文件：

| 文件 | 内容 |
|---|---|
| `README.md` | 取消协议、传播、超时选择、共同 deadline、边界与练习 |
| `example.py` | 九组本地实验、中文事件轨迹和运行时断言 |

完整入口在 CPython 3.14.7 上执行到末尾，运行时断言验证了：

- 正常完成和用户取消都只释放一次资源；
- `cancel()` 返回时 Task 尚未停止，异步 `finally` 完成前也尚未进入最终取消状态；
- 直接等待子 Task 时，父取消向子传播，并等待子清理；
- `CancelledError` 的继承层级以及吞掉取消后 Task 错误成功的结果；
- `timeout()` 的范围内外异常类型，以及未到期范围不改写普通外部取消；
- `wait_for()` 在目标清理期间仍未完成；
- 工具局部预算和 Agent 请求共同 deadline 的不同边界；
- 同步阻塞使超时回调延后；
- `shield()` 下 child 和调用方各自的最终状态。

同时执行了 Ruff 常规格式检查和 Pyright 基础类型检查。静态检查验证的是格式、常见缺陷与类型一致性，不等于运行验证。

未验证的范围包括真实 HTTP、LLM、MCP、数据库、线程、操作系统 I/O 取消、连接池、服务端 deadline、事务回滚、重试退避以及远程副作用撤销。本章也不把 `uncancel()` 当作常规修复方案；吞掉取消会破坏 `timeout()` 等依赖取消协议的组件，业务代码应优先保持传播。

学完后，请尝试不看正文逐层回答：

- `cancel()` 返回、Task 收到 `CancelledError`、`finally` 完成和 `cancelled()` 为真分别在何时？
- 为什么 `except Exception` 不捕获取消，而裸 `except` 会？记录取消后为什么通常要 `raise`？
- 父 Task 直接 await 子 Task 时，父取消沿哪条边传播？保存引用解决了什么，又没有解决什么？
- `timeout()` 为什么在范围内表现为取消、在范围外表现为超时？外部取消为何不能随意改写？
- `wait_for()` 与 `timeout()` 分别限制目标还是范围，为什么两者都可能晚于数字本身返回？
- 为什么共同 deadline 必须用 `loop.time()` 只计算一次，并向内层传递绝对值？
- 本地 Task 已取消，为什么线程、服务器或已经提交的副作用仍可能继续？
