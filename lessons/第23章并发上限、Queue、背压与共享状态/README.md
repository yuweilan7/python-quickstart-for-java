# 第 23 章：并发上限、Queue、背压与共享状态

本章最重要的一句话是：**并发程序不仅要能同时做事，还要明确最多同时做多少、来不及处理的工作放在哪里，以及失败时由谁收尾。**

一次 Agent 请求可能要查询几十条记录、调用多个 MCP tool，或向同一个模型服务发出大量 HTTP 请求。把所有 coroutine 一次性变成 Task，确实获得了并发，却没有自动解决下面的问题：

- 下游最多能承受多少个尚未结束的请求？
- 输入持续到达时，内存里最多积压多少工作？
- 生产者比消费者快时，是等待、丢弃还是拒绝？
- worker、生产者或父请求失败时，其他 Task 怎样退出？
- 多个 Task 修改同一份状态时，单线程事件循环是否足以保证安全？

本章给出两种常用结构：小批量任务使用 `TaskGroup + Semaphore`，大量或流式输入使用固定 worker + 有界 `Queue`。两者解决的是不同层次的问题，不能只用一个“并发数”概括。

学完后，你应当能够：

- 区分在途并发、请求速率、排队容量和连接池容量；
- 用 `async with Semaphore` 管理许可，并解释异常和取消时为何不会泄漏；
- 解释为什么 Semaphore 限制了运行数量，却没有限制已经创建的 Task 数；
- 用 `Queue(maxsize)` 把下游压力传回生产者；
- 正确配对 `put()`、`get()`、`task_done()` 和 `join()`；
- 为正常结束、程序失败和用户取消设计不同的 Queue 关闭路径；
- 识别跨 `await` 的共享状态竞争，并在 Lock 与改变所有权之间作出选择。

## 23.1 先建立 Java → Python 心智模型

可以把本章看成 Java 中“并发许可、阻塞队列、固定 worker 和结构化生命周期”的 asyncio 版本，但不能把 Task 当成线程，也不能照搬线程池的关闭方式。

| Java 中熟悉的概念 | Python 中可先类比的概念 | 不能忽略的边界 |
| --- | --- | --- |
| `Semaphore` | `asyncio.Semaphore` | 前者常让线程等待；后者在 `await` 处暂停 Task，不占住事件循环线程，而且不是线程安全原语 |
| `ArrayBlockingQueue` | `asyncio.Queue(maxsize=n)` | 满队列时 `await put()` 暂停生产 Task，不是阻塞 OS 线程；Queue 也不负责创建 worker |
| 固定大小 `ExecutorService` | 固定数量的长期 worker Task | worker 是 coroutine Task，不是线程；worker 中调用阻塞 API 仍会卡住整个事件循环 |
| structured-concurrency scope | `asyncio.TaskGroup` | TaskGroup 管所有权、等待和失败传播，不提供并发上限，也不返回类似 `allOf` 的结果列表 |
| `CountDownLatch` / 阶段通知 | `asyncio.Event` | Event 没有计数，只保存“未设置/已设置”状态；`set()` 会放行所有等待者，不提供互斥 |
| `ReentrantLock` / `synchronized` | `asyncio.Lock` | asyncio Lock 用于 Task 间互斥，不可用于线程同步，也不要假定它可重入 |

还要特别区分 `Queue.shutdown()` 与 `ExecutorService.shutdown()`：前者关闭的是 Queue 的入队和取队协议，不会替你拥有、取消或等待 worker。worker 的生命周期仍应交给 `TaskGroup`。

本章所有 asyncio 同步原语都只服务于同一个事件循环中的 Task。若共享数据同时被线程访问，应使用线程同步机制或重新划分所有权，不能拿 `asyncio.Lock` 保护线程。

## 23.2 “最多 3 个”到底限制了什么

工程讨论中只说“限流为 3”是不够的，必须先说明单位。

