# 第 18 章：上下文管理器与资源生命周期

一句话概括本章：`with` 把“资源必须在所有离开路径上释放”写成了一条可复用的协议。

文件、数据库连接、事务、锁、HTTP 客户端和工具会话都存在生命周期。获得资源通常不难，难的是保证正常结束、提前 `return`、块内异常和后续资源获取失败时都不会漏掉清理。只在业务代码末尾写一句 `close()` 并不可靠，因为前面的任何跳转或异常都可能绕过它。

对于 Java 工程师，可以先把 Python 的 `with` 类比为 `try-with-resources`，但两者不能完全等同。Python 不要求对象实现名为 `AutoCloseable` 的接口，而是在运行时使用 `__enter__()` 和 `__exit__()` 协议；`as` 后面的值甚至可以不是管理器对象本身。

完成本章后，你应该能够：

- 解释 `with` 如何保证成功进入的资源一定执行退出逻辑；
- 区分上下文管理器、`__enter__()` 返回值和 `as` 目标；
- 预测正常结束、`return`、块内异常和进入失败时的执行顺序；
- 正确实现类上下文管理器和 `@contextmanager`；
- 用多项 `with` 和 `ExitStack` 逆序清理多个资源；
- 判断谁拥有资源、谁只是借用资源；
- 在资源型生成器提前停止时建立明确的关闭边界。

本章只讨论同步上下文管理器。`async with`、`asynccontextmanager` 和 `AsyncExitStack` 会在后续异步章节中展开。

## 18.1 先建立 Java → Python 心智模型

Java 和 Python 解决的是同一个工程问题：资源一旦成功取得，退出使用区间时就必须清理。不过，两门语言的协议和异常语义不同。

| 关注点 | Java `try-with-resources` | Python `with` |
|---|---|---|
| 资源协议 | `AutoCloseable.close()` | `__enter__()` 与 `__exit__()` |
| 协议检查 | 编译期要求资源类型可关闭 | 运行时查找上下文管理协议 |
| 块内变量 | 通常就是声明的资源对象 | `as` 绑定 `__enter__()` 的返回值 |
| 正常退出 | 自动调用 `close()` | 调用 `__exit__(None, None, None)` |
| 块内异常 | 关闭后继续传播，关闭异常有 Java 的 suppressed 规则 | `__exit__()` 能观察异常，并可通过真值抑制它 |
| 多资源 | 从后向前关闭 | 从后向前调用 `__exit__()` |
| 块级作用域 | 资源变量离开 `try` 后不可见 | `as` 名字通常仍可见，但资源可能已经关闭 |

最重要的两个不等价点是：

1. Python 的管理器和块内句柄可以是两个对象。管理器负责生命周期，句柄只暴露允许使用的能力。
2. Python 的 `__exit__()` 返回值会参与异常控制流。误返回一个真值，可能把真正的业务异常吞掉。

因此，看到下面的代码时，不要只理解为“最后自动执行 `close()`”：

```python
with make_session() as session:
    session.call_tool()
```

更准确的理解是：`make_session()` 先产生管理器，管理器成功进入后把一个值交给 `session`，离开缩进块时再由同一个管理器处理退出。

## 18.2 `with` 是协议，不是文件专用语法

一个类只要提供 `__enter__()` 和 `__exit__()`，就可以参与同步 `with` 协议：

```python
class Session:
    def __enter__(self) -> "Session":
        print("打开会话")
        return self

    def call(self) -> str:
        return "调用完成"

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        print("关闭会话")
        return False


with Session() as session:
    print(session.call())
```

输出顺序是：

```text
打开会话
调用完成
关闭会话
```

这里有三个容易混淆的角色：

- `Session()` 创建的对象是 context manager（上下文管理器）；
- `__enter__()` 的返回值才会绑定给 `as session`；
- `__exit__()` 属于管理器，负责离开本次上下文。

上例的 `__enter__()` 返回 `self`，所以管理器和块内值恰好是同一个对象。协议并没有要求这样做。真实 SDK 可以让管理器保留关闭权限，只返回一个受限句柄：

