# 第 14 章：用 dataclass 与 Pydantic 管理结构化数据

一句话概括本章：**内部可信状态重在表达和维护，外部不可信数据重在验证；不要把所有结构都塞进 `dict`，也不要把所有结构都做成 Pydantic 模型。**

Agent 程序会不断搬运结构化数据：模型生成 tool 参数，HTTP 或数据库返回记录，工具再把结果交回 Agent 循环；与此同时，程序还要保存运行阶段、重试次数和事件列表。它们看起来都能用 `dict` 表示，但承担的责任不同。

本章所说的“数据模型”是指用类或类型声明描述一组结构化数据，不是 Python 语言中由 `__iter__`、`__enter__` 等特殊方法组成的广义对象模型。

学完本章，你应该能够：

- 分清 `dict`、`TypedDict`、`dataclass` 和 Pydantic `BaseModel` 的职责；
- 解释 dataclass 自动生成了什么，以及类型注解为什么不会自动校验；
- 分清必填、可缺失、可为 `None`、未知字段和默认值；
- 为 tool 参数选择合适的类型转换与严格模式；
- 验证嵌套输入，保留准确的错误路径，并正确序列化时间等值；
- 用 JSON Schema 描述 tool 参数，同时说清它不能保证什么。

## 14.1 先建立 Java → Python 心智模型

这四种结构不是从低级到高级的升级链，而是解决四类不同问题。

| Python 结构 | 可以先类比为 | 运行时形态 | 常规构造/验证入口会校验 | 适合放在哪里 |
| --- | --- | --- | --- | --- |
| `dict` | `Map<String, Object>` | 可变映射 | 否 | 键本来就动态的临时数据 |
| `TypedDict` | 为固定键 Map 补充的静态接口 | 仍然是普通 `dict` | 否 | 已可信、仍需按字典协议传递的数据 |
| `dataclass` | 精简版 POJO、Lombok 数据类 | 自定义类实例 | 否 | 进程内状态、配置快照、值对象 |
| `BaseModel` | DTO + 数据绑定 + Bean Validation + Schema 生成 | Pydantic 模型实例 | 是 | JSON、LLM、HTTP、数据库等 I/O 边界 |

这些类比都不能完全等同。

- `TypedDict` 不会生成一个可在运行时识别的新容器类型，也不会像 Jackson 反序列化那样检查输入。
- dataclass 虽然会生成构造、显示和比较方法，但不会像 Jakarta Bean Validation 那样根据注解自动拒绝错误值。
- `BaseModel` 把解析、转换、约束和错误收集中在模型入口完成；其默认转换规则、缺失语义和验证时机也不等同于 Java DTO。

选择时先问数据来自哪里：

1. 键集合本来就是开放的，用 `dict`。
2. 键固定、数据已经可信、消费者必须拿到字典，用 `TypedDict`。
3. 数据由当前进程维护，主要需要属性访问、默认值、显示和比较，用 dataclass。
4. 数据刚从进程边界进入，需要拒绝、转换、报错或生成 schema，用 `BaseModel`。

实际项目经常组合使用：先用 `BaseModel` 验证 tool 参数，再用 dataclass 保存执行状态，最后用另一个 `BaseModel` 校验并序列化工具结果。

## 14.2 dict 与 TypedDict：容器没变，静态信息变了

当键由运行时数据决定时，字典就是准确的模型：

```python
counts_by_tool: dict[str, int] = {}
counts_by_tool[tool_name] = counts_by_tool.get(tool_name, 0) + 1
```

这里无法提前列出所有 `tool_name`，强行声明固定字段反而会误导读者。

如果键已经固定，但调用方仍要求字典协议，可以使用 `TypedDict`：

```python
from typing import TypedDict


class ToolSummary(TypedDict):
    request_id: str
    item_count: int


summary: ToolSummary = {"request_id": "req-1", "item_count": 2}
assert type(summary) is dict
```