| 限制 | 回答的问题 | 常用机制 | 达到上限后的行为 |
| --- | --- | --- | --- |
| 在途并发数 | 此刻有多少次操作已经开始但尚未结束 | `Semaphore(3)` 或 3 个串行 worker | 新调用等待空闲槽位 |
| 请求速率 | 一秒或一分钟内允许开始多少次请求 | 令牌桶、滑动窗口、服务端配额策略 | 等待下一时间窗口或明确拒绝 |
| 排队容量 | 有多少项尚未被 worker 领取 | `Queue(maxsize=4)` | `await put()` 等待空位，形成背压 |
| 连接池容量 | 客户端最多建立或借出多少连接 | HTTP/数据库客户端的 pool 配置 | 等待连接、复用连接或按客户端规则失败 |

### Semaphore 不是 QPS 限流器

Semaphore 的计数器没有时间概念。并发上限为 3 时：

- 若每个请求 10 毫秒结束，一秒内仍可能开始很多请求；
- 若每个请求 10 秒才结束，一秒内也可能一个都完成不了。

所以“同时最多 3 个”不能推出“每秒最多 3 次”。真正的速率策略还要定义时间窗口、突发容量、等待还是拒绝，以及服务端返回限流响应时如何处理。重试、退避和 `Retry-After` 会在第 25 章结合真实 HTTP 继续讲解。

### 应用并发上限不等于连接池容量

Semaphore 通常保护一段业务调用，连接池管理实际连接及其复用。假设业务许可为 10，而连接池只能借出 4 个合适的连接，已经获得业务许可的部分 Task 仍会在客户端内部等连接。反过来，连接池允许 20 个连接，也不能让 `Semaphore(3)` 保护的代码超过 3 个在途调用。

两者通常要一起配置，但数值不必相等。连接池还可能按主机、HTTP 版本或流分别计数，应以具体客户端的规则为准。

## 23.3 Semaphore 管的是许可，不是 Task

`asyncio.Semaphore(3)` 内部维护一个从 3 开始的计数器：

1. `acquire()` 在计数大于 0 时把它减 1，然后返回；
2. 计数为 0 时，调用方停在 `await acquire()`；
3. `release()` 把计数加 1，并可能唤醒一个等待者。

推荐把许可写成异步上下文管理器：

```python
import asyncio


async def fetch_one(record_id: int) -> int:
    await asyncio.sleep(0.1)  # 代表异步 I/O
    return record_id * 2


async def limited_fetch(record_id: int, limit: asyncio.Semaphore) -> int:
    async with limit:
        return await fetch_one(record_id)
```

`async with limit` 在语义上等价于：

```python
await limit.acquire()
try:
    result = await fetch_one(record_id)
finally:
    limit.release()
```

`finally` 是关键。需要分别看两个取消时刻：

- Task 在等待许可时被取消：`acquire()` 没有成功返回，也没有进入上下文体，因此不应释放许可；
- Task 已持有许可时被取消：退出上下文时仍会执行释放逻辑，已经取得的许可会被归还。

不要写成 `await acquire(); await work(); release()`。一旦 `work()` 抛异常或收到取消，最后一行就会被跳过，许可数量永久减少。若特殊控制流必须手动获取，成功 `acquire()` 后要立刻建立 `try/finally`。

普通 `Semaphore` 允许误调用过多次 `release()`。`BoundedSemaphore` 会在计数超过初始值时抛 `ValueError`，适合尽早发现不成对释放，但它不能替代正确的上下文管理。

### 许可应该包住真正稀缺的资源

```python
request = build_request(record)  # 本地准备，不占网络槽位

async with request_limit:
    response = await client.send(request)
    body = await response.read()  # 若连接仍被占用，通常也留在许可内

return parse_body(body)  # 本地解析是否移出，要看真正的瓶颈
```

许可取得太早，会让无关工作占住稀缺槽位；释放得太早，又可能让实际受限资源超过上限。真实边界要结合客户端何时归还连接、响应体是否流式读取来确定。

不要读取 Semaphore 的私有计数器来实现业务判断。是否能够进入应由 `await acquire()` 决定；指标应在受保护区域的入口和 `finally` 中单独记录。

### 一万个等待者仍然是一万个 Task

```python
limit = asyncio.Semaphore(3)

async with asyncio.TaskGroup() as group:
    tasks = [
        group.create_task(limited_fetch(record_id, limit))
        for record_id in range(10_000)
    ]
```

这段代码会立即创建 10,000 个 Task。Semaphore 只让其中最多 3 个进入受保护区域，其余 Task 仍然持有 Task 对象、coroutine 状态、上下文和异常传播信息。

