"""第 14 章：为 Agent 的工具输入、输出和内部状态选择数据模型。"""

# 阅读顺序：先看四类数据结构的声明，再看中间的校验/搜索函数，最后从 main() 进入五组实验。
# 完整数据流是：JSON 文本 -> SearchArgs 边界校验 -> 本地工具 -> ToolResult -> JSON 文本；
# RunState 则只记录进程内部状态，不负责校验外部输入。
#
# 标准库分别负责 JSON 文本、轻量数据类、带时区时间与静态类型描述。
import json
from dataclasses import FrozenInstanceError, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any, Literal, Self, TypedDict

# Pydantic 是本章的运行时边界工具：BaseModel 声明模型，Field 补充字段规则，
# validator 表达普通字段类型无法覆盖的规则，ValidationError 汇总失败细节。
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


def print_section(title: str) -> None:
    """让终端输出能和 README 的实验小节逐段对应。"""
    # f-string 会把 title 当前指向的字符串嵌入结果；开头的 \n 先留一行，分隔相邻实验。
    print(f"\n=== {title} ===")


# TypedDict 只为类型检查器描述“这个 dict 应有哪些键及值类型”，不会创建新的容器类，
# 也不会在运行时检查数据。后面的 summary 仍由普通字典字面量构造并按字典协议使用。
class ToolSummary(TypedDict):
    """描述已经在程序内部形成、仍以 dict 传递的固定结构。"""

    # 三个键都没有使用 NotRequired，因此静态约定要求它们同时存在。
    request_id: str
    item_count: int
    cached: bool


# @dataclass 会在执行类定义时读取这些带注解字段，并生成 __init__、__repr__ 和 __eq__。
# 可以类比减少 Java POJO 的样板代码，但字段注解仍不会自动变成运行时类型检查。
@dataclass
class RunState:
    """进程内部的轻量可变状态：这里不承担不可信输入的验证。"""

    # 无默认值的字段是必传初始化参数，并且必须放在下面有默认值的字段之前。
    run_id: str
    # Literal 把静态允许值收窄为三个字符串；普通 dataclass 运行时不会据此拒绝其他值。
    phase: Literal["created", "validated", "completed"] = "created"
    attempts: int = 0
    # default_factory 保存 list 这个“创建函数”；每次缺省构造 RunState 时都会调用一次 list()。
    # 因而每个实例都拿到独立列表；直接写 [] 会表达共享可变默认值，并被 dataclass 拒绝。
    events: list[str] = field(default_factory=list)
    # repr=False 只把调试上下文从自动 __repr__ 隐藏；它仍能访问，也仍参与默认的相等比较。
    # 这只是减少日志噪声，并不是秘密字段的安全保护。
    debug_context: dict[str, str] = field(default_factory=dict, repr=False)


# frozen=True 让 dataclass 生成阻止字段重新绑定/删除的行为；下面会分别尝试“重新绑定 name”
# 与“修改 tags 指向的 list”，借此观察冻结只作用于实例属性这一层。
@dataclass(frozen=True)
class FrozenPlan:
    """frozen 只阻止字段重新绑定，不会递归冻结字段指向的对象。"""

    name: str
    tags: list[str] = field(default_factory=list)


# BaseModel 与 dataclass 的关键区别是：构造/验证时会真正执行运行时解析和约束。
# 这个小模型刻意把“必填”“有默认值”“允许 None”“禁止未知键”拆开观察。
class PresenceModel(BaseModel):
    """只用于观察必填、默认值、可空和未知字段四条独立规则。"""

    # extra="forbid" 表示输入中只要出现未声明的键就报错，避免拼错键后被静默忽略。
    model_config = ConfigDict(extra="forbid")

    # 没有 = 默认值，所以 required_name 与 required_nullable 都必须在输入里出现。
    required_name: str
    # “str | None”只把 None 加入允许值集合，不代表这个键可以缺失。
    required_nullable: str | None
    # 既允许 None 又以 None 为默认值：键可以缺失，缺失时保存 None。
    optional_nullable: str | None = None
    # 类型不含 None，但有默认值：键可缺失，显式传 None 仍会失败。
    retry_count: int = 3


