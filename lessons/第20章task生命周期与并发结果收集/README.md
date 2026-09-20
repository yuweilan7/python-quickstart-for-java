# 第 20 章：Task 生命周期与并发结果收集

一句话先说结论：coroutine 描述“要做什么”，Task 才是事件循环中可独立调度、可追踪结果、需要被明确回收的一次执行。

真实服务经常要同时读取用户资料、搜索知识库、检查策略。写出三个 `async def` 并不难，难的是回答下面这些工程问题：

- 三项工作究竟是顺序执行，还是已经并发启动？
- 哪个对象保存每项工作的业务身份、结果和异常？
- 一个工具先失败时，其他工具会不会继续运行？
- 超时或提前返回后，尚未完成的工作由谁处理？
- 请求已经结束，后台任务还能否安全存活到应用退出？

本章会建立“创建、持有、等待、收集、回收”的完整心智模型。取消协议放在第 21 章，结构化并发 `TaskGroup` 放在第 22 章；这里先把现有项目中最常见的 `create_task()`、`gather()`、`as_completed()` 和 `wait()` 讲清楚。

学完后，你应该能够：

- 区分 coroutine、Task 和 Future；
- 判断一段代码何时真正获得并发性；
- 正确读取 Task 的成功、异常和取消状态；
- 根据结果顺序、提前返回和错误策略选择收集 API；
- 为每个 Task 找到明确的拥有者，并保证退出前没有遗留工作或未取得的异常。

## 20.1 先用 Java 心智模型找准位置

可以先把 `asyncio.Task` 类比成“提交给执行器后得到的 `Future` 句柄”，但不能完全等同。`asyncio` 默认不会为每个 Task 建立线程，也不会把工作自动丢进线程池。事件循环在一个线程内进行协作式调度：当前 Task 执行到真正会暂停的 `await` 时，其他 Task 才有机会前进。

| Python 概念 | 可以先类比的 Java 概念 | 相似点 | 不能等同的地方 |
| --- | --- | --- | --- |
| coroutine 对象 | 尚未提交的任务描述 | 都表示一项待执行工作 | 调用 `async def` 只创建对象，不会自己执行 |
| `asyncio.Task[T]` | `Future<T>` / `CompletableFuture<T>` 加执行句柄 | 保存完成状态、结果或异常，可以被等待 | Task 还负责推进 coroutine，并绑定到事件循环；不代表新线程 |
| `asyncio.Future[T]` | `CompletableFuture<T>` 的结果占位能力 | 都能在未来变为成功、失败或取消 | 它是低层桥接对象，通常由事件循环或库创建；不负责执行 coroutine |
| `await task` | `Future.get()`、`join()` 的等待意图 | 都在结果就绪后继续，并传播失败 | `await` 暂停当前 Task，不阻塞事件循环线程 |
| `task.result()` | `Future.get()` | 都尝试取得结果 | Python 的 `result()` 不会等待；未完成时抛 `InvalidStateError` |
| `asyncio.create_task(coro)` | `executor.submit(...)` | 都产生一个可管理的执行句柄 | `create_task()` 只把 coroutine 安排到当前事件循环，不提供线程级并行、持久化或重试 |

因此，“异步”不等于“多线程”，“并发推进”也不等于“CPU 并行”。如果 coroutine 内部直接调用阻塞式 HTTP 客户端或执行长时间 CPU 计算，事件循环仍会被卡住。如何隔离阻塞 I/O 和 CPU 工作属于后续章节；本章所有例子只讨论 Task 的调度与结果管理。

## 20.2 coroutine、Task、Future 是怎样连起来的

先看三者的数据流：

```text
调用 async def
    |
    v
coroutine 对象：保存参数、局部状态和待执行位置
    |
    +-- 直接 await --> 由当前 Task 沿调用链推进
    |
    +-- create_task(coro)
            |
            v
        新 Task：事件循环独立调度，并保存最终结果、异常或取消状态

Future：表示“将来会有最终状态”的低层 awaitable
Task 是 Future-like 对象，但还会主动推进它包装的 coroutine
```

### 调用 `async def` 只得到一次性的 coroutine 对象

```python
async def load_profile() -> dict[str, str]:
    return {"tier": "gold"}


profile_coro = load_profile()  # 函数体此时还没有运行
profile = await profile_coro  # 当前 Task 从这里开始推进它
```