可以把三个组件的职责记成三句话：

- `TaskGroup` 回答“这些 Task 归谁管、失败时怎样联动”；
- `Semaphore` 回答“多少 Task 可以进入这段代码”；
- `Queue(maxsize)` 回答“尚未开始的工作最多积压多少项”。

## 23.4 两种并发结构应该怎样选择

### 已知的小批量：TaskGroup + Semaphore

小批量结构适合一次请求中的少量独立工作，例如并发调用 8 个 tool：

```python
async def fetch_batch(record_ids: list[int]) -> list[int]:
    limit = asyncio.Semaphore(3)

    async with asyncio.TaskGroup() as group:
        tasks = [
            group.create_task(limited_fetch(record_id, limit))
            for record_id in record_ids
        ]

    return [task.result() for task in tasks]
```

成功退出 TaskGroup 后，每个 Task 都已经结束，才可以同步读取 `result()`。这种结构直观、结果与输入容易对应，但 Task 数仍随输入项数 N 增长。

### 大量或流式输入：有界 Queue + 固定 worker

输入很多、持续流入或总量未知时，不要预先为每项工作创建一个 Task。让一个生产者把普通数据项放进 Queue，再由固定数量的长期 worker 取走：

```text
流式输入
   │
   ▼
生产 Task ─ await put() ─▶ Queue(maxsize=4) ─▶ 3 个 worker Task
               ▲                    │                 │
               └──── 满时暂停 ──────┘       每个 worker 一次处理一项
```

| 维度 | 小批量 + Semaphore | 有界 Queue + 固定 worker |
| --- | --- | --- |
| 长期 Task 数 | 随输入 N 增长 | 生产/协调 Task 加固定 W 个 worker |
| 等待工作的位置 | 已创建 Task 的 coroutine 状态 | 最多 `maxsize` 个 Queue 项 |
| 在途上限 | Semaphore 的许可数 | 通常是 worker 数 W |
| 结果关联 | 每项一个 Task，直接保存句柄 | 给数据带 ID，父任务最后按业务规则整理 |
| 适用输入 | 已知且不大 | 大量、持续或开始时未知 |

“worker 数就是并发数”有一个前提：每个 worker 一次只处理一项，而且内部不再创建额外并发 Task。若多个管道共享同一个下游，或 worker 内部还会扇出请求，仍可能需要一个跨管道共享的 Semaphore。

Queue 方案不是总会更快。对于 5～20 项简单工作，一项一个 Task 往往更容易阅读；Queue 的核心价值是把 Task 数和排队容量变成清楚的资源边界。

## 23.5 有界 Queue 怎样产生背压

创建一个只允许 4 项等待领取的 Queue：

```python
queue: asyncio.Queue[Record] = asyncio.Queue(maxsize=4)

await queue.put(record)   # 满时暂停生产 Task
record = await queue.get()  # 空时暂停消费 Task
```

`maxsize=4` 只计算已经入队但尚未被 `get()` 领取的项目。三个 worker 已经领取并正在处理的三项不在这个数字里。

当 3 个 worker 都在处理慢请求、Queue 中又排了 4 项时，下一次 `await queue.put(record)` 无法完成。生产者让出控制权，直到某个 worker `get()` 一项腾出位置。这就是背压：下游的处理能力通过等待直接传回上游，没有轮询，也没有默认丢弃。

几个容易误判的细节：

- 默认 `maxsize=0` 表示无界，不是容量为 0；
- `put_nowait()` 在满队列上抛 `QueueFull`，它表达“立刻放入或失败”，不会形成等待式背压；
- `qsize()` 是当前准确数量，但跨过下一个 `await` 后就可能变化，不能用快照预订未来槽位；
- `asyncio.Queue[Record]` 的 `Record` 是静态类型信息，运行时不会自动校验入队对象；
- Queue 方法没有 `timeout=` 参数，需要时应在外层使用 `asyncio.timeout()` 或 `wait_for()`。

### `maxsize` 只限制 Queue 自己

若 Queue 容量为 4、worker 数为 3，可以说这个管道最多有 4 项排队、3 项在途。但不能说整个程序最多保存 7 条数据，因为：

