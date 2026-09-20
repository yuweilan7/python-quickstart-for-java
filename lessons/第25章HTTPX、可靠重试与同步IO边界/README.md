# 第 25 章：HTTPX、可靠重试与同步 I/O 边界

把一次同步 HTTP 调用改成 `await client.get(...)`，只是迁移工作的开始。对于 Agent 工具，真正的异步 I/O 单元是从等待并发许可、取得连接开始，经过发送与接收、状态码判断、JSON 解码、Pydantic 校验、可靠重试和同步数据库访问，直到响应与客户端资源被释放为止。

本章只访问本机 `127.0.0.1` 上的测试服务器，或使用 HTTPX 的 `MockTransport`，不依赖公网服务。

```text
请求作用域：建立 deadline、AsyncClient、并发许可
      │
      ├─ 等连接池 → HTTP 发送/接收 → 状态码 → JSON → 模型 → 业务结果
      │
      ├─ 可恢复且可重复：释放许可 → 在剩余预算内退避 → 重试
      │
      └─ 不可恢复或不可重复：保留原始 cause，立即失败

取消：向上传播；清理响应、客户端和其他资源
同步 SDK / sqlite3：把完整阻塞工作单元交给线程，但线程有自己的生命周期
```

## 学习目标

完成本章后，你应当能够：

- 解释为什么 `await client.get(...)` 需要等待，而 `response.json()` 不需要 `await`。
- 正确管理可复用 `httpx.AsyncClient` 的生命周期。
- 区分 connect、read、write、pool timeout，以及总 deadline、`Semaphore` 和连接池上限。
- 分层处理传输、HTTP 状态、JSON、模型校验和业务错误。
- 用 `client.stream(...)`、`aiter_lines()` 消费 JSONL，并在提前退出时可靠关闭响应。
- 设计有限次数、指数退避、jitter、`Retry-After` 和剩余时间预算共同约束的重试。
- 解释并发许可在退避前后释放，对吞吐量和公平性的影响。
- 用 `asyncio.to_thread()` 隔离同步库，并让 SQLite 连接在执行 SQL 的同一线程中创建和关闭。
- 解释“取消等待 `to_thread()` 的 Task”为什么不等于“停止已经运行的线程”。

## 25.1 从 Java 建立异步 I/O 心智模型

| Java 中常见的概念 | Python 中的对应物 | 迁移时必须补上的认识 |
| --- | --- | --- |
| JDK `HttpClient` / Spring `WebClient` | `httpx.AsyncClient` | 发送请求和关闭异步资源可能需要 `await`，工作由事件循环调度 |
| `CompletionStage` / `CompletableFuture` | coroutine 在 `await` 后运行，或显式创建 `Task` | 调用 `async def` 只会创建协程对象，不会自动启动新线程 |
| try-with-resources / `AutoCloseable` | `async with` | 谁创建资源，谁负责退出异步上下文或在应用关闭时统一关闭 |
| HTTP 连接池 | `AsyncClient` 的连接池和 `httpx.Limits` | 连接数、任务数和 QPS 是三个不同的控制维度 |
| 客户端 connect/read timeout | HTTPX 的 connect/read/write/pool timeout | 阶段超时不是一次工具调用的总 SLA；总预算应使用绝对 deadline |
| Resilience4j / Spring Retry | 显式重试状态机、退避、jitter、`Retry-After` | 只重试“可恢复且可重复”的操作，`CancelledError` 必须传播 |
| Jackson + Bean Validation | `response.json()` + Pydantic `model_validate()` | JSON 结构和类型需要在运行时校验 |
| `ExecutorService.submit(...)` | `await asyncio.to_thread(...)` | 取消等待方不会强制终止已开始运行的线程 |
| JDBC 工作单元持有自己的连接 | 在线程中创建、使用并关闭 `sqlite3.Connection` | SQLite 默认连接具有线程约束，不应跨线程共享 |

事件循环的价值不是“把每行代码变成并行代码”，而是在一个任务等待 I/O 时，让其他任务继续推进。只有会把控制权交回事件循环的异步操作才使用 `await`。普通 Python 计算、已经位于内存中的 `response.json()`，以及同步库调用，都不会因为写在 `async def` 里而自动变成异步操作。

### 最低前置知识