coroutine 对象表示一次执行路径。它正常结束、失败或被取消后都不能倒带，再次 `await` 同一个对象会抛 `RuntimeError: cannot reuse already awaited coroutine`。

### `create_task()` 增加了独立调度单元和生命周期句柄

```python
task = asyncio.create_task(load_profile(), name="tool:profile")
profile = await task
```

`create_task()` 把 coroutine 包装成 Task，安排它尽快执行，并立即返回句柄。调用方因此可以：

- 用 `await task` 等待并取得结果；
- 用 `done()`、`cancelled()` 检查状态；
- 在完成后用 `result()`、`exception()` 读取最终状态；
- 用 `name=` 提供诊断名称，并用字典关联请求 ID、工具名等业务身份；
- 在不再需要结果时发出取消请求，并继续等待清理完成。

完成后的 Task 保存着最终状态，所以可以被多次 `await`。它不会重新执行 coroutine；每个等待者观察到的都是同一次执行的结果或异常。

### Future 主要用于衔接底层回调式 API

Future 只表示“结果以后才确定”。例如底层 I/O 可以在 socket 就绪时设置 Future 的结果，从而唤醒正在等待它的 Task。应用层通常不应手工调用 `Future()`、`set_result()` 或 `set_exception()`；需要 Future 时，优先使用事件循环或库提供的对象。

## 20.3 并发性由“何时创建 Task”决定

假设两个工具没有数据依赖：

```python
import asyncio


async def call_tool(name: str, delay: float) -> str:
    await asyncio.sleep(delay)  # 模拟不会阻塞事件循环的 I/O 等待
    return f"{name}:ok"
```

下面三种写法看起来接近，实际生命周期不同。

### 写法一：直接依次 `await`，工作顺序执行

```python
profile = await call_tool("profile", 0.2)
documents = await call_tool("search", 0.1)
```

第二个 `call_tool(...)` 表达式要等第一行完成后才会求值。因此第一项结束前，第二个 coroutine 对象都还不存在。后一步依赖前一步结果时，这正是正确写法。

### 写法二：创建一个就立刻等待，工作仍然不重叠

```python
profile_task = asyncio.create_task(call_tool("profile", 0.2))
profile = await profile_task

search_task = asyncio.create_task(call_tool("search", 0.1))
documents = await search_task
```

这里确实创建了 Task，但第二个 Task 仍要等第一个完成后才创建。相比直接 `await`，它多了一个独立句柄，却没有让两项业务工作并发。

### 写法三：先创建全部 Task，再逐个等待，工作已经并发启动

```python
profile_task = asyncio.create_task(call_tool("profile", 0.2))
search_task = asyncio.create_task(call_tool("search", 0.1))

profile = await profile_task
documents = await search_task
```

当前 Task 第一次暂停时，两个子 Task 都已经可以被事件循环推进。虽然读取结果时先写 `await profile_task`，搜索 Task 仍可更早完成；它会把结果保存在自身状态中，等调用方稍后读取。

所以不能只看 `await task1; await task2` 就判断串行。应先问：两个 Task 是否已经在第一个 `await` 之前全部创建？

### 不要把五种顺序混成一个“先后顺序”

| 顺序 | 由什么决定 | 是否有稳定契约 |
| --- | --- | --- |
| 创建顺序 | `create_task()` 语句的执行顺序 | 代码可直接确定 |
| 首次开始顺序 | 任务工厂和事件循环调度 | 不应依赖偶然顺序 |
| 完成顺序 | I/O 就绪、工作量、异常和取消 | 通常不同于创建顺序 |
| 处理顺序 | 调用方选择等待哪个结果 | 由收集 API 和业务代码决定 |
| 聚合结果顺序 | API 的返回契约 | 例如 `gather()` 明确按输入顺序 |

日志里“先创建”不等于“先完成”。测试并发语义时，应使用 `asyncio.Event` 这样的同步信号固定状态变化，不要用“睡 10 ms 的任务应该先完成”作为脆弱断言。

### Python 3.14：`eager_start` 会改变开始时机

本项目目标版本是 Python 3.14，`create_task()` 可以显式传入 `eager_start`：

```python
task = asyncio.create_task(cached_lookup(key), eager_start=True)
```

