# 第 13 章：类型注解、`TypedDict` 与静态检查

## 先记住一句话

Python 类型注解的作用，是把“这个值应该是什么”写给人、编辑器和静态检查器看；它默认不会像 Java 编译器那样阻止代码运行，也不会替你校验 HTTP、JSON、数据库或 LLM 返回的数据。

因此，可靠的 Python 类型代码需要两道防线：

```text
外部数据（不可信）
    │
    │  运行时校验：字段、实际类型、范围、状态组合
    ▼
明确的内部类型
    │
    │  Pyright：检查后续代码有没有把值用错
    ▼
业务逻辑（可信区）
```

这和 Java 项目里的“反序列化/Bean Validation + 编译器类型检查”解决的是同一组问题，只是 Python 不会自动把这两层绑在一起。

学完本章，你应该能解释：

- `name: str`、`-> list[str]` 到底约束了谁；
- `Any` 和 `object` 为什么都能接任意值，安全性却完全不同；
- “值可以是 `None`”“参数可以省略”“字典键可以缺少”为什么是三件事；
- `TypedDict` 为什么像 DTO 的结构说明，却不是运行时 DTO；
- 如何用 `Literal` 联合表达“状态不同，返回数据形状也不同”；
- 为什么外部 JSON 必须先校验，再交给带精确类型的核心逻辑。

## 1. Java → Python 心智模型

| Java 中熟悉的概念 | Python 中可以先想到 | 最重要的不等价之处 |
| --- | --- | --- |
| 方法参数、返回类型 | 参数注解、`->` 返回类型 | Python 解释器默认不按注解拒绝调用 |
| `Object` | `object` | 取出后也要先确认具体能力，Python 常靠分支收窄 |
| 暂时关闭一段类型检查 | `Any` | `Any` 不是父类，而是让检查器对这条数据流大幅放行 |
| `Map<String, Object>` | `dict[str, object]` | 不能说明每个固定字段各是什么类型 |
| JSON DTO 的字段表 | `TypedDict` | 只描述普通 `dict`，不创建 Bean，也不做运行时校验 |
| nullable `String` | `str | None` | `None` 仍是一个普通值 |
| `enum` 的有限值 | `Literal["ok", "error"]` | 仍是普通字符串，不会生成枚举类 |
| 函数式接口 | `Callable[[参数], 返回值]` | 只描述调用签名，不是有名字和默认方法的接口 |
| 面向接口接收集合 | `Mapping` / `Sequence` | Python 的能力边界与 Java 集合并非一一对应 |
| Java 强制转换 | `typing.cast` | Python 的 `cast` 运行时不检查、不转换 |

可以把注解理解成“源码中的契约说明”。Pyright 像一个独立的编译期检查步骤，但 Python 解释器仍能直接执行没有通过检查的源码。

## 2. 一眼看懂基础注解

```python
from collections.abc import Sequence


def build_labels(prefix: str, ids: Sequence[int]) -> list[str]:
    labels: list[str] = [f"{prefix}-{item_id}" for item_id in ids]
    return labels


result = build_labels("doc", (7, 13))
```

- `prefix: str`：调用方按约定应传字符串；
- `ids: Sequence[int]`：函数只要求“可按顺序读取整数”，`list` 和 `tuple` 都能传；
- `-> list[str]`：函数承诺正常返回字符串列表；
- `labels: list[str]`：局部变量应保存字符串列表；
- `list[str]` 中，外层是容器，方括号里是元素类型。

参数只读，所以接收较宽的 `Sequence[int]`；函数自己创建可变列表，所以返回具体 `list[str]`。思路类似 Java 参数面向 `List` / `Collection`，而不是锁死为 `ArrayList`。

常用容器写法：

| 写法 | 含义 |
| --- | --- |
| `list[str]` | 可变列表，每项应是 `str` |
| `dict[str, int]` | 键应是 `str`，值应是 `int` |
| `tuple[int, str]` | 固定两个位置，类型分别是 `int`、`str` |
| `tuple[str, ...]` | 长度不限，每项都是 `str` |
| `str | None` | `str` 或 `None` |

`tuple[int, str]` 描述的是位置，不是“可以随便混放两种类型”。只有 `tuple[str, ...]` 中的省略号才表示同类元素可以重复任意次。

