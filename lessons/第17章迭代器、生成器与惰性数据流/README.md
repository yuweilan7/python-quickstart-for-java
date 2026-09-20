# 第 17 章：迭代器、生成器与惰性数据流

一句话先说结论：**Python 的迭代器是一条保存了当前位置的数据通道，生成器则是编写这种通道最省力的方式；数据通常不是创建通道时全部产生，而是消费者每调用一次 `next()` 才向上游要一项。**

这个机制很适合处理逐步到达或数量很大的数据，例如：

- 逐行读取日志和 JSONL 文件；
- 按页拉取数据库或 HTTP 查询结果；
- 一边过滤、转换，一边只取前 N 条；
- 接收模型输出片段，为后续异步流打基础。

学完本章，你应该能够解释：

- iterable（可迭代对象）与 iterator（迭代器）有什么区别；
- `for` 如何使用 `iter()`、`next()` 和 `StopIteration`；
- 为什么 `list` 通常可以重复遍历，同一个生成器却通常只能消费一次；
- `yield` 在哪里暂停，下一次消费又从哪里继续；
- 列表推导式和生成器表达式分别在什么时候计算；
- 惰性流水线何时真的节约内存，何时只是把计算推迟；
- 中途异常或提前停止后，部分结果和打开的资源该怎样处理。

## 17.1 先建立 Java → Python 心智模型

Python 和 Java 都有迭代协议，但不要只按语法做一一替换：

| Java 中熟悉的概念 | Python 中可以先类比为 | 相似点 | 不能等同的地方 |
| --- | --- | --- | --- |
| `Iterable<T>` | `Iterable[T]` / iterable | 都表示“可以取得遍历能力” | Python 主要靠 `iter(obj)` 协议，不要求显式实现某个接口 |
| `Iterator<T>` | `Iterator[T]` / iterator | 都保存当前位置，按次返回元素 | Python 没有 `hasNext()`；耗尽由 `StopIteration` 表示 |
| `Stream<T>` 惰性中间操作 | 生成器表达式、生成器函数组成的流水线 | 终端消费会逐项拉动上游 | Python 生成器本身就是一次性 iterator，没有 Java Stream 那套完整操作和并行模型 |
| 手写状态机式 `Iterator` | 含 `yield` 的生成器函数 | 都能保存遍历状态 | `yield` 由解释器保存局部变量和执行位置，不必手写状态字段与 `next()` 分支 |
| `stream.toList()` | `list(iterator)` | 都会把剩余结果物化到内存 | Python 的同一个 iterator 会因此耗尽，之后不能自动重放 |
| try-with-resources | `with`，以及必要时显式 `close()` | 都解决确定性清理 | 生成器暂停在 `with` 内部时，资源也可能保持打开，直到耗尽或关闭 |

同步生成器不能类比成 `CompletableFuture`、Reactor `Flux` 或 Reactive Streams `Publisher`。它在调用 `next()` 的线程里同步执行：如果内部进行了阻塞 HTTP 请求，当前线程照样阻塞。惰性只回答“何时计算”，不自动提供异步、并行或响应式背压。

## 17.2 iterable 提供入口，iterator 保存位置

先把两个术语分开：

- **iterable（可迭代对象）**：`iter(obj)` 能取得一个 iterator；
- **iterator（迭代器）**：`next(iterator)` 能取得下一项，并在对象内部保存当前位置。

`list` 是典型的 iterable 容器。数据保存在 `list` 中，每次 `iter(names)` 通常都会创建一个新的 iterator：

```python
names = ["Ada", "Lin"]

first = iter(names)
second = iter(names)

print(next(first))   # Ada
print(next(second))  # Ada：second 有自己的位置
```

iterator 自己也是 iterable，而且 `iter(iterator)` 通常返回它自己。因此两个变量如果引用同一个 iterator，它们共享的不是数据副本，而是同一个位置：

```python
iterator = iter([10, 20, 30])
alias = iterator

print(next(alias))      # 10
print(list(iterator))   # [20, 30]
print(list(alias))      # []：同一个对象已经耗尽
```

