# 第 28 章：FastAPI、依赖与异步服务生命周期

本章把前面学过的类型、异步 I/O、结构化并发和上下文传播放进一个可运行的 FastAPI 服务。示例是一个离线 Agent 查询服务：它只调用本地工具替身与 `FakeLLM`，不需要 API Key，也不访问公网。

你将看到：

- 外部 JSON 如何先经 Pydantic 校验，再成为可信的 Python 对象；非法请求为何自动返回 422。
- `def` 与 `async def` 路由分别在哪个线程执行，以及普通同步辅助函数不会自动变成异步。
- 如何用 lifespan 创建并关闭共享的 `httpx.AsyncClient`。
- 如何用 `Depends` 和 `yield` 依赖表达普通响应与流式响应的资源边界。
- 如何组合 `asyncio.TaskGroup`、请求级与应用级两个信号量、整体超时预算。
- 普通响应中的失败如何映射为 500，流式响应启动后为何只能发送 `error` 事件。
- `BackgroundTasks` 能做什么、不能做什么。
- 应用内 ASGI 测试与真实 Uvicorn/TCP 断连实验的边界。

## 28.1 一次请求经过哪些层

```text
HTTP 请求
  -> ASGI 中间件：生成或接收 request_id，并写入 ContextVar
  -> Pydantic：解析并校验 QueryRequest
  -> Depends：借用应用服务，获取 RequestLease
  -> 编排器：TaskGroup + 两层 Semaphore + 共享 HTTPX + FakeLLM
  -> 响应模型或 StreamingResponse
  -> yield 依赖清理
  -> 响应体发送完成
  -> BackgroundTasks 执行轻量审计
  -> 中间件 reset ContextVar
```

| 生命周期 | 典型对象 | 所有者 | 结束点 |
| --- | --- | --- | --- |
| 应用进程 | HTTPX 客户端、工具后端、全局并发限制 | FastAPI lifespan | 应用关闭 |
| 单个请求 | request_id、请求模型、RequestLease | 中间件与依赖 | 普通响应返回或流结束 |
| 一批工具 | TaskGroup 中的任务 | 发起编排的协程 | 全部完成，或失败后取消并回收 |
| 一个并发许可 | Semaphore permit | 当前工具调用 | 离开 `async with` |
| 后台回调 | 审计函数 | 响应流程 | 响应发出后执行，或进程提前退出 |

核心原则：长生命周期对象可以被短生命周期代码借用；短生命周期状态不要偷偷塞进共享单例。

## 28.2 Java 到 Python/FastAPI 的概念迁移

| Java / Spring | Python / FastAPI |
| --- | --- |
| `@RestController`、`@PostMapping` | `@app.post(...)` 装饰器 |
| DTO、Jackson、Bean Validation | Pydantic `BaseModel` |
| Spring MVC 参数解析 | 路由函数类型标注 |
| Spring DI | FastAPI `Depends` |
| `@Bean` 销毁、应用生命周期 | `lifespan` |
| try-with-resources | `yield` 依赖或异步上下文管理器 |
| 结构化并发 | `asyncio.TaskGroup` |
| `Semaphore` | `asyncio.Semaphore` |
| `ScopedValue` / `ThreadLocal` | `ContextVar` |
| Servlet stream / WebFlux `Flux` | `StreamingResponse` + 异步迭代器 |
| 响应后的轻量回调 | `BackgroundTasks` |
| Tomcat / Netty | Uvicorn |

Python 是动态语言，但动态不等于没有类型。FastAPI 在 HTTP 边界读取类型标注，Pydantic 在运行时完成解析与校验；业务代码拿到的是可信对象，而不是随处手拆字典。

## 28.3 请求模型、路由与错误边界

本章请求模型的关键约束：

```python
class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(min_length=1, max_length=120)
    tools: list[ToolName] = Field(default_factory=_default_tools, min_length=1, max_length=4)
    max_concurrency: int = Field(default=2, ge=1, le=3)
    timeout_ms: int = Field(default=500, ge=50, le=2_000)
    failure_mode: FailureMode = "best_effort"
```

