"""第 13 章：类型注解、TypedDict 与静态检查。

这个入口只包含能够从头运行到尾、并通过 Pyright 的示例。故意写错类型的代码放在
static_errors.py 中，只交给类型检查器分析，不要执行它。
"""

# 阅读顺序可以分成三层：先看下面的类型契约，再看 parse_* 运行时边界，最后从 main() 的
# 五组演示观察结果。数据会经历“JSON/未知 object -> 校验后的 TypedDict -> 核心查询与格式化”。
# 本文件的所有类型注解主要服务于读者和 Pyright；除非函数体显式判断，否则 Python 解释器
# 不会仅凭注解检查、转换或拒绝运行时对象。

# json 负责把 JSON 文本解析为 Python 对象；解析只保证 JSON 语法正确，不保证业务字段正确。
import json

# Callable、Mapping、Sequence 描述对象具备的“能力”，供下面的只读参数和回调签名使用。
from collections.abc import Callable, Mapping, Sequence

# typing 中这些工具用来表达动态边界、有限字面量、TypedDict 键规则，以及静态类型断言。
from typing import Any, Literal, NotRequired, Required, TypedDict, cast

# `type 名字 = 类型表达式` 是 Python 3.12+ 的类型别名语句，不会创建运行时校验器。
# Literal 把任意 str 收紧到列出的字符串常量，适合稳定、有限的工具名和状态集合。
type ToolName = Literal["search_docs"]
type ResultStatus = Literal["ok", "not_found"]


# TypedDict 按具体键分别描述普通 dict 的静态结构，类似为字典声明一个轻量 DTO 契约；
# 它在运行时不会实例化出 Java Bean，也不会自动校验来自 JSON 的数据。
class SearchArguments(TypedDict):
    """search_docs 的输入结构。"""

    # 未包裹 NotRequired 的字段默认都是必需键；query 的键和值都必须存在且为 str。
    query: str
    # `str | None` 是联合类型：这个键必须存在，但值可为字符串或 None。
    # None 表示调用方明确选择“不按分类过滤”，不能把“可空”误读成“可缺键”。
    category: str | None
    # NotRequired 控制键是否可以不存在；若键存在，其值仍必须是 int。
    # 它只描述结构，不会给字典自动填默认值；缺键时由工具实现采用自己的默认值。
    limit: NotRequired[int]


# 这个结构是外层工具调用契约：name 选择工具，arguments 保存该工具自己的参数。
class SearchToolCall(TypedDict):
    """已经通过边界校验的工具调用。"""

    name: ToolName
    arguments: SearchArguments
    # request_id 与 limit 一样可以完全缺少，但存在时必须为 str。
    request_id: NotRequired[str]


# total=False 改变本类字段的默认规则：字段默认可缺少，适合“多数键都是补充信息”的结构。
class TraceMetadata(TypedDict, total=False):
    """演示 total=False 与 Required 可以组合使用。"""

    # Required 把 request_id 从“默认可缺少”改回必需；它不是说值可以为空。
    request_id: Required[str]
    # label 可以缺键；如果存在则必须是 str。
    label: str
    # elapsed_ms 也可以缺键；如果存在，值还可以明确为 None。
    elapsed_ms: int | None


# Document 仍表示普通字典，只是静态检查器现在知道每个文档字段各自的类型和缺键规则。
class Document(TypedDict):
    document_id: str
    title: str
    # score 键必须存在，但 `float | None` 允许用 None 表达“暂时没有评分”。
    score: float | None
    # snippet 键本身可以不存在；NotRequired 不会允许已存在的值变成 None。
    snippet: NotRequired[str]


# 下面两个 TypedDict 用 status 这个 Literal 字段表达互斥的结果形状：成功必有文档，
# 未找到时文档必须为 None。这比把两者写成彼此无关的宽联合更能表达状态与载荷的关系。
class QuerySuccess(TypedDict):
    status: Literal["ok"]
    document: Document


class QueryNotFound(TypedDict):
    status: Literal["not_found"]
    document: None