```python
manager = ToolSessionManager()
with manager as handle:
    handle.call("search")

assert handle is not manager
```

这可以先类比为 Java 所有者把受限接口借给业务代码，但不能完全等同于 Java 的资源变量：Python 的 `as` 明确接收进入方法的返回值，而不是固定接收管理器本身。

## 18.3 一次 `with` 到底怎样执行

单个 `with` 的核心过程可以概括为：

```text
计算上下文表达式
    ↓
得到管理器并调用 __enter__()
    ↓
把返回值绑定给 as 目标
    ↓
执行 with 块
    ↓
调用同一管理器的 __exit__()
```

官方语义可以用下面的概念代码展开。实际解释器会按特殊方法规则查找协议方法，但生命周期顺序与这里一致：

```python
import sys

manager = EXPRESSION
enter = manager.__enter__
exit_method = manager.__exit__
value = enter()
hit_exception = False
try:
    TARGET = value
    SUITE
except BaseException:
    hit_exception = True
    if not exit_method(*sys.exc_info()):
        raise
finally:
    if not hit_exception:
        exit_method(None, None, None)
```

由此可以推导出一条关键规则：只有 `__enter__()` 正常返回，Python 才承诺为本次 `with` 调用 `__exit__()`。成功进入之后，即使 `as` 目标绑定失败，也会进入退出流程。

常见路径如下：

| 离开路径 | 是否调用本次 `__exit__()` | 传入的异常信息 |
|---|---:|---|
| 运行到块末尾 | 是 | 三个 `None` |
| 块内 `return` | 是 | 三个 `None` |
| 块内 `break` / `continue` | 是 | 三个 `None` |
| 块内抛异常 | 是 | 异常类型、异常对象、traceback |
| 计算上下文表达式时失败 | 否 | 尚未得到管理器 |
| `__enter__()` 抛异常 | 否 | 尚未成功进入 |

`return` 不会抢在清理之前离开函数。Python 会先准备返回值，再调用 `__exit__(None, None, None)`，清理成功后调用方才收到返回值。如果退出逻辑又抛出异常，调用方不会正常收到原返回值。

## 18.4 `__exit__()` 如何看到并决定异常命运

带类型标注的典型签名如下：

```python
from types import TracebackType

def __exit__(
    self,
    exc_type: type[BaseException] | None,
    exc_value: BaseException | None,
    traceback: TracebackType | None,
) -> bool:
    self.close()
    return False
```

正常结束、`return`、`break` 和 `continue` 都不是异常，因此三个参数都是 `None`。如果块内抛出 `ToolCallError("timeout")`：

- `exc_type` 是 `ToolCallError` 类；
- `exc_value` 是消息为 `"timeout"` 的异常对象；
- `traceback` 记录异常经过的调用栈。

退出方法的返回值只影响正在传播的块内异常：

| `__exit__()` 的结果 | 块内异常怎样处理 |
|---|---|
| 假值，例如 `False` 或 `None` | 原异常继续向外传播 |
| 任意真值，例如 `True`、`1`、非空字符串 | 原异常被抑制，继续执行 `with` 后面的语句 |
| `__exit__()` 自己抛出新异常 | 新异常向外传播，原异常成为异常上下文 |

普通资源管理器通常只负责清理，不负责把失败变成成功，因此应返回 `False`，或者不写 `return` 让函数自然返回 `None`。

不要顺手返回清理函数的结果：

```python
# 错误：若 close_and_report() 返回 "closed"，块内异常会被隐藏。
def __exit__(self, exc_type, exc_value, traceback):
    return self.close_and_report()
```

有意抑制异常并非永远错误。例如，一个明确约定“目标文件不存在时使用默认值”的专用管理器，可以只抑制 `FileNotFoundError`。但这必须是公开 API 语义，而且应严格判断异常类型，不能无条件返回真值。

## 18.5 为什么 `__enter__()` 失败必须自己回滚

真实连接往往分阶段建立：

```text
分配 socket → 建立 TLS → 鉴权 → 发布可用会话
```

假设 socket 已分配，而鉴权抛出异常。由于 `__enter__()` 没有正常返回，本次 `with` 不会调用 `__exit__()`。获取逻辑必须回滚自己已经完成的部分初始化：