# 此处不设置模型级 strict，便于稍后把同一份输入分别交给宽松模式和 strict=True。
class ConversionProbe(BaseModel):
    """同一字段分别以默认宽松模式和调用级 strict 模式验证。"""

    count: int


# 该模型把第 10 章的手写用户规则声明为 Pydantic 约束，后面的成对实验会验证：
# 换成模型之后，bool 冒充 int、数字字符串转换和额外字段仍然都不会被放行。
class UserModel(BaseModel):
    """保留第 10 章手写规则中的严格整数与未知字段语义。"""

    model_config = ConfigDict(extra="forbid")

    # strict=True 禁止 Pydantic 把 True 或字符串转换成整数；gt/ge/le 再限制数值范围。
    id: int = Field(strict=True, gt=0)
    # min_length=1 能拒绝空串，却不能拒绝 "   "，所以还需要下面的字段验证器。
    name: str = Field(strict=True, min_length=1)
    age: int = Field(strict=True, ge=0, le=150)
    # bool 也使用 strict=True，明确拒绝 0、1、"true" 等可转换但协议不接受的输入。
    active: bool = Field(strict=True)
    note: str = Field(strict=True)

    # 装饰器从紧邻函数的 @classmethod 开始向上应用：先把函数包装为类方法，
    # 再由 field_validator 把它登记到 name 字段的验证流程中。这不是仅供阅读的 Java 注解。
    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        """长度约束拦不住纯空白文本，所以补上这条业务规则。"""
        # strip() 产生移除首尾空白的新字符串；这里只用它判断，不改变最终保存的原始 name。
        if not value.strip():
            raise ValueError("name 不能只包含空白")
        # validator 的返回值才会继续流入模型；忘记 return 会隐式返回 None。
        return value


# SearchFilters 会作为 SearchArgs.filters 的字段类型。外层收到嵌套 dict 后，Pydantic 会
# 递归验证并把它转换成 SearchFilters 实例，错误位置也会保留 filters 这一层路径。
class SearchFilters(BaseModel):
    """嵌套筛选条件也拥有自己的未知字段策略和约束。"""

    model_config = ConfigDict(extra="forbid")

    # Literal 在 Pydantic 中既参与运行时校验，也会进入生成的 JSON Schema 枚举信息。
    language: Literal["zh", "en"] = "zh"
    # Python 输入可直接给 datetime；JSON 没有时间类型，因此 ISO 8601 字符串会在边界解析。
    published_after: datetime | None = None
    # 每次缺失 keywords 时创建新列表，避免不同模型实例共享后续可变状态。
    keywords: list[str] = Field(default_factory=list)


# SearchArgs 位于 Agent/tool 的不可信输入边界：类型、范围、未知字段和跨字段关系都在这里集中。
class SearchArgs(BaseModel):
    """处在 Agent/tool 边界上的输入模型。"""

    model_config = ConfigDict(extra="forbid")

    # Field 给 str 增加长度、描述等约束；description 主要供生成 schema 和文档使用。
    query: str = Field(
        min_length=1,
        max_length=100,
        description="要检索的非空查询词；首尾空白会被移除",
    )
    # 工具明确只接受真正的 JSON 整数，不能让 "3" 或 true 偷偷变成 3/1；
    # default=3 只处理键缺失，ge/le 让合法范围闭合在 1..5。
    limit: int = Field(default=3, strict=True, ge=1, le=5, description="最多返回几条结果")
    # JSON 自带 boolean 类型，因此这里要求调用者真的发送 true/false，不接受常见替代写法。
    include_snippets: bool = Field(
        default=True,
        strict=True,
        description="是否在结果中包含摘要",
    )
    sort: Literal["relevance", "newest"] = "relevance"
    # default_factory=SearchFilters 表示 filters 缺失时现场构造一个带自身默认值的嵌套模型。
    filters: SearchFilters = Field(default_factory=SearchFilters)

    # 默认字段验证器在 query 的基础类型和 Field 约束通过后运行，用返回值完成规范化。
    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        """字段验证器接收当前字段值，并必须返回后续真正保存的值。"""
        # str 不可变，strip() 返回新对象；原始输入字典/JSON 文本不会被原地修改。
        normalized: str = value.strip()
        # 长度约束已排除 ""，这里继续排除 strip 后才变空的纯空白字符串。
        if not normalized:
            raise ValueError("query 不能只包含空白")
        return normalized

    # mode="after" 表示所有字段及嵌套 filters 都准备好后，再在完整实例上检查字段关系。
    @model_validator(mode="after")
    def published_filter_requires_newest_sort(self) -> Self:
        """模型验证器在字段就绪后检查跨字段业务不变量。"""
        # is not None 用来区分“确实指定了发布时间”与默认的“未启用时间过滤”。
        if self.filters.published_after is not None and self.sort != "newest":
            raise ValueError("指定 published_after 时 sort 必须是 'newest'")
        # after 模式必须返回要继续使用的模型；这里未改实例，所以原样返回 self。
        return self