所以，**“能用于 `for`”不等于“能重复遍历”**。`list`、`tuple` 等容器通常可以反复创建新 iterator；文件对象、生成器对象和大多数 iterator 则代表一条不断向前的消费路径。

### `for` 循环实际依赖什么

下面的 `for`：

```python
for item in values:
    use(item)
```

核心语义可以近似展开为：

```python
iterator = iter(values)

while True:
    try:
        item = next(iterator)
    except StopIteration:
        break
    use(item)
```

运行过程是：

1. `iter(values)` 请求一个 iterator；
2. 每轮通过 `next(iterator)` 请求一项；
3. 有值时执行循环体；
4. iterator 抛出 `StopIteration` 时，`for` 把它当作正常结束；
5. `ValueError`、`OSError` 等其他异常不会被当成“遍历结束”，而是继续向外传播。

一个正确的 iterator 一旦耗尽，后续调用 `next()` 应继续抛 `StopIteration`，不会自动回到开头。手动探测时也可以提供默认值：

```python
missing = object()
item = next(iterator, missing)

if item is missing:
    print("已经耗尽")
```

这里使用唯一哨兵对象，而不是 `None`，因为 `None` 也可能是合法数据。

### 需要第二次遍历时怎么办

根据数据来源选择：

1. 原始对象是 `list`、`tuple` 等容器：重新调用 `iter(container)`；
2. 数据量小，而且确实需要随机访问或重复使用：在明确的位置物化为 `list`；
3. 数据能重新读取或查询：保存一个“创建新 iterator”的函数，每次重新调用；
4. 数据源本身不可重放：把一次性语义写进 API 和业务流程，不要假装能够重来。

不要把一个全局生成器对象反复返回。返回的仍是同一个已经前进、甚至已经耗尽的对象。

## 17.3 生成器：用 `yield` 写一个可暂停的函数

函数体中出现 `yield` 后，它就成为生成器函数。调用生成器函数只创建一个生成器对象，不会立刻从头执行整个函数体：

```python
from collections.abc import Iterator


def count_up_to(limit: int) -> Iterator[int]:
    current = 1
    print("函数体开始")
    while current <= limit:
        print("生产", current)
        yield current
        current += 1


numbers = count_up_to(2)  # 没有打印：只创建对象
print(next(numbers))      # 打印“函数体开始”“生产 1”，然后得到 1
print(next(numbers))      # 从 yield 后恢复，current 变成 2，然后得到 2
```

每次执行 `yield value`，生成器会：

1. 把 `value` 返回给当前这次 `next()`；
2. 暂停在 `yield` 所在位置；
3. 保存局部变量、执行位置和当前异常处理状态；
4. 等下一次 `next()`，再从 `yield` 后面继续。

可以先把它理解成“解释器替你生成了一个状态机”，但不能理解成后台线程。生成器暂停期间没有在后台继续工作，也不会预先生产下一项。

### `yield`、`return` 和 `yield from`

| 写法 | 对当前生成器的作用 | 消费者看到什么 |
| --- | --- | --- |
| `yield item` | 产出一项并暂停 | 当前 `next()` 返回 `item` |
| `return` 或运行到函数末尾 | 结束生成器 | 下一次请求收到 `StopIteration` |
| `return value` | 结束并携带一个完成值 | 普通 `for` 看不到；手动捕获的 `StopIteration.value` 可以看到 |
| `yield from source` | 逐项委托给另一个 iterable | 像连续 `yield` 该来源的每一项 |

组合多个来源时，`yield from` 能避免手写重复的转发循环：

```python
def combined() -> Iterator[str]:
    yield "header"
    yield from ("local-A", "local-B")
    yield from ("cache-C",)


print(list(combined()))
# ['header', 'local-A', 'local-B', 'cache-C']
```

`yield from` 不会自动把子来源先转成 `list`。它还支持更完整的生成器委托协议，但普通单向数据流先掌握“逐项转交”即可。

生成器内部也不要用 `raise StopIteration` 表示正常结束。应写 `return` 或让函数自然结束；从生成器函数内部意外逸出的 `StopIteration` 会被转换成 `RuntimeError`，避免内部错误被外层 `for` 误判成正常耗尽。

### `list(iterator)` 是一次完整消费