- 若输入早已构造成百万项 `list`，它们仍然全在内存中；
- 若所有结果最终收集成一个大列表，结果内存仍随输入增长；
- 客户端、解析器、日志和远端 SDK 可能各有缓冲；
- 当前响应、worker Task 和其他管道也会占用资源。

真正有界的流要求输入逐项产生，结果也逐项写入下游。只在中间放一个有界 Queue，不能自动把两端已经全量物化的数据变成流。

## 23.6 Queue 同时有容量账本和生命周期

Java 工程师最容易把 `get()` 理解成“任务完成”。对 `asyncio.Queue` 来说，领取和完成是两件事。Queue 维护一个未完成工作计数：

```text
put(item) 成功       未完成计数 +1
get() 成功           只转移项目，不修改未完成计数
task_done()           未完成计数 -1
未完成计数变为 0     join() 可以返回
```

worker 的最小正确骨架是：

```python
async def worker(queue: asyncio.Queue[Record]) -> None:
    while True:
        try:
            record = await queue.get()
        except asyncio.QueueShutDown:
            return

        try:
            await process(record)
        finally:
            queue.task_done()
```

`task_done()` 必须在一次成功 `get()` 后恰好调用一次：

- 漏掉一次，未完成计数无法归零，`join()` 可能永久等待；
- 多调用一次，Queue 会抛 `ValueError`；
- `get()` 因关闭或取消而没有返回项目时，不能调用 `task_done()`；
- 处理成功、可恢复业务失败、程序异常或取消，都不能破坏已经领取项目的本地记账。

`task_done()` 的名字容易让人误以为它在证明业务成功。它实际上只是消费者对 Queue 账本的确认。业务成功、失败、重试或补偿必须由结果对象和异常策略另外表达。

### `join()` 只等待账本归零

`await queue.join()` 能说明每次 `put()` 都收到了相应的 `task_done()`；它不能单独证明：

- 每项业务都成功；
- worker 已退出；
- 远端事务或副作用已经提交；
- 消费者没有过早调用 `task_done()`；
- 紧急关闭时被丢弃的项目已经执行。

因此，“完成 Queue 工作”和“结束 worker 生命周期”必须分别设计。

## 23.7 正常关闭、失败关闭和取消是三条路径

本项目要求 Python `>=3.14`，因此可以使用 Python 3.13 加入的 `Queue.shutdown()`。

### 正常结束：停止接收，排空，再退出

生产者成功放完全部输入后，正常协议是：

```python
for record in records:
    await queue.put(record)

queue.shutdown()  # immediate=False：不再接收新项目
await queue.join()  # 等已入队项目完成记账
```

执行顺序是：

1. `shutdown()` 让后续和正在等待的 `put()` 失败，不再允许 Queue 增长；
2. 已经入队的项目仍可被 worker 取走；
3. 每个项目处理完并调用 `task_done()`；
4. 未完成计数归零后，`join()` 返回；
5. Queue 排空后，等待或再次调用 `get()` 的 worker 收到 `QueueShutDown` 并退出；
6. TaskGroup 等协调者和所有 worker 结束后，父作用域才读取最终结果。

空输入也走同一个协议：关闭空 Queue 后，等待 `get()` 的 worker 会收到 `QueueShutDown`，不需要额外的空列表特判。

注意，先 `shutdown()` 再 `join()` 并不矛盾。默认关闭只停止继续入队，不会丢弃已经排队的项目。

### 异常或用户取消：放弃剩余工作并解除等待

若 worker 崩溃、生产者失败，或用户取消父请求，继续等待全部业务完成通常已经没有意义。协调者的 `finally` 可以执行：

```python
queue.shutdown(immediate=True)
```

紧急关闭会清空尚未领取的项目，并解除阻塞在 `put()` 或 `get()` 的调用。被 worker 领取的项目完成记账后，未完成计数才会归零并解除仍在等待的 `join()`。它用于让失败路径有限时收束，不是成功信号。此时 `join()` 的通常不变量被削弱：未执行的排队项目也可能因被清空而不再阻止它返回。

已经被 worker 领取的项目不在 Queue 中。它们仍要在 worker 的 `finally` 中释放本地资源并调用 `task_done()`。紧急关闭也不能撤销已经发往远端的请求、数据库提交或其他副作用。