注解也不会创建或转换值：

```python
count: int
print(count)  # 仍会报变量尚未绑定
```

同理，`value: int = "3"` 不会把字符串变成整数。转换必须明确写 `int("3")`，并处理可能的 `ValueError`。

## 3. 静态约定不等于运行时检查

```python
from typing import Any


def echo_text(text: str) -> str:
    return text


external_value: Any = 42
result = echo_text(external_value)
print(result, type(result).__name__)  # 42 int
```

Pyright 根据函数声明，把调用结果看作 `str`。但 `Any` 让整数实参穿过了静态检查，函数体又只是原样返回，所以运行时得到的仍是 `int`。

这与 Java 有两个根本差异：

1. Java 编译器通常会拒绝把整数传给 `String` 参数；Python 可以不运行检查器而直接执行。
2. `-> str` 不会插入 `str(value)`，也不会插入 `isinstance(value, str)`。

业务要求“原本就必须是字符串”时，应显式检查：

```python
def require_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("必须是字符串")
    return value
```

业务允许数字转字符串时，则明确调用 `str(value)`。校验和转换是两种不同的接口策略，注解不会替你选择。

## 4. `Any` 与 `object`：一个绕过检查，一个要求证明

| 问题 | `Any` | `object` |
| --- | --- | --- |
| 能否接收任意对象 | 可以 | 可以 |
| 能否直接调用未知属性、下标或运算 | 检查器通常放行 | 必须先收窄 |
| 能否避免暴露具体类型 | 通常可以 | 未收窄时不可以 |
| 合适用途 | 隔离确实无类型信息的兼容边界 | 接住未知输入，然后安全判断 |
| 主要风险 | 错误沿数据流扩散 | 多写判断，但错误更早暴露 |

`Any` 不是 Java 的 `Object`：

```python
payload: Any = {"limit": "3"}
next_limit: int = payload["limit"] + 1
```

检查器可能放过这段代码，运行时却会因 `str + int` 抛 `TypeError`。可以把 `Any` 理解成“暂停追问这条数据流”，而不是安全的万能类型。

`object` 则迫使代码先收窄：

```python
def normalize(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("必须是字符串或 None")
    return value.strip()
```

最后一行之前，分支已经排除了 `None` 和其他类型，因此 Pyright 知道 `value` 是 `str`。这种根据控制流缩小可能类型集合的过程叫类型收窄。

常见收窄方式有 `is None`、`isinstance(...)`、`type(value) is int`，以及对 `Literal` 状态字段做相等判断。检查空值时优先写 `is None`；`if not value` 会把 `None`、空字符串、`0` 和空容器混在一起。

还有一个 Java 工程师容易意外的细节：Python 的 `bool` 是 `int` 的子类，`isinstance(True, int)` 为真。如果 JSON 字段必须是“真正的整数”，不能让 `true` 充当 `1`，就写 `type(value) is int`。

## 5. 三种“可选”必须分开

```python
from typing import NotRequired, TypedDict


class SearchArguments(TypedDict):
    query: str
    category: str | None
    limit: NotRequired[int]


def search(
    query: str,
    *,
    category: str | None = None,
    limit: int = 3,
) -> None:
    ...
```

| 真正的问题 | 写法 | 含义 |
| --- | --- | --- |
| 值能不能是空值 | `str | None` | 值可以是 `None` |
| 调用时能不能不传实参 | `limit: int = 3` | 省略后，参数绑定使用默认值 `3` |
| `TypedDict` 中能不能没有键 | `limit: NotRequired[int]` | 可以缺键；存在时必须是整数 |

所以：

- `category: str | None` 没有默认值时，调用仍必须传这个实参；
- `limit: int = 3` 可以省略，但显式传 `None` 不符合 `int`；
- `TypedDict` 中的 `category: str | None` 默认是必需键，只是值可以为空；
- `NotRequired[int]` 不会自动填默认值，也不允许字符串 `"3"`。

Java 的 `Optional<T>` 是一个真实包装对象，而 Python 的 `T | None` 是两个可能类型的联合，API 用法不能直接照搬。

## 6. `TypedDict`：给固定字段的字典写静态结构

```python
from typing import NotRequired, TypedDict


class Document(TypedDict):
    document_id: str
    title: str
    score: float | None
    snippet: NotRequired[str]
```