# 工具输出也要建模：每个目录条目先成为 SearchItem，ToolResult 再以它作为嵌套元素类型。
class SearchItem(BaseModel):
    """工具结果中的一条嵌套记录。"""

    model_config = ConfigDict(extra="forbid")

    title: str
    url: str
    published_at: datetime
    # 有 = None 才表示字段既可显式为 None，也可在输入中完全缺失。
    snippet: str | None = None


# ToolResult 是本地工具到 Agent 循环的输出边界。模型校验让后面的序列化始终面对一致结构。
class ToolResult(BaseModel):
    """可安全序列化回 Agent 循环的工具结果。"""

    model_config = ConfigDict(extra="forbid")

    # Literal 固定工具名；调用者不传时取 "search"，传其他名字则失败。
    tool: Literal["search"] = "search"
    ok: bool = Field(strict=True)
    # 省略 items 时为当前结果创建独立空列表；传入字典元素时也会递归生成 SearchItem。
    items: list[SearchItem] = Field(default_factory=list)
    error: str | None = None

    # 单看 ok: bool 和 error: str | None 无法表达两者必须配套，因此使用模型级验证器。
    @model_validator(mode="after")
    def success_and_error_must_agree(self) -> Self:
        """类型本身表达不了 ok 与 error 的对应关系，必须显式检查。"""
        if self.ok and self.error is not None:
            raise ValueError("成功结果不能同时包含 error")
        if not self.ok and not self.error:
            raise ValueError("失败结果必须说明 error")
        return self


# 这个常量目录在导入/运行本模块、执行到此处时立即构造；它不是每次搜索才联网获取的数据。
# 两个 published_at 都带 UTC 时区，之后才能与解析出的带时区筛选时间安全比较。
CATALOG: list[SearchItem] = [
    SearchItem(
        title="Python 数据模型实践",
        url="https://local.example/python-data-models",
        published_at=datetime(2026, 8, 20, 9, 30, tzinfo=UTC),
        snippet="用结构化边界保护工具调用。",
    ),
    SearchItem(
        title="Agent 工具参数设计",
        url="https://local.example/agent-tool-args",
        published_at=datetime(2026, 7, 15, 8, 0, tzinfo=UTC),
        snippet="从 JSON Schema 到运行时校验。",
    ),
]

# 元组不可变，适合保存手写协议要求的精确键集合；下面会转为 set 做集合差运算。
USER_FIELDS = ("id", "name", "age", "active", "note")