Pyright 可以据此发现漏键、错键和值类型不匹配，IDE 也能补全键名。但运行时的 `summary` 仍是 `dict`。把 `json.loads()` 的结果直接标注成 `ToolSummary`，只是告诉检查器“请相信它”，并没有验证任何数据。

因此，`TypedDict` 最适合描述已经经过边界校验的内部字典。外部 JSON 仍应先验证，不能靠类型标注把愿望变成事实。

参考：Python TypedDict 文档。

## 14.3 dataclass：为可信内部对象减少样板代码

`@dataclass` 是标准库类装饰器。Python 执行类定义时，装饰器读取带注解的字段，并按配置生成常用方法。默认情况下，最值得关注的是：

- `__init__()`：接收初始化参数并保存字段；
- `__repr__()`：生成便于调试的对象表示；
- `__eq__()`：按声明顺序比较两个同类实例的字段值；
- 字段元数据：可以通过 `dataclasses.fields()` 读取。

```python
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class RunState:
    run_id: str
    phase: Literal["created", "done"] = "created"
    attempts: int = 0
    events: list[str] = field(default_factory=list)
    debug_context: dict[str, str] = field(default_factory=dict, repr=False)


first = RunState(run_id="run-1")
second = RunState(run_id="run-2")

first.events.append("validated")
assert first.events == ["validated"]
assert second.events == []
assert first != second
```

`run_id` 没有默认值，因此构造时必须传入，并且要放在有默认值的字段之前。上面的构造、显示与比较行为都来自 dataclass 生成的方法。这可以先类比为 Java 中用 Lombok 减少 POJO 的构造器、`toString()` 和 `equals()` 样板代码，但有三个重要边界。

### 注解不会触发运行时校验

```python
state = RunState(run_id=123)  # 静态检查器会报告，普通 Python 运行时仍会创建实例
assert state.run_id == 123
```

生成的 `__init__()` 只负责赋值，不会因为 `run_id: str` 自动调用 `isinstance()`，也不会把 `123` 转成字符串。可信内部代码配合 Pyright 通常已经够用；从 LLM 或 HTTP 收到的字典不能直接展开成 dataclass 来冒充校验。

`Literal` 在这里同样主要服务于静态检查。普通 dataclass 不会在运行时拒绝其他字符串。

### 可变默认值必须按实例创建

每个 `RunState` 都应该拥有独立的事件列表。`field(default_factory=list)` 保存的是创建默认值的函数；每次构造实例且没有传 `events` 时，生成的 `__init__()` 都会调用一次 `list()`。

不可变默认值可以直接写成 `attempts: int = 0`。可变值不要写成 `events: list[str] = []`；它表达共享对象的危险语义，现代 dataclass 也会在定义类时拒绝这种未哈希的默认值。

### repr、比较和安全不是一回事

`repr=False` 只是不让字段出现在自动生成的 `repr` 中。`debug_context` 仍然可以访问，默认也仍参与相等比较，所以它不能用来保护秘密。

如果缓存等字段不应决定逻辑相等性，可以显式使用 `field(compare=False)`。是否排除应来自对象语义，不能只为得到期望的测试结果。

### frozen 只是浅层禁止重新赋值

```python
@dataclass(frozen=True)
class FrozenPlan:
    name: str
    tags: list[str] = field(default_factory=list)


plan = FrozenPlan("daily", ["agent"])
# plan.name = "weekly"          # FrozenInstanceError
plan.tags.append("search")      # 合法：修改 list，没有重新绑定 plan.tags
```

`frozen=True` 可以先类比为只暴露只读字段的值对象，但它不会递归冻结字段指向的对象。真正要求标签不可变时，应考虑 `tuple[str, ...]`，并继续检查更深层元素是否可变。

参考：Python dataclasses 文档。

## 14.4 BaseModel：先验证边界，再让核心代码相信类型

Pydantic `BaseModel` 也使用类型注解声明字段，但目标不是少写 `__init__()`，而是把不可信输入变成经过验证的对象。

