# 第 27 章：用 pytest 验证异步代码与故障路径

## 先记住一句话

异步测试的核心不是“给 `async def` 加一个标记”，而是把任务何时开始、何时失败、谁负责取消、何时完成清理，都变成可以观察和断言的测试合同。

正常路径通常很好演示：模型返回答案，多个工具都成功，流被完整消费。真正容易在生产环境出问题的，往往是另外一半：

- 坏参数有没有在 I/O 前被拒绝；
- 一个并发任务失败后，其他任务是否被取消并等待完毕；
- timeout 到期后，后台是否还留着 Task；
- 消费者提前离开流时，连接是否已经归还；
- 两个请求交错运行时，`ContextVar` 是否串值；
- 测试替身究竟验证了真实协议的哪一层。

本章把这些问题写成可重复、离线、失败时能准确指出原因的 pytest 测试。学完后，你应该能：

- 使用 pytest 的发现规则、原生 `assert`、参数化和 `raises`；
- 用 fixture 表达资源的创建、借用、作用域和清理；
- 让 pytest-asyncio 管理测试事件循环，避免生命周期错配；
- 正确选择 Fake、Mock、AsyncMock、monkeypatch 和进程内 transport；
- 不依赖随机 `sleep`，稳定验证取消、TaskGroup、并发上限和上下文隔离；
- 检查 `ExceptionGroup` 的真正异常叶子，而不是只验证外层容器；
- 清楚说明一条离线测试证明了什么，以及它没有证明什么。

## 1. Java → Python 心智模型

| Java 中熟悉的做法 | Python / pytest 中可以先想到 | 最重要的不等价之处 |
| --- | --- | --- |
| JUnit 5 的 `@Test` | 名字以 `test_` 开头的函数 | pytest 默认靠命名发现，测试不必放进类中 |
| JUnit assertions / AssertJ | Python 原生 `assert` | pytest 会重写测试模块中的断言，失败时展示表达式中的值 |
| `@ParameterizedTest` + `@MethodSource` | `@pytest.mark.parametrize` | 每组参数在收集阶段成为独立测试项，不是测试函数内部的一次循环 |
| `assertThrows` | `pytest.raises` | 上下文范围应只包真正期望抛错的调用 |
| `@BeforeEach` / `@AfterEach` | function-scope fixture | fixture 还能依赖其他 fixture，并通过函数参数按名字注入 |
| try-with-resources | yield fixture / `async with` | `yield` 前是 setup，`yield` 后是 teardown；资源所有者仍是 fixture |
| JUnit `@TempDir` | pytest 内置 `tmp_path` | 得到的是每个测试独立的 `pathlib.Path` |
| Mockito Mock | `unittest.mock.Mock` | 普通 Mock 的调用结果不可自动 `await` |
| 对异步接口做 Mockito stub | `AsyncMock` 或 async Fake | `called` 和 `awaited` 是两种不同事实 |
| 替换静态调用或全局依赖 | `monkeypatch` | 要替换被测模块查找名字的位置，而不一定是原始定义位置 |
| `CountDownLatch` / `CyclicBarrier` | `asyncio.Event` / `asyncio.Barrier` | 它们协调的是 coroutine Task，不会阻塞事件循环线程 |
| `CompletableFuture` 测试 | 创建 Task 后 `await` | `Task.cancel()` 是协作式取消请求，不是强制终止线程 |
| Java 结构化并发作用域 | `asyncio.TaskGroup` | 多个失败通过 `ExceptionGroup` / `BaseExceptionGroup` 传播 |

pytest fixture 可以先类比为“JUnit 生命周期钩子加参数注入”，但不能完全等同于 Spring 容器。pytest 根据测试函数的参数名构建依赖图，按需创建并缓存值；它不会扫描业务 Bean，也不会提供完整的应用依赖注入生命周期。

pytest-asyncio 可以先类比为“JUnit 的异步测试执行扩展”。它给 coroutine 测试提供正在运行的事件循环，但不会替业务代码决定 Task、客户端和流由谁关闭。

## 2. pytest 怎样找到并执行一条测试

本章的 `pytest.ini` 把发现范围明确为：

