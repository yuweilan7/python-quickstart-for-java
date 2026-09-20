# 第 15 章：Protocol、ABC 与实用泛型

## 先记住一句话

`Protocol` 说明“对象要具备什么能力”，ABC 说明“子类必须继承什么骨架”，泛型说明“几个类型位置之间必须保持什么关系”。

这三类工具经常一起出现在 Agent 框架中。模型客户端、搜索后端和工具实现需要被替换；框架内部可能需要统一生命周期；一个工具的输入类型又必须与成功输出类型一路传到注册表调用端。

本章围绕一个同步搜索工具讲清这些问题。学完后，你应该能：

- 判断一个需求该用普通函数、`Protocol` 还是 ABC；
- 解释结构化子类型与 Java `implements` 的区别；
- 说明 `@runtime_checkable` 为什么不是运行时类型校验；
- 从 `identity[T]`、`Result[T]` 读出类型参数连接了哪些位置；
- 逐项读懂 `Tool[SearchRequest, list[SearchHit]]`；
- 区分 union、constraint（约束）与 bound（上界）；
- 为只读参数选择 `Mapping` / `Sequence`，并解释 `list` 的不变性。

本章只讨论同步、本地且可控的例子，不涉及大型依赖注入容器、元类或并发工具调度。

## 15.1 Java → Python 心智模型

| Java 中熟悉的概念 | Python 中可以先想到 | 最重要的不等价之处 |
| --- | --- | --- |
| 接口与 `implements` | `Protocol` | 实现类通常不用声明继承；检查器按成员结构判断兼容性 |
| 抽象类与抽象方法 | `ABC`、`@abstractmethod` | 没有编译阶段强制；不完整子类在实例化时被拒绝 |
| 只靠约定调用某个方法 | duck typing | Python 直接查找并调用成员，不先确认接口身份 |
| `instanceof SomeInterface` | `isinstance(x, RuntimeProtocol)` | 运行时协议只做浅层成员检查，不校对完整签名 |
| `<T> T identity(T value)` | `def identity[T](value: T) -> T` | 注解主要服务静态分析，不插入运行时检查或转换 |
| `Function<A, B>` 等函数式接口 | `Callable[[A], B]` | 兼容的函数或可调用对象自动满足，不需要声明实现 |
| 带复杂方法签名的函数式接口 | 定义 `__call__` 的 `Protocol` | 可以表达关键字参数名等规则，仍是结构化匹配 |
| `List<T>` / `Map<K, V>`、只读视图 | `Sequence[T]` / `Mapping[K, V]` | 限制接口可见能力，不保证底层对象真的不可变 |
| 泛型 sealed result | 成功为 `Success[T]`，失败为 `Failure` | union 类型别名不是运行时密封层次，注解也不会验值 |
| Bean Validation / 反序列化校验 | 显式校验、Pydantic 等 | `Protocol`、ABC 和泛型都不能替代外部数据校验 |

可以把 Pyright 类比成项目额外执行的一道编译期检查，但不能完全等同于 `javac`：即使类型检查没有通过，Python 解释器仍然可以直接运行源码。类型提示不会自动改变运行时对象。

## 15.2 先看完整数据流

```text
MemorySearchBackend  ┐
FixtureSearchBackend ├─→ 满足 SearchBackend → 被 SearchTool 依赖 ─┐
OfflineSearchBackend ┘                                            ├─→ ToolRegistry
empty_search_tool ─────────────→ 直接满足 Tool 协议 ──────┘

SearchRequest → registry.invoke(...) → 选中的工具 → Result[list[SearchHit]]
```

这里有三层不同的约束：

1. `SearchBackend` 只描述所有搜索后端都要提供的能力；
2. `Tool[InputT, OutputT]` 把输入类型与成功输出类型带过调用边界；
3. `ToolRegistry[InputT, OutputT]` 保证同一个注册表中的工具使用同一组输入、输出类型。

运行时仍然只有普通对象、属性查找、函数调用和异常。图中的类型关系主要由 Pyright 检查。

## 15.3 Duck typing：先用能力，不先问类名