可以先把它类比为 Java 中“Jackson 绑定 DTO + Bean Validation 校验 + OpenAPI Schema 生成”的组合，但不能完全等同：Pydantic 的解析和校验由模型入口统一触发，并且默认允许一部分类型转换。

```python
from pydantic import BaseModel, ConfigDict, Field


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=100)
    limit: int = Field(default=3, strict=True, ge=1, le=5)


args = SearchArgs.model_validate({"query": "Python", "limit": 2})
assert args.query == "Python"
assert args.limit == 2
```

数据流应当是：

```text
不可信 dict / JSON 文本
          │
          │ 解析类型、应用约束、运行 validator
          ▼
    SearchArgs 实例
          │
          ▼
只接收可信模型的核心业务函数
```

本章统一使用 Pydantic v2 的两个显式入口：

```python
# 已经有 Python 对象，通常是 dict
args = SearchArgs.model_validate(python_data)

# 手上还是 str 或 bytes 形式的 JSON
args = SearchArgs.model_validate_json(raw_json)
```

`model_validate_json()` 同时解析 JSON 文本并验证字段。JSON 语法错误、字段类型错误和业务 validator 失败都会以 `ValidationError` 报告。已经有 Python 对象时直接使用 `model_validate()`，不必先序列化再解析。

## 14.5 必填、可空、默认值和未知字段互不等价

Java DTO 常把“字段没传”和“字段值为 null”都表现成 `null`。Pydantic v2 会明确区分这两种输入状态。

| 字段声明 | 键能否缺失 | 值能否为 `None` | 键缺失时的结果 |
| --- | --- | --- | --- |
| `name: str` | 否 | 否 | `missing` 错误 |
| `name: str = "guest"` | 是 | 否 | 使用 `"guest"` |
| `name: str | None` | 否 | 是 | `missing` 错误 |
| `name: str | None = None` | 是 | 是 | 使用 `None` |

`T | None` 只表示合法值集合中包含 `None`，不会自动提供默认值。**可空由类型决定，可缺失由默认值决定。**

未知字段又是独立问题。Pydantic 默认采用 `extra="ignore"`，未声明的键可能被静默丢弃。对 tool 参数来说，这会让拼错的 `limti` 消失，然后悄悄启用 `limit` 的默认值。

```python
class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    limit: int = 3
```

`extra="forbid"` 会把未知键变成 `extra_forbidden` 错误。是否使用它要由协议兼容策略决定：封闭 tool 参数通常适合禁止未知字段，开放元数据容器则可能需要 `ignore` 或 `allow`。

## 14.6 类型转换与 strict：协议接受什么，必须主动决定

Pydantic 默认偏向把“可以理解的输入”转换成目标类型。这对表单和环境变量很方便，但 tool 协议通常需要更精确的边界。

以 `int` 为例：

| 输入 | 默认宽松 `int` | `strict=True` 的 `int` |
| --- | --- | --- |
| `"3"` | 转成 `3` | 拒绝 |
| `True` | 转成 `1` | 拒绝 |
| `None` | 拒绝 | 拒绝 |
| 键缺失 | 无默认值则拒绝 | 无默认值则拒绝 |

Python 中 `bool` 是 `int` 的子类，所以手写代码里的 `isinstance(True, int)` 会得到 `True`。如果业务要求“必须是真正的整数”，应明确写：

```python
limit: int = Field(default=3, strict=True, ge=1, le=5)
```

严格策略可以放在字段、模型配置或一次验证调用上：

```python
from pydantic import BaseModel


class ConversionProbe(BaseModel):
    count: int


ConversionProbe.model_validate({"count": "3"}, strict=True)
```

不要机械地给所有字段开启全局 strict。JSON 没有 `datetime` 原生类型，时间通常需要从 ISO 8601 字符串解析；JSON 本来就有 number 和 boolean，整数和布尔字段则更适合按协议严格检查。Python 对象输入与 JSON 输入在少数严格模式规则上也可能不同，应使用真实边界数据测试。