建议先掌握第 12、21、22、23、24 章，并确认以下边界：

- `Semaphore` 限制同时进入临界区的协程数量，不直接限制每秒请求数。
- `loop.time()` 是单调时钟，适合建立绝对 deadline。
- `async with` 表达资源所有权和退出时清理。
- `async for` 是顺序消费；流是否能及时关闭，取决于它所在的异步上下文。

## 25.2 HTTP 请求到底在等待什么

最小的异步请求如下：

```python
import httpx


async def fetch_payload(url: str) -> object:
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()
```

| 操作 | 是否 `await` | 原因 |
| --- | --- | --- |
| `client.get(url)` | 是 | 可能等待连接池、建连、写请求和读响应 |
| `response.raise_for_status()` | 否 | 检查已经在内存中的响应元数据 |
| `response.json()` | 否 | 对已经收到的响应体做同步 JSON 解码 |
| `client.aclose()` | 是 | 关闭异步连接池和底层传输资源 |

下面的写法混淆了 coroutine 和普通方法：

```python
# 错误：client.get(...) 返回的是协程；response.json() 不是异步方法。
response = client.get(url)
payload = await response.json()
```

### 共享客户端与资源所有权

`AsyncClient` 应当由一个清晰、较长生命周期的所有者管理。应用级客户端可以复用连接和配置；函数内部创建的客户端则必须由函数关闭。

```python
async def fetch_pair(client: httpx.AsyncClient, first: str, second: str) -> tuple[object, object]:
    first_response = await client.get(first)
    second_response = await client.get(second)
    first_response.raise_for_status()
    second_response.raise_for_status()
    return first_response.json(), second_response.json()
```

不要在热循环中为每个元素创建一次 `AsyncClient`。本章的本地 fixture 会记录客户端端口：同一个客户端在本次运行中可能复用同一条 TCP 连接，但是否复用仍取决于服务端、协议和连接状态。确定的是，客户端必须在其所有者退出时关闭。

### 阶段超时、总预算与容量控制

HTTPX 将超时拆成四类：

| 超时 | 限制的等待阶段 |
| --- | --- |
| `connect` | 建立网络连接 |
| `read` | 从网络读取下一段响应数据 |
| `write` | 向网络写下一段请求数据 |
| `pool` | 等待连接池中的可用连接 |

```python
timeout = httpx.Timeout(connect=1.0, read=2.0, write=2.0, pool=0.5)
limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)

async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
    response = await client.get(url)
```

这些机制解决的问题并不相同：

| 机制 | 控制对象 | 不能替代什么 |
| --- | --- | --- |
| HTTPX timeout | 一次连接、读、写或池等待阶段 | 包含多次重试和退避的总工具预算 |
| `asyncio.timeout_at(deadline)` | 从当前作用域到绝对 deadline 的总时间 | 连接池容量或业务并发策略 |
| `asyncio.Semaphore` | 同时进入一段代码的协程数量 | QPS 或连接池连接数 |
| `httpx.Limits` | HTTP 连接与 keep-alive 连接数量 | 业务级并发和速率限制 |

QPS 需要单独的速率限制策略。还要注意：如果事件循环中直接执行了长时间同步工作，连超时回调本身都可能无法按时得到调度。

### 把失败分层

响应从网络进入业务代码，至少经过五层：

```text
传输层 → HTTP 状态层 → JSON 语法层 → 模型校验层 → 业务规则层
```

```python
from pydantic import BaseModel, ConfigDict


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    title: str
    score: float
```

本章的 `document_from_response()` 将 `httpx.HTTPStatusError`、`json.JSONDecodeError` 和 Pydantic `ValidationError` 转换成不同的领域异常，并用 `raise ... from exc` 保留原始原因。业务规则错误属于第五层，应按工具契约单独表达。不要用一个宽泛的 `except Exception: return {}` 同时吞掉状态码、格式、校验和程序缺陷。

### 流式 JSONL

JSONL 是“一行一个 JSON 值”。应当按逻辑行消费，而不是假设一个 TCP 字节块恰好就是一条记录。

```python
import json


async def read_first_event(client: httpx.AsyncClient, url: str) -> object:
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line:
                return json.loads(line)
    raise RuntimeError("stream ended before the first event")
```

