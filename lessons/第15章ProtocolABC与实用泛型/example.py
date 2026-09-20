"""第 15 章：用 Protocol、ABC 与泛型组织同步工具接口。

正常入口只包含可控、确定性的本地实验。故意保留的静态类型错误位于
static_errors.py；那个文件只交给 Pyright 分析，不作为程序执行。
"""

# 建议按文件从上往下读：先认识泛型值与搜索协议，再看工具注册表和 ABC，
# 最后由 main() 依次调用六组 demonstrate_* 函数，把前面“只定义、尚未执行”的代码跑一遍。
# 主数据流是：本地/fixture 数据 -> SearchBackend -> SearchTool -> Result[list[SearchHit]]。
# 类型注解负责把这条关系告诉 Pyright；真正运行时仍是普通 Python 对象、属性查找和函数调用。

# json 与 Path 共同读取本章固定的本地 fixture；ABC/abstractmethod 负责抽象基类约束；
# dataclass 生成数据类样板方法；collections.abc 中的类型描述调用、只读映射和只读序列能力；
# typing 中的 Generic/TypeVar 演示旧泛型语法，其余名称服务于协议、收窄和运行时浅检查。
import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Protocol, TypeVar, cast, runtime_checkable


def print_section(title: str) -> None:
    """让终端输出与 README 的实验顺序对应。"""
    # f-string 会先求值花括号里的 title，再拼出字符串；开头的 \n 先留一个空行分隔实验。
    # -> None 是静态返回提示，不会在运行时自动检查这个函数是否真的返回 None。
    print(f"\n=== {title} ===")


# Python 3.12+ 的类型参数语法把“这个 T 属于谁”直接写在函数名旁边。
# T 不是“任意值”的别名，而是一次调用中需要保持一致的类型变量：参数和返回值共享同一个 T。
def identity[T](value: T) -> T:
    """输入是什么具体类型，静态返回类型就保持为什么类型。"""
    # 这里直接返回传入对象本身，不复制、不转换，也不按注解做运行时校验。
    # 例如传入 int 时 Pyright 推断结果仍是 int；运行时只是让返回值引用同一个 int 对象。
    return value


def first[T](values: Sequence[T]) -> T:
    """把容器元素类型与返回类型关联起来；空输入没有可返回的 T。"""
    # Sequence[T] 表示可按索引读取的一组 T；这里只读长度和首项，所以 list 与 tuple 都可传入。
    # 空 list/tuple 在布尔上下文中为 False。若为空，就没有真实元素可以兑现返回类型 T。
    if not values:
        raise ValueError("first() 不能接收空序列")
    # 经过上面的保护后取下标 0；返回类型仍与序列的元素类型绑定，而不是退化成 object/Any。
    return values[0]


# @dataclass 在类定义时加工类，为字段生成 __init__、__repr__ 和比较方法。
# frozen=True 禁止实例创建后重新给字段赋值，但不保证字段内部引用的可变对象也深度不可变。
@dataclass(frozen=True)
class Success[T]:
    """成功分支保存具体的 T，不把结果退化成 object 或 Any。"""

    # 构造 Success(15) 时 value 保存真实的 int；T 让静态检查器把该类型继续传给调用方。
    value: T


@dataclass(frozen=True)
class Failure:
    """失败分支保存原异常对象，调用方仍能看到异常类型和消息。"""

    # 保存异常对象而非仅保存字符串，后续仍可检查 ValueError/OSError 等具体类型。
    error: Exception


# type 声明的是带类型参数的类型别名：Result[int] 表示 Success[int] 或 Failure。
# 它不会额外包一层运行时对象；实际值仍是两个 dataclass 之一，可用 isinstance 分支判断。
type Result[T] = Success[T] | Failure


# 旧源码会先在模块级创建 TypeVar，再让类显式继承 Generic[T]。
# TypeVar 创建供类型检查器关联多个位置的类型变量；它不会限制随后赋给 _value 的运行时对象。
LegacyT = TypeVar("LegacyT")


class LegacyBox(Generic[LegacyT]):  # noqa: UP046 - 本章需要展示旧框架源码写法
    """Python 3.11 及更早版本也能解析的传统泛型写法。"""

    def __init__(self, value: LegacyT) -> None:
        # self 指当前实例；单下划线表达“类内部使用”的约定，不是 Java private 的强制访问控制。
        self._value: LegacyT = value

    def get(self) -> LegacyT:
        # 构造参数与返回注解复用 LegacyT，因此 LegacyBox[str].get() 静态上仍返回 str。
        return self._value