# `|` 在类型表达式中表示联合：QueryResult 的值只能符合上述两个结构之一。
type QueryResult = QuerySuccess | QueryNotFound
# Callable 的内层 `[QueryResult]` 是位置参数类型列表，最后的 str 是返回值类型。
# 因此 ResultFormatter 表示“接收一个查询结果，同步返回字符串”的可调用对象。
type ResultFormatter = Callable[[QueryResult], str]


# 自定义异常继承 ValueError，因此既能单独捕获 ContractError，也保留“值不符合约定”的语义。
class ContractError(ValueError):
    """外部数据不满足本章约定时抛出的运行时错误。"""


# `-> None` 表示函数只负责副作用，不向调用方返回业务结果；这里的副作用是打印章节标题。
def print_section(title: str) -> None:
    # f-string 会先求值花括号里的 title，再把结果嵌入字符串；开头的 \n 先输出一个空行。
    print(f"\n=== {title} ===")


# 参数写 Sequence[int]，说明这里只要求“能按顺序读取整数”，调用方可以传 tuple 或 list；
# 返回 list[str] 则明确表示函数创建并交出一个仍可追加、修改的新列表。
def build_labels(prefix: str, document_ids: Sequence[int]) -> list[str]:
    """演示参数、返回值、局部变量和容器注解。"""
    # 列表推导式会依次取出 document_ids 中的每个整数；f-string 把前缀与当前 ID 拼成字符串。
    # 输入 (7, 13) 会得到新列表 ["doc-7", "doc-13"]，并不会修改传入的序列。
    labels: list[str] = [f"{prefix}-{document_id}" for document_id in document_ids]
    return labels


# 这个极简函数故意不写 isinstance：后面的实验用它证明注解不是 Java 编译期强制类型，
# 动态调用完全可能把 int 绑定到 text，并原样返回同一个对象。
def echo_text(text: str) -> str:
    """注解描述预期契约，但函数体不会自动检查或转换 text。"""
    return text


# object 可以接住任意对象，但静态检查器不允许直接把它当 str 使用；必须先通过分支逐步收窄。
def normalize_optional_text(value: object) -> str | None:
    """先判断 None，再用 isinstance 把 object 收窄为 str。"""
    # `is None` 判断是否正是 None 单例；不能用 `if not value`，否则空字符串也会被混为一谈。
    if value is None:
        return None
    # 前一分支已排除 None；这里再拒绝非 str，剩余路径中 Pyright 能确定 value 是 str。
    if not isinstance(value, str):
        raise ContractError("值必须是字符串或 None")
    # strip() 返回去掉首尾空白的新字符串，不会原地修改不可变的 str。
    return value.strip()


# `*` 后面的 location 是仅限关键字参数：调用时必须写 location="..."，让错误路径更醒目。
# 返回 Mapping 而非 dict，表示下游只依赖按键读取和遍历，不承诺会修改传入映射。
def expect_mapping(value: object, *, location: str) -> Mapping[str, object]:
    """把不可信 object 校验成只读的字符串键映射视图。"""
    # JSON 的 object 通常解析成 dict，但 value 的静态类型是 object，必须先在运行时确认。
    if not isinstance(value, dict):
        raise ContractError(f"{location} 必须是对象")
    # 直接遍历 dict 得到键；all(...) 只有所有键都是 str 才为 True，发现首个反例就停止。
    if not all(isinstance(key, str) for key in value):
        raise ContractError(f"{location} 的键必须都是字符串")

    # cast 运行时会原样返回 value，不做验证或复制。这里能使用它，是因为上面的两个分支
    # 已证明对象是 dict 且所有键为 str；映射值暂时仍可能是任意对象，所以统一记为 object。
    return cast(Mapping[str, object], value)


# 该辅助函数只检查字段集合，不读取字段值。allowed 是允许出现的键名集合。
def reject_unknown_keys(
    mapping: Mapping[str, object],
    *,
    allowed: set[str],
    location: str,
) -> None:
    # set(mapping) 从映射的键构造集合；集合差集 `-` 留下“输入中有、允许集合中没有”的键。
    unknown: set[str] = set(mapping) - allowed
    if unknown:
        # sorted() 只为稳定错误输出的顺序，不改变 unknown；这样多个错字段也容易对照。
        raise ContractError(f"{location} 出现未知字段：{sorted(unknown)}")