`query` 会先去掉首尾空白，`tools` 不允许重复；未知字段因 `extra="forbid"` 被拒绝。路由本身直接接收模型：

```python
@application.post("/query", response_model=QueryResponse)
async def query(payload: QueryRequest) -> QueryResponse:
    return await orchestrate_query(payload, services, request_id)
```

`response_model` 也是输出合同：FastAPI 会序列化并校验返回值，同时生成 OpenAPI schema。

| 状态码 | 本章含义 |
| --- | --- |
| 200 | 成功，或 `best_effort` 下的部分成功 |
| 404 | 教学输入 `query == "missing"` |
| 422 | 空查询、非法工具、范围越界、重复或未知字段 |
| 500 | `strict` 模式工具失败 |
| 503 | lifespan 尚未启动，应用服务不可用 |
| 504 | 整体请求预算耗尽 |

## 28.4 `def` 与 `async def` 路由

- `async def` 路由直接运行在事件循环线程，适合原生异步调用链。
- 普通 `def` 路由由 FastAPI/Starlette 放到线程池执行，适合同步阻塞调用链。
- 在 `async def` 中直接调用普通函数时，普通函数仍在事件循环线程执行，不会因为调用者是异步函数就自动进线程池。

错误示例：

```python
async def route() -> dict[str, str]:
    result = blocking_sdk_call()  # 会阻塞事件循环
    return {"result": result}
```

原生异步 API 应使用 `async def`；完整的同步阻塞链可使用 `def` 路由；短期无法替换的同步函数可明确使用 `asyncio.to_thread`。运行 `/execution/sync` 与 `/execution/async` 可以观察线程名。

## 28.5 lifespan 管理共享资源

```python
@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake_tool_transport))
    services = Services(client)
    application.state.services = services
    try:
        yield
    finally:
        services.closed = True
        await client.aclose()
```

启动过程是：Uvicorn 启动、进入 lifespan、创建共享客户端、执行 `yield`、处理请求；关闭时恢复 `yield` 后面的代码并关闭客户端。

需要注意：

- 多 worker 时每个进程都有自己的 lifespan 和客户端池。
- 仅调用 `create_app()` 不会自动进入 lifespan。
- `app.state` 只是存放对象，不会自动给对象增加线程安全保证。
- 本章的锁和信号量只协调当前进程、当前事件循环。

`Services` 拥有 HTTPX 客户端、工具后端、`FakeLLM`、全局信号量与并发探针；工具后端只是借用共享客户端。

## 28.6 `Depends` 与 `yield` 依赖

```python
def get_services(request: Request) -> Services:
    services = cast(Services | None, getattr(request.app.state, "services", None))
    if services is None or services.closed:
        raise HTTPException(status_code=503, detail="应用服务尚未就绪")
    return services

ServicesDependency = Annotated[Services, Depends(get_services)]
```

这不是字段注入，也不是一个完整容器。FastAPI 解析函数参数形成依赖图，并在每次请求中计算或复用依赖值。

`open_request_lease` 用 `try/finally` 记录资源借用与释放：

```python
async def open_request_lease(request: Request) -> AsyncIterator[RequestLease]:
    lease = RequestLease(current_request_id(), request.app.state.trace)
    await lease.acquire()
    try:
        yield lease
    finally:
        await lease.release()
```

| scope | 释放时机 | 本章用途 |
| --- | --- | --- |
| `"function"` | 路由函数返回后、响应发送前 | 普通 `/query` |
| `"request"` | 整个响应发送结束后；这是默认值 | `/query/stream` |

普通响应的观测顺序：

```text
dependency:acquired
route returns
dependency:released
response:body-sent
background:audit-finished
```

流式响应的观测顺序：

```text
dependency:acquired
route returns StreamingResponse
generator keeps consuming
response:body-sent
dependency:released
```

## 28.7 任务所有权、容量与预算

三件事不要混在一起：