`eager_start=True` 允许 coroutine 在 `create_task()` 调用期间立即运行，直到第一次阻塞；如果它始终没有阻塞，Task 甚至可能在 `create_task()` 返回前就已经成功或失败。省略参数时，是否 eager 由事件循环的任务工厂决定。

这不只是性能开关，而是执行顺序的变化。任何依赖“`create_task()` 返回后我还能先修改共享状态”的代码，都可能在 eager 模式下失效。业务正确性不应依赖 Task 恰好在哪一轮循环首次运行。

## 20.4 Task 的最终状态应该怎样读取

Task 未完成时处于 pending。结束后只有三类最终结果：正常返回、抛出异常、完成取消。`done()` 对这三类都会返回 `True`，所以它不等于“执行成功”。

| 状态 | `done()` | `cancelled()` | `result()` | `exception()` |
| --- | --- | --- | --- | --- |
| pending | `False` | `False` | 抛 `InvalidStateError` | 抛 `InvalidStateError` |
| 成功 | `True` | `False` | 返回结果 | 返回 `None` |
| 普通异常 | `True` | `False` | 重新抛原异常 | 返回异常对象 |
| 完成取消 | `True` | `True` | 抛 `CancelledError` | 抛 `CancelledError` |

业务代码最常用也最安全的方式是直接等待：

```python
try:
    value = await task
except ToolCallError as error:
    log_tool_failure(error)
```

`await task` 会先等待最终状态，再返回成功值或传播异常。只有在已经明确知道 Task 完成时，才适合同步调用 `result()` 或 `exception()`，例如检查 `wait()` 返回的 `done` 集合。

状态检查顺序也很重要：取消同样属于 done，而 `exception()` 在取消状态会抛 `CancelledError`。通用检查通常先判断 `cancelled()`，再判断 `done()`，最后读取异常或结果。

`task.cancel()` 只是发出请求，不会同步把 Task 改成 cancelled。请求要在事件循环再次推进 Task 时送入 coroutine，coroutine 还有机会执行 `finally`。完整传播和资源清理规则见第 21 章。

## 20.5 创建 Task 就意味着承担所有权

直接 `await coroutine` 时，工作自然属于当前调用链。调用 `create_task()` 后，事件循环中出现了一个可独立存活的执行单元，创建者必须回答四个问题：

1. 谁保存它的强引用和业务身份？
2. 谁等待它完成？
3. 谁读取并处理它的结果或异常？
4. 所属请求或应用退出时，谁让它结束并完成清理？

在一次请求内，最直观的做法是显式保存所有 Task：

```python
tasks = {
    "profile": asyncio.create_task(load_profile(), name="tool:profile"),
    "search": asyncio.create_task(search_documents(), name="tool:search"),
}

profile = await tasks["profile"]
documents = await tasks["search"]
```

Task 的 `name=` 主要服务于日志和诊断；`dict[str, Task]` 才明确保存了业务身份与强引用。

下面这种“提交后不管”不是可靠后台队列：

```python
asyncio.create_task(report_metric())  # 教学反例
```

事件循环只保存 Task 的弱引用。没有其他强引用时，Task 可能在完成前被垃圾回收；即使它完成了，异常也可能无人取得，最终出现 `Task exception was never retrieved`。而且 `create_task()` 没有持久化、重试、跨进程恢复或应用退出协议。

根据生命周期选择拥有者：

- 请求内的相关子任务：在请求返回前全部等待；新代码可在第 22 章改用 `TaskGroup`。
- 跨请求但属于当前进程的任务：交给应用级监督器，登记名称，在 shutdown 时统一等待并记录结果。
- 进程重启后仍必须执行的工作：交给消息队列或持久化任务系统，不要依赖裸 Task。

## 20.6 `gather()`：等待一组结果，并保持输入顺序

`gather(*aws)` 接受多个 awaitable。裸 coroutine 会被自动安排为 Task；已经是 Task 的输入则保持原对象不变。全部成功时，它返回一个列表，列表位置对应输入位置，而不是完成顺序。

```python
async def load(name: str, delay: float) -> str:
    await asyncio.sleep(delay)
    return name


tasks = [
    asyncio.create_task(load("profile", 0.3)),
    asyncio.create_task(load("search", 0.1)),
    asyncio.create_task(load("policy", 0.2)),
]
results = await asyncio.gather(*tasks)
assert results == ["profile", "search", "policy"]
```