Pyright 现在知道：

- `document_id`、`title`、`score` 必须有键；
- `score` 的值可以是 `float` 或 `None`；
- `snippet` 可以缺键，存在时必须是 `str`。

可以先类比成 Java DTO 的字段契约，但不能完全等同：

```python
document: Document = {
    "document_id": "doc-13",
    "title": "类型注解",
    "score": None,
}
print(type(document))  # <class 'dict'>
```

运行时它仍是普通 `dict`。它不会自动解析 JSON、检查缺字段、转换字段类型，也不能通过 `isinstance(document, Document)` 来校验。`TypedDict` 最适合描述“外部值校验通过后，内部代码按什么结构使用”。

大多数字段都可缺少时，可以反过来写：

```python
from typing import Required, TypedDict


class TraceMetadata(TypedDict, total=False):
    request_id: Required[str]
    label: str
    elapsed_ms: int | None
```

`total=False` 让字段默认可缺少，`Required[str]` 再把 `request_id` 改回必需键。注意 `elapsed_ms` 同时包含两个维度：键可以缺少；键存在时，值还可以是 `None`。

如果只有少数字段可缺少，优先保持默认的 `total=True`，只给个别字段写 `NotRequired[...]`，更容易阅读。

## 7. `Literal`：把状态和载荷绑在一起

只写 `status: str` 和 `document: Document | None`，会允许“状态成功但文档为空”这种矛盾组合。更准确的建模方式是：

```python
from typing import Literal, TypedDict


class QuerySuccess(TypedDict):
    status: Literal["ok"]
    document: Document


class QueryNotFound(TypedDict):
    status: Literal["not_found"]
    document: None


type QueryResult = QuerySuccess | QueryNotFound
```

`Literal["ok"]` 表示只能是这个具体字符串。`QueryResult` 表示值只能符合两个完整结构之一。状态字段因此能区分联合成员：

```python
def format_result(result: QueryResult) -> str:
    if result["status"] == "not_found":
        return "没有找到"
    return result["document"]["title"]
```

最后一行中，Pyright 已根据 `status` 排除 `QueryNotFound`，所以 `document` 一定是 `Document`。这可以类比 Java sealed hierarchy 的不同结果子类，但 Python 没有创建子类实例；值仍是普通字典，状态仍是普通字符串。外部传来的 `"timeout"` 仍需运行时拒绝。

`type QueryResult = ...` 是 Python 3.12 引入的类型别名语法。本项目要求 Python 3.14，因此可以直接使用。别名只是给复杂类型起业务名字，不是构造函数或校验器。

## 8. 回调和集合：只索取真正需要的能力

```python
from collections.abc import Callable, Sequence

type ResultFormatter = Callable[[QueryResult], str]


def format_all(
    result: QueryResult,
    formatters: Sequence[ResultFormatter],
) -> list[str]:
    return [formatter(result) for formatter in formatters]
```

`Callable[[QueryResult], str]` 的内层列表是参数类型，最后的 `str` 是返回类型。它可以类比一个“接收查询结果、返回字符串”的 Java 函数式接口。

不要习惯性写 `Callable[..., Any]`：省略号放弃参数形状检查，`Any` 又放弃返回值检查。复杂关键字参数和装饰器签名需要后续的 `Protocol`、`ParamSpec` 等能力，本章不展开。

集合参数也应按能力选择：

| 函数需要的能力 | 推荐参数类型 | 常见实参 |
| --- | --- | --- |
| 按键读取，不修改 | `Mapping[K, V]` | `dict`、其他映射 |
| 按顺序读取，不修改 | `Sequence[T]` | `list`、`tuple` |
| 需要 `append` 或原地排序 | `list[T]` | `list` |
| 需要新增、删除或修改键 | `dict[K, V]` | `dict` |

这与 Java“参数面向接口”是同一思路。但 `str` 也有序列行为；如果业务要求“多个独立字符串”，而单个字符串会导致逐字符遍历，还要在运行时单独拒绝 `str`。

## 9. `cast` 与 `reveal_type`

### `cast()` 不等于 Java 强制转换

```python
from typing import cast

raw: object = 42
claimed = cast(str, raw)
print(type(claimed))  # <class 'int'>
```