`list(iterator)` 会反复调用 `next()`，直到 iterator 耗尽，并把所有结果保存下来。调用结束后：

- 剩余元素已经全部生产；
- 中途副作用已经发生；
- iterator 已经耗尽；
- 返回的 `list` 占用保存全部结果引用所需的内存。

因此，`list(...)` 不是更换语法外壳，而是一个明确的物化边界。测试小数据、获取长度、使用下标或重复遍历时，它很合适；面对无界流或只需要前几项的数据时，则要谨慎。

## 17.4 列表推导式与生成器表达式：只差括号，执行时机不同

```python
squares_list = [value * value for value in values]
squares_iterator = (value * value for value in values)
```

| 问题 | 列表推导式 `[...]` | 生成器表达式 `(...)` |
| --- | --- | --- |
| 创建时 | 遍历输入、计算全部结果并创建 `list` | 创建生成器 iterator |
| 元素表达式与过滤条件 | 创建 `list` 时执行 | 消费相应候选项时执行 |
| 结果内存 | 保存全部结果引用 | 通常只保存暂停状态和当前项 |
| 同一个结果能否重复遍历 | 可以 | 通常不可以，耗尽即结束 |
| 适合 | 小而有界，需要完整结果 | 单次流水线，可能很大、可能提前停止 |

生成器表达式有一个容易漏掉的精确规则：**最左侧 `for` 的 iterable 表达式会立即求值，并立即对结果调用 `iter()`；元素表达式、过滤条件和后续 `for` 才惰性执行。**

```python
def prepare() -> list[int]:
    print("准备输入")
    return [1, 2, 3]


squares = (value * value for value in prepare())
# 此时已经打印“准备输入”，但平方还没有计算

print(next(squares))  # 1：现在只计算第一项
```

如果最左侧调用本身是生成器函数，调用它仍然只返回尚未启动的生成器对象，因此其函数体要等消费时才执行。

### 惰性何时才真的降低峰值内存

一条流水线通常要同时满足以下条件，才能获得流式处理的内存优势：

- 上游可以逐项提供数据，没有先创建巨大的 `list`；
- 过滤、转换等中间阶段没有偷偷 `list(source)`、全量排序或全量分组；
- 下游逐项处理或提前停止，没有再次收集所有结果；
- 处理完的元素没有被缓存、闭包或日志集合长期引用；
- 单个元素本身不是一个巨型对象。

下面三种写法都可能打破全程惰性：

```python
source = load_everything()  # 上游已经全部放进 list
stream = (transform(x) for x in source)

cached = list(stream)        # 中间或末尾重新收集全部结果
sorted_items = sorted(stream)  # 排序通常也必须先看完全部输入
```

这些操作不一定错误。业务需要排序、总数、随机访问或重复遍历时，就应该物化；关键是把这个边界写清楚，不要把“外面套了生成器”描述成“全程流式”。

## 17.5 拉式流水线：消费者反向驱动上游

本章项目把 `documents.jsonl` 处理成搜索结果：

```text
数据方向：JSONL 文件 -> 校验 -> 过滤 -> 转换 -> 最多 N 条 -> 消费者
调用方向：JSONL 文件 <- next <- next <- next <- next <- 消费者
```

数据从源头流向消费者，但控制请求从消费者反向传递。消费者要一项，限量器向转换器要一项，转换器向过滤器要一项，过滤器再向文件源要一行。如果某行没有通过过滤，过滤器会继续拉取，直到找到下一条合格记录或上游耗尽。

这种组合方式的价值不只是少占内存：消费者如果拿够两项就停止，上游后续的文件读取、JSON 解析、转换和副作用都不会发生。

### 限制 N 条时，不要多取第 N+1 条

下面的写法会先从 `source` 取得一项，再执行循环体中的判断：

```python
# 反例：index == limit 时，item 已经从上游取出来了。
for index, item in enumerate(source):
    if index >= limit:
        break
    yield item
```

如果“取得一项”意味着发起 HTTP 请求、移动数据库游标或可能触发异常，这次多拉取就有实际成本。可以精确控制 `next()` 次数：