`aiter_bytes()` 产生的是网络分块，分块边界不等于 JSONL 行边界。即使读取到第一条记录就 `return` 或 `break`，包住响应的 `async with` 仍会退出，从而关闭响应并把连接归还给连接池。

### `MockTransport` 和真实 loopback 的边界

| 方式 | 适合验证 | 不能证明 |
| --- | --- | --- |
| `httpx.MockTransport` | 状态码、头部、响应体、重试状态机和确定性故障序列 | 真实 TCP 分块、内核缓冲、建连和网络阶段超时 |
| `127.0.0.1` 本地服务器 | 连接复用、流式行、慢响应、同步客户端和线程行为 | 公网链路、代理、TLS 和生产服务特性 |

本章同时使用两种方式：重试用 `MockTransport` 构造可重复的失败序列；HTTP 流、连接复用与同步 I/O 使用本地服务器。

## 25.3 可靠重试是一个有限状态机

一次失败只有同时满足两个条件才有资格重试：

1. 失败是暂时的，下一次尝试可能成功。
2. 操作可安全重复，重复发送不会制造不可接受的副作用。

### 默认策略

| 失败 | 默认是否考虑重试 | 说明 |
| --- | --- | --- |
| `ConnectError` / `ConnectTimeout` | 是 | 通常尚未取得业务响应，但仍受次数和预算限制 |
| `ReadError` / `ReadTimeout` | 仅只读请求 | 服务端可能已经处理请求，写操作风险更高 |
| `WriteError` / `WriteTimeout` | 谨慎；通常仅只读或有幂等保证 | 不一定知道服务端收到多少数据 |
| `RemoteProtocolError` | 有限重试 | 可能是短暂连接或协议问题 |
| HTTP 429 | 是 | 尊重合法的 `Retry-After`，同时受总 deadline 限制 |
| HTTP 502 / 503 / 504 | 是 | 常见暂时性上游故障 |
| HTTP 400 / 401 / 403 / 404 | 否 | 修改请求或权限之前，重复发送通常没有意义 |
| JSON 解码 / 模型校验失败 | 否 | 响应契约已经不符合预期 |
| 业务错误 | 由业务契约决定 | 必须显式定义 |
| `asyncio.CancelledError` | 否 | 立即向上传播，不包装成普通失败 |

本章默认不把 `PoolTimeout` 当成远端可恢复故障，因为它通常暴露的是本地容量、并发或反压问题。如果系统确实要对它重试，也应当通过显式策略开启。

```text
开始 attempt
   │
   ├─ deadline 是否还有预算？──否──> RetryBudgetExceeded
   │
   ├─ 获取并发许可，发送只读请求
   │        │
   │        ├─ 成功且响应合法 ──> 返回
   │        └─ 失败 ──> 分类
   │
   ├─ 不可恢复 / 不可重复 ──> 保留 cause，失败
   ├─ 已到 max_attempts ─────> RetryExhausted
   └─ 释放许可 → 计算 delay → 检查剩余预算 → sleep → 下一次 attempt
```

### 次数、退避、jitter 和 `Retry-After`

`max_attempts` 表示总尝试次数，而不是“首次之外还能重试多少次”。指数退避的基础值可以写成：

```python
base_delay = min(policy.max_delay, policy.base_delay * (2 ** (attempt - 1)))
delay = random.uniform(0.0, base_delay)
```

- 指数退避降低多个失败客户端同时再次打向服务端的概率。
- full jitter 进一步打散同步重试；测试中可注入固定随机种子以获得确定结果。
- 合法的 `Retry-After` 可以是秒数或 HTTP 日期，应解析并截断到策略允许的最大等待值。
- `Retry-After`、本地退避、尝试次数和总 deadline 必须共同生效，不能由其中一个绕过其他限制。

相关协议概念包括 RFC 9110 中的 `Retry-After` 和幂等方法语义。

### 使用绝对 deadline

每次重试重新设置一个完整相对超时，会让总耗时随尝试次数膨胀。正确做法是在调用入口计算一次绝对 deadline，此后每一步都使用同一个剩余预算：

```python
loop = asyncio.get_running_loop()
deadline = loop.time() + total_timeout

remaining = deadline - loop.time()
if remaining <= 0:
    raise TimeoutError("retry budget exhausted")

async with asyncio.timeout(remaining):
    response = await client.get(url)
```