Python 调用方法时，默认不会先查对象是否声明实现某个接口：

```python
class MemoryBackend:
    def search(self, query: str, *, limit: int) -> list[str]:
        return [query][:limit]


backend = MemoryBackend()
hits = backend.search("Protocol", limit=1)
```

解释器只会在 `backend` 上查找 `search`，按参数规则完成绑定，然后执行方法。对象有合适的成员，调用就可能成功；缺少成员或参数不匹配，错误通常到实际执行这条路径时才出现。

这就是 duck typing。它适合轻量实现和测试替身，但“能调”不等于“契约已经表达清楚”。公共 API 如果只依赖口头约定，IDE 和静态检查器就难以及早发现拼错方法名、传错参数或返回错误类型。

`Protocol` 的作用，是把这份已有的能力约定写成可静态检查的结构。

## 15.4 Protocol：Python 的结构化接口

```python
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SearchHit:
    title: str


class SearchBackend(Protocol):
    def search(self, query: str, *, limit: int) -> Sequence[SearchHit]: ...
```

这个协议声明了四件事：

- 对象要有名为 `search` 的成员；
- `query` 应接收 `str`；
- `limit` 必须以 `limit=...` 的关键字形式传入，并且类型为 `int`；
- 调用方只依赖一个可顺序读取的 `SearchHit` 序列。

实现类不必写 `class Xxx(SearchBackend)`：

```python
class MemorySearchBackend:
    def search(self, query: str, *, limit: int) -> list[SearchHit]:
        return [SearchHit(title=query)][:limit]


backend: SearchBackend = MemorySearchBackend()
```

Pyright 会比较成员结构和签名。`list[SearchHit]` 提供了 `Sequence[SearchHit]` 承诺的只读能力，所以这个更具体的返回值可以满足协议。通过协议使用结果时，调用方只能依赖协议承诺的 `Sequence`，不能擅自假定具体实现一定返回 `list`。

这叫结构化子类型。Java 接口通常采用名义类型：类要显式 `implements SearchBackend` 才属于该接口。Python `Protocol` 更像“只要形状兼容就算满足”，因而特别适合：

- 接入无法修改继承关系的第三方对象；
- 为测试写一个很小的 fake；
- 让多个互不相关的类暴露同一项能力；
- 避免调用方依赖具体后端名称。

协议也不会向隐式实现类复制代码。即使协议方法写了默认实现，没有继承协议的类也不会得到那段方法体。要复用行为，应使用组合、普通辅助函数或 ABC。

## 15.5 `runtime_checkable` 只检查“有没有成员”

普通自定义协议不能直接用于 `isinstance()`。加上 `@runtime_checkable` 后可以检查所需成员是否存在：

```python
from typing import runtime_checkable


@runtime_checkable
class SearchBackend(Protocol):
    def search(self, query: str, *, limit: int) -> Sequence[SearchHit]: ...
```

但这不是 Java `instanceof` 接口类型，也不是反射式的完整签名验证。考虑这个故意写错的实现：

```python
class WrongBackend:
    def search(self, query: int, *, limit: str) -> list[SearchHit]:
        return []
```

| 检查 | 结果 | 原因 |
| --- | --- | --- |
| `isinstance(WrongBackend(), SearchBackend)` | `True` | 运行时找到了 `search` 成员 |
| 某次动态调用 | 可能碰巧成功 | 注解不会在调用时验证实参 |
| 赋给 `SearchBackend` 变量并运行 Pyright | 报错 | 静态检查会比较参数与返回签名 |

因此，运行时协议通过不能证明参数个数、参数名、类型、返回值或业务行为正确。它适合在动态加载插件后做一次浅层能力探测，不能代替 Pyright，也不能代替对 JSON、HTTP 或 LLM 输出的运行时校验。

## 15.6 ABC：需要共享骨架时再要求继承

ABC 是 abstract base class（抽象基类）。它适合表达“子类必须参与这个继承体系，并复用统一流程”：