无论三项工作的实际完成顺序怎样，`results` 都按 profile、search、policy 排列。因此 `gather()` 很适合“并发取得固定字段，最后按固定位置组装请求”的场景。

### 一个子 Task 失败时，兄弟 Task 默认继续运行

默认 `return_exceptions=False`。第一个普通异常会立即传播给等待 `gather()` 的调用方，但其他输入不会因此被自动取消：

```python
tasks = [asyncio.create_task(call()) for call in tool_calls]

try:
    results = await asyncio.gather(*tasks)
except Exception:
    # 原异常传播后，兄弟任务可能仍在运行；继续收齐所有最终状态。
    await asyncio.gather(*tasks, return_exceptions=True)
    raise
```

这段收尾代码选择“让兄弟任务自然完成”。如果业务要求“一项失败，其他项停止”，应按第 21 章的协议请求取消并等待清理，或在第 22 章使用具有结构化失败语义的 `TaskGroup`。

还要区分 `gather()` 的聚合对象和子 Task：

- 子 Task 抛普通异常：聚合等待提前失败，其他子 Task 默认继续。
- `gather()` 自身被取消：取消会传给尚未完成的输入。
- 异常已传播、聚合对象已经 done 后，再调用 `gather.cancel()`：不能倒回去取消仍在运行的兄弟。

如果异常直接离开顶层 coroutine，`asyncio.run()` 关闭事件循环时会取消遗留任务。此时日志里“兄弟被取消”是 runner 的退出清理，不能反推为 `gather()` 的普通异常策略。

### `return_exceptions=True` 只改变交付形式

```python
outcomes = await asyncio.gather(*tasks, return_exceptions=True)

for task, outcome in zip(tasks, outcomes, strict=True):
    if isinstance(outcome, BaseException):
        record_failure(task.get_name(), outcome)
    else:
        consume(outcome)
```

普通异常不再被 raise，而是作为对应输入位置的列表元素返回。它并不等于“错误已经处理”；调用方仍要记录、分类、转换或决定是否允许部分成功。

## 20.7 `as_completed()`：谁先完成就先处理谁

当结果越早返回越有价值，例如更新 UI、输出流式进度或尝试多个只读数据源，可以使用 `as_completed()`。本项目使用 Python 3.14，应优先采用异步迭代形式：

```python
tasks = {
    "profile": asyncio.create_task(load_profile(), name="tool:profile"),
    "search": asyncio.create_task(search_documents(), name="tool:search"),
}
task_names = {task: name for name, task in tasks.items()}

async for finished_task in asyncio.as_completed(tasks.values()):
    name = task_names[finished_task]
    result = await finished_task
    print(name, result)
```

从 Python 3.13 起，异步迭代时，如果输入本来就是 Task 或 Future，产出的就是对应原对象。因此可用对象身份找回业务名称。产出的 Task 已完成，但仍要 `await` 它或调用 `result()` 才能取得结果；如果该 Task 失败，异常会在这里传播。

旧代码还常使用普通迭代形式：

```python
for next_result in asyncio.as_completed(tasks.values()):
    result = await next_result
```

普通 `for` 产出的是新的包装 coroutine，不是输入的原 Task，不能用它查询上面的 `task_names`。这两种形式都合法，但对象身份规则不同。

设置 `timeout=` 后，如果总超时时间内仍有输入未完成，迭代会抛 `TimeoutError`。`as_completed()` 不会自动取消剩余任务；调用方仍需保存它们，并决定继续等待、转交还是取消。

## 20.8 `wait()`：只报告 done/pending，策略由调用方决定

`wait()` 适合“先观察一部分状态，再决定下一步”：

```python
done, pending = await asyncio.wait(
    tasks,
    timeout=1.0,
    return_when=asyncio.FIRST_COMPLETED,
)
```

Python 3.11 起不能把裸 coroutine 直接传给 `wait()`；应传入非空的 Task/Future iterable。它返回两个无序集合，不保留输入位置语义。

| `return_when` | 返回时机 |
| --- | --- |
| `FIRST_COMPLETED` | 任意一项成功、失败或取消完成 |
| `FIRST_EXCEPTION` | 任意一项以异常结束；若始终无异常，则等同 `ALL_COMPLETED` |
| `ALL_COMPLETED` | 全部完成或取消；默认值 |