此外，默认值通常不会因为“模型定义成功”就自动经历与输入相同的验证。模型作者应保证默认值本身正确；确实需要校验默认值时，再显式了解 `validate_default`。

参考：Strict Mode 与 Conversion Table。

## 14.7 嵌套模型：让结构、转换和错误路径一起递归

嵌套对象直接声明为另一个模型类型：

```python
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: Literal["zh", "en"] = "zh"
    published_after: datetime | None = None
    keywords: list[str] = Field(default_factory=list)
```

外层 `SearchArgs.filters` 收到字典后，Pydantic 会递归生成 `SearchFilters` 实例。JSON 中的时间字符串可以被解析成 `datetime`，列表中的每个元素也会按 `str` 检查。失败位置因此可以精确到 `filters.keywords.1`，而不是只剩一句“参数错误”。

这里还有一个容易被忽略的边界：`datetime` 只说明值是日期时间，不自动保证它带时区。当前项目输入使用带 `Z` 的 UTC 时间，因此能与目录中的 UTC 时间比较；生产协议如果禁止无时区时间，还应增加明确约束，例如使用 Pydantic 的 `AwareDatetime` 或自定义验证。

在 Pydantic 模型里，`Literal["zh", "en"]` 不再只是静态提示。运行时验证会拒绝其他值，相应枚举也会进入 JSON Schema。这一点与普通 dataclass 中的 `Literal` 不同。

## 14.8 validator：只表达类型和 Field 覆盖不了的规则

先使用字段类型、`Literal` 和 `Field` 表达机械规则。只有确实需要规范化或跨字段关系时，再编写 validator。

### 字段 validator 处理单个值

`min_length=1` 能拒绝空字符串，却不能拒绝只含空白的字符串。字段 validator 可以先去掉首尾空白，再检查结果：

```python
from pydantic import BaseModel, Field, field_validator


class Query(BaseModel):
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("text 不能只包含空白")
        return normalized
```

默认的字段 validator 在基础类型和 `Field` 约束通过后运行。它的返回值才是后续真正保存的值；忘记 `return` 会隐式返回 `None`。

装饰器在这里不是仅供框架扫描的 Java 注解。Python 会先按照装饰器语义包装函数，Pydantic 再把它登记进模型的验证流程。

### 模型 validator 处理字段之间的不变量

```python
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, model_validator


class SearchWindow(BaseModel):
    published_after: datetime | None = None
    sort: Literal["relevance", "newest"] = "relevance"

    @model_validator(mode="after")
    def require_newest_sort(self) -> Self:
        if self.published_after is not None and self.sort != "newest":
            raise ValueError("指定时间过滤时必须按最新排序")
        return self
```

`mode="after"` 表示字段和嵌套模型准备好后，再检查完整实例。实例方法检查完必须返回要继续使用的模型；没有修改时返回 `self`。

本章 `ToolResult` 还用模型 validator 保证：成功结果不能同时带 `error`，失败结果必须提供错误说明。注意它只保证代码明确写出的关系，并没有自动保证“失败时 items 一定为空”等未声明规则。

自定义 validator 是普通 Python 代码，越多就越需要针对顺序、错误和边界值单独测试。其任意逻辑也未必能完整表达进 JSON Schema。

参考：Pydantic Validators。

## 14.9 ValidationError：保留结构化错误，不要压成一句话

下面的输入同时包含严格整数、枚举、时间、列表元素和未知字段错误：

```json
{
  "query": "Python",
  "limit": true,
  "filters": {
    "language": "fr",
    "published_after": "昨天",
    "keywords": ["agent", 7]
  }
}
```

`ValidationError.errors()` 返回结构化错误列表。每项最常用的字段是：

- `loc`：从根模型走到错误值的路径，如 `("filters", "keywords", 1)`；
- `msg`：适合展示给人的说明；
- `type`：适合程序识别的错误类别，如 `int_type`、`missing`；
- `input`：导致失败的原始值。

可以把位置格式化为：

```text
filters.keywords.1: Input should be a valid string (string_type)
```