# 现代语法把 T 的作用域收在 Box 类内，并隐式把该类声明为泛型类，不再继承 Generic[T]。
class Box[T]:
    """同一个 Box 关系的 Python 3.12+ 写法；本项目以它为主。"""

    def __init__(self, value: T) -> None:
        self._value: T = value

    def get(self) -> T:
        # 与旧写法一样，类型关系只服务于静态分析；运行时返回的就是先前保存的对象引用。
        return self._value


# SearchHit 是后端统一产出的数据记录，四个字段都由调用者按属性读取。
# dataclass 不会根据 str 注解做运行时校验；fixture 路径会在构造它之前显式验证外部数据。
@dataclass(frozen=True)
class SearchHit:
    document_id: str
    title: str
    snippet: str
    source: str


# SearchRequest 把工具输入收拢成一个对象；省略 limit 时，生成的 __init__ 会使用不可变整数 3。
@dataclass(frozen=True)
class SearchRequest:
    query: str
    limit: int = 3


# 普通 Protocol 主要供静态检查。装饰器在类定义时把它标为可放进 isinstance() 的运行时协议；
# 这种运行时检查仍只看所需成员是否存在，不核对注解、完整参数签名或业务结果。
@runtime_checkable
class SearchBackend(Protocol):
    """调用方只要求同步 search 能力，不要求实现类继承本协议。"""

    # `*` 后的 limit 只能按名字传入，如 search("Protocol", limit=2)，不能作为第二个位置参数。
    # `...` 是协议里的占位方法体：这里声明调用形状，不提供给隐式实现类复用的代码。
    # 返回 Sequence 表示调用方只依赖读取能力；实现可返回能力更具体的 list[SearchHit]。
    def search(self, query: str, *, limit: int) -> Sequence[SearchHit]: ...


class MemorySearchBackend:
    """普通类没有显式继承 SearchBackend，但结构满足协议。"""

    def __init__(self, documents: Sequence[SearchHit]) -> None:
        # 构造器只读取输入，因此接收 list/tuple；tuple(...) 建立浅层快照，
        # 避免调用方随后增删原序列的元素。元素对象本身不会被深拷贝。
        self._documents = tuple(documents)

    def search(self, query: str, *, limit: int) -> list[SearchHit]:
        # casefold() 做适合无大小写匹配的 Unicode 归一，split() 再按空白拆成查询词列表。
        # 例如 "Protocol ABC" 会得到 ["protocol", "abc"]。
        terms = query.casefold().split()
        # 注解说明这个可变列表只收集 SearchHit；Python 运行时不会凭注解阻止放入其他对象。
        matches: list[SearchHit] = []
        # 每轮 document 都是构造器快照中的一个 SearchHit。
        for document in self._documents:
            # f-string 把标题和摘要拼成一个待搜索字符串，再统一大小写以便比较。
            searchable = f"{document.title} {document.snippet}".casefold()
            # 生成器逐个产生“查询词是否出现”的布尔值；all() 遇到首个 False 就停止。
            # 只有所有查询词都存在才收录。若查询词为空，all(空序列) 按定义为 True。
            if all(term in searchable for term in terms):
                # append 原地修改 matches，加入的是现有 SearchHit 引用，不会复制该对象。
                matches.append(document)
        # 切片返回新 list，原始 matches 与内部文档快照不变。经 SearchTool 调用时 limit 已为正数，
        # 所以这里最多取前 limit 项；若绕过工具直接传负数，Python 会按负下标切片规则解释它。
        return matches[:limit]


def require_text(mapping: Mapping[str, object], key: str, *, row: int) -> str:
    """对本地 fixture 做最小运行时校验，避免用 cast 冒充已验证数据。"""
    # Mapping 只承诺按键读取；get() 在缺键时返回 None，因此 value 先按宽泛 object 处理。
    value: object | None = mapping.get(key)
    # or 会短路：不是 str 时不会继续调用 value.strip()；是 str 时再拒绝仅含空白的内容。
    # isinstance 分支也让静态检查器在成功路径上把 value 收窄为 str。
    if not isinstance(value, str) or not value.strip():
        # f-string 中 !r 使用 repr 形式展示 key，例如 'title'，便于定位坏字段。
        raise ValueError(f"fixture 第 {row} 行的 {key!r} 必须是非空字符串")
    # strip() 返回去掉两端空白的新字符串，不会原地修改 JSON 解码得到的值。
    return value.strip()