### TaskGroup 才是所有者

一条完整管道应有清楚的所有权：

```text
run_pipeline：拥有本次批处理
│
├─ Queue：保存等待领取的普通数据，并由本函数关闭
├─ TaskGroup：拥有全部长期 Task
│  ├─ producer/coordinator：生产、正常 shutdown、等待 join
│  ├─ worker-0：一次处理一个已领取项目
│  ├─ worker-1：一次处理一个已领取项目
│  └─ worker-2：一次处理一个已领取项目
└─ 父作用域：仅在 TaskGroup 成功退出后合并最终结果
```

异常策略也应与所有权对齐：

| 情况 | 子任务怎样处理 | 整批结果 |
| --- | --- | --- |
| 一项可预期业务失败 | 转为带错误字段的结果，照常 `task_done()` | 其他项目继续，可得到部分失败结果 |
| worker 程序错误 | 不吞异常，让它离开 worker | TaskGroup 取消同伴，整批失败 |
| 生产者无法继续读取输入 | 不伪造“输入结束” | TaskGroup 取消 worker，整批失败 |
| 用户取消父 Task | `finally` 清理并继续传播取消 | 调用方仍收到 `CancelledError` |

只捕获已经定义好恢复语义的业务异常。若 worker 用 `except Exception` 把解析器缺陷、断言失败等全部包装成普通结果，TaskGroup 就看不到失败，也无法启动兄弟取消和统一清理。

### Python 3.12 及更旧版本：哨兵协议

旧版本没有 `Queue.shutdown()`，常见替代方案是在所有真实项目之后，为每个 worker 放一个不会与业务数据混淆的哨兵：

```python
STOP = object()

for _ in range(worker_count):
    await queue.put(STOP)
```

三个 worker 需要三个哨兵；哨兵经过 `get()` 后也要配对 `task_done()`。一个哨兵不会自动广播。更重要的是，生产者失败时可能根本来不及投递哨兵，甚至会卡在向满队列投递哨兵的途中，所以异常路径仍要靠 TaskGroup 取消并等待 worker。

## 23.8 单线程事件循环仍然会发生逻辑竞争

在同一个事件循环线程内，一个 Task 只会在可暂停点把执行权交还给事件循环。因此不跨 `await` 的一小段同步代码通常不会被另一个 asyncio Task 插入。

但只要一个不变量被拆在 `await` 两侧，就可能发生丢失更新：

```python
snapshot = counter.value
await something()
counter.value = snapshot + 1
```

假设两个 Task 都先读到 0，然后都在 `await` 处暂停。它们恢复后会分别把 1 写回同一个字段，两次加一最终只得到 1。动态调度是否经常触发并不重要；只要存在这种交错，代码就不正确。

### Event 只能协调阶段

`asyncio.Event` 保存一个布尔状态：

- 初始未设置时，`wait()` 会暂停；
- `set()` 后，所有当前等待者都可继续，以后的 `wait()` 也会立即返回；
- `clear()` 后，未来的 `wait()` 再次需要等待。

它适合表达“所有 worker 已启动”“两个 Task 都完成读取”“现在允许进入下一阶段”。它不会限制只有一个 Task 修改状态，因此不是互斥锁。本章示例正是用 Event 让两个 Task 确定都读到旧值，从而稳定复现竞争，而不是用随机 `sleep()` 碰运气。

### 必须共享一个不变量时使用 Lock

```python
async with counter_lock:
    snapshot = counter.value
    counter.value = snapshot + 1
```

`asyncio.Lock` 同一时刻只允许一个 Task 进入临界区，离开 `async with` 时释放。锁应包住必须一起观察和修改的不变量，而不是习惯性包住整个函数。

尤其不要无条件持锁等待慢网络：

```python
async with state_lock:
    response = await slow_network_call()  # 所有状态使用者一起排队
    state[key] = response
```

若网络调用与共享不变量无关，先在锁外完成 I/O，再短暂持锁提交结果：

```python
response = await slow_network_call()

async with state_lock:
    state[key] = response
```