# 返回 object 是刻意的：这个函数只能证明键存在，不知道每个业务字段应该是什么具体类型。
def require_key(mapping: Mapping[str, object], key: str, *, location: str) -> object:
    """显式区分“键缺少”和“键存在、值为 None”。"""
    # 先用成员判断处理缺键，不能直接依赖 mapping.get()：get() 返回 None 时无法区分两种情况。
    if key not in mapping:
        # f-string 中的 `!r` 使用 repr(key)，字符串键会带引号，错误信息的字段边界更清楚。
        raise ContractError(f"{location} 缺少字段 {key!r}")
    return mapping[key]


# 这是从未知 object 到可信 str 的边界：既检查实际类型，也拒绝清理后为空的文本。
def require_non_empty_string(value: object, *, location: str) -> str:
    """检查并清理字符串；location 只是用于报错的字段路径标签。"""
    if not isinstance(value, str):
        raise ContractError(f"{location} 必须是字符串")
    # normalized 是一个新 str；value 原对象不变。空白字符串会在 strip() 后变成 ""。
    normalized: str = value.strip()
    # 空字符串在布尔上下文中为 False，所以 not normalized 能统一拒绝 "" 和纯空白文本。
    if not normalized:
        raise ContractError(f"{location} 不能为空")
    return normalized


# 该函数是 SearchArguments 的运行时可信边界：输入可以是任何 object，成功时返回重新构造的
# TypedDict；失败则立即抛 ContractError，不让未验证字段流入 search_documents()。
def parse_search_arguments(value: object) -> SearchArguments:
    """运行时校验外部工具参数，成功后才返回 TypedDict 类型。"""
    # 先验证整体形状，再执行白名单检查；这两步之后仍只知道每个值是 object。
    mapping: Mapping[str, object] = expect_mapping(value, location="arguments")
    reject_unknown_keys(
        mapping,
        allowed={"query", "category", "limit"},
        location="arguments",
    )

    # 故意拆成两步：先证明 query 键存在，再检查它的值是非空字符串。
    query_value: object = require_key(mapping, "query", location="arguments")
    query: str = require_non_empty_string(query_value, location="arguments.query")

    # category 与 query 一样是必需键，但值规则不同：None 原样保留，str 则清理首尾空白。
    category_value: object = require_key(mapping, "category", location="arguments")
    if category_value is None:
        # 局部注解明确告诉读者和检查器，这个变量之后可能走两种类型路径。
        category: str | None = None
    else:
        category = require_non_empty_string(category_value, location="arguments.category")

    # 新建字典只写入已验证并规范化的值，不把不可信 mapping 原样 cast 后继续传递。
    arguments: SearchArguments = {"query": query, "category": category}
    # NotRequired 意味着需要先判断键是否存在；不存在时不写入，默认值留给查询函数决定。
    if "limit" in mapping:
        limit_value: object = mapping["limit"]
        # bool 是 int 的子类，isinstance(True, int) 会成立；type(...) is int 才会拒绝布尔值。
        # 短路 or 会在任一条件成立时拒绝：既要类型恰好为 int，也要落在闭区间 1～10。
        if type(limit_value) is not int or not 1 <= limit_value <= 10:
            raise ContractError("arguments.limit 必须是 1 到 10 的整数")
        arguments["limit"] = limit_value
    return arguments