```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
```

不传文件路径时，pytest 从 `tests/` 递归查找 `test_*.py`，再收集模块顶层的 `test_*` 函数，以及 `Test*` 类中的 `test_*` 方法。测试类不是必需的，也不应该为了分组而承担共享可变状态。

最小测试只是一个普通函数：

```python
def test_model_accepts_valid_search_args() -> None:
    args = SearchArgs.model_validate({"query": "  asyncio  ", "limit": 2})

    assert args.query == "asyncio"
    assert args.limit == 2
```

执行顺序是：pytest 收集函数，调用函数，Pydantic 校验并规范化输入，两个 `assert` 检查公开结果，函数正常返回后测试通过。

Python 的 `assert` 原本只会在条件为假时抛 `AssertionError`。pytest 在导入测试模块时会重写断言，因此 `assert actual == expected` 失败时通常能展开左右值、集合差异或第一个不同位置。一条断言仍应表达一条清晰合同，不要把多个无关结论塞进一个巨大布尔表达式。

不要用 `python -O -m pytest` 运行测试。优化模式会移除未经 pytest 重写的普通 `assert`，例如业务模块和插件中的断言，可能让测试失去本应检查的条件。

### 精确运行一个测试节点

从本章目录可以只运行一个“文件 `::` 测试函数”节点：

```bash
../../../../.venv/bin/python -m pytest \
  tests/test_async_runtime.py::test_cancellation_waits_for_start_then_checks_finally -vv
```

这相当于在 JUnit 中只运行一个测试方法，适合定位失败和快速迭代。

## 3. 用参数化和 `raises` 描述输入合同

同一条规则对应多组输入时，不要在测试函数里手写循环。参数化会让每组输入成为独立测试项：

```python
@pytest.mark.parametrize(
    ("raw_args", "bad_field"),
    [
        pytest.param({"query": "", "limit": 2}, "query", id="empty-query"),
        pytest.param({"query": "ok", "limit": 0}, "limit", id="limit-too-small"),
        pytest.param({"query": "ok", "limit": "2"}, "limit", id="strict-integer"),
    ],
)
def test_model_rejects_bad_search_args(raw_args: object, bad_field: str) -> None:
    with pytest.raises(ValidationError) as raised:
        SearchArgs.model_validate(raw_args)

    locations = {str(error["loc"][0]) for error in raised.value.errors()}
    assert bad_field in locations
```

这里有三个重要行为：

1. pytest 在收集阶段生成三个测试项，并在报告中使用自定义 `id`；
2. 一项失败不会遮住另外两项的结果；
3. 参数值不会被自动深拷贝，共享并修改同一个 list 或 dict 仍可能污染后续测试项。

`pytest.raises(ExpectedError)` 只证明上下文中的代码抛出了目标类型或其子类。因此作用域要尽量窄，并继续检查异常的关键字段、消息或精确类型：

```python
# 正确：只有预期失败的操作位于 raises 中。
with pytest.raises(ValidationError) as raised:
    SearchArgs.model_validate(raw_args)
assert "query" in str(raised.value)

# 反例：准备代码碰巧抛出同类异常时，测试也可能假通过。
with pytest.raises(ValidationError):
    raw_args = complicated_setup()
    SearchArgs.model_validate(raw_args)
```

Java 的 `assertThrows(RuntimeException.class, ...)` 也会接受子类；这里同理。合同若要求精确类型，可以继续断言 `raised.type is ValidationError`。

## 4. fixture：测试借用资源，fixture 拥有资源

fixture 是 pytest 按测试参数名解析的依赖：

```python
@pytest.fixture
def isolated_events() -> list[str]:
    return []

def test_starts_empty(isolated_events: list[str]) -> None:
    assert isolated_events == []
```

pytest 发现 `isolated_events` 参数后，先执行同名 fixture，再把返回值传给测试。fixture 默认是 function 作用域，所以每个测试得到新的 list，不依赖执行顺序。

### 作用域决定缓存多久