def load_fixture(path: Path) -> tuple[SearchHit, ...]:
    """读取并校验本章自带 JSON；这里只复刻本章实际需要的四个字段。"""
    # with 类似 Java try-with-resources：离开缩进块时关闭文件，即使 json.load() 抛异常也一样。
    # encoding 明确按 UTF-8 解码本章包含中文的 fixture。
    with path.open(encoding="utf-8") as file:
        # json.load() 把文本解析成 Python 的 list/dict/str 等对象。先标成 object，
        # 表示外部数据尚未验证，不能提前假定它就是 list[SearchHit]。
        decoded: object = json.load(file)

    # 顶层必须是 JSON 数组对应的 list；失败就停止，避免在错误形状上继续遍历。
    if not isinstance(decoded, list):
        raise TypeError("fixture 顶层必须是数组")

    # 逐行验证成功后才构造领域对象，最后再转成不可增删的 tuple 返回。
    documents: list[SearchHit] = []
    # enumerate(..., start=1) 同时给出从 1 开始的展示行号和当前原始元素，并解包到两个变量。
    for row, raw_item in enumerate(decoded, start=1):
        # JSON object 应解码为 dict；all(...) 再逐个确认键都是 str。
        # or 短路保证 raw_item 不是 dict 时，右侧不会错误地把它当字典迭代。
        if not isinstance(raw_item, dict) or not all(
            isinstance(key, str) for key in raw_item
        ):
            raise ValueError(f"fixture 第 {row} 行必须是字符串键对象")

        # 上面的分支已经逐个证明键是 str；cast 只把已验证事实告知检查器。
        # cast 不转换、不复制，也不会在运行时再次检查 raw_item。
        item: Mapping[str, object] = cast(Mapping[str, object], raw_item)
        # 四次 require_text 分别取值并校验。只有全部成功才创建 SearchHit 并追加到 documents；
        # 任一字段失败会直接抛异常，不会留下一个字段不完整的 SearchHit。
        documents.append(
            SearchHit(
                document_id=require_text(item, "document_id", row=row),
                title=require_text(item, "title", row=row),
                snippet=require_text(item, "snippet", row=row),
                source=require_text(item, "source", row=row),
            )
        )
    # tuple(...) 建立返回容器；调用方能顺序读取命中，但不能对这个容器 append/remove。
    return tuple(documents)


class FixtureSearchBackend:
    """从本地 fixture 建立后端；对外仍只有同一个 search 协议。"""

    def __init__(self, path: Path) -> None:
        # 组合而非继承：先加载并验证 fixture，再把实际匹配工作委托给已实现的内存后端。
        self._delegate = MemorySearchBackend(load_fixture(path))

    def search(self, query: str, *, limit: int) -> list[SearchHit]:
        # 对外签名与 SearchBackend 一致；关键字参数显式转交给 delegate。
        return self._delegate.search(query, limit=limit)


class SignatureMismatchBackend:
    """教学反例：成员名存在，但注解签名与 SearchBackend 不一致。"""

    # 协议要求 query: str、limit: int，这里故意反写。Pyright 会比较并拒绝这种结构兼容性。
    def search(self, query: int, *, limit: str) -> list[SearchHit]:
        # 函数体只把参数插入字符串，所以动态调用传入 str/int 恰好也能走通。
        # 这不改变其声明的参数类型与协议冲突的事实。
        return [
            SearchHit(
                document_id="misleading-1",
                title=f"运行时检查只看成员：{query}",
                snippet=f"limit 的运行时值是 {limit!r}",
                source="signature-mismatch-demo",
            )
        ]


def load_runtime_candidate() -> object:
    """模拟动态加载：调用方在运行前只知道候选对象是 object。"""
    # 返回注解故意隐藏具体类，模拟插件/配置加载边界，让后面的 isinstance 分支真正承担收窄。
    return SignatureMismatchBackend()


class OfflineSearchBackend:
    """可控失败替身：不联网，只同步抛出预期的 OSError。"""

    def search(self, query: str, *, limit: int) -> Sequence[SearchHit]:
        # raise 会中断当前调用，把异常交给外层 SearchTool 的 except；!r 让查询词带引号显示。
        raise OSError(f"本地后端暂不可用：query={query!r}, limit={limit}")


# 两个类型参数分别贯穿输入位置和成功输出位置：它们可以不同，但各自出现处必须保持关系。
class Tool[InputT, OutputT](Protocol):
    """输入类型、调用参数与 Result 中的成功类型形成一条关系。"""

    # 定义 __call__ 后，实例或普通函数只要签名兼容，就可当 Tool 使用。
    # `/` 之前的 tool_input 只能按位置传入；Result[OutputT] 把成功值类型继续传到调用端。
    def __call__(self, tool_input: InputT, /) -> Result[OutputT]: ...