API 层可以再把这些细节映射成自己的错误协议，但不要捕获后只返回“参数错误”，也不要把 `str(error)` 当作唯一机器接口。记录 `input` 前要考虑密码、token、个人信息和大文本泄漏。

与常见手写校验相比，Pydantic 还能在一次验证中收集多项独立字段错误；但模型级 validator 是否执行，仍取决于前置字段是否已经成功形成可用模型。

参考：Pydantic Error Handling。

## 14.10 验证发生在何时：不要让可信对象再次失真

`model_validate()`、`model_validate_json()` 和常规模型构造会执行验证。但“曾经验证通过”不代表对象此后永远合法。

Pydantic 模型默认允许属性重新赋值，而且默认不会校验赋入的新值：

```python
args = SearchArgs.model_validate({"query": "Python", "limit": 2})
args.limit = "many"  # 默认不触发赋值校验，静态检查器会报告
```

工程上可以选择：

- 把边界模型当作验证后的快照，不再原地改字段；
- 使用 `model_copy()` 创建新对象，但要了解 `update` 数据默认不会重新验证；
- 确实需要可变且每次赋值都检查时，使用 `ConfigDict(validate_assignment=True)`；
- 需要不可变接口时，再评估 `ConfigDict(frozen=True)` 及其浅层边界。

`model_construct()` 会跳过常规验证，只适合已经完全可信且清楚其语义的高级场景，不能用来接收外部数据。

这与 Java 中“DTO 曾经通过 Bean Validation，后来 setter 又写入非法值”的问题类似：验证保证的是某个时刻和入口，不是对象终身不变量。

## 14.11 序列化：Python 字典、JSON 兼容字典与 JSON 文本

“得到字典”和“得到 JSON 文本”是不同操作。

| 调用 | 返回类型 | `datetime` 等值 | 适合场景 |
| --- | --- | --- | --- |
| `model_dump()` | `dict` | 仍是 Python `datetime` | Python 代码继续计算或断言 |
| `model_dump(mode="json")` | `dict` | 变成 ISO 8601 `str` | 仍想操作字典，但值需兼容 JSON |
| `model_dump_json()` | `str` | 已编码进 JSON 文本 | 直接传输或保存 |

```python
from datetime import datetime

from pydantic import BaseModel


class Event(BaseModel):
    happened_at: datetime


event = Event.model_validate({"happened_at": "2026-01-01T00:00:00Z"})
python_data = event.model_dump()
json_ready = event.model_dump(mode="json")
json_text = event.model_dump_json()

assert isinstance(python_data["happened_at"], datetime)
assert json_ready["happened_at"] == "2026-01-01T00:00:00Z"
assert isinstance(json_text, str)
```

直接把包含 `datetime` 的 `model_dump()` 结果交给标准库 `json.dumps()`，通常会得到 `TypeError`。方法名里有 dump 不代表所有嵌套值都已经 JSON 化。

本章为工具输出单独定义 `SearchItem` 和 `ToolResult`。这使“本地工具 → Agent 循环”也成为受控边界：嵌套结果先形成一致结构，再通过 `model_dump_json()` 递归编码。

序列化只改变表示，不会重新证明现实事实。格式正确的 URL 可能打不开，合法时间也可能是上游捏造的。

参考：Pydantic Serialization。

## 14.12 JSON Schema：描述 tool 参数，不替代服务端验证

```python
schema = SearchArgs.model_json_schema()
```

返回值是可 JSON 化的 Python `dict`。本章模型生成的 schema 包含：

- 顶层 object、properties 和必填字段；
- `query` 的长度与说明；
- `limit` 的默认值、整数类型和 1..5 上下界；
- `sort`、`language` 的枚举；
- `SearchFilters` 的嵌套定义；
- `additionalProperties: false` 表达的未知字段禁止策略。

这些信息可以放入支持 JSON Schema 的 tool/function 参数描述，让模型知道应该生成什么形状。它相当于给调用者协议说明，而不是给服务端免检通行证。