`timeout` 到期时，`wait()` 不会抛 `TimeoutError`，也不会取消输入。未完成项只会出现在 `pending` 集合中。因为成功、异常和取消都属于 done，读取结果时仍要分类：

```python
for task in done:
    if task.cancelled():
        report_cancelled(task)
    elif (error := task.exception()) is not None:
        report_failed(task, error)
    else:
        consume(task.result())

# pending 仍在运行，不能直接丢弃。
if pending:
    await asyncio.gather(*pending, return_exceptions=True)
```

示例最后选择继续等待 pending。真实业务也可以把它们转交给另一个明确拥有者，或者请求取消并等待取消完成；无论选哪种，`wait()` 本身都不会替调用方完成生命周期管理。

## 20.9 怎样选择结果收集方式

| 需求 | 推荐方式 | 关键契约 |
| --- | --- | --- |
| 后一步依赖前一步结果 | 直接顺序 `await` | 依赖关系明确，不需要并发 |
| 全部成功后按固定字段组装结果 | 显式创建 Task，再 `gather()` | 并发等待，结果按输入顺序 |
| 允许部分失败，并要收齐每项状态 | `gather(..., return_exceptions=True)` | 异常成为对应位置的元素，仍需处理 |
| 谁先完成就先消费谁 | 异步迭代 `as_completed()` | 按完成顺序产出，显式 Task 身份可保留 |
| 等首项、首个异常或一段时间后再决策 | `wait()` | 返回 done/pending，不自动处置 pending |
| 一组相关子任务需要统一失败和退出边界 | 第 22 章的 `TaskGroup` | 结构化并发提供更强生命周期保证 |
| 进程级长期后台工作 | 应用监督器或持久化任务系统 | 需要明确所有权、停止、异常和恢复策略 |

`gather()`、`as_completed()` 和 `wait()` 都能“同时等待多个对象”，但它们解决的是不同问题：

- `gather()` 关注最终结果列表；
- `as_completed()` 关注完成事件的先后；
- `wait()` 关注某个时点的状态分区。

先写清业务需要哪一种结果语义，再选 API，不要机械地把它们互换。

## 20.10 Java 工程师最容易踩的坑

| 常见误判 | 正确理解 |
| --- | --- |
| 调用 `async def` 后工作已经开始 | 只创建了 coroutine 对象；必须 `await`、创建 Task 或交给其他调度 API |
| `create_task()` 会启动一个新线程 | 默认只是在当前事件循环增加协作调度单元 |
| `await task1; await task2` 一定串行 | 若 Task 已提前全部创建，它们已经能够并发推进 |
| 创建顺序就是完成顺序 | 完成取决于 I/O、工作量和失败；聚合 API 还有自己的排列规则 |
| `task.result()` 像 `Future.get()` 一样阻塞 | pending 时立即抛 `InvalidStateError`；等待要用 `await` |
| `done()` 为真就一定有成功结果 | 异常和取消也属于 done |
| Task 和 coroutine 都只能 `await` 一次 | coroutine 对象只能执行一次；完成 Task 可以重复 `await` |
| `gather()` 一项失败会自动取消全部 | 默认普通异常只向等待者传播，兄弟继续运行 |
| `return_exceptions=True` 已经处理了异常 | 它只改变异常的交付形式 |
| `as_completed()` 两种迭代形式返回同类对象 | 异步迭代可产出原 Task；普通迭代产出包装 coroutine |
| `wait()` 返回 pending 说明它们已取消 | pending 仍在运行或等待，必须继续管理 |
| `create_task()` 可以充当可靠任务队列 | 它没有持久化、重试、恢复和天然监督者 |

## 20.11 动手练习

先写出预期状态和事件顺序，再运行代码验证。不要用精确耗时作为正确性依据。

### 练习 1：比较三种启动方式

为 `load_profile`、`search_docs`、`check_policy` 准备 started/release/finished 三组 Event，分别实现：

1. 直接逐个 `await`；
2. 创建一个 Task 后立刻 `await`；
3. 先创建全部 Task，再逐个 `await`。