```python
from collections.abc import Iterable, Iterator


def take(items: Iterable[str], limit: int) -> Iterator[str]:
    iterator = iter(items)
    for _ in range(limit):
        try:
            yield next(iterator)
        except StopIteration:
            return
```

`limit == 0` 时，上游一次也不会被拉取。工程代码还应用明确负数是报错还是按零处理。标准库的 `itertools.islice(items, limit)` 也适合完成这种截取。

### 外部数据仍要在运行时校验

本章给流水线元素标注了 `Document` 和 `SearchHit`。这些注解帮助 IDE、类型检查器和读者理解每层输入输出，但不会自动验证 `json.loads()` 得到的真实对象。

因此文件源仍要检查：顶层是不是字典、必需字段是否存在、字段值类型是否正确。通过边界校验后，后续生成器才可以诚实地按 `Iterator[Document]` 处理。类型注解不能替代 JSON、HTTP、数据库等外部边界的运行时校验。

## 17.6 异常发生在消费时，之前的结果不会自动回滚

对生成器函数来说，创建和执行是两个时间点：

```python
stream = load_jsonl(path, events)  # 只创建生成器，通常还没有打开文件
first = next(stream)               # 现在才可能发生文件、解码和字段校验错误
```

假设流水线的事件顺序是：

```text
产出 doc-001 -> 产出 doc-003 -> 转换 doc-error 时抛异常
```

前两项已经真实交给消费者。对应的日志、写入、计费等副作用也可能已经发生。生成器不会提供事务语义，无法因为第三项失败而自动撤销前两项。

需要保留部分结果时，应在消费边界逐项保存，并捕获具体异常：

```python
partial = []

try:
    for item in pipeline:
        partial.append(item)
except DocumentTransformError as error:
    report(error, partial)
```

如果直接写 `results = list(pipeline)`，异常会阻止赋值完成。`list()` 内部已经收集的临时结果不会通过 `results` 交付给调用方。是否保留部分结果、是否回滚副作用、从哪里重试，都必须由业务层明确决定。

异常从生成器内部传播出去后，这个生成器通常已经终止。不要尝试从同一对象的失败位置继续；需要重试时，应根据数据源是否可重放、操作是否幂等，创建一条新的迭代路径。

## 17.7 提前停止时，谁负责关闭资源

生成器可以暂停在 `with` 块内部：

```python
from collections.abc import Generator
from pathlib import Path


def load_lines(path: Path) -> Generator[str]:
    with path.open(encoding="utf-8") as file:
        for line in file:
            yield line.rstrip("\n")
```

第一次 `next()` 会打开文件并停在 `yield`。如果消费者完整耗尽生成器，控制流会自然离开 `with` 并关闭文件；但消费者也可能 `break`、只取前 N 条，或在下游转换时抛异常。此时文件源可能仍暂停着。

本章示例采用明确的所有权规则：**创建并持有文件源的代码负责最终关闭它。**

```python
source = load_jsonl(DATA_PATH, events)
pipeline = build_pipeline(source, limit=2, events=events)

try:
    results = consume_hits(pipeline, events)
finally:
    source.close()
```

`close()` 会在生成器暂停的 `yield` 位置触发 `GeneratorExit`，从而让内部的 `finally` 和已经进入的 `with` 有机会执行清理。已经结束或尚未启动的生成器也可以安全关闭。

不要把“当前使用 CPython”当成资源管理策略，也不要依赖对象何时被垃圾回收。还要注意：关闭某个外层生成器，并不普遍保证它会关闭任意上游 iterable。本章保留真正文件源的引用，并直接关闭资源所有者。更系统的资源生命周期写法会在第 18 章继续展开。

## 17.8 类型注解：什么时候写 `Iterable`、`Iterator` 或 `Generator`

最常见的生成器签名如下：

```python
from collections.abc import Iterable, Iterator


def normalize(lines: Iterable[str]) -> Iterator[str]:
    for line in lines:
        text = line.strip()
        if text:
            yield text
```

- 参数写 `Iterable[str]`：函数只要求能够取得字符串，不强迫调用方传 `list`；
- 返回写 `Iterator[str]`：调用方拿到一条保存位置、通常一次性消费的字符串流；
- `Iterator[Document]` 中的 `Document` 表示每次产出的元素类型；
- 注解服务于阅读和静态检查，不会改变运行时求值方式，也不会验证每个元素。