```python
from abc import ABC, abstractmethod
from collections.abc import Mapping


class ManagedTool(ABC):
    def __init__(self) -> None:
        self._call_count = 0

    def run(self, payload: Mapping[str, object]) -> str:
        self._call_count += 1
        return self.execute(payload)

    @abstractmethod
    def execute(self, payload: Mapping[str, object]) -> str: ...
```

具体子类继承 `ManagedTool` 后，会真正获得 `__init__()`、计数状态和 `run()` 模板流程。若子类没有实现 `execute()`，类定义可以完成，但实例化时会抛出 `TypeError`。

这与 Java 抽象类解决的是同一类问题，但约束时机不同：Java 编译器通常在编译阶段拒绝不完整具体类；Python 的 `ABCMeta` 主要在运行时创建实例时拦截。`@abstractmethod` 也不负责检查传入字典的字段。比如 `payload["text"]` 必须为字符串，仍要在 `execute()` 的 I/O 边界显式判断。

### 函数、Protocol 与 ABC 怎么选

| 实际需求 | 优先选择 | 原因 |
| --- | --- | --- |
| 只有一个无状态动作 | 普通函数 + 精确 `Callable` | 依赖最少，调用关系直接 |
| 多个无关对象只需提供同一能力 | `Protocol` | 按结构兼容，不强迫继承 |
| 第三方类或测试替身不能改基类 | `Protocol` | 实现方无需知道协议存在 |
| 子类必须复用状态或模板流程 | ABC | 继承会真正带来代码和状态 |
| 不完整子类必须在实例化时失败 | ABC + `@abstractmethod` | 提供运行时实例化约束 |
| 外部值要逐字段验证 | 显式校验或验证库 | 这不是接口抽象负责的问题 |

`Protocol` 不是 ABC 的新版本，ABC 也不是更严格的 `Protocol`。一个项目完全可以对外暴露协议，在框架内部用 ABC 管理受控生命周期。

## 15.7 泛型表达的是关系，不是“什么都能传”

先看一个丢失关系的签名：

```python
def accept_unknown(value: object) -> object:
    return value
```

它允许传入 `SearchHit`，但静态返回类型只是 `object`。调用方不能直接访问 `.title`，因为签名没有说明返回值与输入值属于同一类型。

泛型把同一个类型变量放进多个位置：

```python
def identity[T](value: T) -> T:
    return value


number = identity(15)       # T 求解为 int，结果是 int
text = identity("search")  # T 求解为 str，结果是 str
```

这与 Java 的 `<T> T identity(T value)` 很接近：每次调用都求出合适的 `T`，参数与返回位置保持关系。它不等于 `Any`：`Any` 会大幅放宽检查，而 `T` 恰恰要求相关位置保持一致。

运行时，`identity()` 只是原样返回同一个对象。`T` 不会复制、转换或验证这个对象，也不能写 `isinstance(value, T)`。泛型参数主要供静态工具使用。

### 容器元素也能参与关系

```python
from collections.abc import Sequence


def first[T](values: Sequence[T]) -> T:
    if not values:
        raise ValueError("first() 不能接收空序列")
    return values[0]
```

`Sequence[T]` 中的 `T` 是元素类型，`-> T` 把这个类型带到返回值：传入 `tuple[str, ...]` 得到 `str`，传入 `list[SearchHit]` 得到 `SearchHit`。

类型关系不能凭空制造一个元素。空序列没有 `T` 可以返回，因此 API 必须选择明确策略：抛异常、返回 `T | None`，或改用能表达“非空”的数据结构。本章示例选择抛出 `ValueError`。

## 15.8 用 `Result[T]` 保留成功值的具体类型

工具失败有时是正常业务分支，调用方希望把成功与失败都作为数据处理。可以定义：

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Success[T]:
    value: T


@dataclass(frozen=True)
class Failure:
    error: Exception