| scope | 复用范围 | 适合的资源 | 主要风险 |
| --- | --- | --- | --- |
| `function` | 每个测试项一次 | 可变数据、客户端、临时业务资源 | 创建次数较多，但隔离最好 |
| `class` | 一个测试类 | 类内共享的只读配置 | 类内测试可能互相污染 |
| `module` | 一个测试模块 | 昂贵且只读的模块资源 | 模块内出现顺序依赖 |
| `package` | 一个测试包 | 包级只读资源 | 所有权不容易看见 |
| `session` | 整次 pytest 进程 | 极昂贵且不可变的环境元数据 | 一次修改污染大量测试 |

不要只为了减少创建次数，就把可变集合、事务中的数据库连接或绑定事件循环的客户端提升为 session。昂贵资源确实必须共享时，共享层尽量只读；每个测试会修改的数据仍保持 function scope，或者为每项提供明确的回滚与重建。

### `yield` 把资源生命周期分成两半

```python
@pytest.fixture
def note_database(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "agent-notes.sqlite3"
    initialize_notes(path)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)
```

执行顺序是：

```text
fixture setup → yield path → 测试使用 path → 回到 fixture → finally 清理
```

测试成功、断言失败或抛出异常，pytest 都会继续执行已经到达 `yield` 的 fixture teardown。多个 fixture 按依赖顺序创建，并对已成功创建的部分按反方向清理。这与嵌套 try-with-resources 的“后创建先关闭”很像。

但如果异常发生在某个 fixture 到达 `yield` 之前，它自己的 `yield` 后代码不会执行。setup 每取得一项资源，就要尽快进入相应的上下文管理器或注册安全清理，不能等所有 setup 完成后才统一补救。

## 5. pytest-asyncio：事件循环由谁拥有

pytest 本身不会等待 coroutine 测试。本章安装 pytest-asyncio，并在 strict 模式下显式声明异步测试和异步 fixture：

```ini
asyncio_mode = strict
asyncio_default_fixture_loop_scope = function
asyncio_default_test_loop_scope = function
```

```python
@pytest.mark.asyncio
async def test_async_behavior() -> None:
    assert asyncio.get_running_loop().is_running()

@pytest_asyncio.fixture
async def model_client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://model.test",
    ) as client:
        yield client
```

strict 模式下：

- coroutine 测试需要 `@pytest.mark.asyncio`；
- async fixture 需要 `@pytest_asyncio.fixture`；
- 本章每个测试函数使用独立事件循环；
- pytest-asyncio 在该循环中把测试作为 Task 运行，所以测试体直接 `await`。

### 为什么不能再调用 `asyncio.run()`

`asyncio.run()` 是同步程序最外层的 runner：它创建事件循环，执行一个异步入口，最后关闭循环。pytest-asyncio 已经在当前线程启动了循环，因此下面的写法会失败：

```python
@pytest.mark.asyncio
async def test_wrong() -> None:
    result = asyncio.run(call_tool())  # RuntimeError：不能嵌套运行事件循环
```

测试中应直接写：

```python
@pytest.mark.asyncio
async def test_right() -> None:
    result = await call_tool()
```

本章的普通脚本 `example.py` 没有外部 runner，所以只在最外层调用一次 `asyncio.run()`；测试入口则完全交给 pytest-asyncio。这是两个不同入口的所有权边界。

### fixture scope 与 loop scope 是两条维度

fixture 的 scope 决定 fixture 值缓存多久，loop scope 决定异步 setup/teardown 在哪个事件循环执行。异步 fixture 的 loop scope 不能比缓存 scope 更窄，否则一个仍在复用的资源可能所属循环已经关闭。

本章不共享异步资源，所以测试与 fixture 都采用 function loop scope。若确实要跨测试共享客户端，需要同时设计缓存作用域、事件循环作用域和可变状态隔离，不能只把 `scope="module"` 加到一个地方。

## 6. Fake、Mock、AsyncMock 与 monkeypatch 怎样选

测试替身的目标是控制边界，而不是把所有对象都变成 Mock。