def parse_tool_call(value: object) -> SearchToolCall:
    """把外部 dict 边界收窄成可以交给工具实现的精确类型。"""
    # 第 1 层：检查 tool_call 最外层必须是只含已知键的字典。location 只用于拼错误路径，
    # 不会自动导航到输入中的同名字段。
    mapping: Mapping[str, object] = expect_mapping(value, location="tool_call")
    reject_unknown_keys(
        mapping,
        allowed={"name", "arguments", "request_id"},
        location="tool_call",
    )

    # 第 2 层：取出的 name 暂时是 object；与唯一允许的 Literal 值比较后才接受。
    name: object = require_key(mapping, "name", location="tool_call")
    if name != "search_docs":
        raise ContractError("tool_call.name 必须是 'search_docs'")

    # 第 3 层：先取出 arguments（仍是 object），再交给专门函数检查内部三项字段。
    arguments_value: object = require_key(mapping, "arguments", location="tool_call")
    arguments: SearchArguments = parse_search_arguments(arguments_value)
    # 到这里 name 的值和 arguments 的结构都经过验证，可以构造内部使用的精确契约。
    parsed: SearchToolCall = {"name": "search_docs", "arguments": arguments}

    # request_id 是可缺少键；缺少时不向 parsed 填入任何内容，一旦出现则验证并规范化。
    if "request_id" in mapping:
        request_id_value: object = mapping["request_id"]
        parsed["request_id"] = require_non_empty_string(
            request_id_value, location="tool_call.request_id"
        )
    return parsed


def search_documents(arguments: SearchArguments) -> QueryResult:
    """可控的本地查询替身；不执行 HTTP、数据库或真实 Agent 调用。"""
    # 方括号访问这两个必需键；casefold() 返回适合无视大小写比较的新字符串。
    normalized_query: str = arguments["query"].casefold()
    requested_category: str | None = arguments["category"]
    # limit 键可以不存在；dict.get 的第二个参数 3 才是缺键时真正使用的运行时默认值。
    # NotRequired 本身没有向 arguments 插入 3。
    limit: int = arguments.get("limit", 3)

    # `tuple[Document, ...]` 中的省略号表示可有任意多个同类型元素，不是一个三点占位值。
    # 外层 tuple 表达样本集合在函数内不会增删；每个 Document 在运行时仍是普通 dict。
    documents: tuple[Document, ...] = (
        {
            "document_id": "doc-13",
            "title": "类型注解与工具契约",
            "score": 0.97,
            "snippet": "静态约束不能替代外部输入校验。",
        },
        {
            "document_id": "doc-07",
            "title": "函数与回调",
            "score": None,
        },
    )

    # 这个本地样本里的文档都视为 python 分类。短路 and 会先排除“不筛选”的 None；
    # 若调用方明确给了其他分类，立刻返回未找到联合成员，不再进入标题循环。
    if requested_category is not None and requested_category != "python":
        return {"status": "not_found", "document": None}

    # `documents[:limit]` 创建一个最多含 limit 项的新 tuple 切片；循环只扫描这个范围。
    for document in documents[:limit]:
        normalized_title: str = document["title"].casefold()
        # `in` 对字符串执行子串判断；找到第一篇匹配文档后立即返回成功联合成员。
        if normalized_query in normalized_title:
            return {"status": "ok", "document": document}
    # 循环耗尽仍未 return，说明没有匹配，返回正常的 not_found 状态而不是抛异常。
    return {"status": "not_found", "document": None}


# 与请求解析相同，这里把不可信结果文档逐字段校验，并重建成内部可信的 Document。
def parse_document(value: object) -> Document:
    mapping: Mapping[str, object] = expect_mapping(value, location="result.document")
    reject_unknown_keys(
        mapping,
        allowed={"document_id", "title", "score", "snippet"},
        location="result.document",
    )

    # 嵌套调用按“先检查键存在，再检查值类型与内容”的顺序工作；任一步失败都会抛出
    # 带完整字段路径的 ContractError，不会继续构造半成品文档。
    document_id: str = require_non_empty_string(
        require_key(mapping, "document_id", location="result.document"),
        location="result.document.document_id",
    )
    title: str = require_non_empty_string(
        require_key(mapping, "title", location="result.document"),
        location="result.document.title",
    )

    # score 是必需但可空字段：先区分缺键；若不是 None，则要求实际类型恰好为 float。
    # 这里有意不接受 int，也不把字符串 "0.98" 隐式转换成浮点数。
    score_value: object = require_key(mapping, "score", location="result.document")
    if score_value is not None and type(score_value) is not float:
        raise ContractError("result.document.score 必须是浮点数或 None")

    # 先用三个必需字段建立新字典；经上面的分支，score_value 已收窄为 float | None。
    document: Document = {"document_id": document_id, "title": title, "score": score_value}
    # snippet 可以缺键，但只要出现就必须是清理后的非空字符串。
    if "snippet" in mapping:
        document["snippet"] = require_non_empty_string(
            mapping["snippet"], location="result.document.snippet"
        )
    return document