但这不是机械规则。若业务要求“按版本读取、发请求、仅在版本未变化时提交”，完整不变量跨越了网络等待，应使用版本检查、不可变快照或单一状态所有者，而不是直接拆锁。

### 优先消除共享写入

本章完整示例让每个 worker 只写自己的结果列表，所有 worker 退出后，父任务再按管道合并结果。另一种做法是让子任务返回局部值，由父任务在 TaskGroup 结束后求和。两种方式都不需要多个 Task 共同读改写一个聚合器。

选择顺序可以记成：

1. 能让一个 Task 单独拥有状态，就先改变所有权；
2. 必须共享同一个不变量时，才用 Lock 保护最小但完整的临界区；
3. Event 只传递阶段事实，不提供互斥；
4. 若持锁期间还要 `await`，画出“谁持锁、正在等待谁”，检查是否可能循环等待。

## 23.9 常见迁移错误与排查顺序

| 错误判断或写法 | 典型表现 | 正确方向 |
| --- | --- | --- |
| `Semaphore(3)` 等于每秒 3 次 | 快请求一秒内远超 3 次 | 单独设计带时间窗口的速率限制 |
| 给海量项目逐个建 Task 再套 Semaphore | 在途很少，但 Task 和内存仍很多 | 有界 Queue + 固定 worker |
| `Queue()` 不设正数 `maxsize` | 快生产者持续堆积内存 | 明确容量，让 `await put()` 产生背压 |
| 成功路径才调用 `task_done()` | 异常或取消后 `join()` 永久等待 | 成功 `get()` 后立即建立 `try/finally` |
| `get()` 失败也调用 `task_done()` | 抛 `ValueError` 或账本失真 | 只为实际领取的项目记完成 |
| `join()` 返回就认为 worker 已退出 | TaskGroup 一直等在 worker 的下次 `get()` | 再设计 shutdown 或足量哨兵 |
| 三个 worker 只投一个哨兵 | 一个退出，两个永久等待 | 每 worker 一个哨兵，或使用 Queue shutdown |
| worker 捕获所有 `Exception` | 程序缺陷被伪装成业务失败 | 只恢复明确的业务异常 |
| 生产者失败后仍等待正常 `join()` | 剩余项目无人处理，管道死等 | 让异常离开 Task，由 TaskGroup 联动取消和紧急关闭 |
| 把紧急 shutdown 当成功处理完成 | 部分项目从未执行 | 只把它用于放弃和清理路径 |
| 认为单线程就没有竞争 | 跨 `await` 的状态更新丢失 | 改变所有权，或用 Lock 保护不变量 |
| 持锁等待无关慢 I/O | 其他 Task 长时间排队 | 缩短临界区并重新审视状态协议 |

Queue 管道挂住时，按下面顺序检查：

1. 每次成功 `put()` 的项目最终是否被某个 worker `get()`，或在紧急关闭时被明确丢弃？
2. 每次成功 `get()` 是否在所有返回、异常和取消路径恰好调用一次 `task_done()`？
3. `join()` 返回后，worker 是否还能收到关闭信号？
4. 生产者失败时，是否还有 worker 在等永远不会到来的数据或哨兵？
5. 所有长期 Task 是否属于同一个 TaskGroup？
6. 是否有过宽的异常捕获阻止 TaskGroup 感知真正失败？

## 23.10 递进练习

### 练习 1：证明 Semaphore 只限制在途数量

并发查询 6 个本地替身，许可数为 2。使用 Event 让已经进入的调用暂停，再断言只有 2 个调用进入受限区，但已经创建了 6 个 Task。取消一个持有许可的 Task 后，再证明等待者可以取得被归还的许可。

验收点：

- 使用 `async with semaphore`，不访问私有计数器；
- 最大在途为 2，记录 Task 数仍为 6；
- 不用墙钟延迟猜测调度阶段；
- 能解释为什么该实验没有证明“每秒最多 2 次”。

### 练习 2：实现一个有界 worker 池

用 2 个固定 worker 处理 `range(10)`，Queue 容量为 3，每项结果为平方。生产完成后使用正常 `shutdown()` 和 `join()`，最后由父任务按输入顺序整理结果。

验收点：