| 手段 | 最适合回答的问题 | 不适合证明的事情 |
| --- | --- | --- |
| 普通函数 / 小 Fake | 输入怎样映射为结果，或对象怎样经历多个状态 | 真实网络、真实数据库、生产性能 |
| Mock | 同步依赖被调用几次、收到什么参数 | async 调用是否真的被等待 |
| AsyncMock | async 依赖被 await 几次、收到什么参数 | 完整的并发或资源状态机 |
| monkeypatch | 一个测试期间临时替换属性、环境变量或字典项 | 跨进程依赖或任意位置的全局替换 |
| MockTransport | HTTPX 怎样构造请求、处理状态和解析响应 | DNS、socket、TLS、代理和真实连接池 |

### 普通 Mock 不能冒充 async 调用

```python
plain_mock = Mock(return_value="普通字符串")
await plain_mock("prompt")  # TypeError：str 不可等待
```

调用 AsyncMock 才会先得到 awaitable；等待该对象后，才得到配置的 `return_value`：

```python
model_call = AsyncMock(return_value="离线模型结果")
result = await ask_model(model_call, "解释 TaskGroup")

assert result == "离线模型结果"
model_call.assert_awaited_once_with("解释 TaskGroup")
```

`model_call.called` 只说明发生过函数调用，不说明返回的 coroutine 被等待。异步合同应使用 `assert_awaited*` 系列断言。

### monkeypatch 要替换“使用位置”

假设被测模块执行：

```python
from gateway import call_model_api
```

导入完成后，被测模块已经拥有自己的 `call_model_api` 名字。只修改 `gateway.call_model_api` 不一定能影响它；应 patch 被测模块实际查找的属性：

```python
fake_api = AsyncMock(return_value="patched-result")
monkeypatch.setattr(agent_runtime, "call_model_api", fake_api)
```

pytest 会在当前测试结束后撤销 monkeypatch。这个机制类似局部名字重绑定，不是 Java 动态代理，也不会自动替换其他模块已经绑定的同名对象。

### 选择顺序

1. 只需要固定输入输出时，优先普通函数或很小的 Fake；
2. 需要检查同步调用的次数和参数时，用 Mock；
3. 接口是 async 时，用 AsyncMock 并断言 await；
4. 依赖尚未通过参数注入时，用 monkeypatch 替换被测模块的使用位置；
5. 替身开始复制复杂生产协议时，增加更真实的一层测试，而不是继续堆 Mock 配置。

## 7. 稳定异步测试：同步状态，不猜时间

异步测试最通用的结构是：

```text
安排任务
  → 等待被测对象进入确定状态
  → 触发成功、失败、取消或放行
  → await 所有者直到任务到达终态
  → 断言结果、异常、兄弟任务和资源清理
```

关键是“进入确定状态”。`await asyncio.sleep(0.01)` 只能猜目标 Task 大概运行到哪里；CI 负载、事件循环调度和机器速度都可能改变结果。`Event`、`Barrier`、队列或可观察调用记录能表达真实的先后关系。

### 取消不是同步终止

稳定的取消测试可以写成：

```python
task = asyncio.create_task(wait_for_tool(lifecycle))
await lifecycle.started.wait()  # 已确认进入资源区

task.cancel("测试主动取消")
with pytest.raises(asyncio.CancelledError):
    await task

assert lifecycle.cleaned.is_set()
assert lifecycle.events == ["started", "cleaned"]
assert task.cancelled()
```

`task.cancel()` 只是安排在 Task 的下一次可取消点抛出 `CancelledError`。只有继续 `await task`，调用方才能确认异常已传播、`finally` 已执行、Task 已到达终态。只调用 `cancel()` 就结束测试，可能留下 pending Task，也没有证明连接或锁已经释放。

### 测试 timeout 是保险丝，不是业务实现

等待阶段可以加一个较宽的测试超时：

```python
async with asyncio.timeout(1.0):
    await lifecycle.started.wait()
```

它防止实现退化后整套 CI 永久挂住，但不能替代业务合同：

- 它不能证明被测服务使用了正确 deadline；
- 它不能替被测代码执行 `finally`；
- 它不能“硬杀”吞掉取消的代码；
- 清理本身需要等待时，墙钟耗时可能超过触发 timeout 的那个时刻。