`cast(str, raw)` 只告诉检查器“后续把它当 `str`”，运行时原样返回对象。Java 的 `(String) value` 会检查对象并可能抛 `ClassCastException`；Python 的 `typing.cast` 连这一步都没有。

因此，不能拿 `cast(SearchArguments, json.loads(text))` 代替外部数据校验。合理用法是：运行时分支已经证明某个事实，但检查器无法完整推导时，用局部 `cast` 补充这个事实。先有证据，再有 `cast`。

### `reveal_type()` 询问检查器的当前判断

```python
from typing import reveal_type

value: object = "python"
if isinstance(value, str):
    reveal_type(value)  # Pyright 报告 str
```

它适合学习和排查类型收窄。本章把这类实验放在 `static_errors.py` 中交给 Pyright 分析，不把该文件作为普通程序执行。

## 10. 外部 JSON：先验证，再获得精确类型

`json.loads()` 只保证 JSON 语法正确，不保证业务契约。下面都是合法 JSON，却不一定是合法搜索参数：

```json
{"query": "typing", "category": null}
{"query": 13, "category": null}
{"query": "typing"}
{"query": "typing", "category": null, "limit": true}
```

它们分别是：正常值、`query` 类型错误、缺少必需键、用布尔值冒充整数。

边界代码通常按下面的顺序：

1. 用 `object` 接住未知结果；
2. 检查最外层是不是字典；
3. 检查必需键、未知键策略和字段的实际类型；
4. 检查范围、空字符串以及状态与载荷的组合；
5. 重新构造一个精确的 `TypedDict`；
6. 从这里开始，核心逻辑只接收已验证类型。

一个精简的校验器如下：

```python
from collections.abc import Mapping
from typing import cast


def parse_arguments(raw: object) -> SearchArguments:
    if not isinstance(raw, dict):
        raise ValueError("arguments 必须是对象")
    if not all(isinstance(key, str) for key in raw):
        raise ValueError("arguments 的键必须是字符串")

    data = cast(Mapping[str, object], raw)
    if "query" not in data or "category" not in data:
        raise ValueError("缺少必需字段")

    query, category = data["query"], data["category"]
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 必须是非空字符串")
    if category is not None and not isinstance(category, str):
        raise ValueError("category 必须是字符串或 None")

    parsed: SearchArguments = {"query": query.strip(), "category": category}
    if "limit" in data:
        limit = data["limit"]
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("limit 必须是 1 到 10 的整数")
        parsed["limit"] = limit
    return parsed
```

这里的 `cast` 只表达前两个分支已经证明的事实：“这是字典，而且键都是字符串”。字段仍被逐个检查，并没有把整个外部对象直接伪装成 `SearchArguments`。

真实服务还要明确未知字段策略、错误格式、嵌套结构、长度限制和版本兼容。也可以使用 Pydantic 等运行时模型库减少手写校验，但“运行时校验”和“静态使用检查”仍是两层职责。

## 11. Pyright 在开发流程中的位置

根目录 `pyproject.toml` 配置为：

```toml
[tool.pyright]
pythonVersion = "3.14"
typeCheckingMode = "basic"
```

`pythonVersion` 告诉 Pyright 按哪版语法和标准库类型分析，并不决定真正执行程序的解释器。`basic` 能发现本章核心契约错误，但不承诺发现所有问题。是否升级到严格模式，应由整个项目统一决定。

可以把流程类比为：

```text
Java:   javac 类型检查 + 程序中的边界校验
Python: Pyright 静态检查 + 程序中的边界校验 + Python 解释器执行
```

Ruff 负责格式和常规代码规则，不是类型检查器；Pyright、Ruff 和运行测试互不替代。

## 12. Java 工程师最容易踩的坑

- **把注解当运行时门禁**：`def f(x: int)` 不会自动拒绝字符串。
- **期待注解自动转换**：`"3"` 不会因为目标写了 `int` 就变成整数。
- **把 `Any` 当 `Object`**：`Any` 会让检查器放弃很多保护，应限制在很窄的边界。
- **把 `object` 当 `Any` 用**：取出后必须先证明具体类型。
- **把 `T | None` 当“可以省略”**：可空、参数默认值和 `TypedDict` 缺键互不替代。
- **认为 `TypedDict` 会校验 JSON**：运行时它仍是普通字典。
- **用 `cast` 消灭错误**：没有运行时证据的 `cast` 只是在掩盖风险。
- **回调都写 `Callable[..., Any]`**：参数数量和返回值约束都会丢失。
- **参数一律写具体 `list` / `dict`**：只读函数会无意拒绝具有同等能力的对象。
- **用 `if not value` 判断 `None`**：空字符串、`0` 和空容器也会被误判。