class SearchTool:
    """把任意合规 SearchBackend 适配为 Tool[SearchRequest, list[SearchHit]]。"""

    def __init__(self, backend: SearchBackend) -> None:
        # 字段只依赖协议，不依赖 Memory/Fixture/Offline 的具体类，便于替换后端。
        self._backend: SearchBackend = backend

    def __call__(self, request: SearchRequest, /) -> Result[list[SearchHit]]:
        # 业务规则需要显式运行时判断；SearchRequest.limit 的 int 注解本身不会完成校验。
        # 非正数直接变成 Failure 数据，不调用后端，也不在这里抛给调用方。
        if request.limit < 1:
            return Failure(ValueError("limit 必须大于 0"))

        try:
            # 协议只承诺 Sequence；list(...) 将任意合规返回序列物化为工具拥有的具体列表。
            hits: list[SearchHit] = list(
                self._backend.search(request.query, limit=request.limit)
            )
        except (OSError, ValueError) as error:
            # 圆括号列出两类预期边界异常，as error 把当前原始异常对象绑定到局部变量。
            # 只把已预期的边界失败转成 Result；原异常对象没有被替换或吞掉。
            return Failure(error)
        # 只有验证和后端调用都成功才到这里；类型检查器推断 Success 的 T 是 list[SearchHit]。
        return Success(hits)


def empty_search_tool(request: SearchRequest, /) -> Result[list[SearchHit]]:
    """普通函数也能满足带 __call__ 的 Tool 协议。"""
    # 与 SearchTool 保持相同输入规则和 Result 形状，证明满足协议不要求包装成某个类。
    if request.limit < 1:
        return Failure(ValueError("limit 必须大于 0"))
    # [] 是本次调用新建的空 list；Success 的目标类型由函数返回注解保持为 list[SearchHit]。
    return Success([])


class ToolRegistry[InputT, OutputT]:
    """小型同构注册表：同一实例中的工具共享一组输入/输出类型。"""

    def __init__(self, tools: Mapping[str, Tool[InputT, OutputT]]) -> None:
        # 公共参数只需读取，所以接收 Mapping；调用者可传 dict 或其他只读映射实现。
        # dict(tools) 做浅复制，让注册表拥有可增删键的容器；工具对象本身仍与输入映射共享。
        self._tools: dict[str, Tool[InputT, OutputT]] = dict(tools)

    def register(self, name: str, tool: Tool[InputT, OutputT]) -> None:
        # 下标赋值原地更新内部 dict；同名键会被新工具覆盖。
        # 当前实例的 InputT/OutputT 同时约束初始工具、后续注册工具和 invoke() 结果。
        self._tools[name] = tool

    def invoke(self, name: str, tool_input: InputT) -> Result[OutputT]:
        # get() 在名字不存在时返回 None。注册表值类型不包含 None，所以可据此判断缺失。
        tool: Tool[InputT, OutputT] | None = self._tools.get(name)
        if tool is None:
            # 未知名字也是预期调用结果，用 Failure 保存 LookupError，而非让 KeyError 直接冒出。
            return Failure(LookupError(f"未注册工具：{name}"))
        # 分支通过后 tool 被收窄为 Tool[InputT, OutputT]；像函数一样调用其 __call__。
        return tool(tool_input)


# 类型别名把“一个位置参数、返回 str”的固定回调形状起成业务名称。
type HitFormatter = Callable[[Sequence[SearchHit]], str]


def format_titles(hits: Sequence[SearchHit]) -> str:
    """简单位置参数回调可用 Callable 精确描述。"""
    # 生成器逐项惰性产生 title，join() 再用顿号拼成一个 str，不创建中间标题列表。
    # 空序列会拼出空字符串；空字符串为假，因此 or 返回右侧的展示占位文本。
    return "、".join(hit.title for hit in hits) or "（空结果）"


def apply_formatter(hits: Sequence[SearchHit], formatter: HitFormatter) -> str:
    # formatter 可以是函数、带 __call__ 的实例或其他可调用对象；这里按 Callable 约定调用。
    return formatter(hits)


class PrefixedFormatter(Protocol):
    """__call__ 协议能写出关键字参数名和调用规则。"""

    # 基础 Callable 适合位置参数形状；协议还能明确 prefix 必须作为同名关键字传入。
    # 实现无需继承本类，但 Pyright 会比较参数名称、位置规则和返回类型。
    def __call__(self, hits: Sequence[SearchHit], *, prefix: str) -> str: ...