当公共 API 需要明确暴露 `send()`、`throw()`、`close()` 或生成器完成值时，可以写得更具体：

```python
Generator[YieldType, SendType, ReturnType]
```

本项目使用 Python 3.14，其中后两个类型参数默认是 `None`，所以 `Generator[Document]` 等价于 `Generator[Document, None, None]`。

### 同步生成器不是 asyncio coroutine

| 对象 | 常见创建方式 | 谁驱动它 | 怎样取结果 | 阻塞 I/O 会怎样 |
| --- | --- | --- | --- | --- |
| 同步生成器 | 含 `yield` 的 `def` | 当前消费者 | `next()` / `for` | 阻塞当前线程 |
| coroutine | 调用 `async def` | `await` 与事件循环 | `await` | 只有正确使用异步 I/O 才能让出执行权 |
| 异步生成器 | 含 `yield` 的 `async def` | 异步消费者与事件循环 | `async for` | 取决于内部是否等待异步 I/O |

三者都可能暂停，但协议、调度方式和使用场景不同。本章只讨论同步生成器；coroutine 在第 19 章展开，异步流在第 24 章展开。

## 17.9 Java 工程师最容易踩的坑

1. **把 iterable 当成可重放集合。** iterator 自己也是 iterable；先确认拿到的是容器、工厂，还是一次性数据流。
2. **为了调试执行 `print(list(stream))`，随后再交给业务代码。** 调试语句已经耗尽原对象。
3. **生成器内部先写 `cached = list(source)`。** 这只是把全量读取推迟到第一次消费。
4. **认为生成器表达式创建时绝无副作用。** 最左侧 iterable 表达式及其 `iter()` 会立即执行。
5. **用 `enumerate` 取到元素后才检查上限。** 判断时第 N+1 项已经被拉取。
6. **认为惰性等于异步。** 同步生成器里的阻塞调用仍阻塞当前线程。
7. **认为异常会撤回已经 `yield` 的结果。** 生成器不是事务，部分结果和副作用已经发生。
8. **只测试完整消费。** 真实消费者会提前停止，也会在某一项中途失败，清理路径必须覆盖这些情况。

选择数据结构时，可以使用下面的判断：

| 需求 | 优先选择 | 原因 |
| --- | --- | --- |
| 数据小，需要长度、下标、排序或重复遍历 | `list` / 列表推导式 | 结果完整，语义直接 |
| 单次、短小的按需计算链 | 生成器表达式 | 紧凑，逐项计算 |
| 有分支、日志、异常或清理 | 生成器函数 | `yield` 周围能清楚表达控制流 |
| 组合多个 iterable | `yield from` | 直接逐项委托，不重复写转发循环 |
| 公共函数只需要读取输入 | 接收 `Iterable[T]` | `list`、`tuple` 和一次性流都能传入 |
| 返回带当前位置的一次性流 | 返回 `Iterator[T]` | 把消费语义写进接口 |
| 后续确实需要完整结果 | 在明确边界 `list(...)` | 内存成本和所有权可见 |

## 17.10 运行本章项目

本章只使用标准库，不需要安装额外依赖。根 `pyproject.toml` 要求 Python >=3.14。

目录中的文件职责如下：

| 文件 | 职责 |
| --- | --- |
| `README.md` | 建立迭代协议、生成器和惰性数据流的完整心智模型 |
| `example.py` | 打印六组生产/消费事件，并用断言验证关键边界 |
| `documents.jsonl` | 本地、固定、可逐行读取的数据源 |

从仓库根目录运行：

```bash
.venv/bin/python -B java_to_python/chapters/17_iterators_generators/example.py
```

`-B` 只阻止生成 `.pyc`，不改变运行语义。`java_to_python/run_all.py` 目前只包含前 10 章，因此不要用它运行本章。

阅读输出时，不必背所有日志，重点验证以下因果关系：