def format_validation_error(error: ValidationError) -> list[str]:
    """保留每条错误的嵌套路径和类型，而不是压成一句“参数错误”。"""
    # 用 list 收集全部错误，而不是遇到第一项就抛弃后续细节；调用者可选择逐行打印或拼接。
    lines: list[str] = []
    # errors() 返回由 dict 组成的列表；include_url=False 只省略文档链接，不影响路径和类型。
    for detail in error.errors(include_url=False):
        # loc 是从根模型走到坏值的路径元组，例如 ("filters", "keywords", 1)。
        # 生成器逐个把字段名/下标转成 str，再由 ".".join(...) 得到 filters.keywords.1。
        # 模型整体 validator 的 loc 可能为空，此时 or 会选右侧的可读占位符。
        location: str = ".".join(str(part) for part in detail["loc"]) or "<model>"
        # msg 面向读者，type 更适合作为程序识别的错误类别；这里同时保留两者。
        lines.append(f"{location}: {detail['msg']} ({detail['type']})")
    return lines


def manual_validate_user(data: object) -> dict[str, object]:
    """第 10 章规则的最小只读复刻，用于和模型校验对照。"""
    # 参数先写成最宽的 object，因为边界数据可能根本不是字典；先验证容器形状再取键。
    if not isinstance(data, dict):
        raise TypeError("用户必须是对象")

    # set 只关心成员，不关心顺序。两边集合必须完全相等，才能同时排除缺键和额外键。
    expected_fields: set[Literal["id", "name", "age", "active", "note"]] = set(USER_FIELDS)
    actual_fields: set[Any] = set(data)
    if actual_fields != expected_fields:
        # 集合差分别算出缺少/多出的键；sorted 让错误输出不受 set 遍历顺序影响。
        missing: list[Literal["id", "name", "age", "active", "note"]] = sorted(
            expected_fields - actual_fields
        )
        extra: list[Any] = sorted(actual_fields - expected_fields)
        raise ValueError(f"字段不符: missing={missing}, extra={extra}")

    # 只有字段集合确认无误后才逐个读取，所以这些 [] 访问不会因缺键抛出 KeyError。
    # 变量此时仍指向输入字典中的原对象，下一段再逐项检查类型和值域。
    user_id: Any = data["id"]
    name: Any = data["name"]
    age: Any = data["age"]
    active: Any = data["active"]
    note: Any = data["note"]

    # 这里刻意用 type(...) is int，而不是 isinstance(..., int)：bool 是 int 的子类，
    # isinstance(True, int) 会得到 True，但业务上布尔值不能充当用户 ID 或年龄。
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("id 必须是正整数")
    # 短路 or 保证只有 name 已经是 str 时才调用 strip()，避免错误类型再触发属性异常。
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name 必须是非空字符串")
    # 0 <= age <= 150 是 Python 链式比较，相当于同时满足 0 <= age 和 age <= 150。
    if type(age) is not int or not 0 <= age <= 150:
        raise ValueError("age 必须是 0 到 150 的整数")
    if type(active) is not bool:
        raise ValueError("active 必须是布尔值")
    if not isinstance(note, str):
        raise TypeError("note 必须是字符串")

    # 校验成功后返回一个新 dict，并按协议顺序显式放入字段；不会原地修改调用者的 data。
    return {"id": user_id, "name": name, "age": age, "active": active, "note": note}


def local_search(args: SearchArgs) -> ToolResult:
    """确定性的本地替身：演示数据流，不声称模拟真实搜索质量与网络故障。"""
    # 初始时 candidates 与 CATALOG 指向同一个 list；这里只读取它，不会修改目录常量。
    candidates: list[SearchItem] = CATALOG
    # 只有调用者真正提供了发布时间（解析结果非 None）才启用过滤。
    if args.filters.published_after is not None:
        # 列表推导式创建新 list：逐个保留发布时间大于等于下限的 SearchItem。
        # 本讲输入里的时间带 Z，解析后与目录的 UTC 时间都含时区，因此可以比较实际先后；
        # 字段类型本身没有强制时区，生产协议若允许其他格式，还需另行约束这一点。
        candidates = [
            item
            for item in candidates
            if item.published_at >= args.filters.published_after
        ]

    # 输出列表单独创建，稍后根据 limit 截取候选项，并按 include_snippets 决定元素表示。
    selected: list[SearchItem] = []
    # candidates[: args.limit] 返回至多 limit 个元素的新列表；目录不足时不会越界报错。
    for item in candidates[: args.limit]:
        if args.include_snippets:
            # 需要摘要时可直接收集已经过 SearchItem 校验的目录元素。
            selected.append(item)
        else:
            # 不需要摘要时重新构造一个 SearchItem，把 snippet 明确设为 None；
            # 重新构造会再次经过模型验证，也不会原地改坏 CATALOG 中的共享对象。
            selected.append(
                SearchItem(
                    title=item.title,
                    url=item.url,
                    published_at=item.published_at,
                    snippet=None,
                )
            )
    # ToolResult 会检查 ok/items/error 的类型以及成功状态的不变量，再交给调用方。
    return ToolResult(ok=True, items=selected)