def format_with_prefix(hits: Sequence[SearchHit], *, prefix: str) -> str:
    # 这个普通函数的签名恰好满足 PrefixedFormatter；f-string 将前缀与格式化结果连接。
    return f"{prefix}{format_titles(hits)}"


# ABC 走名义继承：子类必须明确继承 ManagedTool，才能复用状态和 run() 模板流程。
class ManagedTool(ABC):
    """ABC 用继承提供共享流程，并阻止缺少实现的子类实例化。"""

    def __init__(self) -> None:
        # 每次构造实例都会得到独立的整数计数；_ 前缀仍只是内部使用约定。
        self._call_count = 0

    @property
    def call_count(self) -> int:
        # @property 让调用方写 managed.call_count 读取值，而不是 managed.call_count()。
        # 没有定义 setter，所以公共接口只读；内部仍可给 _call_count 重新赋值。
        return self._call_count

    def run(self, payload: Mapping[str, object]) -> str:
        # 这是所有子类共享的模板流程：先累计尝试次数，再分派到子类 execute()。
        # 计数发生在调用前，所以 execute() 即使抛异常，这次尝试也已经被计入。
        self._call_count += 1
        return self.execute(payload)

    # 装饰器在类创建时登记抽象成员。它不像普通类型注解那样仅供静态工具使用：
    # 仍未实现 execute() 的子类会在“实例化”时被 ABCMeta 以 TypeError 拒绝。
    @abstractmethod
    def execute(self, payload: Mapping[str, object]) -> str:
        """子类必须实现核心动作；共享的 run() 仍由基类提供。"""


class UppercaseManagedTool(ManagedTool):
    # 方法名和签名覆盖抽象成员，因此这个具体子类可以实例化。
    def execute(self, payload: Mapping[str, object]) -> str:
        # Mapping[str, object] 只保证键为 str、值是某种对象；get 缺键时也会得到 None。
        text: object | None = payload.get("text")
        # ABC 只约束类结构，不验证 payload 数据，因此边界值仍需在运行时收窄。
        if not isinstance(text, str):
            raise TypeError("payload.text 必须是字符串")
        # str 不可变；upper() 返回新字符串，不会修改传入映射中的原值。
        return text.upper()


class IncompleteManagedTool(ManagedTool):
    """故意没有实现 execute，因此仍是抽象类。"""


# 这个最小协议只描述“可读取、结果为 str 的 title 属性”，不要求对象属于某个具体模型类。
class HasTitle(Protocol):
    # 用 property 描述属性式读取；调用方写 selected.title，而不是 selected.title()。
    @property
    def title(self) -> str: ...


# `T: HasTitle` 是上界：T 可以是任意满足 HasTitle 的更具体类型，且返回时保留该具体类型。
def first_titled[T: HasTitle](values: Sequence[T]) -> T:
    """bound 允许使用 title 能力，同时保留调用者的具体元素类型。"""
    # first() 把元素类型 T 带到 selected；上界又保证函数体能安全读取 selected.title。
    selected: T = first(values)
    # 空字符串在布尔上下文中为 False；这里只校验标题非空，不改变 selected。
    if not selected.title:
        raise ValueError("title 不能为空")
    return selected


# `A: (str, bytes)` 是有限约束（constraint）：每次调用从候选中求出一个共同的 A。
# 两个参数不能一个选 str、另一个选 bytes；传入子类时，静态结果会归一到匹配的候选家族。
def combine_text[A: (str, bytes)](left: A, right: A) -> A:
    """约束要求两个位置选择同一个 str/bytes 家族，并保留该家族结果。"""
    # 相同类型关系保证 + 对本次家族有定义：str 做文本拼接，bytes 做字节拼接。
    return left + right


def passthrough_union(value: str | bytes) -> str | bytes:
    """普通 union 只说单个位置可为两者之一，不建立输入与输出的同型关系。"""
    # 运行时仍原样返回对象；差异在静态信息：返回注解只承诺 str | bytes 这个可能集合。
    return value


def append_debug_marker(values: list[object]) -> None:
    """能接收 list[object] 的函数有权插入任意 object。"""
    # list 可变，append() 会原地污染调用者持有的同一个列表。这正是 list 元素类型不变的原因：
    # 若把 list[SearchHit] 放宽成 list[object]，这里合法追加的 str 会破坏原列表的元素保证。
    values.append("debug-marker")