```python
def __enter__(self):
    self._allocate_transport()
    try:
        self._authenticate()
        return self._build_handle()
    except BaseException:
        self._release_transport()
        raise
```

这里捕获 `BaseException` 不是为了把所有失败转换成业务结果，而是为了释放已取得的底层资源后原样重抛。这样即使遇到 `KeyboardInterrupt` 等退出信号，也不会遗留已分配的资源。回滚操作最好是幂等的，避免错误路径再次清理时产生第二个故障。

这个边界与 Java 构造资源时的情况相似：如果第二个资源初始化失败，已经成功创建的第一个资源会由外层结构关闭；但第二个对象内部已经完成的部分初始化，仍要由它自己的获取逻辑负责回滚。调用方既不知道完成了哪些步骤，也不应该猜测半初始化对象能否安全执行完整 `close()`。

## 18.6 `try/finally` 是底层控制流，`with` 是可复用封装

只有一处使用资源时，最基本的安全骨架是：

```python
resource = acquire()
try:
    use(resource)
finally:
    release(resource)
```

正常结束、`return` 和异常都会在离开 `try` 时先执行 `finally`。如果 `acquire()` 自己失败，它不在 `try` 范围内，因此仍须按照上一节的原则自行回滚部分初始化。

下面两种写法都不安全：

```python
# use() 失败会绕过 release()。
resource = acquire()
use(resource)
release(resource)

# finally 中的 return 会覆盖原返回值或正在传播的异常。
try:
    use(resource)
finally:
    return release(resource)
```

Python 3.14 还会对 `finally` 中的 `return`、`break` 或 `continue` 发出 `SyntaxWarning`。工程代码应让 `finally` 专注于清理，不要在其中改写原控制流。

当多处代码都需要同一种生命周期时，再把这段 `try/finally` 封装成类协议。类实现适合以下情况：

- 对象有 `NEW → OPEN → CLOSED` 之类的显式状态；
- 获取过程需要配置、多个辅助方法或失败回滚；
- 管理器要返回与自身不同的借用句柄；
- 退出时要根据异常类型执行明确策略。

所以 `with` 不是 `try/finally` 的替代品。前者把后者表达的清理边界封装成了可组合协议。

## 18.7 用 `@contextmanager` 表达线性生命周期

如果生命周期可以按“获取 → 使用 → 清理”顺序写清，`contextlib.contextmanager` 能省去类的样板代码：

```python
from collections.abc import Generator
from contextlib import contextmanager

@contextmanager
def managed_session() -> Generator[Session]:
    session = Session()
    session.open()
    try:
        yield session
    finally:
        session.close()

with managed_session() as session:
    session.call()
```

装饰器把生成器函数转换成“每次调用都创建一个上下文管理器”的工厂。这里的 `yield` 不是在生产一串业务数据，而是在划分上下文的进入和退出：

1. 调用 `managed_session()` 得到管理器，生成器函数体还没有完整执行；
2. `with` 进入时运行到 `yield`，把 `session` 绑定给 `as` 目标；
3. 生成器暂停，转而执行 `with` 块；
4. 块正常结束时，从 `yield` 后恢复；
5. 块内抛异常时，同一个异常会在暂停的 `yield` 位置重新抛出；
6. `finally` 负责关闭资源，然后生成器结束。

一次上下文生命周期必须恰好执行一次 `yield`。如果生成器正常结束却没有执行 `yield`，就没有进入值；执行第二次 `yield`，退出过程又无法结束。`contextmanager` 会用 `RuntimeError` 拒绝这两种实现。若到达 `yield` 之前的获取代码自己抛出异常，向外传播的就是该获取异常。

异常路径还有一个很隐蔽的坑：

```python
@contextmanager
def managed_session():
    session = open_session()
    try:
        yield session
    except BaseException as error:
        log(error)
        raise
    finally:
        session.close()
```

块内异常会回到 `yield` 处。如果 `except` 只记录日志却没有重新 `raise`，生成器会正常结束，`contextmanager` 就会认为异常已经被处理。只需要清理时，省略 `except`、只保留 `try/finally` 最稳妥；确实需要记录时，要用裸 `raise` 保留原异常和 traceback。