本章源码中的 `fetch_read_only_with_retry()` 还会在睡眠前检查：等待时间是否已经吃掉剩余预算。

### 退避时是否持有并发许可

默认实现只在一次网络尝试期间持有 `Semaphore`，失败后先释放许可，再退避。这样其他任务可以使用容量，通常吞吐量和公平性更好。

另一种策略是让一次逻辑调用从首次尝试到最终结果始终占有许可。它可以限制“在途业务调用数”，但失败风暴时大量槽位会在睡眠中被占住。选择哪一种必须由容量模型明确决定，不能偶然取决于 `async with semaphore` 放在循环内还是循环外。

### 写操作不能盲目重试

POST 或其他写操作发生 read timeout，只能说明客户端没有按时读到响应，不能说明服务端没有执行。可靠写入通常需要至少一种额外协议：

- 明确的幂等业务操作。
- 服务端支持并持久化校验的 idempotency key。
- 可查询操作状态的请求标识。
- 能够处理“结果未知”的补偿或人工确认流程。

## 25.4 把同步库隔离出事件循环

`async def` 不会改写函数体中的同步调用。下面的代码会阻塞事件循环：

```python
import requests


async def wrong(url: str) -> object:
    response = requests.get(url, timeout=2.0)
    response.raise_for_status()
    return response.json()
```

本章用一个 heartbeat 实验对比两种情况：直接调用同步 HTTP 时，心跳无法按时推进；把完整同步工作单元交给 `to_thread()` 后，事件循环仍能调度其他任务。

```python
def sync_get_json(url: str) -> object:
    with requests.get(url, timeout=2.0) as response:
        response.raise_for_status()
        return response.json()


async def fetch_json(url: str) -> object:
    return await asyncio.to_thread(sync_get_json, url)
```

参数必须是函数对象和参数，而不是已经执行完的结果：

```python
# 错误：requests.get(url) 在进入 to_thread 之前已经阻塞了事件循环。
result = await asyncio.to_thread(requests.get(url))
```

线程边界应包住一个有意义的同步工作单元：创建同步客户端或连接、执行阻塞 I/O、解析必要结果、关闭资源，然后把纯 Python 结果返回异步世界。与显式管理 `ExecutorService` 相比，`to_thread()` 简洁，但对线程生命周期和强制停止的控制更弱。

### SQLite：连接属于执行 SQL 的线程

```python
import sqlite3
from pathlib import Path


def query_database(database_path: Path, minimum_score: float) -> list[tuple[int, str, float]]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT id, title, score FROM documents WHERE score >= ? ORDER BY id",
            (minimum_score,),
        ).fetchall()
    return [(int(row[0]), str(row[1]), float(row[2])) for row in rows]


async def load_documents(database_path: Path) -> list[tuple[int, str, float]]:
    return await asyncio.to_thread(query_database, database_path, 0.8)
```

默认 `sqlite3.Connection` 具有线程检查。不要在事件循环线程创建连接，再把它交给工作线程。让完整工作单元在线程中创建、使用并关闭连接，也让临时数据库文件的生命周期更容易验证。

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| 原生 async 驱动 | 与取消、连接池和事件循环更自然地集成 | 需要迁移库和调用方式 |
| `to_thread()` 适配同步驱动 | 改造小，适合遗留库或低并发工具 | 线程数量、取消语义、连接所有权需要额外设计 |

### 取消 Task 不会杀掉线程

当等待 `to_thread()` 的 Task 被取消，等待方会很快收到 `CancelledError`；已经在线程中运行的同步函数通常仍会继续，直到自行返回或失败。源码中的 `lingering_sync_work()` 用 `threading.Event` 证明了这一点。

因此，调用者仍需负责：

- 给同步客户端设置它自己的超时。
- 使重复执行和延迟完成保持安全。
- 让资源在线程内通过 `with` / `finally` 清理。
- 如果库支持，传入协作式停止信号。
- 控制线程池和上游并发，避免无限堆积。

`to_thread()` 主要用于可能释放 GIL 的阻塞 I/O。纯 Python CPU 密集工作不会因此获得理想的并行加速，通常应考虑进程池、原生扩展或重新设计工作负载。

## 25.5 把所有边界组合起来

`demonstrate_combined()` 展示一条完整工具调用：