type Result[T] = Success[T] | Failure
```

`Result[list[SearchHit]]` 表示：成功时 `value` 精确为 `list[SearchHit]`，失败时保留原异常对象。调用方通过 `isinstance(result, Success)` 判断后，Pyright 会把联合收窄到成功分支。

它可以先类比为 Java 中 `Result<T>` 的成功/失败 sealed 层次，但不能完全等同：这里的 `Result[T]` 只是类型别名，不会生成新的运行时包装类，也不会阻止未检查的代码传入别的对象。由于泛型信息不用于这种运行时分支，也不要写 `isinstance(result, Success[int])`；检查实际类 `Success` 即可。

`Result[T]` 更不意味着捕获一切。示例中的 `SearchTool` 只把预计会发生的 `OSError` 和 `ValueError` 变成 `Failure`，并保留原始异常。未预期的程序缺陷应继续暴露，退出或中断信号也不应为了得到漂亮返回类型而被吞掉。如果现有调用栈已有统一异常处理，直接抛异常可能更合适。

## 15.9 新旧泛型语法都要会读

本项目要求 Python 3.14，主写法采用 Python 3.12 引入的类型参数语法：

```python
class Box[T]:
    def __init__(self, value: T) -> None:
        self._value = value

    def get(self) -> T:
        return self._value
```

兼容 Python 3.11 及更早版本的框架常用传统写法：

```python
from typing import Generic, TypeVar

LegacyT = TypeVar("LegacyT")


class LegacyBox(Generic[LegacyT]):
    def __init__(self, value: LegacyT) -> None:
        self._value = value

    def get(self) -> LegacyT:
        return self._value
```

两者表达同一关系：构造时保存的类型与 `get()` 返回类型一致。

| 问题 | 传统写法 | Python 3.12+ 写法 |
| --- | --- | --- |
| 类型变量声明位置 | 模块级 `TypeVar("LegacyT")` | 函数名或类名后的 `[T]` |
| 泛型类标记方式 | 继承 `Generic[LegacyT]` | `class Box[T]` 已隐式声明泛型 |
| 泛型函数写法 | 在注解中复用模块级 `T` | `def identity[T](...)` |

不要混写 `class Box[T](Generic[T])`。新语法已经建立泛型参数，不需要再继承 `Generic[T]`。

## 15.10 Union、constraint 与 bound 解决不同问题

这三类写法都在限制类型，但问题不同。

### Union：一个位置可能是哪几种类型

```python
def passthrough(value: str | bytes) -> str | bytes:
    return value
```

`str | bytes` 只承诺值属于这个集合。返回注解仍是整个 union，没有表达“返回类型一定和本次输入的具体类型相同”。

### Constraint：多个位置要选同一个有限候选

```python
def combine[A: (str, bytes)](left: A, right: A) -> A:
    return left + right
```

一次调用会从 `str`、`bytes` 中为 `A` 选择同一个候选。两个字符串可以，两个字节串也可以；`combine("tool-", b"result")` 会被 Pyright 拒绝。传入 `str` 的子类时，结果通常归一为约束中列出的 `str`。

### Bound：至少具备某项能力，同时保留具体子类型

```python
class HasTitle(Protocol):
    @property
    def title(self) -> str: ...


def first_titled[T: HasTitle](values: Sequence[T]) -> T:
    item = first(values)
    if not item.title:
        raise ValueError("title 不能为空")
    return item
```

`T: HasTitle` 表示 `T` 的上界是 `HasTitle`。函数体可以安全读取 `.title`，但返回值仍保留调用方传入的具体类型，例如 `SearchHit`，不会退化成 `HasTitle`。

| 写法 | 要回答的问题 | 多位置关系 | 结果类型特点 |
| --- | --- | --- | --- |
| `str | bytes` | 这个值可能是哪几种类型？ | 不自动关联 | 仍是 union |
| `A: (str, bytes)` | 本次调用从有限候选选哪一个？ | 同一个 `A` 贯穿各位置 | 归一到候选类型 |
| `T: HasTitle` | 值至少要具备什么能力？ | 同一个 `T` 贯穿各位置 | 保留具体子类型 |

传统语法分别写作 `TypeVar("A", str, bytes)` 和 `TypeVar("T", bound=HasTitle)`。同一个类型变量不能同时拥有 constraints 和 bound。

## 15.11 `Callable` 与 `Protocol.__call__`

对于固定的位置参数和返回值，`Callable` 最直接：

```python
from collections.abc import Callable, Sequence