获取动作如果在到达 `try` 和 `yield` 之前失败，仍然要自己回滚部分初始化。这一点与类管理器的 `__enter__()` 失败完全相同。

## 18.8 多个资源为什么按相反顺序退出

下面两种写法语义相同：

```python
with Resource("A") as a, Resource("B") as b, Resource("C") as c:
    use(a, b, c)

with Resource("A") as a:
    with Resource("B") as b:
        with Resource("C") as c:
            use(a, b, c)
```

进入顺序是 `A → B → C`，退出顺序是 `C → B → A`。后取得的资源可能依赖先取得的资源，例如游标依赖连接、事务依赖会话。后进先出可以先拆掉依赖者，再拆基础设施。

如果 B 进入失败，执行结果是：

- B 没有成功进入，所以不会调用 B 的 `__exit__()`；
- B 内部的部分初始化由 B 自己回滚；
- 已成功进入的 A 会退出；
- C 从未开始进入。

这与 Java `try-with-resources` 的多资源展开方式很接近。固定少量资源时，多项 `with` 最容易读。当资源数量只有运行时才知道时，则需要 `ExitStack`。

## 18.9 `ExitStack`：动态建立退出栈

假设文件列表来自配置，源码无法预先写出 N 层 `with`：

```python
from contextlib import ExitStack

with ExitStack() as stack:
    files = [stack.enter_context(open(path, encoding="utf-8")) for path in runtime_paths]
    process(files)
```

`enter_context(manager)` 会立即调用该管理器的 `__enter__()`。只有进入成功，对应的退出方法才会压入栈中；它的返回值则交给调用方使用。离开外层 `with` 时，已登记的方法按后进先出顺序调用。

如果第三个资源进入失败，时间线如下：

```text
资源 1 进入成功，退出方法入栈
资源 2 进入成功，退出方法入栈
资源 3 进入失败
    ├─ 资源 3 自己回滚部分初始化
    ├─ ExitStack 退出资源 2
    └─ ExitStack 退出资源 1
```

`ExitStack` 还可以登记普通回调，因此适合把不同清理形式组合在一个动态边界里。它是偏底层的工具，不应取代本来就很清楚的单个 `with`。另外，垃圾回收 `ExitStack` 对象时不会隐式执行已登记的回调；应把它放进 `with`，或由明确所有者调用 `close()`。

`pop_all()` 可以把整组退出责任转移到一个新的 `ExitStack`，但不会当场清理。只有当 API 确实需要跨作用域转移所有权时才使用它，并把新所有者写进接口契约。

## 18.10 所有权：谁创建，谁负责关闭

“对象有 `close()` 方法”不等于“当前函数有权调用它”。处理资源前，先回答三个问题：谁创建、谁使用、谁关闭。

```text
请求处理函数（所有者）
├─ 创建会话并进入 with
├─ 把句柄借给 search_tool（只使用）
├─ 把同一句柄借给 summary_tool（只使用）
└─ 离开 with，统一关闭一次
```

| 角色 | 资源从哪里来 | 可以做什么 | 关闭责任 |
|---|---|---|---|
| 所有者 | 自己创建，或明确接收所有权 | 使用、转借、决定生命周期 | 必须在所有路径清理 |
| 借用者 | 通过参数临时获得 | 在约定范围内使用 | 默认不能关闭 |
| 新所有者 | API 明确转移了所有权 | 接管后续生命周期 | 从转移点开始负责清理 |

下面的借用者看似负责，实际会破坏共享会话：

```python
def run_tool(client):
    try:
        return client.call()
    finally:
        client.close()  # 第一个工具结束后，其他借用者无法继续使用。
```

正确做法是让真正创建客户端的外层边界统一关闭。Python 的 `with` 不创建块级变量作用域，因此 `as handle` 的名字离开块后通常还存在；但它指向的底层资源已经关闭，继续调用属于生命周期错误。

当函数既允许“自己创建客户端”，又允许“调用方传入共享客户端”时，两条所有权路径必须分开：