def run_search(raw_json: str, state: RunState) -> ToolResult:
    """走通原始 JSON → 边界校验 → 工具 → 结构化结果。"""
    # raw_json 仍是 str。model_validate_json 同时解析 JSON 并执行嵌套模型、Field 和 validator；
    # 如果失败会直接抛 ValidationError，所以下面的内部状态不会被误记为 validated。
    args: SearchArgs = SearchArgs.model_validate_json(raw_json)
    # dataclass 是可信的内部可变状态，这里用属性赋值和 list.append 原地记录阶段与事件。
    state.phase = "validated"
    state.events.append(f"validated:{args.query}")

    # local_search 只接收已经通过边界校验的 SearchArgs，而不是继续操作原始字符串/字典。
    result: ToolResult = local_search(args)
    state.phase = "completed"
    # len(result.items) 读取结构化结果的条目数，再由 f-string 形成可追踪的事件文本。
    state.events.append(f"completed:{len(result.items)}")
    return result


def demonstrate_data_structure_choices() -> None:
    """观察 TypedDict 的运行时本质，以及 dataclass 自动生成的常用行为。"""
    print_section("1. TypedDict 与 dataclass 各自负责什么")

    # 变量注解让 Pyright 知道固定键结构；等号右侧仍是普通 dict 字面量，没有运行时构造器。
    summary: ToolSummary = {"request_id": "req-001", "item_count": 2, "cached": False}
    # type(...).__name__ 取得实际类名，预期打印 dict，从运行时角度印证 TypedDict 的定位。
    print("TypedDict 值的运行时类型:", type(summary).__name__)

    # 三次构造都调用 dataclass 自动生成的 __init__。前两个字段值相同，用于观察 __eq__；
    # second 则用于确认 events 的默认列表是否真的按实例隔离。
    first = RunState(run_id="run-001", debug_context={"token": "不进入 repr"})
    same_values = RunState(run_id="run-001", debug_context={"token": "不进入 repr"})
    second = RunState(run_id="run-002")

    # fields(RunState) 返回字段描述对象；列表推导式依次提取 name，证明隐藏字段仍是正式字段。
    print("dataclass 字段:", [item.name for item in fields(RunState)])
    # print 会调用自动 __repr__；debug_context 因 repr=False 不显示，但依然参与下一行 eq 比较。
    print("自动 repr 隐藏 debug_context:", first)
    print("自动 eq 按字段值比较:", first == same_values)
    # append 原地修改 first 的列表。随后同时打印两份列表和 is 身份比较：
    # first 有事件、second 仍为空且 is 为 False，三项共同证明没有共享默认列表。
    first.events.append("created")
    print(
        "default_factory 没有共享列表:", first.events, second.events, first.events is second.events
    )

    # 这个忽略标记只服务于教学反例：类型检查器会报错，但运行时 dataclass 仍照单全收。
    # 必须原样保留 pyright 指令；它不改变 Python 执行，只抑制这一行预期中的静态诊断。
    invalid_state = RunState(run_id=123)  # pyright: ignore[reportArgumentType]
    # 同时观察保存的值和实际类型，预期是 123 与 int，而不是被注解自动转成 str。
    print("注解没有触发运行时校验:", invalid_state.run_id, type(invalid_state.run_id).__name__)

    # name 是冻结实例上的属性，tags 则指向一个仍可变的 list；下面分别测试两层行为。
    plan = FrozenPlan(name="daily-search", tags=["agent"])
    try:
        # 对冻结字段重新绑定会抛 FrozenInstanceError；忽略指令保留这个故意的类型错误示例。
        plan.name = "changed"  # pyright: ignore[reportAttributeAccessIssue]
    except FrozenInstanceError:
        # 捕获预期异常后继续实验，不让整个课程脚本在这里中断。
        print("frozen 拒绝字段重新赋值: FrozenInstanceError")
    # append 修改的是 tags 指向的嵌套 list 内容，没有执行 plan.tags = ...，所以仍然合法。
    plan.tags.append("mutable-child")
    print("frozen 内的 list 仍能修改:", plan.tags)