## 8. TaskGroup：先决定失败语义，再写断言

并发工具不是一律“一个失败全部失败”。要先区分业务合同。

### 可选任务：只把预期业务失败变成数据

```python
async def _as_optional_outcome(name: str, call: ToolCall) -> ToolOutcome:
    try:
        return ToolOutcome(name=name, value=await call(), error=None)
    except ToolBusinessError as error:
        return ToolOutcome(name=name, value=None, error=str(error))
```

这里只把 `ToolBusinessError` 视为合同允许的部分结果。`ToolProgramError`、`TypeError` 和取消不能被 `except Exception` 粗暴吞掉，否则程序缺陷会伪装成业务失败。

相应测试应同时断言：成功项仍存在、已知失败项保留正确错误、所有成员都完成清理。不要只断言最终列表长度。

### 必需任务：一个失败，兄弟也必须被收集

全部工具都是必需项时，成员异常离开 coroutine，TaskGroup 会取消尚未结束的兄弟，并等待它们退出，然后把未处理错误组合后抛给父任务。

稳定测试应按以下顺序控制：

1. 用 Event 等待三个 Fake 都进入 `started`；
2. 只放行会抛 `ToolProgramError` 的成员；
3. 保证另外两个兄弟仍停在可取消等待点；
4. `await` 父请求并取得 `ExceptionGroup`；
5. 检查真实异常叶子、兄弟确实收到取消、所有 `finally` 都已完成。

测试不应断言兄弟的启动或清理先后顺序。Task 调度顺序通常不是业务合同；“全部已清理”才是。

### deadline 要覆盖整个并发作用域

本章用一次绝对截止时间包住整个任务组：

```python
async with asyncio.timeout_at(deadline):
    return await run_required_tools(tools)
```

期限到达后，当前父 Task 收到取消，TaskGroup 再取消并等待子任务；离开 timeout 上下文后，调用方看到 `TimeoutError`。测试要验证：

- 调用方收到 `TimeoutError`；
- 未完成工具观察到取消；
- 工具的异步清理在返回前完成。

不要断言“必须恰好在 0.200 秒返回”。deadline 决定何时发起取消，资源清理仍需要时间。

## 9. ExceptionGroup：检查叶子，而不是外壳

TaskGroup 可能报告嵌套异常组。下面的断言过宽：

```python
with pytest.raises(ExceptionGroup):
    await request
```

它只证明出现了异常组；组里即使是意外的 `TypeError`，测试也会通过。更可靠的做法是取得异常组，递归展开叶子，再按业务语义检查：

| 异常叶子 | 建议分类 | 默认处理方向 |
| --- | --- | --- |
| `ToolBusinessError` | 预期业务失败 | 只有合同允许时才转成部分结果 |
| `TimeoutError` | deadline / timeout | 记录预算层次，不伪装成业务拒绝 |
| `CancelledError` | 上游取消 | 本层清理后继续传播 |
| 其他异常 | 程序缺陷 | 保留 traceback，让请求和测试失败 |

`CancelledError` 继承 `BaseException`。包含它的手工异常组必须是 `BaseExceptionGroup`，不一定是普通 `ExceptionGroup`。TaskGroup 因成员失败而取消兄弟时，兄弟正常传播的 `CancelledError` 通常不会成为最终错误叶子，所以还要通过 Fake 状态单独证明兄弟确实被取消并清理。

pytest 9 也提供 `pytest.RaisesGroup` 描述异常组形状。本章显式展开叶子，是为了让“如何分类失败”的过程更容易观察；只关心固定组结构时可以进一步使用 `RaisesGroup`。

## 10. 三类容易写成随机测试的并发合同

### 并发上限：测状态，不测速率

“六项工作在 300 毫秒内完成”不能证明在途数从未超过 2。更稳妥的 Fake 在 Semaphore 保护区域记录状态：

```python
self.active += 1
self.max_active = max(self.max_active, self.active)
if self.active == self.expected_peak:
    self.peak_reached.set()
try:
    await self.release.wait()
finally:
    self.active -= 1
```

测试等 `peak_reached` 后，在闸门仍关闭时断言 `active == 2`，再放行全部工作。结束后检查：