Schema 不能保证：

1. LLM 一定生成符合 schema 的参数；
2. 未写入模型的业务规则自动成立；
3. 任意 Python validator 都能完整转换成声明式 schema；
4. URL、标题、时间和数字对应真实、最新且有权限的数据；
5. 数据库唯一性、库存、鉴权和其他外部状态成立。

因此，服务端仍必须执行 Pydantic 验证。结构校验只是业务正确性的第一层，不是全部。

参考：Pydantic JSON Schema。

## 14.13 把四种结构放进一条 tool 数据流

本章示例把职责拆成三个核心对象：

- `SearchArgs(BaseModel)`：接收不可信工具参数，禁止未知字段，严格校验整数和布尔值；
- `ToolResult(BaseModel)`：约束工具输出，递归包含 `SearchItem`，并检查 `ok` 与 `error` 的关系；
- `RunState(dataclass)`：保存当前进程自己维护的运行阶段和事件，不验证外部输入。

完整数据流如下：

```text
模型生成的 JSON 文本
          │
          │ SearchArgs.model_validate_json()
          ▼
   SearchArgs 实例 ──────────────► local_search()
          │                            │
          │ 更新可信内部状态            ▼
          └──────► RunState        ToolResult
                                       │
                                       │ model_dump_json()
                                       ▼
                               可传输的 JSON 文本
```

输入中的 `filters` 最初是字典，`published_after` 最初是字符串。验证成功后：

- `args.filters` 是 `SearchFilters`；
- `published_after` 是 `datetime`；
- `query` 的首尾空白已被字段 validator 去掉；
- `limit`、布尔值、枚举、未知字段和跨字段关系都已经检查；
- 核心函数不再重复解析原始字典键。

示例中的 `local_search()` 是确定性的本地替身，只读取两个固定条目，不联网、不需要 API Key。它只实际使用时间过滤、条数和是否包含摘要；`language` 与 `keywords` 用来演示嵌套验证，并没有实现真实搜索语义。

这个替身能证明本章的数据流、校验、状态更新和序列化，但不能证明真实 HTTP 场景中的 DNS、超时、状态码、限流、重试、取消、鉴权和内容准确性。

### 与第 10 章手写校验的迁移对照

示例还复刻了第 10 章的一组用户规则，验证迁移到模型后没有悄悄放宽协议。

| 手写规则 | Pydantic 声明 |
| --- | --- |
| `id` 必须是真正的正整数 | `Field(strict=True, gt=0)` |
| `age` 必须是 0..150 的真正整数 | `Field(strict=True, ge=0, le=150)` |
| `active` 必须是真正的 `bool` | `bool` 字段使用 `Field(strict=True)` |
| 字段集合必须精确匹配 | 必填字段 + `ConfigDict(extra="forbid")` |
| `name` 不能只含空白 | 长度约束 + `field_validator` |

Pydantic 的优势是声明集中、嵌套路径统一、schema 和序列化可以复用。手写校验在规则极少或协议高度特殊时仍然合理。迁移的关键不是“换了更高级的库”，而是保持原业务接受和拒绝的集合不变。

## 14.14 Java 工程师最容易误判的地方

1. **把注解当运行时约束。**普通函数、`TypedDict` 和 dataclass 的类型注解主要服务于静态检查；外部数据必须真正走验证入口。
2. **把 `T | None` 当成可缺失。**没有默认值时仍然必填。
3. **忘记 Pydantic 默认忽略未知字段。**封闭协议应明确考虑 `extra="forbid"`。
4. **把自动转换当成总是友好。**`"3"` 和 `True` 被转成整数可能改变协议语义。
5. **给所有字段一刀切 strict。**时间等 JSON 不原生支持的类型本来就需要解析。
6. **忘记 validator 必须返回值。**字段 validator 返回要保存的值，after 模型 validator 返回模型。
7. **把验证过一次理解成永久合法。**默认属性赋值和某些高级构造/复制入口可能绕开再次验证。
8. **混淆三种 dump 结果。**`model_dump()` 的字典可能仍含 Python 专用对象。
9. **把 frozen 当深度不可变。**它不递归冻结 `list`、`dict` 等子对象。
10. **把 schema 当安全证明。**它既不执行服务端校验，也不验证权限、数据库状态和现实事实。