1. 调用生成器函数后事件数仍为 0，第一次 `next()` 才开始生产；
2. 第二次 `next()` 从上一个 `yield` 后恢复，局部变量没有重置；
3. 列表推导式立即计算全部元素，生成器表达式按消费次数计算；
4. 限制两条合格结果时只读取 JSONL 前三行，第四行 `doc-error` 没有被提前拉取；
5. 放宽上限后，转换到 `doc-error` 才失败，之前两条结果已经交付；
6. 提前停止后文件尚未自动关闭，显式 `close()` 后关闭事件出现。

可选的静态检查与格式检查：

```bash
.venv/bin/ruff check java_to_python/chapters/17_iterators_generators
.venv/bin/ruff format --check java_to_python/chapters/17_iterators_generators
.venv/bin/pyright java_to_python/chapters/17_iterators_generators/example.py
```

本地实验能验证同步拉取顺序、逐行读取、过滤转换、限量、部分失败和文件关闭。它没有模拟真实 HTTP 分块、数据库游标、LLM token 边界、网络背压、超时重试或 asyncio 调度；换成真实 I/O 客户端后，这些行为仍需集成测试。

## 17.11 递进练习

### 练习 1：观察共享迭代位置

要求：为 `[10, 20, 30]` 创建一个 iterator 和两个别名，交替调用 `next()`，最后用手写 `while` / `StopIteration` 消费剩余项。

验收标准：

- 运行前能写出每次 `next()` 的结果；
- 能证明两个别名共享位置，而新调用 `iter(list)` 会从头开始；
- 耗尽后分别使用异常和默认值两种方式探测。

### 练习 2：区分创建时和消费时

要求：编写一个会记录事件的输入构造函数和一个 `trace_double(value)`，分别放入列表推导式和生成器表达式。输入固定为 `[1, 2, 3, 4]`，生成器表达式只消费两项。

验收标准：

- 列表推导式创建时计算四项；
- 生成器表达式只计算两项；
- 能解释为什么生成器表达式创建时仍出现了最左侧输入构造事件。

### 练习 3：实现不多拉一项的 `take`

要求：实现 `take(source, limit)`，并用可计数的数据源分别测试 `limit=0`、`2`、`10`。数据源最多产出五项，不允许内部先 `list(source)`。

验收标准：

- `limit=0` 时上游生产 0 次；
- `limit=2` 时上游恰好生产 2 次；
- `limit=10` 时得到全部 5 项并自然耗尽；
- 明确负数参数在创建时还是首次消费时失败，并解释原因。

### 练习 4：处理 JSONL 的部分失败

要求：复制一份 JSONL 到临时目录，先放两行合法记录，再放一行非法 JSON，最后再放一行合法记录。逐项收集成功结果，捕获具体错误，并确保所有路径都关闭文件。

验收标准：

- 错误只在消费到非法行时发生；
- 能看到错误前已经交付的两项；
- 不声称错误后的记录已经处理；
- 正常耗尽、中途异常、提前停止都能观察到确定的关闭证据。

### 练习 5：模拟模型文本片段

要求：用同步生成器依次产出 `"你"`、`"好"`、`"!"`，消费者在两项后停止，并分别记录生产和消费事件。

验收标准：

- 创建生成器时没有生产事件；
- 只消费两项就不会生产第三项；
- 明确说明该替身只验证同步迭代和提前停止，不验证真实模型、网络传输、token 切分或异步行为。

## 17.12 本章小结

- iterable 负责提供 iterator，iterator 负责保存当前位置并逐项返回数据；
- `for` 会反复调用 `next()`，并把 `StopIteration` 当作正常结束；
- 生成器函数调用时只创建对象，`yield` 在消费时产出一项、暂停并保存状态；
- 同一个 iterator 通常只能消费一次，需要重放时必须创建新 iterator 或明确物化；
- 生成器表达式大部分逻辑惰性执行，但最左侧 iterable 表达式会立即求值；
- 真正的低峰值内存依赖上游、中间阶段和消费者共同保持逐项处理；
- 消费阶段可能已经产生部分结果，异常不会自动回滚；
- 提前停止时资源可能仍处于打开状态，必须建立明确的关闭责任；
- 同步生成器只是同步拉式迭代，不会自动变成 asyncio、并行计算或响应式流。