让控制器在第三种方式中按 policy、profile、search 放行。验证前两种方式中后一个工具只会在前一个完成后开始；第三种方式要先观察到三者都 started，并区分完成顺序与读取结果顺序。

### 练习 2：处理 `gather()` 的部分失败

显式创建三个有名字的 Task，让 search 在另外两项仍被 Event 阻塞时抛 `LookupError("index unavailable")`。捕获默认 `gather()` 的异常后验证：

- search 已 done；
- 两个兄弟仍 pending 且没有取消；
- 放行兄弟后，用 `return_exceptions=True` 收齐三项最终状态；
- 程序退出时没有未取得的异常或遗留 Task。

### 练习 3：完成顺序与状态快照

先用 Python 3.14 的 `async for ... in as_completed(...)` 按 search、policy、profile 的完成顺序处理三个显式 Task，并用 `dict[Task, str]` 找回身份。再创建一组 Task，验证：

- `wait(timeout=0)` 对未放行的 Task 返回空 done 和完整 pending；
- `FIRST_COMPLETED` 只报告第一项最终状态；
- `wait()` 没有取消 pending；
- 离开入口前，调用方明确处理了所有 pending。

### 练习 4：为后台任务补上拥有者

审查下面的反例，分别说明强引用、业务身份、异常收集和退出清理缺失在哪里：

```python
async def handle_request() -> str:
    asyncio.create_task(write_audit_log())
    asyncio.create_task(report_metric())
    return "accepted"
```

实现一个最小应用级监督器：注册时保存 Task 和名称；`shutdown()` 时给有限任务发送结束信号，等待全部最终状态，逐项记录成功或异常，最后再删除引用。

## 20.12 运行本章示例

本章只使用 Python 标准库，不需要安装额外依赖。

| 文件 | 作用 |
| --- | --- |
| `README.md` | 建立 Task 生命周期、状态与结果收集的心智模型 |
| `example.py` | 用 `asyncio.Event` 门控九组本地实验，并用断言验证 API 契约 |

从仓库根目录运行：

```bash
.venv/bin/python -B java_to_python/chapters/20_asyncio_tasks/example.py
```

`-B` 只是不生成 `.pyc`，不改变异步行为。运行时重点观察：

- 直接 `await`、立即等待新 Task、先创建全部 Task 的开始与完成顺序；
- `eager_start=True` 如何改变 `create_task()` 返回前后的事件；
- pending、成功、异常、取消四种状态下的查询结果；
- `gather()` 的完成顺序、结果顺序和异常传播后兄弟 Task 的状态；
- `as_completed()` 的处理顺序，以及 `wait()` 的 done/pending；
- 入口结束前是否还有未回收的其他 Task。

项目已配置 Ruff 和 Pyright，可继续检查：

```bash
.venv/bin/ruff check java_to_python/chapters/20_asyncio_tasks
.venv/bin/ruff format --check java_to_python/chapters/20_asyncio_tasks
.venv/bin/pyright java_to_python/chapters/20_asyncio_tasks/example.py
```

`java_to_python/run_all.py` 目前只运行前 10 章，因此第 20 章需要使用上面的独立命令。

## 20.13 本章小结

- coroutine 是一次性的待执行路径；Task 是被事件循环调度并保存最终状态的执行句柄。
- `create_task()` 会增加一个必须被拥有、等待、收集和清理的生命周期对象。
- 是否并发主要看 Task 何时创建，而不是后来逐个 `await` 的书写顺序。
- `done()` 包含成功、异常和取消；pending 时调用 `result()` 或 `exception()` 不会等待。
- `gather()` 按输入顺序返回；普通异常默认不会取消兄弟 Task。
- `as_completed()` 按完成顺序交付；`wait()` 只返回 done/pending 状态快照。
- 任何提前返回都不能自动解除调用方对剩余 Task 的责任。
- 裸 `create_task()` 不是后台队列。没有强引用、异常处理和退出协议，就没有可靠的任务所有权。

建议合上文档后回答三个问题：两个 Task 先创建再依次 `await` 为什么仍是并发？`gather()` 传播异常后兄弟 Task 由谁收尾？`wait()` 返回 pending 后，哪个对象仍对它们负责？如果能结合 Task 状态和所有权回答清楚，本章的核心就掌握了。

## 参考资料

- Python 3.14: Coroutines and Tasks
- Python 3.14: Futures