def demonstrate_generics() -> None:
    # 本组先用普通值观察 T 怎样连接参数与返回值，再比较 Result 的两个分支和新旧泛型类语法。
    print_section("1. 泛型表达多个位置之间的关系")

    # 两次调用运行时都只是普通函数调用；静态分析分别把 T 求解为 int 和 str。
    # 元组的类型可读作“任意长度的 str 序列”，first() 再把元素类型带到返回值。
    number = identity(15)
    title = first(("Protocol", "ABC", "Generic"))
    # type(value).__name__ 读取实际运行时类名，用来对照类型检查器推断和真实对象是否一致。
    print("identity(15) 保留值/类型：", number, type(number).__name__)
    print("first(tuple[str, ...]) 返回 str：", title, type(title).__name__)

    # 空 tuple 写作 ()。first() 无法产生 T，于是抛 ValueError；这里只捕获预期异常，
    # 让实验继续执行，同时打印失败原因，而不是把所有 Exception 都吞掉。
    try:
        first(())
    except ValueError as error:
        print("空序列没有可返回的 T：", error)

    # 两个变量都声明为 Result[int]，但运行时实际分别装着 Success[int] 和 Failure。
    # Failure 不需要 int 值：它保存的是这次失败的原异常对象。
    success: Result[int] = Success(number)
    failure: Result[int] = Failure(ValueError("保留的原始失败"))
    # isinstance 根据实际 dataclass 分支收窄联合；Success 分支中 value 因而仍按 int 使用。
    if isinstance(success, Success):
        print("Result[int] 成功值仍可做整数计算：", success.value + 1)
    # Failure 分支可以同时观察异常类名和异常消息，证明没有只留下一个错误字符串。
    if isinstance(failure, Failure):
        print("失败分支保留异常：", type(failure.error).__name__, failure.error)

    # 两个 Box 都由实参推断元素类型为 str；get() 把各自保存的值原样返回。
    # 输出相同能力是为了说明这里只更换了泛型声明语法，没有更换运行时容器行为。
    legacy: LegacyBox[str] = LegacyBox("旧语法")
    modern: Box[str] = Box("3.12+ 主写法")
    print("传统 TypeVar/Generic 与新语法：", legacy.get(), "|", modern.get())


def demonstrate_protocol_backends() -> tuple[SearchBackend, SearchBackend]:
    # 本组构造两个没有继承 SearchBackend 的类，再让同一段调用代码通过协议类型使用它们。
    print_section("2. duck typing 与可替换的 SearchBackend")

    # 外层 tuple 是传给内存后端的初始只读序列；其中每项都是 frozen dataclass SearchHit。
    # 这两条数据故意分别覆盖 Protocol 和 ABC 查询，方便观察匹配结果。
    memory_backend = MemorySearchBackend(
        (
            SearchHit(
                document_id="memory-1",
                title="Protocol 入门",
                snippet="结构满足即可替换实现。",
                source="memory",
            ),
            SearchHit(
                document_id="memory-2",
                title="ABC 生命周期",
                snippet="继承基类获得共享流程。",
                source="memory",
            ),
        )
    )
    # __file__ 是当前源码路径；with_name() 只替换文件名，因此不依赖启动程序时的工作目录。
    # FixtureSearchBackend 会立即读取、校验 JSON，并在内部建立另一个 MemorySearchBackend。
    fixture_path: Path = Path(__file__).with_name("search_fixture.json")
    fixture_backend = FixtureSearchBackend(fixture_path)

    # 两个类都没写“继承 SearchBackend”，但方法结构兼容，仍可放进协议类型的同一个 tuple。
    # 这份注解是给 Pyright 的静态契约；运行时 tuple 只保存两个普通实例引用。
    backends: tuple[SearchBackend, SearchBackend] = (memory_backend, fixture_backend)
    # 每轮 backend 指向其中一个实现；调用者不检查类名，也不知道数据来自内存还是 JSON。
    for backend in backends:
        hits: list[SearchHit] = backend.search("Protocol", limit=2)
        # 列表推导式从每个 SearchHit 读取 document_id，生成只用于展示的新 str 列表。
        print(type(backend).__name__, "->", [hit.document_id for hit in hits])

    # runtime_checkable 让 isinstance 可运行；列表推导式分别得到两个布尔值。
    # True 只证明 search 成员存在，不证明完整签名与行为，下一组实验专门展示这个边界。
    print("两种实现都通过运行时成员检查：", [isinstance(item, SearchBackend) for item in backends])
    # 把两个后端交给后续注册表实验继续复用，具体类型在返回注解中抽象为协议。
    return backends