- `TaskGroup` 表达任务所有权和失败耦合，退出前会等待或取消并回收子任务。
- `Semaphore` 只限制同时进入关键区的数量，不拥有任务。
- `asyncio.timeout` 给整个请求设共享预算，而不是给每个工具重复发一份完整预算。

```python
request_limit = asyncio.Semaphore(payload.max_concurrency)
async with asyncio.timeout(payload.timeout_ms / 1_000):
    async with asyncio.TaskGroup() as group:
        tasks = {
            tool: group.create_task(
                execute_tool(tool, payload, services, request_limit, request_probe),
                name=f"tool:{tool}",
            )
            for tool in payload.tools
        }
```

每个工具同时经过请求级信号量和应用级信号量。若两个请求各允许 2 个工具并发，而应用级上限是 4，则全局峰值可到 4。整体预算覆盖等待许可、工具调用、清理和 `FakeLLM` 汇总。

| 模式 | 工具失败时的合同 |
| --- | --- |
| `best_effort` | 将失败包装成 `ToolResult(ok=False)`，保留其他成功结果 |
| `strict` | 失败传播给 TaskGroup，兄弟任务被取消并回收；普通响应映射为 500 |

## 28.8 `ContextVar` 传播请求上下文

```python
token = _request_id.set(request_id)
try:
    await self.app(scope, receive, send)
finally:
    _request_id.reset(token)
```

`ContextVar` 适合传播 request_id 这类请求上下文，不适合存业务结果。每次 `set` 后保存 token，并在 `finally` 中 reset，避免后续请求继承旧值。

适合应用级共享的对象包括 HTTP 连接池、只读模型和全局并发限制；请求模型、结果列表、错误列表和请求级探针应保持在请求内部。多 worker 是多个进程，内存状态并不共享。

## 28.9 StreamingResponse 与应用事件

流式接口使用 NDJSON，每行是一个完整 JSON 事件：

```text
start -> tool_result -> tool_result -> answer -> done
```

网络 chunk 边界不等于 JSON 行边界，客户端必须增量缓冲并按换行解析。

普通路由在响应启动前失败，仍可返回 404、500 或 504；流式路由一旦发出 `http.response.start`，HTTP 状态已经是 200，之后只能在事件协议里发送 `error`，再发送 `done`。客户端必须同时检查 HTTP 状态和事件协议。

## 28.10 断连并不自动等于取消

`network_disconnect_demo.py` 用真实回环 TCP 和 Uvicorn 做版本相关实验：

- 普通 `/query`：客户端断开后的 0.25 秒观察窗内，当前组合中的工具任务仍在运行，随后由实验主动放行。
- 流式 `/query/stream`：首个事件发出后断开，当前组合会取消流生成器，生成器的 `TaskGroup` 随之取消并回收两个工具任务。

这只是本章锁定版本下的观测，不是所有 ASGI 服务器、响应类型和远程 SDK 的永久保证。若业务要求确定的取消语义，应设计显式取消协议、幂等操作与服务端状态机。

## 28.11 `BackgroundTasks` 的边界

`BackgroundTasks` 适合响应发送后的轻量、进程内工作，例如记录教学审计日志。它不提供持久消息、跨进程确认、自动重试、死信队列或状态查询；进程崩溃时任务可能丢失，长任务还会继续占用服务器资源。

关键任务应交给持久任务系统，并设计 job_id、幂等键、状态查询、重试次数、下次重试时间与最后错误。

## 28.12 应用内测试与真实网络实验

应用内测试使用 `httpx.ASGITransport`，它会直接调用 ASGI 应用，不经过 DNS、TCP、TLS 或 Uvicorn。必须显式进入 lifespan：

```python
async with LifespanManager(application) as manager:
    transport = httpx.ASGITransport(app=manager.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://in-process") as client:
        response = await client.get("/health")
```

断连行为则用 `network_disconnect_demo.py` 启动真实 Uvicorn，并从回环 TCP socket 主动断开。