- 长期基础设施 Task 数不随输入项数增长；
- 任意时刻 `qsize() <= 3`；
- 每次成功 `get()` 都在 `finally` 中配对一次 `task_done()`；
- `join()` 返回后，两个 worker 也都已经退出；
- 最终结果是 `[0, 1, 4, ..., 81]`。

### 练习 3：覆盖三种失败路径

分别注入生产者读取失败、worker 程序错误和用户取消。可恢复业务失败应转为结果；另外三种情况都必须让 TaskGroup 收束，并且不能返回看似完整的成功列表。

验收点：

- 程序错误不会被包装成普通业务结果；
- 非正常路径使用紧急关闭，但不声称剩余工作已完成；
- 所有 worker 的 `finally` 都已运行；
- 用户取消最终仍以 `CancelledError` 传播；
- 事件循环中没有遗留后台 Task。

### 练习 4：比较 Lock 与状态所有权

让两个 Task 各执行一次“读计数—await—写计数”。先用 Event 稳定构造丢失更新，再分别用 Lock 和“子任务返回局部结果、父任务合并”修复。

验收点：

- 能指出哪个 `await` 拆开了读改写；
- Lock 版得到正确总数；
- 所有权版不共享可变计数器；
- 能解释 Event 为什么不能替代 Lock。

## 23.11 本章小结

把本章压缩成一条设计顺序：

1. 先确定受限资源和单位：在途数、速率、排队数还是连接数；
2. 小批量用 TaskGroup 管生命周期、Semaphore 管受限区；
3. 大量或流式输入用有界 Queue 承载等待工作、固定 worker 控制 Task 数；
4. 把 Queue 的容量、完成记账和 worker 关闭当成三套协议分别设计；
5. 正常路径排空，失败和取消路径放弃剩余工作并确定性清理；
6. 跨 `await` 的共享读改写仍会竞争，优先改变所有权，必要时再加 Lock。

只要能回答下面五个问题，就已经建立了本章的核心心智模型：

1. 为什么 `Semaphore(3)` 不能保证每秒最多 3 次请求？
2. 为什么 10,000 个等待 Semaphore 的 Task 仍然占用 10,000 份 Task 状态？
3. `Queue(maxsize=4)` 限制了哪些数据，又没有限制哪些内存？
4. `task_done()`、`join()` 和 `shutdown()` 分别负责什么？
5. worker、生产者或父请求失败时，谁传播异常、谁关闭 Queue、谁等待所有 Task？

## 23.12 运行本章示例

本章只有两个文件：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 建立并发容量、背压、生命周期和共享状态的完整模型 |
| `example.py` | 用可控 Event 和运行时断言验证正常、异常、取消与竞争路径 |

根 `pyproject.toml` 要求 Python `>=3.14`。本章只使用 `asyncio` 和 `dataclasses`，不需要安装额外依赖。请在仓库根目录运行：

```bash
.venv/bin/python -B java_to_python/chapters/23_asyncio_concurrency_control/example.py
```

`-B` 只阻止生成 `.pyc`，不改变程序行为。`java_to_python/run_all.py` 只包含前 10 章，因此不能用它运行本章。

完整示例会依次验证：

- 四种容量限制的单位不同；
- 等待许可和持有许可时取消都不会泄漏 Semaphore 许可；
- 9 项小批量创建 9 个 Task，但最大在途为 3；
- 24 项输入经过容量为 4 的 Queue 和 3 个 worker，生产者确实遇到背压；
- 空输入、worker 崩溃、生产者崩溃和用户取消都能让 worker 退出；
- 漏掉 `task_done()` 会卡住 `join()`，而 `join()` 返回也不会自动关闭 worker；
- 两个 Task 跨 `await` 修改共享计数时会丢失更新，Lock 与局部所有权都能修复。

运行静态检查和格式检查：

```bash
.venv/bin/ruff check java_to_python/chapters/23_asyncio_concurrency_control
.venv/bin/ruff format --check java_to_python/chapters/23_asyncio_concurrency_control
.venv/bin/pyright java_to_python/chapters/23_asyncio_concurrency_control/example.py
```

进一步核对 API 细节可查阅 Python 3.14 官方文档：

- `Semaphore`、`Lock` 与 `Event`；
- `Queue`、`task_done()`、`join()` 与 `shutdown()`；
- `TaskGroup`。