- `max_active == 2`：确实用满两个槽位，也从未超过上限；
- 返回结果与输入的对应关系正确；
- `active == 0`：所有 `finally` 都归还了探针状态。

Semaphore 验证的是同时在途数，不是单位时间请求数。不要把并发上限测试写成 QPS 限流测试。

### 提前退出异步流：返回前就应完成关闭

`async for` 提前 `return` 或 `break` 时，资源所有者应该显式关闭异步生成器：

```python
@asynccontextmanager
async def owned_token_stream(state):
    stream = token_stream(state)
    async with aclosing(stream):
        yield stream
```

`aclosing()` 在离开 `async with` 时等待 `stream.aclose()`。因此调用者拿到第一个 token 并返回时，生成器的 `finally` 已经执行，而不是等事件循环退出时碰运气回收。

一条完整测试应检查返回值、closed 状态、关闭次数恰好为 1，以及必要的事件顺序。这个 Fake 只证明异步生成器所有权；它没有验证 HTTP 分块、SSE/JSONL framing、连接池、背压或远端断开。

### ContextVar：用屏障制造确定性交错

两个请求是否隔离，不能只 `gather()` 后期待它们“碰巧交错”。可以让两个 Task 都设置请求 ID，再在 `Barrier(2)` 汇合：

```python
token = request_id_var.set(request_id)
try:
    before = request_id_var.get()
    await barrier.wait()
    after = request_id_var.get()
    return before, after
finally:
    request_id_var.reset(token)
```

两个请求的结果必须分别是 `("req-A", "req-A")` 和 `("req-B", "req-B")`。调用结束后，父上下文仍应读取默认值 `"-"`，证明 token 已恢复。

这里证明的是同一事件循环中 Task 的 Context 隔离，以及函数入口正确 reset。它没有覆盖线程池、子进程、远程服务或分布式追踪传播。

## 11. I/O 测试要说清验证层次

“没有访问公网”和“只做了纯单元测试”不是一回事。本章在本地选择不同真实程度的边界：

| 测试手段 | 实际验证 | 没有验证 |
| --- | --- | --- |
| Pydantic 直接调用 | 坏参数、严格类型、未知字段、错误位置 | HTTP、模型真实输出分布 |
| AsyncMock 模型函数 | 调用参数、次数、确实 await | SDK 序列化、网络和服务端行为 |
| HTTPX MockTransport | 请求路径与 query、状态处理、JSON 到响应模型 | DNS、socket、TLS、代理、连接池、真实网络 timeout |
| `tmp_path` 下的 SQLite 文件 | 真实建表、SQL、参数绑定、读取和连接关闭 | 生产数据库驱动、权限、迁移、网络和锁竞争 |
| Event 驱动 Fake | Task 状态机、取消和清理的因果顺序 | 操作系统或远端资源是否真正支持取消 |

HTTP fixture 仍创建真实 `httpx.AsyncClient`，只是把 transport 替换成进程内 handler。业务函数会真实构造 Request、处理状态码、解析 JSON 并做 Pydantic 校验，但不会创建 socket。

SQLite fixture 则在 `tmp_path` 中创建真实文件，比 `Mock(sqlite3.Connection)` 更容易发现 SQL 语法和参数绑定错误，又不会接触长期数据库。注意 sqlite3 connection 的上下文管理器负责事务提交或回滚，并不负责关闭连接；本章被测代码额外使用 `contextlib.closing()`。

## 12. 用一条故意失败的测试验证“测试真的会红”

只看全绿测试，可能不知道断言能否识别目标缺陷。本章保留了一个错误实现：它读取异步生成器的第一项后直接返回，没有先调用 `aclose()`。

受控失败文件是 `tests/intentional_failure_demo.py`。文件名不匹配 `test_*.py`，所以正常套件不会收集它。只有显式指定时才运行：

```bash
../../../../.venv/bin/python -B -m pytest tests/intentional_failure_demo.py -q
```

预期命令以状态码 1 退出，并显示：

```text
AssertionError: 缺陷：提前返回时没有关闭异步生成器
```