1. 创建一次调用共享的绝对 deadline。
2. 使用可复用的 `AsyncClient` 与有限的连接池。
3. 在每次 HTTP 尝试期间获取并发许可。
4. 将响应依次通过状态、JSON 和 Pydantic 校验。
5. 只对可恢复的只读失败执行有限重试，退避前释放许可。
6. 将 SQLite 查询作为完整同步工作单元交给 `to_thread()`。
7. 在异步上下文退出时关闭响应、客户端和本地 fixture。

独立项目结构如下：

```text
第25章HTTPX、可靠重试与同步IO边界/
├── README.md
├── example.py
├── local_http_fixture.py
└── requirements.txt
```

## 25.6 Java 工程师常见错误

| 错误 | 后果 | 修正 |
| --- | --- | --- |
| 在热循环中反复 `AsyncClient()` | 丢失连接复用，增加建连和清理成本 | 由更长生命周期的所有者共享客户端 |
| 忘记关闭客户端或流式响应 | 连接泄漏，连接池最终耗尽 | 使用 `async with` 或在关闭钩子中 `aclose()` |
| 把每个 bytes chunk 当成一条 JSON | 网络分块可能拆开或合并逻辑记录 | JSONL 使用 `aiter_lines()` 再逐行解码 |
| 把 read timeout 当成总 deadline | 重试和退避会让总耗时无限膨胀 | 在入口建立一次绝对 deadline |
| 用 `Semaphore` 当 QPS 限流器 | 只限制并发，不限制单位时间请求数 | 单独设计速率限制 |
| 对所有 `TransportError` 无限重试 | 本地容量问题和永久故障被掩盖 | 显式白名单、有限次数、总预算 |
| 退避期间一直持有许可 | 槽位被睡眠任务占据，降低公平性 | 默认在退避前释放许可 |
| 写请求超时后直接重发 | 服务端可能重复执行副作用 | 使用幂等协议、请求键或状态查询 |
| 在 `async def` 中直接 `requests.get()` | 阻塞事件循环 | 用 async 客户端或 `to_thread()` |
| 写成 `to_thread(sync_call())` | 同步调用在线程切换前已经执行 | 传入 callable：`to_thread(sync_call, arg)` |
| 在线程之间共享默认 SQLite 连接 | 触发线程检查或出现所有权混乱 | 在线程工作单元内创建和关闭连接 |
| 认为取消 Task 会杀掉线程 | 后台工作仍继续并可能产生副作用 | 设计库级超时、协作停止和幂等清理 |

面对一个 HTTP 或同步 SDK 工具，先问五个问题：

1. 谁拥有客户端、响应、连接或数据库连接？
2. 哪些步骤真正会阻塞，是否会阻塞事件循环？
3. 哪些错误可恢复，操作是否可安全重复？
4. 所有尝试、退避和同步收尾是否共享同一个时间预算？
5. 取消发生后，底层网络、线程和副作用还会继续多久？

## 25.7 练习

### 练习 1：共享客户端与连接复用

要求：用同一个 `AsyncClient` 连续请求本地 `/document`，再用两个独立客户端请求。读取 fixture 的请求记录，比较客户端端口。

输入：本章 `LocalHttpFixture`。

验收：能够说明同一客户端为何“可能复用”连接；断言只依赖本次本地运行的观察结果，不把端口复用写成协议保证；所有客户端均被关闭。

### 练习 2：流式 JSONL 的提前退出

要求：请求 `/events`，使用 `client.stream()` 和 `aiter_lines()` 只读取第一条合法 JSON，然后立即退出。

输入：fixture 逐行发送的 JSONL 事件流。

验收：返回第一条事件；不把 bytes chunk 当作记录；提前退出后响应仍被关闭，后续请求可以正常完成。

### 练习 3：带 deadline 的只读重试

要求：为 GET 实现重试，处理暂时性传输错误、429、502、503、504，支持指数退避、jitter、`Retry-After`、`max_attempts` 和绝对 deadline。

输入：`MockTransport` 依次返回失败、失败、成功；再构造预算不足和不可重试响应。

验收：尝试次数有限；不可重试错误立即失败；取消向上传播；退避不会超过剩余预算；最终异常保留 cause。

### 练习 4：比较两种并发许可范围