## 28.13 常见迁移误区

| 误区 | 正确认识 |
| --- | --- |
| `async def` 会让同步 SDK 自动异步 | 同步调用仍会阻塞事件循环 |
| 共享 HTTP 客户端等于所有对象都能共享 | 只共享线程安全或只读的长生命周期对象 |
| `ContextVar` 就是安全的全局变量 | 必须 reset，并测试线程边界 |
| `Semaphore` 会管理任务生命周期 | 它只限制容量；任务所有权由 TaskGroup 表达 |
| 每个工具各有完整超时就是请求超时 | 请求应共享一个端到端预算 |
| `yield` 依赖总在路由返回后立刻释放 | 释放时机取决于 dependency scope |
| 流式处理中还能改 HTTP 状态码 | 响应启动后应改用应用级 error 事件 |
| 客户端断开必然取消服务端所有任务 | 行为依赖服务器、版本、响应类型和下游 SDK |
| `BackgroundTasks` 是可靠队列 | 它只是进程内的轻量回调 |
| `ASGITransport` 是端到端网络测试 | 它不经过真实网络栈或 Uvicorn |

## 28.14 运行项目

本章按 Python 3.14 及以上编写。所有命令都在本目录执行。

安装依赖：

```bash
python -m pip install -r requirements-dev.txt
```

运行应用内实验：

```bash
python example.py
```

运行 5 个测试：

```bash
python -m pytest -q
```

启动服务：

```bash
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

普通查询：

```bash
curl -X POST http://127.0.0.1:8000/query \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: demo-ordinary" \
  -d '{"query":"生命周期","tools":["search","weather"],"max_concurrency":2}'
```

观察 422：

```bash
curl -X POST http://127.0.0.1:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query":" ","timeout_ms":1,"extra":"forbidden"}'
```

流式查询：

```bash
curl -N -X POST http://127.0.0.1:8000/query/stream \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: demo-stream" \
  -d '{"query":"流式响应","tools":["search","clock"]}'
```

运行真实网络断连实验：

```bash
python network_disconnect_demo.py
```

文件职责：

| 文件 | 作用 |
| --- | --- |
| `app.py` | FastAPI 应用、模型、依赖、生命周期、并发编排与流式协议 |
| `example.py` | 应用内可运行实验 |
| `network_disconnect_demo.py` | 真实 Uvicorn/TCP 断连实验 |
| `test_app.py` | 5 个应用内异步测试 |
| `requirements.txt` | 运行依赖 |
| `requirements-dev.txt` | 测试依赖 |

## 28.15 练习

1. 给请求模型增加可选 `client_tag`：只允许字母、数字、`-` 和 `_`，最长 24 个字符；校验必须放在模型层，并断言 422 的 `loc`。
2. 同时发送 3 个请求，请求级并发分别设为 1、2、3，验证全局峰值仍不超过 4。用 `asyncio.Event` 做屏障，不用 `sleep` 猜时序。
3. 写一个健壮的增量 NDJSON 客户端：允许一行跨多个 chunk，验证 `answer` 或 `error` 后最终出现 `done`，并测试客户端提前关闭与超时。
4. 把关键审计从 `BackgroundTasks` 迁移成持久任务合同。定义 job_id、幂等键、状态、重试次数、下次重试时间与最后错误；不要求真的接入消息中间件。

## 28.16 小结

- FastAPI 用类型标注和 Pydantic 把不可信 JSON 转成可信模型。
- lifespan 拥有应用级资源，`yield` 依赖拥有请求级资源。
- `TaskGroup` 管任务所有权，`Semaphore` 管容量，`timeout` 管端到端预算。
- `ContextVar` 传播请求上下文，但必须 reset。
- 流式响应启动后，失败必须进入应用事件协议。
- `BackgroundTasks` 只适合轻量、非关键的响应后工作。
- 应用内 ASGI 测试与真实网络实验验证的是不同边界。

建议先运行 `example.py`，再对照 `app.py` 阅读每一层的生命周期与清理点。