type HitFormatter = Callable[[Sequence[SearchHit]], str]


def render(hits: Sequence[SearchHit], formatter: HitFormatter) -> str:
    return formatter(hits)
```

它可以先类比为 Java 的 `Function<List<SearchHit>, String>`。但 Python 不要求函数或对象声明实现某个函数式接口；只要调用签名兼容，就能传给 `render()`。

基础 `Callable` 不适合表达关键字参数名等复杂规则。这时定义带 `__call__` 的协议：

```python
class PrefixedFormatter(Protocol):
    def __call__(self, hits: Sequence[SearchHit], *, prefix: str) -> str: ...
```

普通函数 `format_hits(hits, *, prefix=...)` 可以直接满足该协议。如果实现把关键字参数命名为 `label`，调用方按协议传 `prefix=...` 就会失败，Pyright 因而会报告签名不兼容。

下一章的装饰器还需要保留“任意参数列表”的完整关系，那会用到 `ParamSpec`。本章先记住：一个普通 `T` 代表一个值类型，不能代表任意长的参数列表。

## 15.12 把关系带进 `Tool` 和注册表

工具协议有两个类型参数：

```python
class Tool[InputT, OutputT](Protocol):
    def __call__(self, tool_input: InputT, /) -> Result[OutputT]: ...
```

逐项代入 `Tool[SearchRequest, list[SearchHit]]`：

| 签名位置 | 代入后的类型 | 含义 |
| --- | --- | --- |
| `InputT` | `SearchRequest` | 调用时接收搜索请求 |
| `OutputT` | `list[SearchHit]` | 成功值是具体命中列表 |
| `Result[OutputT]` | 成功为 `Success[list[SearchHit]]`，否则为 `Failure` | 失败不抹掉成功值类型 |
| `__call__` | 对象可以像函数一样调用 | 类实例和普通函数都可能满足协议 |

`/` 表示 `tool_input` 是仅限位置参数，调用时写 `tool(request)`，不能写 `tool(tool_input=request)`。

注册表继续复用同一组类型变量：

```python
class ToolRegistry[InputT, OutputT]:
    def register(self, name: str, tool: Tool[InputT, OutputT]) -> None: ...

    def invoke(self, name: str, value: InputT) -> Result[OutputT]: ...
```

本章采用同构注册表：同一个实例里的所有工具都接收 `SearchRequest`，并产生 `Result[list[SearchHit]]`。因此工具实现、注册、调用和结果处理之间的关系不会退化成 `Any`。

真实 Agent 注册表常常同时保存搜索、天气、数据库等输入输出完全不同的工具。一个普通 `dict[str, Tool[object, object]]` 无法让检查器仅凭运行时字符串恢复每个名字对应的类型。实际项目可能使用 `Literal` + 重载、代码生成、运行时模型校验，或在一个明确边界做受控类型擦除。不要为了把异构工具塞进字典，就宣称 `Tool[Any, Any]` 仍然保留了类型安全。

## 15.13 为什么只读参数优先 `Mapping` / `Sequence`

公共参数应只索取实现真正需要的能力：

```python
def first[T](values: Sequence[T]) -> T: ...


class ToolRegistry[InputT, OutputT]:
    def __init__(self, tools: Mapping[str, Tool[InputT, OutputT]]) -> None:
        self._tools = dict(tools)
```

`first()` 只读取长度和下标，因此不需要调用方必须提供 `list`。注册表构造器只读取初始映射，所以接收 `Mapping`；内部确实要增删条目时，再复制为自己拥有的 `dict`。

这与 Java“参数面向所需的最小集合接口”思路相似，但 `Sequence` / `Mapping` 并不承诺深度不可变，也不取得对象所有权。别处仍可能修改底层列表或字典。若必须保存稳定快照，需要显式复制。

### 为什么 `list[SearchHit]` 不能当成 `list[object]`

```python
def append_marker(values: list[object]) -> None:
    values.append("debug-marker")