def demonstrate_runtime_protocol_limit() -> None:
    # 本组把“运行时能看到成员”与“静态签名真正兼容”故意拆开，避免把浅检查当类型验证。
    print_section("3. runtime_checkable 不检查完整签名或业务行为")

    # 候选值的静态类型先视为 object，isinstance 分支才会按 runtime-checkable 协议做类型收窄。
    candidate: object = load_runtime_candidate()
    # 实际对象确实有名为 search 的成员，所以尽管参数注解完全相反，这里仍得到 True。
    passed: bool = isinstance(candidate, SearchBackend)
    print("错误注解签名仍通过 isinstance：", passed)
    if passed:
        # True 分支内，类型检查器把 candidate 收窄成 SearchBackend，允许按协议写法调用。
        # 运行时只做普通参数绑定，不执行注解校验；反例函数体又只格式化参数，所以碰巧能执行。
        hits: Sequence[SearchHit] = candidate.search("Protocol", limit=1)
        # 这里有一项固定反例结果，因此安全取下标 0；它只能证明当前假实现走通这一条路径。
        print("动态调用碰巧走通：", hits[0].title)
    # 同一个对象赋给 SearchBackend 的静态反例放在不可执行文件中，避免正常演示因故意错误失败。
    print("静态赋值为何失败请运行 Pyright 检查 static_errors.py")


def print_tool_result(label: str, result: Result[list[SearchHit]]) -> None:
    # Success/Failure 是可区分的运行时类；先判断成功分支，也同步帮助 Pyright 收窄联合。
    if isinstance(result, Success):
        # value 的静态类型是 list[SearchHit]，运行时再展示其真实类名和元素数量。
        print(f"{label}：成功，具体值类型={type(result.value).__name__}，数量={len(result.value)}")
    else:
        # Result 只有两个成员，排除 Success 后此处就是 Failure，可以直接访问原异常 error。
        print(f"{label}：失败，{type(result.error).__name__}：{result.error}")


def demonstrate_registry(
    backends: tuple[SearchBackend, SearchBackend],
) -> list[SearchHit]:
    # 本组把协议后端包装成统一 Tool，并让同构注册表调度五条可观察的成功/失败路径。
    print_section("4. Tool[Input, Output]、注册表与 Result[Output]")

    # 映射中所有值共享 Tool[SearchRequest, list[SearchHit]] 这一组类型参数：
    # 前两个是可调用类实例，第三个是普通函数，结构兼容使它们可放进同一注册表。
    tools: Mapping[str, Tool[SearchRequest, list[SearchHit]]] = {
        "memory_search": SearchTool(backends[0]),
        "fixture_search": SearchTool(backends[1]),
        "empty_search": empty_search_tool,
    }
    # 构造器会复制映射；随后注册的离线工具只在 registry 内部 dict 中增加一个名字。
    registry = ToolRegistry(tools)
    registry.register("offline_search", SearchTool(OfflineSearchBackend()))

    # 同一个不可变请求对象送给每个工具，便于只比较实现路径差异。
    request = SearchRequest(query="Protocol", limit=2)
    # 五次 invoke 都返回相同静态类型 Result[list[SearchHit]]；运行时分支分别是：
    # 两个真实命中、一个空成功、一个保存 OSError 的失败、一个保存 LookupError 的失败。
    memory_result = registry.invoke("memory_search", request)
    fixture_result = registry.invoke("fixture_search", request)
    empty_result = registry.invoke("empty_search", request)
    offline_result = registry.invoke("offline_search", request)
    missing_result = registry.invoke("missing", request)

    # 每个二元组是（展示标签，Result）；for 在每轮按位置解包为 label 和 result。
    # 统一交给 print_tool_result 后，一个失败不会中止剩余场景。
    for label, result in (
        ("内存工具", memory_result),
        ("fixture 工具", fixture_result),
        ("普通函数工具", empty_result),
        ("受控后端失败", offline_result),
        ("未知工具", missing_result),
    ):
        print_tool_result(label, result)

    # 后续 Callable/ABC 实验需要实际命中，因此只从 fixture 的成功分支取值。
    if isinstance(fixture_result, Success):
        # Result 的 T 是 list[SearchHit]，分支收窄后无需 Any/cast 就能取 SearchHit。
        selected: SearchHit = first(fixture_result.value)
        print("成功分支首项：", selected.document_id, selected.title)
        # 返回的是 Success 内保存的同一个 list 引用，没有在此复制。
        return fixture_result.value
    # fixture 是仓库内固定合法数据；走到这里说明示例自身假设被破坏，应作为程序错误停止。
    raise AssertionError("内置 fixture 应产生成功结果")