故意失败的断言仍放在 `try/finally` 中，测试最后会等待泄漏流的 `aclose()`。测试报告会变红，但不会把未关闭的异步生成器继续遗留给事件循环。

不要把它改名为 `test_intentional_failure_demo.py` 放入正常 CI。正确修复也不是删除断言，而是让资源所有者使用 `aclosing()` 或等价的明确生命周期。

## 13. Java 工程师最容易踩的坑

1. 在 async 测试里调用 `asyncio.run()`：pytest-asyncio 已经拥有运行中的循环；直接 `await`。
2. 只调用 `task.cancel()`：这只是取消请求；还要 `await task` 并断言清理和终态。
3. 用很短的 `sleep` 等任务启动：这是调度猜测；改用 Event、Barrier、队列或调用记录。
4. 把测试 timeout 当业务清理：它只能让测试有限失败，不能替被测代码释放资源。
5. 让普通 Mock 返回字符串给 async 接口：字符串不可等待；使用 AsyncMock 或 async Fake。
6. patch 原始定义位置：被测模块可能已经绑定旧名字；patch 实际使用位置。
7. 让 session fixture 返回可变对象：测试会互相污染；缩小 scope 或提供副本/回滚。
8. strict 模式下用普通装饰器声明 async fixture：插件不会接管；使用 `@pytest_asyncio.fixture`。
9. 只断言出现 ExceptionGroup：未知程序错误也能满足；继续检查异常叶子。
10. 把 MockTransport 当真实网络：它不经过 socket；网络行为需要单独的回环或集成测试。
11. 测试结束时仍有未等待 Task：创建者保存引用，并在成功、失败和取消路径都收集它。
12. 期待事件循环最终替你关闭流：请求边界早已结束；资源所有者应在局部作用域明确关闭。

写异步测试前，可以依次问自己：

1. 我要验证的是结果、交互、状态转换，还是某一层真实 I/O？
2. 最小够用的替身是函数、Fake、Mock、AsyncMock，还是 MockTransport？
3. 谁创建 Task、客户端、流和数据库文件？谁负责在所有退出路径等待并关闭？
4. 执行顺序能否由 Event、Barrier 或状态证明，而不是由耗时猜测？
5. timeout 是业务合同，还是仅用于防止测试挂住？
6. 失败后是否检查了异常叶子、兄弟取消、`finally` 和零遗留任务？
7. 当前替身没有覆盖的行为，是否已经清楚写出？

## 14. 运行本章项目

### 环境和依赖

仓库根 `pyproject.toml` 要求 Python >=3.14。本章运行依赖为 HTTPX 0.28.1 和 Pydantic 2.13.5；开发依赖再增加 pytest 9.1.1 与 pytest-asyncio 1.4.0。

原课件从仓库根目录安装：

```bash
.venv/bin/python -V
.venv/bin/python -m pip install \
  -r java_to_python/chapters/27_pytest_async/requirements-dev.txt
.venv/bin/python -m pip check
```

在本独立还原目录中，可使用等价命令：

```powershell
py -3.14 -m pip install -r requirements-dev.txt
py -3.14 -m pip check
```

### 运行可观察演示

原课件命令：

```bash
.venv/bin/python -B java_to_python/chapters/27_pytest_async/example.py
```

独立目录命令：

```powershell
py -3.14 -B example.py
```

观察重点不是记住七段输出，而是确认：取消后 `finally` 已执行、必需任务失败会回收兄弟、并发峰值为 2、提前返回前流已关闭、两个 request id 没有串值、进程内 HTTP 和临时数据库都不会访问生产资源，以及错误实现确实暴露了未关闭状态。

### 运行正常测试套件

原课件从本章目录运行：

```bash
(cd java_to_python/chapters/27_pytest_async && \
  ../../../../.venv/bin/python -B -m pytest)
```

独立目录命令：

```powershell
py -3.14 -B -m pytest
```

之所以进入本章目录，是为了让 `chapter27/` 作为正常本地包出现在导入路径中。测试没有修改 `sys.path`，也没有导入名字以数字开头的章节目录。

### 运行静态检查