def demonstrate_presence_and_strictness() -> None:
    """用 None、缺失、未知字段、字符串和 bool 观察不同规则。"""
    print_section("2. 必填/默认/可空，以及宽松/严格转换")

    # required_nullable 虽然必填，但是式 None 属于它的允许值；另外两个缺失字段使用默认值。
    present_none: PresenceModel = PresenceModel.model_validate(
        {"required_name": "demo", "required_nullable": None}
    )
    # model_dump() 返回普通 Python dict，便于同时观察 None、None 默认值和整数默认值。
    print("显式 None 可通过可空字段，默认值已补入:", present_none.model_dump())

    # 每个场景是（展示标签，要验证的 Python 字典），用同一循环对比两种输入错误。
    presence_cases: list[tuple[str, dict[str, object]]] = [
        # 这里是真的没有 required_nullable 键，与上面的显式 None 是两种不同输入状态。
        ("缺少仍然必填的可空字段", {"required_name": "demo"}),
        (
            "出现未知字段",
            # 两个必填字段都存在，单独加入 unexpected，以确认 extra="forbid" 生效。
            {"required_name": "demo", "required_nullable": None, "unexpected": "x"},
        ),
    ]
    # for 会把每个二元组按位置解包：label 得到说明，payload 得到当前字典。
    for label, payload in presence_cases:
        try:
            PresenceModel.model_validate(payload)
        except ValidationError as error:
            # 这些都是预期反例；格式化并打印后继续下一轮，而不是终止整组实验。
            print(label + ":", " | ".join(format_validation_error(error)))

    # 四份输入覆盖“可转换字符串、bool、显式 None、缺键”，稍后每份都验证两次。
    probe_cases: list[tuple[str, dict[str, object]]] = [
        ('"3"', {"count": "3"}),
        ("True", {"count": True}),
        ("None", {"count": None}),
        ("缺失", {}),
    ]
    for label, payload in probe_cases:
        # observations 只属于当前输入；收集宽松/严格两种结果后再并排打印。
        observations: list[str] = []
        # 内层元组序列固定提供（展示名，strict 参数）；再次用解包得到两个局部变量。
        for mode, strict in (("宽松", False), ("严格", True)):
            try:
                # 调用级 strict 覆盖本次验证行为，但不修改 ConversionProbe 的类定义。
                probe: ConversionProbe = ConversionProbe.model_validate(payload, strict=strict)
                # !r 使用 repr 展示值，可清楚区分整数 3 与字符串 '3'；同时打印实际类型。
                observations.append(f"{mode}=通过({probe.count!r}, {type(probe.count).__name__})")
            except ValidationError as error:
                # 模型只有 count 一个字段，因此取第一项即可展示拒绝类别。
                error_type: str = error.errors(include_url=False)[0]["type"]
                observations.append(f"{mode}=拒绝({error_type})")
        # "; ".join(...) 把本轮两条观察连接成一个字符串，便于横向比较模式差异。
        print(f"输入 {label}:", "; ".join(observations))