def parse_query_result(value: object) -> QueryResult:
    """覆盖成功、未找到、字段缺失和字段类型不匹配。"""
    # 先限定结果最外层只能有 status/document 两个键，再分别要求它们存在。
    mapping: Mapping[str, object] = expect_mapping(value, location="result")
    reject_unknown_keys(mapping, allowed={"status", "document"}, location="result")

    status: object = require_key(mapping, "status", location="result")
    document_value: object = require_key(mapping, "document", location="result")

    # status 充当“判别字段”：每个字面量决定 document 必须具有哪一种形状。
    if status == "ok":
        # 成功分支把未知 document_value 交给文档解析器，验证后才装入 QuerySuccess。
        return {"status": "ok", "document": parse_document(document_value)}
    if status == "not_found":
        # 未找到是正常结果，不是异常；但协议要求它与 document=None 成对出现。
        if document_value is not None:
            raise ContractError("status 为 'not_found' 时 document 必须是 None")
        return {"status": "not_found", "document": None}
    # 未进入任一已知 Literal 分支，说明外部状态超出了当前协议。
    raise ContractError("result.status 必须是 'ok' 或 'not_found'")


def format_result(result: QueryResult) -> str:
    """Literal 字段让类型检查器按分支收窄联合类型。"""
    # 判断 status 后，Pyright 知道该分支是 QueryNotFound，document 必然是 None。
    if result["status"] == "not_found":
        return "没有找到文档"

    # 早返回已经排除未找到成员，因此这里的 result 是 QuerySuccess，document 是 Document，
    # 无需再写 `if document is not None`。缺少可选 snippet 时由 get() 提供展示文本。
    document: Document = result["document"]
    snippet: str = document.get("snippet", "（响应未提供摘要）")
    return f"{document['document_id']} | {document['title']} | {snippet}"


# 该格式化器满足同一个 ResultFormatter 签名，但只读取两个联合成员都拥有的 status 字段。
def format_status(result: QueryResult) -> str:
    return f"状态={result['status']}"


# Sequence 表示这里只会按顺序读取回调，不需要调用方必须提供可变 list；tuple 同样可用。
def apply_formatters(
    result: QueryResult,
    formatters: Sequence[ResultFormatter],
) -> list[str]:
    """只读取回调序列，返回自己新建且还可能继续追加的 list。"""
    # 推导式每轮把同一个 result 传给一个 formatter，并按原顺序收集返回的 str。
    return [formatter(result) for formatter in formatters]


# 第一组实验把函数签名、局部变量和三类常见容器注解放在一起观察。
def demonstrate_basic_annotations() -> None:
    print_section("1. 基础注解、容器与箭头")
    # build_labels 接收 Sequence，所以这里可直接传不可变 tuple；返回值则是新建的 list[str]。
    labels: list[str] = build_labels("doc", (7, 13))
    # tuple[int, int] 按位置规定恰好两个整数，不等于“任意长度的整数元组”。
    coordinate: tuple[int, int] = (13, 2)
    # 字典的三个值类型不同，因此统一用最安全的共同上界 object；取值后应先收窄再专用操作。
    metadata: dict[str, object] = {
        "labels": labels,
        "coordinate": coordinate,
        "published": True,
    }

    # 这次调用确实传 str，因此实际结果符合 -> str；下一组会故意经 Any 传入 int 作对照。
    result: str = echo_text("箭头描述返回值的预期类型")
    # 这一组输出依次观察可变列表、固定位置元组、异构值字典，以及返回对象的真实运行时类型。
    print("list[str] 返回值:", labels)
    print("固定长度 tuple[int, int]:", coordinate)
    print("dict[str, object] 可容纳不同值类型:", metadata)
    print("调用表达式的实际结果/类型:", result, type(result).__name__)