```python
from contextlib import nullcontext

def execute(client=None):
    scope = Client() if client is None else nullcontext(client)
    with scope as active_client:
        return active_client.call()
```

可以先把 `nullcontext(client)` 理解为“为了统一控制流而创建的空作用域”：进入时返回原对象，退出时什么也不做。它让借用对象可以复用同一段 `with` 代码，但不会擅自接管关闭责任。

## 18.11 生成器提前停止时，`break` 不等于关闭

资源型生成器可能在第一次消费时打开会话，然后暂停在 `yield`：

```python
def stream_results(manager, commands):
    with manager as session:
        for command in commands:
            yield session.call(command)
```

如果消费者自然耗尽生成器，控制流最终会离开内部 `with`。但消费者可能只取第一项：

```python
stream = stream_results(manager, commands)
for result in stream:
    consume(result)
    break
```

此时 `stream` 仍引用暂停在 `yield` 处的生成器，内部资源可能仍然打开。

`for` 协议负责调用 `iter()`、`next()` 并处理 `StopIteration`。`break` 只停止循环，不会承诺对任意迭代器调用 `close()`。不要把 CPython 在某些情况下很快回收临时生成器的现象当成资源管理契约；只要仍有引用，生成器就可能继续暂停。

对生成器调用 `close()`，会在它暂停的 `yield` 位置引发 `GeneratorExit`，使外围的 `finally` 或 `with` 有机会执行清理。也可以用 `closing()` 把所有权边界写进结构：

```python
from contextlib import closing

with closing(stream_results(manager, commands)) as stream:
    for result in stream:
        consume(result)
        break
# closing 在这里调用 stream.close()。
```

`closing()` 适用于调用方确实拥有、提供 `close()` 但没有上下文协议的对象。如果对象本身已经是上下文管理器，应优先使用它的原生协议；也不要假设每一种迭代器都有 `close()`。

## 18.12 常见迁移陷阱与选择标准

Java 工程师最容易误判的地方集中在以下几类：

1. 把 `with` 当作文件关键字。它使用通用协议，锁、事务、会话都可以参与。
2. 认为 `as` 一定绑定管理器。它绑定的是 `__enter__()` 返回值。
3. 只在正常路径写 `close()`。`return` 和异常都可能绕过这句代码。
4. 指望进入失败后自动退出。`__enter__()` 没有成功返回时，不会调用本次 `__exit__()`。
5. 从 `__exit__()` 返回清理结果。任何真值都会抑制块内异常。
6. 在 `@contextmanager` 中记录异常后漏写 `raise`。这会把失败伪装成成功。
7. 按获取顺序关闭多个依赖资源。正确顺序通常是后进先出。
8. 借到共享客户端后主动关闭。能使用不代表拥有关闭权。
9. 认为 `break` 会关闭任意生成器。它只结束循环。
10. 依赖析构或垃圾回收释放外部资源。回收时机不是稳定的业务边界。

面对实际需求时，可以按下表选择：

| 需求 | 优先写法 | 原因 |
|---|---|---|
| 一处局部获取与清理 | `try/finally` | 控制流直接可见 |
| 对象有状态、配置和多个方法 | 类实现 `__enter__` / `__exit__` | 状态与协议放在一起 |
| 生命周期是线性过程 | `@contextmanager` + 一次 `yield` | 获取与清理相邻，样板少 |
| 固定少量资源 | 多项 `with` | 进入和退出顺序清楚 |
| 运行时才知道资源数量 | `ExitStack` | 动态登记并逆序回收 |
| 只有 `close()`，没有上下文协议 | `closing()` | 将拥有的对象适配给 `with` |
| 传入共享资源 | 普通借用或 `nullcontext()` | 使用但不接管关闭责任 |

## 18.13 运行本章示例

本章承接第 10 章的文件 `with`、第 16 章的装饰器和第 17 章的生成器。目录中只有两个文件：

| 文件 | 作用 |
|---|---|
| `README.md` | 建立同步上下文管理器的完整心智模型 |
| `example.py` | 用可观察事件和断言验证各种生命周期路径 |