def demonstrate_callable_and_abc(hits: Sequence[SearchHit]) -> None:
    # 本组先比较两种“可调用对象”签名，再观察 ABC 如何复用状态并拒绝不完整子类实例化。
    print_section("5. Callable、__call__ Protocol 与 ABC")

    # format_titles 是普通函数，签名满足 HitFormatter，apply_formatter 通过参数间接调用它。
    print("Callable[[Sequence[SearchHit]], str]：", apply_formatter(hits, format_titles))
    # 普通函数也能结构化满足 __call__ 协议；变量注解要求它保留 prefix 这个关键字名。
    formatter: PrefixedFormatter = format_with_prefix
    print("__call__ 协议保留关键字规则：", formatter(hits, prefix="命中："))

    # 具体 ABC 子类继承 __init__、run 和 property；run 内再动态分派到覆盖后的 execute。
    managed = UppercaseManagedTool()
    print("ABC 共享 run 流程：", managed.run({"text": "agent tool"}))
    # 上一次 run 已在调用 execute 前把实例计数从 0 增至 1。
    print("ABC 共享调用次数：", managed.call_count)

    # 不完整子类的“类定义”本身允许存在；真正调用类对象尝试创建实例时才抛 TypeError。
    try:
        # 此处故意触发运行时规则；行尾指令只压制 Pyright 对已知抽象类用法的静态诊断。
        IncompleteManagedTool()  # pyright: ignore[reportAbstractUsage]
    except TypeError as error:
        # 只捕获预期的实例化错误，并展示类型；对象没有创建成功，任何方法也没有运行。
        print("未实现 abstractmethod 不能实例化：", type(error).__name__)


def demonstrate_bounds_constraints_and_variance(hits: list[SearchHit]) -> None:
    # 本组用同一批 SearchHit 对照 bound、constraint、union，再故意演示可变列表的类型污染。
    print_section("6. bound、约束、union 与可变容器")

    # hits 的元素满足 HasTitle；bound 允许读取 title，同时把具体 SearchHit 类型带回 selected。
    selected: SearchHit = first_titled(hits)
    print("bound 保留 SearchHit：", selected.document_id, selected.title)
    # 两次合法调用分别让受约束的 A 统一选择 str 家族和 bytes 家族。
    print("约束让两个位置同为 str：", combine_text("tool-", "result"))
    print("约束让两个位置同为 bytes：", combine_text(b"tool-", b"result"))
    # 普通 union 的实际值仍是传入的 str，但签名只表达返回值属于两种可能之一。
    print("union 只声明可能集合：", passthrough_union("query"))

    # [:1] 浅复制一个只含首项的新 list：容器与 hits 分离，内部 SearchHit 引用仍共享。
    contaminated: list[SearchHit] = hits[:1]
    # 这行故意越过 list 的静态不变性；局部副本让反例不会污染原 hits。
    # 行尾指令仅关闭这一行的 Pyright 参数诊断，不会改变运行时调用。
    append_debug_marker(contaminated)  # pyright: ignore[reportArgumentType]
    # 推导式显示污染后两个元素的实际类名，稳定结果是 SearchHit 与 str。
    print("若放行 list[SearchHit] -> list[object]：", [type(item).__name__ for item in contaminated])
    # Sequence 不提供 append，适合只读消费者；要修改时才应请求准确元素类型的 list。
    print("因此只读公开参数优先 Sequence；需要追加时才索取 list")


def main() -> None:
    """按从最小泛型到完整工具接口的顺序运行所有同步实验。"""
    # 前三组定义/验证基础关系；第二组返回的两个协议后端会继续流入注册表。
    demonstrate_generics()
    backends: tuple[SearchBackend, SearchBackend] = demonstrate_protocol_backends()
    demonstrate_runtime_protocol_limit()
    # 注册表返回 fixture 的命中列表，最后两组把它作为回调输入和泛型/方差样本继续使用。
    hits: list[SearchHit] = demonstrate_registry(backends)
    demonstrate_callable_and_abc(hits)
    demonstrate_bounds_constraints_and_variance(hits)


# 导入本模块时，Python 会创建上面的函数与类，但 __name__ 是模块名，因此不会自动跑实验。
# 直接执行 example.py 时 __name__ 才等于 "__main__"，于是从 main() 开始按顺序产生六组输出。
if __name__ == "__main__":
    main()