hits: list[SearchHit] = []
# append_marker(hits)  # Pyright 拒绝
```

如果允许传入，`append_marker()` 有权向列表加入任意 `object`，于是原本保证只含 `SearchHit` 的 `hits` 被混入了 `str`。因此可变 `list` 的元素类型默认是不变的。

只读的 `Sequence` 没有 `append()`，`Sequence[SearchHit]` 可以安全交给只按 `Sequence[object]` 读取的消费者，这称为协变。完整协变/逆变推导属于进阶主题；当前先使用一条工程规则：只读 API 优先公开只读抽象，确实要修改时才接收准确元素类型的具体可变容器。

## 15.14 Java 工程师最容易踩的坑

1. 把 `Protocol` 当成必须实现的 Java 接口。这样会丢掉结构化匹配的主要价值。需要共享代码或强制继承时，需求才更接近 ABC。
2. 把 `@runtime_checkable` 当成完整反射校验。它只确认成员存在，不验证参数和返回值。
3. 把 `Any` 当成泛型。`Any -> Any` 放弃关系；`T -> T` 保留关系。
4. 认为泛型注解会在运行时验值。Python 不会因 `Box[int]` 自动阻止字符串进入对象。
5. 把 constraint 写成 union。Union 表达候选集合，共享的 constraint 才表达多个位置要选择同一家族。
6. 让 `Result[T]` 捕获所有异常。只把调用方计划处理的失败变成数据，并保留原异常。
7. 用 `Tool[Any, Any]` 掩盖异构注册表。代码能装进字典，不代表输入输出关系仍被检查。
8. 把 `list[具体类型]` 放宽成 `list[object]`。接收方可以写入其他对象，破坏原列表保证。
9. 把接口注解当外部数据验证。JSON、HTTP、数据库和 LLM 输出仍需运行时校验。

## 15.15 运行本章项目

本章只使用标准库，没有新增运行依赖。仓库根 `pyproject.toml` 要求 Python >=3.14；这使 `def identity[T](...)` 等 Python 3.12+ 语法可以直接运行。

本章文件分工：

| 文件 | 用途 |
| --- | --- |
| `README.md` | 独立教程与练习 |
| `example.py` | 六组可从头运行的同步实验 |
| `search_fixture.json` | 固定、只读的本地搜索数据 |
| `static_errors.py` | 故意保留的静态类型错误，只交给 Pyright |

从仓库根目录运行正常示例：

```bash
.venv/bin/python -B java_to_python/chapters/15_protocols_generics/example.py
```

`-B` 只用于阻止生成 `.pyc`。现有 `java_to_python/run_all.py` 只支持前 10 章，因此本章单独运行。输出分为六组，重点观察：

- `identity()`、`first()` 和 `Result[T]` 如何保留具体类型；
- 内存后端与 fixture 后端如何通过同一个 `SearchBackend` 被调用；
- 错误签名为何能通过浅层 `isinstance()`，却不能通过静态检查；
- 类实例工具与普通函数如何进入同一个注册表；
- `Callable`、`__call__` 协议和 ABC 各自解决什么问题；
- 绕过列表不变性检查后，`SearchHit` 列表如何被混入 `str`。

fixture 路径由 `Path(__file__)` 定位，不依赖启动时工作目录。加载器会先验证 JSON 顶层数组、每行对象和四个必需字符串字段，再创建 `SearchHit`。示例不联网、不需要 API Key，也没有后台线程或文件写入。它不能模拟真实后端的超时、鉴权、分页、连接池和取消行为。

## 15.16 同时验证运行行为和静态关系

正常示例应该通过 Pyright：

```bash
.venv/bin/pyright --project pyproject.toml \
  java_to_python/chapters/15_protocols_generics/example.py
```

故意错误文件应该以非零状态退出：

```bash
.venv/bin/pyright --project pyproject.toml \
  java_to_python/chapters/15_protocols_generics/static_errors.py