# 第二组把同一类未知输入分别交给 Any、object 和 cast，比较三者的静态与运行时后果。
def demonstrate_runtime_boundary() -> None:
    print_section("2. 注解不会运行时检查；Any、object 与 cast")

    # Any 让类型检查器放弃这条数据流的大部分保护，因此 int 可以穿过要求 str 的调用；
    # echo_text 的函数体也没有自动检查或转换参数，返回的仍是原整数 42。
    value_from_untyped_library: Any = 42
    # 用 object 接住返回结果，避免继续相信错误的静态 str 假设，并在下面观察真实对象类型。
    returned_value: object = echo_text(value_from_untyped_library)
    print("把 int 经 Any 传给 -> str 的函数，实际返回:", returned_value)
    print("实际返回类型仍是:", type(returned_value).__name__)

    # 字典里实际保存的是字符串 "3"；Any 使下标结果和加法都被静态放行。
    any_payload: Any = {"limit": "3"}
    try:
        # 真正执行时，str 与 int 没有定义 `+`，于是抛 TypeError 并转入 except。
        print("Any 计算结果:", any_payload["limit"] + 1)
    except TypeError as error:
        # `as error` 把当前异常对象绑定到变量；这里只打印异常类名，让演示输出保持稳定。
        print("Any 隐藏的问题在运行时暴露:", type(error).__name__)

    # object 同样能引用任意对象，但检查器要求先证明能力，因而不会让错误静默扩散。
    object_payload: object = {"limit": "3"}
    if isinstance(object_payload, dict):
        # 外层判断后才能调用 dict.get；取出的具体值仍未知，所以显式保留为 object。
        limit_value: object = object_payload.get("limit")
        # 使用精确类型判断排除 bool；只有真实 int 才能安全执行下面的 + 1。
        if type(limit_value) is int:
            print("object 收窄后的 limit:", limit_value + 1)
        else:
            # 本例实际走这里并报告 str，计算从未发生，因此没有 TypeError。
            print("object 要求先检查，拒绝实际类型:", type(limit_value).__name__)

    # cast(str, untrusted) 只把“请静态地当成 str”告诉检查器；它既不调用 str()，也不复制对象。
    untrusted: object = 42
    claimed_text: str = cast(str, untrusted)
    # 因而真实类型仍是 int；这说明不能用 cast 冒充外部数据的运行时校验。
    print("cast(str, 42) 后的实际类型:", type(claimed_text).__name__)


# 第三组沿完整请求数据流前进，并把校验后的查询结果返回给最后一组回调实验继续使用。
def demonstrate_tool_call() -> QueryResult:
    print_section("3. 校验工具请求，再交给带类型的核心逻辑")
    # 三引号创建跨行 str；其中的 null 属于 JSON 语法，json.loads 后才会成为 Python 的 None。
    raw_json = """
    {
        "name": "search_docs",
        "arguments": {"query": "类型注解", "category": null},
        "request_id": "req-013"
    }
    """

    # json.loads 把文本解析成普通 Python 对象，但不会应用 SearchToolCall 业务规则。
    # 立刻收口为 object 后，后续只能先检查再下标访问，避免 Any 绕开边界。
    decoded: object = json.loads(raw_json)
    # parse_tool_call 逐层检查并重建数据；返回后 call 才拥有可信的 TypedDict 静态类型。
    call: SearchToolCall = parse_tool_call(decoded)
    arguments: SearchArguments = call["arguments"]
    # 核心查询只接收已校验参数，并返回 status 能区分形状的 QueryResult。
    result: QuerySuccess | QueryNotFound = search_documents(arguments)

    # get 的 "local" 是这里直接写出的备用值：request_id 缺键时才使用，不会去其他地方查找。
    request_id: str = call.get("request_id", "local")
    # total=False 没有让所有键都可缺少：Required 指定的 request_id 仍必须出现在字面量中。
    trace: TraceMetadata = {"request_id": request_id}
    # 输出依次验证：TypedDict 的运行时本质、清理后的参数、缺键与 None 的区别、Required，
    # 最后再把查询联合交给 format_result，让它按 status 选择展示路径。
    print("TypedDict 值的运行时类型:", type(call).__name__)
    print("通过校验后的 arguments:", arguments)
    print("limit 是可缺少键，当前是否存在:", "limit" in arguments)
    print("category 是必需键，但当前值是否为 None:", arguments["category"] is None)
    print("total=False 中被 Required 标记的键:", trace)
    print("工具结果:", format_result(result))
    return result