## 14.15 运行本章示例

本章包含三个文件：

- `README.md`：独立教程、选择标准、边界与练习；
- `example.py`：五组确定性实验和完整本地 tool 数据流；
- `requirements.txt`：Pydantic v2 运行依赖。

课程原仓库要求 Python >=3.14。这个独立还原目录中可直接安装本章依赖：

```bash
python -m pip install -r requirements.txt
```

再运行示例：

```bash
python -B example.py
```

`-B` 只是不生成 `__pycache__`，不改变程序语义。原仓库现有的 `run_all.py` 只覆盖前 10 章，因此本章使用独立命令。

建议按五段输出观察：

1. `TypedDict` 的运行时类型仍是 `dict`，dataclass 自动生成显示和比较行为；
2. 缺失、`None`、未知字段、宽松转换和严格模式得到不同结果；
3. 手写校验与 Pydantic 对合法/非法用户记录做出相同决定；
4. Python 模式字典、JSON 模式字典和 JSON 文本中的时间类型不同；
5. 完整 pipeline 更新 `RunState`，输出 `ToolResult`，并打印 schema 摘要与嵌套错误路径。

静态检查命令：

```bash
ruff check .
ruff format --check .
pyright example.py
```

原课程在 CPython 3.14.7、Pydantic 2.13.5、Ruff 0.16.5 和 Pyright 1.1.411 下运行并检查。版本约束以课程仓库的 `pyproject.toml` 和本章 `requirements.txt` 为准。

## 14.16 递进练习

### 练习 1：扩展内部状态

给 `RunState` 增加 `warnings: list[str]` 和不进入 `repr` 的 `trace_context`。创建两个实例，只修改第一个，验证列表没有共享、`repr` 没有泄漏上下文，并解释为什么这里使用 dataclass。

### 练习 2：画出存在性与严格模式矩阵

给 `SearchArgs` 增加一个注释字段，分别尝试“必填可空”和“可缺失可空”。再测试 `limit` 的 `"3"`、`True`、`None` 和缺失，记录每份输入的接受或拒绝结果以及错误类型。

### 练习 3：约束嵌套域名

给 `SearchFilters` 增加 `domains: list[str]`。去除每项首尾空白并拒绝空项，确认错误路径能定位到 `filters.domains.1`，同时观察生成的 schema。

### 练习 4：补齐失败结果

让 `local_search()` 在查询词为 `"offline"` 时返回结构合法的失败 `ToolResult`。验证正常结果和受控失败都能序列化，而 `ok=False, error=None` 会被模型 validator 拒绝。

### 练习 5：恢复批量唯一性规则

为 `list[UserModel]` 设计批模型，增加“用户 ID 不重复”的集合级验证。验证重复 ID 得到明确错误，`id=True` 先被严格整数规则拒绝，并解释为什么 JSON Schema 不能证明数据库中的全局唯一性。

## 14.17 本章小结

- `dict` 适合真正动态的键；`TypedDict` 为可信字典增加静态形状，不增加运行时验证。
- dataclass 适合可信内部对象，自动生成样板方法，但类型注解不负责运行时检查。
- `BaseModel` 适合 I/O 边界，负责解析、约束、错误定位、序列化与 schema。
- 必填、可空、默认值和未知字段是独立维度；宽松转换与 strict 是协议决策。
- 嵌套模型让数据转换和错误路径一起递归；validator 只补充声明式字段规则无法表达的逻辑。
- 验证只对实际经过的入口和时刻负责；赋值、绕过验证的 API、外部状态和现实事实仍需单独治理。
- JSON Schema 能指导 tool 调用，但服务端必须继续验证，业务层仍要检查权限、唯一性和真实性。