def demonstrate_manual_and_model_validation() -> None:
    """证明改用模型后没有悄悄放宽第 10 章的边界规则。"""
    print_section("3. 手写校验与 Pydantic 校验保持同一业务语义")

    # 基准记录故意标为 dict[str, object]：键是字符串，但各字段值的具体类型不同。
    valid_user: dict[str, object] = {
        "id": 1,
        "name": "Alice",
        "age": 28,
        "active": True,
        "note": "普通字段",
    }
    # 每个测试场景仍是（标签，payload）二元组。字典的 | 运算会创建新字典：
    # 右侧同名键覆盖左侧，未覆盖字段沿用 valid_user，且原基准字典不会被修改。
    cases: list[Any] = [
        ("合法记录", valid_user),
        # True 会覆盖 id，用来确认两套校验都识别 Python 中 bool 是 int 子类这个陷阱。
        ("bool 冒充 id", valid_user | {"id": True}),
        ("字符串冒充 age", valid_user | {"age": "28"}),
        # role 没有覆盖旧键，而是形成第六个键，用来对照精确字段集合/extra="forbid"。
        ("未知字段", valid_user | {"role": "admin"}),
    ]

    # 每轮把同一 payload 依次交给手写函数和 Pydantic，两个结果仅转成展示文字，
    # 从而可以检查迁移实现后“接受/拒绝”的业务决定是否一致。
    for label, payload in cases:
        try:
            manual_validate_user(payload)
            manual_result = "通过"
        except (TypeError, ValueError) as error:
            # 手写实现按规则主动抛这两类异常；圆括号表示捕获其中任意一种。
            manual_result: str = f"拒绝({error})"

        try:
            # model_validate 接收已经存在的 Python dict；这里不需要先转成 JSON 文本。
            UserModel.model_validate(payload)
            model_result = "通过"
        except ValidationError as error:
            # Pydantic 把字段/模型失败统一汇总为 ValidationError，保留路径后再拼成一行。
            model_result: str = "拒绝(" + " | ".join(format_validation_error(error)) + ")"
        # 本轮两套校验都完成后才并排打印；一个反例失败不会阻止后续场景继续运行。
        print(f"{label}: 手写={manual_result}; 模型={model_result}")


def demonstrate_serialization() -> None:
    """对比 Python 模式字典、JSON 模式字典和 JSON 文本。"""
    print_section("4. model_dump 的 Python/JSON 模式与 model_dump_json")

    # model_validate 接收 Python dict。嵌套 filters 也先是 dict，其中时间还是字符串；
    # 成功后 args 是 SearchArgs，args.filters 是 SearchFilters，时间字段已解析成 datetime。
    args: SearchArgs = SearchArgs.model_validate(
        {
            # query 首尾有空格，方便确认字段 validator 的返回值才是模型最终保存的内容。
            "query": " Python Agent ",
            # 指定 published_after 会触发跨字段规则，因此 sort 必须同时为 newest。
            "sort": "newest",
            "filters": {"published_after": "2026-01-01T00:00:00Z"},
        }
    )
    # 三种 API 的输出层级相同，但容器/时间值所处的表示阶段不同：
    # 1) Python 模式是 dict，datetime 仍可供 Python 计算；
    # 2) JSON 模式仍是 dict，但嵌套时间已变成 JSON 兼容 str；
    # 3) model_dump_json 直接返回完成编码的 JSON str。
    python_dump: dict[str, Any] = args.model_dump()
    json_mode_dump: dict[str, Any] = args.model_dump(mode="json")
    json_text: str = args.model_dump_json()

    # 连续 [] 先从外层 dict 取得 filters 子字典，再取得其中的 published_after。
    python_time: Any = python_dump["filters"]["published_after"]
    json_time: Any = json_mode_dump["filters"]["published_after"]
    # repr 会把字符串边界显示出来，因此能直接看到 query 首尾空白已被移除。
    print("validator 返回值真正保存，query:", repr(args.query))
    # 这三行同时输出实际类型和值，避免把“Python 字典”和“JSON 文本”混为一谈。
    print("Python 模式时间:", type(python_time).__name__, python_time)
    print("JSON 模式字典时间:", type(json_time).__name__, json_time)
    print("model_dump_json 返回:", type(json_text).__name__, json_text)