# 第四组把四段 JSON 文本映射到场景名，用同一循环观察成功、正常未找到和两种契约错误。
def demonstrate_result_validation() -> None:
    print_section("4. 查询结果的四条边界路径")
    # Mapping[str, str] 表示这里只需要只读“场景名 -> JSON 文本”；多行字符串中的缩进是
    # JSON 允许的空白。成功场景特意缺 snippet，而两个反例分别破坏必需键与值类型。
    cases: Mapping[str, str] = {
        "成功（可选 snippet 缺少）": """
        {"status": "ok", "document": {
            "document_id": "doc-13", "title": "类型注解", "score": 0.98
        }}
        """,
        "未找到": '{"status": "not_found", "document": null}',
        "缺少必需字段 title": """
        {"status": "ok", "document": {"document_id": "doc-13", "score": 0.98}}
        """,
        "score 类型不匹配": """
        {"status": "ok", "document": {
            "document_id": "doc-13", "title": "类型注解", "score": "0.98"
        }}
        """,
    }

    # items() 每轮产生一个二元组，再按位置解包为 label 和 text。
    for label, text in cases.items():
        # 每段文本先只做 JSON 语法解析；显式写成 object，迫使下一步通过业务验证器。
        decoded: object = json.loads(text)
        try:
            parsed: QuerySuccess | QueryNotFound = parse_query_result(decoded)
        except ContractError as error:
            # 捕获本例预期的边界异常，使一个坏输入不会中断循环，后续场景仍可继续比较。
            print(f"{label}: 拒绝 -> {error}")
        else:
            # try 内没有抛 ContractError 才执行 else；此时 parsed 是可信 QueryResult。
            print(f"{label}: 接受 -> {format_result(parsed)}")


# 第五组先演示精确 Callable 序列，再复用 object 收窄函数覆盖 None、str 与错误类型三条路径。
def demonstrate_callable_and_collections(result: QueryResult) -> None:
    print_section("5. 精确回调与接收能力")
    # tuple[ResultFormatter, ...] 表示任意长度、每项都满足相同签名；这里放入两个普通函数对象。
    formatters: tuple[ResultFormatter, ...] = (format_result, format_status)
    # apply_formatters 依次调用它们，因此得到两个字符串组成的新列表。
    print("Sequence[Callable[[QueryResult], str]]:", apply_formatters(result, formatters))

    # 前两次分别走 None 早返回和 str.strip() 路径，可直接观察联合类型的两种合法输入。
    print("None 收窄:", normalize_optional_text(None))
    print("str 收窄:", normalize_optional_text("  tool output  "))
    try:
        # int 不属于 `str | None` 的运行时契约，函数会主动抛自定义异常。
        normalize_optional_text(13)
    except ContractError as error:
        print("类型不匹配由运行时校验拒绝:", error)


# main 只编排演示顺序：前两组建立基础，第三组产生结果，第四组独立验证边界，
# 第五组再消费第三组返回的 tool_result。定义 main 时函数体尚未执行，调用时才从上到下运行。
def main() -> None:
    demonstrate_basic_annotations()
    demonstrate_runtime_boundary()
    tool_result: QuerySuccess | QueryNotFound = demonstrate_tool_call()
    demonstrate_result_validation()
    demonstrate_callable_and_collections(tool_result)


# 模块作为脚本运行时，解释器把 __name__ 设为 "__main__"，于是调用 main()；
# 若将本文件 import 到其他模块，这个条件为 False，只完成上面的定义，不自动打印演示内容。
if __name__ == "__main__":
    main()