```

当前 `static_errors.py` 预期产生 7 个错误、4 条类型信息，分别覆盖协议签名、`identity()`、`first()`、工具输出、关键字回调、列表不变性、constraint，以及四个 `reveal_type()` 结果。不要直接执行这个文件；`reveal_type()` 和故意错误是静态实验。

代码风格检查：

```bash
.venv/bin/ruff check java_to_python/chapters/15_protocols_generics
.venv/bin/ruff format --check java_to_python/chapters/15_protocols_generics
```

本次使用 CPython 3.14.7、Pyright 1.1.411 和 Ruff 0.16.5 验证：正常示例完整运行，主文件为 0 个类型错误，故意错误文件得到预期 7 个错误和 4 条类型信息，Ruff 两项检查均通过。

运行通过只证明当前真实路径可以执行；Pyright 通过只证明静态签名关系满足当前规则；Ruff 通过只证明代码符合配置的格式和常规检查。三者不能互相替代。

## 15.17 递进练习

建议另建练习文件，不要修改 `static_errors.py` 的预期诊断数量。

### 练习 1：增加一个结构化后端

- **要求**：实现 `PrefixSearchBackend`，只匹配标题前缀，不显式继承 `SearchBackend`。
- **输入**：两个 `SearchHit`，查询 `"Protocol"`，分别设置 `limit=1` 和 `limit=2`。
- **验收**：能赋给 `SearchBackend` 变量并通过 Pyright；调用方不按类名分支；空匹配返回空序列。

### 练习 2：组合泛型结果

- **要求**：实现 `map_success[T, U](result: Result[T], transform: Callable[[T], U]) -> Result[U]`。
- **输入**：`Success(3)` 加整数转字符串函数，以及 `Failure(ValueError("bad"))`。
- **验收**：成功结果推断为 `Result[str]`；失败前后的异常是同一个对象；不使用 `Any` 或 `cast`；转换函数抛出的异常不被悄悄吞掉。

### 练习 3：比较 Protocol 与 ABC

- **要求**：定义只要求 `close()` 的 `Closable` 协议，再定义带共享 `closed` 状态的 `ManagedResource(ABC)`，各写一个具体实现。
- **输入**：正常关闭两次，并尝试实例化一个缺少抽象方法的子类。
- **验收**：现有对象可以结构化满足协议；ABC 子类继承共享状态；不完整子类实例化得到 `TypeError`；能解释两种设计分别解决什么。

### 练习 4：选择 union、constraint 或 bound

- **要求**：分别为“原样返回 `str | bytes`”“合并两个同编码家族的值”“从有 `name` 属性的序列取第一项并保留具体类型”设计签名。
- **输入**：`str`、`bytes`、`str` 子类，以及两个具有 `name` 属性的不同数据类。
- **验收**：Pyright 拒绝混合文本/字节合并；`reveal_type()` 能观察 constraint 的归一结果和 bound 保留的具体类型；能解释为什么不能全部改成 union。

### 练习 5：扩展同构注册表

- **要求**：增加 `names() -> Sequence[str]` 和 `unregister(name: str) -> Result[Tool[InputT, OutputT]]`。
- **输入**：注册两个搜索工具，删除一个，再删除未知名字。
- **验收**：名字顺序和删除结果可观察；取回的工具仍保留输入输出关系；未知名字保存 `LookupError`；不使用 `Tool[Any, Any]`。

## 15.18 本章小结

- Duck typing 是 Python 的运行方式；`Protocol` 把隐含能力写成静态结构契约。
- `@runtime_checkable` 只做浅层成员探测，不验证完整签名或业务行为。
- ABC 通过显式继承复用状态和流程，并在实例化时拒绝未完成抽象方法的子类。
- 泛型的核心是保留参数、容器元素、回调和返回值之间的类型关系，不是接受任意值。
- Union 表达可能集合，constraint 表达有限候选中的同一选择，bound 表达最低能力并保留子类型。
- 简单回调用 `Callable`；要表达关键字名等复杂调用规则时，用带 `__call__` 的 `Protocol`。
- `Mapping` / `Sequence` 适合只读消费者；可变 `list` 不能随意放宽元素类型。
- 静态类型无法替代运行时校验。来自 JSON、HTTP、数据库和 LLM 的数据始终要在边界验证。