## 13. 运行本章示例

正常入口只使用 Python 标准库，不联网、不访问数据库、不需要 API Key，也没有额外运行依赖。项目要求 Python 3.14 或更高版本。

若还没有虚拟环境，可以先创建：

```bash
python3.14 -m venv .venv
```

在本课目录运行：

```bash
.venv/bin/python -B example.py
```

Windows PowerShell 可使用：

```powershell
.venv\Scripts\python.exe -B example.py
```

`-B` 只是禁止生成 `.pyc` 缓存。运行时重点观察：

- 注解为 `str` 的函数为什么仍可能实际收到并返回 `int`；
- `Any` 如何把错误推迟到运行时，`object` 如何迫使代码先判断；
- `TypedDict` 的真实运行时类型为什么仍是 `dict`；
- 必需但可空的 `category` 与可缺少的 `limit` 有什么区别；
- 成功、未找到、缺字段和字段类型错误如何得到不同结果；
- `Literal` 状态如何帮助格式化逻辑收窄联合类型。

三个文件的职责：

| 文件 | 用途 |
| --- | --- |
| `README.md` | 不看源码也能先建立类型系统心智模型 |
| `example.py` | 运行正常路径与受控失败，观察真实行为 |
| `static_errors.py` | 故意保留静态错误，只交给 Pyright，不要普通执行 |

### 静态检查

当前环境提供 Pyright 时，在本课目录运行：

```bash
pyright --project pyproject.toml example.py
```

正常入口应得到 `0 errors`。再检查故意写错的教学文件：

```bash
pyright --project pyproject.toml static_errors.py
```

这条命令应非零退出，并报告 6 个错误和 4 条类型信息，分别用于观察：错误参数类型、未收窄 `object`、`TypedDict` 缺必需键、可缺少键的错误值、非法 `Literal` 状态和错误回调签名。其中 `Any` 参与的错误表达式故意不会得到诊断，这正是本章要观察的风险。

Pyright 和 Ruff 是开发检查工具，根项目没有把它们声明为可重建的运行依赖。新环境中没有 `pyright` 并不影响标准库示例运行；请按项目统一方式准备开发工具，不要把当前机器恰好安装了工具误认为依赖已声明。

## 14. 三个小练习

1. 定义搜索参数：`query` 必需；`category` 必需但可为 `None`；`limit` 可缺少但存在时必须是整数。分别测试缺 `category`、`category=None`、缺 `limit`、`limit=None`。
2. 从 `payload: object = {"limit": "3"}` 开始，写一个只接受 1～10 整数的解析器。确保 `3` 通过，`"3"`、`True`、缺键和 `11` 按明确规则处理，核心计算不含 `Any`。
3. 给 `QueryResult` 增加 `permission_denied` 状态。它必须带字符串 `reason`，不能带文档；让 Pyright 能按状态收窄，让运行时解析器拒绝未知状态。

## 15. 本章小结

1. 类型注解描述契约，不负责运行时验证或转换。
2. 未知外部值优先用 `object` 接住，检查后再进入核心逻辑。
3. `Any` 是逃生口，不是通用建模方案。
4. `T | None`、参数默认值和 `NotRequired[T]` 分别处理可空、可省略实参和可缺键。
5. 固定字段字典用 `TypedDict` 描述，但外部数据仍要运行时校验。
6. 有限状态用 `Literal`；状态决定载荷时，用可区分联合把两者绑在一起。
7. 回调写精确 `Callable`，只读参数优先按 `Mapping` / `Sequence` 接收。
8. `cast` 只能补充已被证明的事实，不能代替校验。

如果你能解释“为什么 Pyright 通过不代表 JSON 安全”，并能画出“`object` → 运行时校验 → `TypedDict` → 核心逻辑”这条数据流，本章核心就掌握了。