`example.py` 使用本地 `FakeToolSession` 模拟“分配传输层 → 握手 → 使用 → 释放”，不访问网络，也不需要 API Key。它会验证：

- 正常退出和块内 `return` 都先清理；
- 块内异常会传给 `__exit__()`，默认继续向外传播；
- 进入失败不会调用 `__exit__()`，而由获取逻辑自行回滚；
- 类管理器和生成器管理器误吞异常的两种反例；
- 多项 `with` 与 `ExitStack` 的逆序退出；
- 所有者统一关闭，借用者只使用；
- 生成器在 `break` 后通过 `close()` 或 `closing()` 显式清理。

在本次独立还原目录中运行：

```bash
python -B example.py
```

从仓库根目录运行：

```bash
.venv/bin/python -B java_to_python/chapters/18_context_managers/example.py
```

`-B` 只阻止生成 `.pyc` 缓存，不改变示例语义。成功时，最后会看到：

```text
所有第 18 章示例与运行时断言均已完成。
```

现有 `java_to_python/run_all.py` 只支持前 10 章，因此应直接运行本章脚本。项目只使用标准库，没有新增运行依赖。仓库根目录的 `pyproject.toml` 要求 Python `>=3.14`。当前项目环境对应的检查命令是：

```bash
.venv/bin/ruff check java_to_python/chapters/18_context_managers
.venv/bin/ruff format --check java_to_python/chapters/18_context_managers
.venv/bin/pyright java_to_python/chapters/18_context_managers/example.py
```

本章验证的是同步协议、本地状态机和确定的清理顺序，不覆盖真实 HTTP/MCP/数据库客户端的握手、连接池、超时、线程安全和服务端状态。替换成第三方库后，应根据锁定版本的官方文档和集成测试重新确认关闭、重复关闭与关闭后使用的行为。

进一步核对语言和标准库语义，可以阅读 Python 3.14 官方文档：

- `with` 语句
- `contextlib.contextmanager`
- `contextlib.ExitStack`
- `generator.close()`

## 18.14 递进练习

### 练习 1：预测协议轨迹

实现一个最小 `TraceContext`，分别从块末尾、块内 `return` 和块内 `ValueError("bad input")` 离开。要求先写出预测事件，再运行核对：正常和 `return` 路径收到三个 `None`，异常路径的清理事件先于外层捕获事件。

### 练习 2：修复部分初始化泄漏

模拟两阶段 `open_client()`：第一阶段把 `transport_open` 设为 `True`，第二阶段遇到 `"bad-token"` 时抛 `AuthenticationError`。要求失败后不进入块体、不调用 `__exit__()`，但 `transport_open` 已恢复为 `False`，原异常仍向外传播。

### 练习 3：修复两种异常误吞

分别修复一个无条件 `return True` 的类管理器，以及一个在 `@contextmanager` 的 `except` 中只记录却不重抛的实现。让块内固定抛 `ToolCallError("quota exceeded")`，确认两种实现都清理一次，并让外层收到原错误及原 traceback。

### 练习 4：动态会话与中途失败

使用 `ExitStack` 依次进入 `"one"`、`"two"`、`"broken"`、`"never"`。让 `"broken"` 在进入时失败并自行回滚。最终应观察到：`"never"` 从未尝试，`"two"` 和 `"one"` 由栈逆序关闭，而 `"broken"` 的 `__exit__()` 没有执行。

## 18.15 本章小结

可以用五句话记住本章：

1. `with` 管理的是控制流边界，不只是文件。
2. `as` 接收 `__enter__()` 的返回值；进入成功后，所有离开路径都会调用 `__exit__()`。
3. 进入失败时，部分初始化必须由获取逻辑自己回滚；退出时默认不要抑制业务异常。
4. 固定资源用 `with`，动态资源用 `ExitStack`，线性生命周期可以用 `@contextmanager`。
5. 资源清理的根本问题是所有权：创建者负责关闭，借用者只在有效期内使用。

如果你能不看代码说明“第三个资源进入失败时谁清理谁”，并能解释为什么 `break` 后暂停的资源型生成器仍可能持有资源，就已经建立了同步资源生命周期的完整心智模型。