def demonstrate_pipeline_schema_and_errors() -> None:
    """运行完整工具数据流，并展示 schema 摘要与嵌套错误路径。"""
    print_section("5. 原始 JSON → 校验模型 → 工具 → 可序列化结果")

    # 先用普通 Python dict 组织示例输入，再由 json.dumps 编码成真正的 JSON str，
    # 模拟 Agent/tool 框架传来的原始参数文本，而不是直接把可信字典交给模型。
    raw_json: str = json.dumps(
        {
            "query": "Python",
            "limit": 2,
            "include_snippets": True,
            "sort": "newest",
            "filters": {
                "language": "zh",
                "published_after": "2026-01-01T00:00:00Z",
                "keywords": ["agent", "tool"],
            },
        },
        # 保留中文文本身，不转义为 \uXXXX；这只影响文本表现，不影响解码后的值。
        ensure_ascii=False,
    )
    # 内部状态在边界输入之外单独创建；run_search 会按 validated -> completed 更新它。
    state = RunState(run_id="run-pipeline")
    result: ToolResult = run_search(raw_json, state)
    # state 展示内部副作用，result 则是已经校验的工具输出模型。
    print("内部状态:", state.phase, state.events)
    print("可发送的 JSON 结果:")
    # indent=2 只为终端可读性缩进；返回值仍是可传输的 JSON 字符串。
    print(result.model_dump_json(indent=2))

    # model_json_schema() 返回普通 Python dict，用声明描述调用者应生成的 tool 参数形状。
    schema: dict[str, Any] = SearchArgs.model_json_schema()
    # properties 也是嵌套字典；按键取出 limit 子 schema，查看默认值、类型和上下界。
    limit_schema: Any = schema["properties"]["limit"]
    # get 在键缺失时返回 None/{}，适合只做观察，不让展示代码因可选 schema 键中断。
    print("tool schema 必填字段:", schema.get("required"))
    print("tool schema 的 limit:", limit_schema)
    # $defs 保存可复用的嵌套模型定义；sorted 让展示顺序稳定。
    print("tool schema 包含嵌套定义:", sorted(schema.get("$defs", {})))

    # 这份 JSON 同时放入多种错误：严格整数、Literal、时间解析、列表元素类型，
    # 以及内外两层未知字段，用来证明 ValidationError 能一次保留多个嵌套位置。
    invalid_nested_json: str = json.dumps(
        {
            "query": "Python",
            "limit": True,
            "filters": {
                "language": "fr",
                "published_after": "昨天",
                "keywords": ["agent", 7],
                "unknown_filter": "x",
            },
            "unknown_argument": "x",
        },
        ensure_ascii=False,
    )
    try:
        SearchArgs.model_validate_json(invalid_nested_json)
    except ValidationError as error:
        # 捕获整次边界失败后逐条输出；format_validation_error 已把 loc 转成点分路径。
        print("嵌套错误逐项定位:")
        for line in format_validation_error(error):
            print(" -", line)

    # 字段类型都合法不代表字段组合合理：ok=True 与非空 error 会触发
    # ToolResult 的 after validator。
    try:
        ToolResult(ok=True, error="不应同时出现")
    except ValidationError as error:
        print("跨字段不变量错误:", " | ".join(format_validation_error(error)))

    # Schema 能描述已声明的结构、类型和一部分约束，却不会执行搜索、鉴权或查数据库，
    # 也无法判断这个本地标题/URL 是否对应现实世界的真实事实。
    print("边界提醒: schema/校验保证已声明的结构与规则，不证明搜索内容事实正确")


def main() -> None:
    """依次运行全部确定性实验。"""
    # 函数定义本身只是建立可调用对象；真正调用从这里开始，并按源码顺序产生五段输出。
    demonstrate_data_structure_choices()
    demonstrate_presence_and_strictness()
    demonstrate_manual_and_model_validation()
    demonstrate_serialization()
    demonstrate_pipeline_schema_and_errors()


# 模块被直接作为脚本执行时，__name__ 等于 "__main__"，于是调用入口；
# 如果别的模块只是 import 本文件，则上面的类/函数会被定义，但五组演示不会自动运行。
if __name__ == "__main__":
    main()