```bash
.venv/bin/ruff check java_to_python/chapters/27_pytest_async
.venv/bin/ruff format --check java_to_python/chapters/27_pytest_async
source .venv/bin/activate
pyright java_to_python/chapters/27_pytest_async/example.py \
  java_to_python/chapters/27_pytest_async/chapter27 \
  java_to_python/chapters/27_pytest_async/tests
```

完整运行验证运行时行为，Ruff 检查格式和常规代码规则，Pyright 做静态类型检查。三者不能互相替代。

### 文件职责

| 文件 | 作用 |
| --- | --- |
| `README.md` | 独立教程、测试设计原则、运行方法和练习 |
| `example.py` | 打印七类可观察行为；只在最外层创建一次事件循环 |
| `chapter27/agent_runtime.py` | 本章使用的最小 Agent 风格被测模块 |
| `tests/conftest.py` | function-scope、同步和异步 yield fixture |
| `tests/test_validation_and_doubles.py` | 校验、参数化、fixture 和测试替身 |
| `tests/test_async_runtime.py` | 取消、TaskGroup、deadline、并发、流、ContextVar 和异常组 |
| `tests/test_io_boundaries.py` | 进程内 HTTP transport 与临时 SQLite 文件 |
| `tests/intentional_failure_demo.py` | 正常发现范围外的受控失败演示 |
| `pytest.ini` | 发现规则、strict 模式和 function loop scope |
| `requirements.txt` | 示例与业务模块的运行依赖 |
| `requirements-dev.txt` | 运行依赖加测试依赖 |

本章所有正常测试都离线运行。它们没有验证公网、真实 LLM/MCP 服务、DNS、TLS、代理、付费 API、生产数据库驱动或高并发事务，也没有承诺 deadline 精确到某个墙钟毫秒。

## 15. 递进练习

### 练习 1：补齐响应模型的错误矩阵

为 `ModelReply` 增加三组参数化坏输入：缺少 `answer`、`answer` 为空、`answer` 为整数。直接调用 `model_validate()`，给每组输入可读的 id，并检查错误字段。暂时不要引入 HTTP。

### 练习 2：让异步清理也需要放行

给 `ToolLifecycle` 增加 `cleanup_started` 和 `cleanup_may_finish` 两个 Event。测试先等任务 started，再取消；等 cleanup_started 后先证明 Task 尚未 done，最后放行清理并等待 `CancelledError`。整个测试不使用随机 sleep，并检查资源只关闭一次。

### 练习 3：未知错误不能成为部分成功

在三个 optional tool 中让一个抛 `ToolProgramError`，另外两个停在 Event。不要把 `run_optional_tools()` 改成捕获所有异常。检查最终异常叶子、另外两个 Task 的取消和清理状态，并确保没有未等待 Task。

### 练习 4：增加两种 HTTP 响应失败

用两个独立 MockTransport handler 分别返回非法 JSON，以及合法 JSON 但字段不符合 `ModelReply`。测试应区分 JSON 解码失败和 Pydantic 校验失败，同时说明它仍没有验证真实网络。

### 练习 5（选学）：审计共享事件循环的所有权

只在一个新测试模块中，让两个相邻测试与一个 async fixture 使用 module loop scope。确认 fixture 在同一循环内 setup 和 teardown，并解释这个资源为什么值得共享，以及怎样避免可变状态污染。

## 16. 本章小结

pytest 提供发现、断言、参数化和 fixture；pytest-asyncio 负责让 coroutine 测试在明确的事件循环中运行。真正决定异步测试质量的，是下面四个问题：

1. 是否用可观察状态建立了确定的执行顺序；
2. 是否等待每个 Task 到达终态，并检查 `finally` 已完成；
3. 是否按业务语义区分部分失败、整体失败、timeout、取消和程序缺陷；
4. 是否准确说明测试替身覆盖的边界，而没有把进程内测试夸大成真实系统验证。

如果一条测试只能证明“这次运行没报错”，它还不是可靠的异步测试。如果它能明确回答“哪个状态已经发生、谁拥有资源、故障怎样传播、最后留下了什么”，它才真正构成一条回归合同。