要求：分别实现“每次尝试持有许可”和“整个逻辑调用持有许可”，让多个任务同时经历暂时失败与退避。

输入：容量较小的 `Semaphore` 和确定性的 MockTransport 故障序列。

验收：记录时间线，说明哪一种允许其他任务在某个调用退避时前进，以及两种策略各自限制的对象。

### 练习 5：同步 I/O、SQLite 与取消

要求：先直接在事件循环中调用同步 `/slow` 请求，再改用 `to_thread()`；在线程中创建、查询并关闭 SQLite 连接；最后取消一个等待长同步工作的 Task。

输入：本地 fixture、临时 SQLite 文件、`threading.Event`。

验收：直接调用时 heartbeat 被阻塞，`to_thread()` 时 heartbeat 可推进；SQLite 不跨线程共享连接；Task 被取消后能观察到线程仍继续并最终清理。

## 25.8 本章小结与边界

- `await` 标记真正可能让出控制权的异步等待，不是“异步语法装饰”。
- 共享 `AsyncClient` 提供连接复用，但必须有清晰的所有权和关闭路径。
- 阶段 timeout、总 deadline、并发许可、连接池和速率限制解决不同问题。
- 可靠重试需要错误分类、可重复性、次数上限、退避、jitter 和统一预算。
- 流式数据要按协议边界解析，并确保提前退出也关闭响应。
- 同步 SDK 可以通过线程适配，但取消等待方不会终止已运行的线程。
- SQLite 连接应在线程工作单元内创建、使用和关闭。

课件验证环境：

| 组件 | 版本 |
| --- | --- |
| CPython | 3.14.7 |
| HTTPX | 0.28.1 |
| Pydantic | 2.13.5 |
| Requests | 2.34.2 |
| Ruff | 0.16.5 |
| Pyright | 1.1.411 |

示例在运行时会断言连接观察、流式结果、错误分层、重试次数、预算、并发许可、heartbeat、SQLite 线程边界和取消后的线程状态。它验证的是本机、当前解释器与这些库版本下的行为，不证明公网网络、代理、TLS、HTTP/2、多进程部署或生产数据库驱动具有完全相同的时序。

口述检查题：

1. 为什么 `response.json()` 不需要 `await`？
2. read timeout 与一次工具调用的总 deadline 有何不同？
3. 为什么默认不把 `PoolTimeout` 当作远端暂时故障？
4. 为什么 GET 的 read timeout 通常比 POST 更适合自动重试？
5. 退避前释放 `Semaphore` 会改变什么？
6. 为什么取消等待 `to_thread()` 的 Task 后，同步函数还可能继续？

## 25.9 运行示例

| 文件 | 用途 |
| --- | --- |
| `example.py` | HTTPX、错误分层、流式 JSONL、重试、同步 I/O、SQLite 和组合示例 |
| `local_http_fixture.py` | 仅绑定 `127.0.0.1` 的本地 HTTP 测试服务器 |
| `requirements.txt` | 本章第三方依赖及固定版本 |
| `README.md` | 概念、边界、练习和运行说明 |

本次还原为独立课程目录。从当前目录运行：

```bash
python -V
python -m pip install -r requirements.txt
python -m pip check

python -B example.py http
python -B example.py retry
python -B example.py sync
python -B example.py combined
python -B example.py all
```

课件在完整课程仓库中展示的等价命令是：

```bash
.venv/bin/python -V
.venv/bin/python -m pip install -r java_to_python/chapters/25_async_http_io/requirements.txt
.venv/bin/python -m pip check

.venv/bin/python -B java_to_python/chapters/25_async_http_io/example.py http
.venv/bin/python -B java_to_python/chapters/25_async_http_io/example.py retry
.venv/bin/python -B java_to_python/chapters/25_async_http_io/example.py sync
.venv/bin/python -B java_to_python/chapters/25_async_http_io/example.py combined
.venv/bin/python -B java_to_python/chapters/25_async_http_io/example.py all

.venv/bin/ruff check java_to_python/chapters/25_async_http_io
.venv/bin/pyright java_to_python/chapters/25_async_http_io
```

建议进一步查阅 HTTPX 的异步支持、超时、资源限制、异常与传输文档，Python 的 `asyncio` Task、timeout 和 `to_thread` 文档，以及 SQLite 连接线程约束和 Pydantic 模型校验文档。
