"""第 17 章：迭代器、生成器与惰性数据流。

每一段都打印生产与消费事件。示例只访问同目录的受控 JSONL 数据，不访问网络，
也不要求 API Key。
"""

# 建议按 main() 中的六个 demonstrate_* 函数依次阅读：先理解迭代协议和生成器暂停，
# 再看推导式、yield from，最后把这些规则放进 JSONL 拉式流水线。
# 流水线的数据方向是“文件 -> 过滤 -> 转换 -> 限量 -> 消费者”；真正执行时则由消费者调用
# next()，一层层向上游索要下一项。events 会同时记录两种方向上的事件，方便核对先后顺序。

# json.loads() 把文件中的一行 JSON 文本解析成 Python 对象；此时对象结构仍不可信，
# 后面还要交给 parse_document() 做运行时边界校验。
import json

# 这些泛型描述函数需要或返回的迭代能力。它们主要服务于阅读和静态类型检查，
# 不会在运行时替我们验证每个元素的真实类型。
from collections.abc import Generator, Iterable, Iterator

# Path 用对象表示路径；相比手工拼接字符串，它能直接提供 open()、name 等路径操作。
from pathlib import Path

# TypedDict 用于描述“具有固定键和值类型的字典”，但运行时得到的仍是普通 dict。
from typing import TypedDict

# __file__ 是当前源码文件的路径；with_name() 只替换文件名，所以无论从哪个工作目录启动，
# 都会定位到与 example.py 同目录的 documents.jsonl。
DATA_PATH: Path = Path(__file__).with_name("documents.jsonl")
# 关闭动作会以这条固定消息写入 events。后面的检查用成员测试确认清理路径确实执行过。
CLOSED_EVENT = "源：文件已关闭"


# Document 是 JSONL 数据通过校验后在流水线内部使用的结构约定。
# 它不会像 Java DTO 那样创建 Document 实例：值仍是 dict，通过 document["title"] 访问字段；
# 这里列出的键只让编辑器、类型检查器和读者知道流水线可以依赖哪些内容。
class Document(TypedDict):
    """从 JSONL 边界校验后，流水线内部使用的文档结构。"""

    document_id: str
    title: str
    category: str
    score: float


# SearchHit 是转换后的精简字典，只保留消费者真正需要的标识和展示文本。
# 把输入、输出定义成两个 TypedDict，可以让转换前后的字段边界在类型提示中保持清晰。
class SearchHit(TypedDict):
    """转换阶段交给消费者的精简结果。"""

    document_id: str
    label: str


# 为输入格式错误定义专门异常，调用方就能把“数据不符合约定”与其他 ValueError 场景区分开。
# 继承 ValueError 表明错误值已经存在，但内容或类型不满足当前数据边界。
class DocumentFormatError(ValueError):
    """某行 JSON 的字段不满足本地文档约定。"""


# 这个异常只服务于中途失败实验，用来证明：某一项转换失败前，先前 yield 的结果已经交付。
class DocumentTransformError(RuntimeError):
    """教学用的可控转换失败。"""


# -> None 表示函数只产生打印副作用，没有有意义的返回值。
# f-string 中的 {title} 会在调用时求值并嵌入字符串；开头的 \n 先输出一个空行分隔实验。
def print_section(title: str) -> None:
    print(f"\n=== {title} ===")


# events 是调用方传入的可变 list。本函数先原地 append，再打印同一条消息；
# 因此调用结束后，外部仍可通过原来的 list 引用检查完整事件顺序。
def record(events: list[str], message: str) -> None:
    """同时保存和打印事件，便于断言，也便于肉眼观察真实顺序。"""
    events.append(message)
    print(f"  事件 | {message}")


# Iterable[str] 只要求 values 能产生字符串，不限制它必须是 list；tuple 或生成器也都能传入。
# 返回值则明确物化成 list，便于与真实 for 循环的消费结果比较。
def consume_like_for(values: Iterable[str]) -> list[str]:
    """显式写出 for 循环最核心的 iter/next/StopIteration 过程。"""
    # iter(values) 向可迭代对象请求一个迭代器；真正的当前位置保存在 iterator 中。
    iterator: Iterator[str] = iter(values)
    consumed: list[str] = []

    # Python 的 for 会替我们重复 next() 并把 StopIteration 当作正常结束。
    # 这里手写同样的协议；故意用无限循环，把“何时停止”交给迭代器决定。
    while True:
        try:
            # 每次 next() 只索取一项；若有值，item 就绑定到这次返回的字符串。
            item: str = next(iterator)
        except StopIteration:
            # 只捕获协议规定的耗尽信号。生产过程中的其他异常仍应向调用方传播。
            print("  手动消费者收到 StopIteration，循环结束")
            break
        # 只有成功取得值才会到这里；先观察，再把该值保存到最终的急切结果中。
        print(f"  手动消费者拿到：{item}")
        consumed.append(item)
    # while 结束说明输入已耗尽，返回的新 list 保存了所有已经消费的字符串引用。
    return consumed


# 只要函数体里出现 yield，它就是生成器函数。调用 counted_source(...) 只创建生成器对象，
# 函数体要等第一次 next() 才开始；Iterator[int] 表示它随后会逐项产出整数。
def counted_source(limit: int, events: list[str]) -> Iterator[int]:
    """每次只生产一个整数，并让被保留的局部状态可以观察。"""
    produced = 0
    record(events, f"生成器函数体开始，produced={produced}")

    # produced 同时是产出数量和下一次恢复时要继续使用的局部状态。
    while produced < limit:
        produced += 1
        record(events, f"生产第 {produced} 个值")
        # yield 把当前整数交给这次 next() 的调用者，并暂停整个函数，而不是退出函数。
        yield produced
        # 只有调用方再次请求值，控制流才会从上一个 yield 后面恢复；produced 不会重新初始化。
        record(events, f"从 yield 后恢复，produced 仍为 {produced}")

    # 函数自然结束或执行不带值的 return，都会让下一次 next() 收到 StopIteration。
    record(events, "执行 return，后续 next 将得到 StopIteration")


# 这是普通函数而非生成器函数，所以调用时会立即执行函数体并返回完整 list。
# 第 3 个实验把它放在生成器表达式最左侧，用事件证明那一小部分会在创建表达式时求值。
def build_eager_iterable(events: list[str]) -> list[int]:
    """普通函数用于暴露生成器表达式最左侧表达式的立即求值。"""
    record(events, "计算最左侧 iterable 表达式并创建 list")
    return [1, 2, 3]


# 把平方计算包装成函数只是为了记录“元素表达式何时真正求值”；返回值仍是普通 int。
def trace_square(value: int, events: list[str]) -> int:
    record(events, f"计算 {value} 的平方")
    return value * value


# Generator[产出类型, send 类型, return 完成值类型] 比 Iterator 能表达更完整的生成器协议。
# 这里逐项 yield str，不接收 send() 的非 None 值，最终 return 的完成值也是 str。
def one_chunk_then_return(events: list[str]) -> Generator[str, None, str]:
    """return 的字符串不是 yield 出去的数据，而是生成器完成值。"""
    record(events, "准备 yield 一个数据块")
    yield "数据块"
    # 再次 next() 后从上面的 yield 恢复；return 的字符串进入 StopIteration.value，
    # 它不会像又写了一次 yield 那样成为普通遍历结果。
    record(events, "执行 return，结束生成器")
    return "完成值"


# 把任意字符串 iterable 加上来源名前缀再逐项转发。传入 tuple 时仍不会先复制成 list。
def named_source(name: str, values: Iterable[str], events: list[str]) -> Iterator[str]:
    record(events, f"{name} 开始")
    # for 每轮从 values 取一个 value；item 是当前轮新建的格式化字符串。
    for value in values:
        item: str = f"{name}:{value}"
        record(events, f"{name} yield {item}")
        # 每产出一项就暂停，所以下一轮要等下游再次请求时才会发生。
        yield item
    # 走到这里表示 values 已自然耗尽；没有 yield 的 return 只结束本层生成器。
    record(events, f"{name} return")


# 这个生成器展示两种产出方式：yield 自己的一项，yield from 委托另一个 iterable。
def combine_sources(events: list[str]) -> Iterator[str]:
    """yield 负责单个值，yield from 逐项转交两个来源。"""
    record(events, "组合器开始")
    yield "组合器:开头"
    # yield from 会依次转交子生成器的每一项及其暂停过程，不会预先物化整个子来源。
    yield from named_source("本地", ("A", "B"), events)
    yield from named_source("缓存", ("C",), events)
    record(events, "组合器 return")


# raw 标成 object，表示 json.loads() 的边界输出尚不能信任；它可能是 dict、list、数字等。
# 参数列表中的 * 让 line_number 只能按关键字传入，调用处必须明确写出行号的含义。
# 校验通过后返回符合 Document 结构的普通 dict，并统一把 score 转成 float。
def parse_document(raw: object, *, line_number: int) -> Document:
    """做最小运行时校验，避免用类型注解冒充 JSON 边界验证。"""
    if not isinstance(raw, dict):
        raise DocumentFormatError(f"第 {line_number} 行必须是 JSON object")

    # dict.get() 在键不存在时返回 None，便于下面把“缺字段”和“值类型错误”统一拒绝。
    # 此处只提取允许的字段，输入里即使有额外键也不会被带入流水线内部。
    document_id: Any | None = raw.get("document_id")
    title: Any | None = raw.get("title")
    category: Any | None = raw.get("category")
    score: Any | None = raw.get("score")

    # `or not value` 还会拒绝空字符串；每个分支都带行号，错误能定位回 JSONL 原文。
    if not isinstance(document_id, str) or not document_id:
        raise DocumentFormatError(f"第 {line_number} 行 document_id 必须是非空字符串")
    if not isinstance(title, str) or not title:
        raise DocumentFormatError(f"第 {line_number} 行 title 必须是非空字符串")
    if not isinstance(category, str) or not category:
        raise DocumentFormatError(f"第 {line_number} 行 category 必须是非空字符串")
    # bool 在 Python 中是 int 的子类，所以 isinstance(True, (int, float)) 为 True；
    # 业务分数不应接受布尔值，必须先单独排除，再用类型元组同时接受 int 和 float。
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise DocumentFormatError(f"第 {line_number} 行 score 必须是数字")

    # 新建字典切断了对原始解析结果的依赖；float(score) 让内部数值表示稳定为浮点数。
    return {
        "document_id": document_id,
        "title": title,
        "category": category,
        "score": float(score),
    }


# Generator[Document] 暴露了 close()，持有者可在提前停止时显式触发 finally。
# 这是拉式文件源：首次消费才打开文件，每恢复一次最多继续读到下一条有效文档。
def load_jsonl(path: Path, events: list[str]) -> Generator[Document]:
    """打开后逐行解析；函数内部没有先 list(...) 缓存所有文档。"""
    # 这行位于生成器函数体内，所以调用 load_jsonl(...) 时还不会执行；
    # 第一次 next(source) 才会从这里开始运行并尝试打开文件。
    record(events, f"源：准备打开 {path.name}")
    # opened 区分“成功进入 with 后的关闭”与“open() 自身失败”，避免记录不存在的关闭动作。
    opened = False
    try:
        # with 类似 Java try-with-resources；但生成器在内部 yield 时，with 代码块也会一起暂停，
        # 文件会保持打开，直到继续耗尽、异常退出或生成器被显式 close()。
        with path.open("r", encoding="utf-8") as file:
            opened = True
            record(events, "源：文件已打开")
            # 文件对象本身可迭代，每轮惰性读取一行。enumerate(..., start=1) 同时生成从 1
            # 开始的行号，并将二元组解包到 line_number、line，正好对应编辑器中的行号。
            for line_number, line in enumerate(file, start=1):
                record(events, f"源：读取第 {line_number} 行")
                # strip() 返回去掉两端空白的新字符串；空行不进入 JSON 解析，直接请求下一行。
                text: str = line.strip()
                if not text:
                    continue

                # loads() 处理的是 str 而不是文件；非法 JSON 会在消费到这一行时直接抛异常。
                raw: object = json.loads(text)
                # 关键字实参让“当前枚举行号传给仅限关键字参数”一目了然。
                document: Document = parse_document(raw, line_number=line_number)
                record(events, f"源：yield {document['document_id']}")
                # 暂停前把一条已校验字典交给过滤器；下一行要等整个下游再次拉取才读取。
                yield document
    finally:
        # 源自然耗尽、源内部在消费时抛异常，或持有者显式 close()，都会关闭文件并经过这里。
        # 如果 open() 自己失败，opened 仍为 False，不能错误声称一个从未打开的文件已被关闭。
        if opened:
            record(events, CLOSED_EVENT)


# 输入只要求 Iterable[Document]，因此既能接 list/tuple，也能直接串接一次性的文件生成器。
# 参数列表中的 * 表示后面三项必须使用关键字传参，避免多个字符串、数字实参含义混淆。
def filter_documents(
    documents: Iterable[Document],
    *,
    category: str,
    minimum_score: float,
    events: list[str],
) -> Iterator[Document]:
    # 下游每要一项，本层就从 documents 逐条寻找下一条符合两个条件的文档。
    for document in documents:
        # and 具有短路语义；这里两侧只是 dict 读取和比较，不会提前消费更多上游数据。
        accepted: bool = document["category"] == category and document["score"] >= minimum_score
        # 条件表达式按“为真时的值 if 条件 else 为假时的值”读取，只选择一段展示文本。
        decision: Literal["通过"] | Literal["丢弃"] = "通过" if accepted else "丢弃"
        record(events, f"过滤：{document['document_id']} -> {decision}")
        if accepted:
            # 通过时转交原来的 Document 字典引用，不复制；丢弃时循环继续向上游取下一项。
            yield document


# 转换器仍然是一进一出的惰性阶段，只在下游索取时才构造对应 SearchHit。
def transform_documents(documents: Iterable[Document], events: list[str]) -> Iterator[SearchHit]:
    for document in documents:
        # 受控错误放在真正构造结果之前，用来模拟“前几项成功，后续某项转换失败”。
        if document["document_id"] == "doc-error":
            record(events, "转换：doc-error 抛出受控异常")
            raise DocumentTransformError("doc-error 无法转换")

        # 这里新建一个 SearchHit 字典，不修改上游 Document。
        # f-string 的 :.2f 格式规格把分数显示为固定两位小数，例如 0.95 -> "0.95"。
        hit: SearchHit = {
            "document_id": document["document_id"],
            "label": f"{document['title']} ({document['score']:.2f})",
        }
        record(events, f"转换：yield {hit['document_id']}")
        # 只有这个结果被请求时才 yield，因此错误文档之后的记录不会被提前转换。
        yield hit


# 这个限量器通过显式 next() 精确控制拉取次数，而不是先取得第 limit + 1 项再判断停止。
def take_hits(hits: Iterable[SearchHit], limit: int, events: list[str]) -> Iterator[SearchHit]:
    """最多拉取 limit 项，不为判断停止而多拉取第 limit + 1 项。"""
    if limit < 0:
        # 因为本函数含 yield，调用只创建生成器；这个检查也要等首次消费时才执行。
        raise ValueError("limit 不能小于 0")

    # 统一取得一个迭代器并保存其位置。若 hits 已是迭代器，iter(hits) 通常仍返回它自己。
    iterator: Iterator[SearchHit] = iter(hits)
    # range(1, limit + 1) 产生 1 到 limit；limit 为 0 时循环零次，上游一次也不会被拉取。
    for position in range(1, limit + 1):
        try:
            # 每轮只在这里向上游请求一项，所以成功次数最多就是 limit。
            hit: SearchHit = next(iterator)
        except StopIteration:
            # 上游不足 limit 项属于正常结束；生成器中的 return 会让下游也收到 StopIteration。
            record(events, "限量：上游已经耗尽")
            return
        record(events, f"限量：放行第 {position} 条 {hit['document_id']}")
        yield hit

    # 完成恰好 limit 轮后直接结束，不再用一次多余 next() 探测上游是否还有数据。
    record(events, f"限量：已达到 {limit} 条，不再请求上游")


# build_pipeline 本身没有 yield，是普通函数；调用时会执行组装代码并立即返回最外层生成器。
# 各阶段函数都含 yield，所以这里创建它们并不会遍历 documents，也不会打开文件源。
def build_pipeline(
    documents: Iterable[Document],
    *,
    limit: int,
    events: list[str],
) -> Iterator[SearchHit]:
    """这里只组装惰性阶段，不消费 documents。"""
    # 第一层只保存筛选规则及上游引用；关键字参数让两个业务条件清晰可见。
    filtered: Iterator[Document] = filter_documents(
        documents,
        category="python",
        minimum_score=0.8,
        events=events,
    )
    # 转换器把 filtered 作为自己的上游，限量器再包住转换器，形成一条相互引用的惰性链。
    transformed: Iterator[SearchHit] = transform_documents(filtered, events)
    # 返回的是尚未启动的 take_hits 生成器；真正的数据流动由后续消费者的 next()/for 触发。
    return take_hits(transformed, limit, events)


# 这是明确的“物化边界”：把逐项到达的 SearchHit 保存成可重复读取的 list。
# 函数不捕获上游异常；若消费失败，异常会传播给调用方，局部 results 不会作为返回值交付。
def consume_hits(hits: Iterable[SearchHit], events: list[str]) -> list[SearchHit]:
    """消费者决定何时把惰性流物化为 list。"""
    results: list[SearchHit] = []
    # for 会一直拉取直到上游 StopIteration；每项先记录消费事件，再追加同一字典引用。
    for hit in hits:
        record(events, f"消费者：收到 {hit['document_id']}")
        results.append(hit)
    # 只有上游自然结束时才运行到这里并返回完整结果。
    return results


def demonstrate_iteration_protocol() -> None:
    print_section("1. iterable、iterator、next 与 StopIteration")
    # list 是保存元素的可迭代容器。每次 iter(container) 都创建一条从开头出发的新迭代路径，
    # 所以下面两个对象互不共享当前位置。
    container: list[str] = ["A", "B", "C"]
    first_iterator: Iterator[str] = iter(container)
    second_iterator: Iterator[str] = iter(container)

    # is 比较两个名字是否指向同一对象，不是比较内容；这里预期打印 True。
    print("  list 两次 iter 得到不同迭代器：", first_iterator is not second_iterator)
    # list(container) 内部各自创建并耗尽一个新迭代器，不会消耗 container 本身保存的元素，
    # 所以两次都能得到 ["A", "B", "C"]。
    print("  容器第一次遍历：", list(container))
    print("  容器第二次遍历：", list(container))

    # 赋值不会复制迭代器。shared_iterator 和 alias 是同一个对象的两个名字，位置完全共享。
    shared_iterator: Iterator[str] = iter(container)
    alias: Iterator[str] = shared_iterator
    # 迭代器的 iter() 返回它自己；alias 取走 "A" 后，另一个名字也只能从 "B" 继续。
    print("  iter(iterator) 仍是自己：", iter(shared_iterator) is shared_iterator)
    print("  alias 取走一个值：", next(alias))
    print("  原名字只能看到剩余值：", list(shared_iterator))
    # 上一行的 list(...) 已持续消费到 StopIteration，同一个对象不会自动复位。
    print("  同一个迭代器第二次已经为空：", list(alias))

    # 单元素 tuple 必须写成 ("唯一值",)，结尾逗号才定义 tuple；圆括号本身只是分组。
    exhausted: Iterator[Literal["唯一值"]] = iter(("唯一值",))
    print("  next 取得：", next(exhausted))
    try:
        # 唯一值已被取走，再调用 next() 会按协议抛 StopIteration。
        next(exhausted)
    except StopIteration:
        print("  再次 next：捕获 StopIteration")
    # next(iterator, default) 把耗尽转换为默认返回值，适合不想写 try/except 的探测场景。
    print("  next(iterator, default)：", next(exhausted, "<已耗尽>"))

    # tuple 也是 Iterable[str]；辅助函数会展示 for 在解释器内部依赖的核心协议。
    print("  手写 for 的核心过程，结果：", consume_like_for(("甲", "乙")))


def demonstrate_generator_state() -> None:
    print_section("2. yield 暂停并保留局部状态")
    # events 从空 list 开始。调用生成器函数只得到 source 对象，尚未进入 counted_source 函数体，
    # 所以下一行观察到的长度应是 0。
    events: list[str] = []
    source: Iterator[int] = counted_source(3, events)
    print("  调用生成器函数后事件数：", len(events))

    # 第一次 next() 从函数开头运行到 yield 1；第二次先从 yield 后恢复，再运行到 yield 2。
    print("  第一次 next 返回：", next(source))
    # 生成器表达式逐条检查事件文本，startswith("生产") 为每条消息产生 bool；
    # bool 可按 1/0 被 sum() 相加，因此结果就是已经出现的生产事件数量。
    print("  此时生产事件数：", sum(message.startswith("生产") for message in events))
    print("  第二次 next 返回：", next(source))
    # list(source) 从当前位置继续，而不是重新调用 counted_source；这里只会收集尚未产出的值 3。
    print("  list(source) 消费剩余值：", list(source))
    # 生成器耗尽后会稳定保持耗尽状态，带默认值的 next() 因而返回展示用字符串。
    print("  耗尽后带默认值的 next：", next(source, "<已耗尽>"))

    # 重新调用生成器函数才会得到一条全新的迭代路径和新的局部状态。
    batch_events: list[str] = []
    print("\n  新生成器交给 list(...)：")
    # 这次 list() 一次拉取到自然耗尽，因此三个 yield 值都会被立即物化。
    all_values: list[int] = list(counted_source(3, batch_events))
    print("  list 一次消费得到：", all_values)
    print("  list 调用期间生产数量：", sum(message.startswith("生产") for message in batch_events))


def demonstrate_comprehensions() -> None:
    print_section("3. 列表推导式与生成器表达式")
    # 列表推导式按“对右侧 iterable 的每个 value，计算左侧表达式并收集”来读。
    # 创建 squares 的这一行就会计算三次，并把三个整数全部保存进 list。
    list_events: list[str] = []
    squares: list[int] = [trace_square(value, list_events) for value in (1, 2, 3)]
    print("  列表推导式创建完成，结果：", squares)

    expression_events: list[str] = []
    # 生成器表达式不会立刻计算 trace_square，但最左侧 for 的 iterable 表达式
    # build_eager_iterable(...) 会立即调用，并对返回的 list 立即取得 iterator。
    lazy_squares: Generator[int, None, None] = (
        trace_square(value, expression_events) for value in build_eager_iterable(expression_events)
    )
    # 因而此时只有“创建 list”事件，没有任何平方计算事件。
    print("  生成器表达式刚创建时的事件：", expression_events)
    # 第一次 next() 只计算 1 的平方；list() 随后从当前位置计算 2、3 并耗尽表达式。
    print("  next 只计算一项：", next(lazy_squares))
    print("  list(...) 再计算剩余项：", list(lazy_squares))
    # lazy_squares 是迭代器而不是可重复遍历的结果容器，耗尽后再次 list() 得到空列表。
    print("  同一表达式再次遍历：", list(lazy_squares))

    stream_events: list[str] = []
    # 这里最左侧调用 counted_source(...) 的确立即发生，但它是生成器函数：调用只返回对象，
    # 函数体依然没有启动，所以创建 fully_lazy 后 events 仍为空。
    fully_lazy: Generator[int, None, None] = (trace_square(value, stream_events) for value in counted_source(2, stream_events))
    print("  最左侧本身是生成器时，创建后的事件：", stream_events)
    # 首次 next(fully_lazy) 会先启动上游产出 1，再把 1 交给 trace_square，最后返回平方结果 1。
    print("  首次消费后得到：", next(fully_lazy))


def demonstrate_yield_return_and_delegation() -> None:
    print_section("4. yield、return 与 yield from")
    return_events: list[str] = []
    # 创建时函数体未运行；第一次 next() 才记录事件并取得唯一的 yield 值“数据块”。
    generator: Generator[str, None, str] = one_chunk_then_return(return_events)
    print("  第一次 next 得到 yield 值：", next(generator))
    try:
        # 第二次恢复会执行 return "完成值"，协议通过 StopIteration 结束迭代。
        next(generator)
    except StopIteration as completion:
        # `as completion` 把异常对象绑定到变量；return 的参数保存在它的 value 属性中。
        # 普通 for/list 会自行吞掉 StopIteration，因此只有这种手动消费才能直接观察完成值。
        print("  return 没有再产出一项；完成值是：", completion.value)

    delegation_events: list[str] = []
    # combine_sources 也是生成器函数，创建 combined 时还未记录“组合器开始”。
    combined: Iterator[str] = combine_sources(delegation_events)
    print("  组合生成器刚创建时事件数：", len(delegation_events))
    # list() 负责拉到耗尽，最终顺序是组合器自己的开头、本地两项、缓存一项。
    print("  消费组合结果：", list(combined))


def demonstrate_file_pipeline() -> None:
    print_section("5. JSONL -> 过滤 -> 转换 -> 最多消费 N 条")
    events: list[str] = []
    # source、pipeline 都只是尚未启动的生成器对象。build_pipeline 仅连接各阶段的引用，
    # 所以到 print 时 events 仍为空，documents.jsonl 也尚未打开。
    source: Generator[Document, None, None] = load_jsonl(DATA_PATH, events)
    pipeline: Iterator[SearchHit] = build_pipeline(source, limit=2, events=events)
    print("  流水线组装后事件数（文件尚未打开）：", len(events))

    # 先绑定空 list，使 finally 之后仍有一个定义好的 results 可供打印和断言。
    results: list[SearchHit] = []
    try:
        # consume_hits 的 for 从最下游开始拉取；被过滤的行不会计入 limit，但确实已经从文件读取。
        results = consume_hits(pipeline, events)
        # limit=2 会让限量器主动停止，此时文件源仍暂停在第三行的 yield，尚未自然离开 with。
        print("  达到上限后、显式 close 前已关闭：", CLOSED_EVENT in events)
    finally:
        # take_hits 达到上限时上游仍可继续；创建并持有真正文件源的代码负责结束它。
        # close() 会在源暂停处触发清理，使 load_jsonl 的 with 和 finally 都确定执行。
        source.close()

    # 生成器表达式只筛选以固定前缀开头的事件，sum(bool) 得出真实文件读取次数。
    read_count: int = sum(message.startswith("源：读取第") for message in events)
    print("  最终结果：", results)
    print("  实际只读取行数：", read_count)
    print("  close 后已关闭：", CLOSED_EVENT in events)

    # 列表推导式从两条 SearchHit 中抽取 id；验证第一、三行通过筛选且顺序不变。
    assert [hit["document_id"] for hit in results] == ["doc-001", "doc-003"]
    # 为得到两条合格结果只需读三行；第四行 doc-error 不应被限量器多拉取。
    assert read_count == 3
    # any() 会短路检查事件流；结果为 False 证明连相关日志都没有出现，即错误文档未被触碰。
    assert not any("doc-error" in message for message in events)


def demonstrate_boundaries_and_failure() -> None:
    print_section("6. 空输入、自然耗尽、中途异常与提前停止")

    print("\n  -- 空输入 --")
    empty_events: list[str] = []
    # () 是空 tuple，可作为不会产出任何 Document 的 iterable；首次拉取会沿各层传播耗尽信号。
    empty_results: list[SearchHit] = consume_hits(build_pipeline((), limit=3, events=empty_events), empty_events)
    print("  空输入结果：", empty_results)
    assert empty_results == []

    print("\n  -- 上游少于 N 条，自然耗尽 --")
    # tuple[Document, ...] 中的 ... 表示可以有任意多个 Document 元素。
    # 外层 tuple 不可变，但内部两个 dict 仍是可变对象；本例只读取它们，不做修改。
    safe_documents: tuple[Document, ...] = (
        {
            "document_id": "memory-1",
            "title": "内存文档一",
            "category": "python",
            "score": 0.91,
        },
        {
            "document_id": "memory-2",
            "title": "内存文档二",
            "category": "python",
            "score": 0.89,
        },
    )
    exhaustion_events: list[str] = []
    # limit=5 大于实际两项，因此不是限量器主动截断，而是上游 tuple 先自然耗尽。
    exhausted_pipeline: Iterator[SearchHit] = build_pipeline(safe_documents, limit=5, events=exhaustion_events)
    exhausted_results: list[SearchHit] = consume_hits(exhausted_pipeline, exhaustion_events)
    print("  自然耗尽结果数：", len(exhausted_results))
    # exhausted_pipeline 是同一个已经耗尽的生成器对象，再次 list() 不会重跑 safe_documents。
    print("  同一流水线再次遍历：", list(exhausted_pipeline))
    assert len(exhausted_results) == 2

    print("\n  -- 消费到中途才抛异常，先前结果仍已交付 --")
    failure_events: list[str] = []
    # limit=10 足够大，会继续读取到第四行 doc-error，而不是像上一实验那样提前停止。
    failing_source: Generator[Document, None, None] = load_jsonl(DATA_PATH, failure_events)
    failing_pipeline: Iterator[SearchHit] = build_pipeline(failing_source, limit=10, events=failure_events)
    # 这里不用 results = list(...)，而是逐项 append；这样异常发生时仍能观察已交付的部分结果。
    partial_results: list[SearchHit] = []
    # 先给异常消息一个稳定默认值，便于 try/except 之后做精确断言。
    caught_message: Literal[""] = ""
    try:
        # 前两条合格 hit 会先完成记录和 append；迭代到 doc-error 时才跳出 for 进入 except。
        for hit in failing_pipeline:
            record(failure_events, f"消费者：收到 {hit['document_id']}")
            partial_results.append(hit)
    except DocumentTransformError as error:
        # 只捕获本实验预期的转换异常，其他异常不会被误报成同一种业务失败。
        caught_message = str(error)
        print("  消费者捕获：", type(error).__name__, caught_message)
    finally:
        # 无论是否得到预期异常，都显式关闭仍可能暂停的文件源。
        failing_source.close()

    # 未处理异常从生成器内部逸出后，这条 failing_pipeline 已终止，不能从失败点继续。
    print("  异常前已收到：", [hit["document_id"] for hit in partial_results])
    print("  异常后流水线已耗尽：", list(failing_pipeline))
    print("  异常路径文件已关闭：", CLOSED_EVENT in failure_events)
    # 三个断言分别验证部分结果、具体错误内容和资源清理，避免只检查“确实抛错”。
    assert [hit["document_id"] for hit in partial_results] == ["doc-001", "doc-003"]
    assert caught_message == "doc-error 无法转换"
    assert CLOSED_EVENT in failure_events

    print("\n  -- 消费者提前停止时，break 本身不替持有者关闭源 --")
    early_events: list[str] = []
    early_source: Generator[Document, None, None] = load_jsonl(DATA_PATH, early_events)
    early_pipeline: Iterator[SearchHit] = build_pipeline(early_source, limit=10, events=early_events)
    # 只调用一次 next()，沿整条链拿到第一条合格结果后就停下；源此时暂停在自己的 yield。
    first_hit: SearchHit = next(early_pipeline)
    record(early_events, f"消费者：只要第一条 {first_hit['document_id']}，随后停止")
    # 仍持有 early_pipeline/early_source 引用，不能依赖对象回收时机自动关闭文件。
    print("  停止消费后、显式 close 前已关闭：", CLOSED_EVENT in early_events)
    early_source.close()
    print("  显式 close 后已关闭：", CLOSED_EVENT in early_events)
    # 外层流水线对象本身尚未直接 close，但它再次向已关闭的源拉取时只会收到耗尽信号。
    print("  上游关闭后流水线剩余结果：", list(early_pipeline))
    assert CLOSED_EVENT in early_events


# main 只负责安排演示顺序；上面的 def 在导入模块时创建函数对象，但不会自动执行函数体。
def main() -> None:
    demonstrate_iteration_protocol()
    demonstrate_generator_state()
    demonstrate_comprehensions()
    demonstrate_yield_return_and_delegation()
    demonstrate_file_pipeline()
    demonstrate_boundaries_and_failure()
    print("\n所有第 17 章示例与运行时断言均已完成。")


# Python 直接运行此文件时，内置变量 __name__ 为 "__main__"，于是调用 main()。
# 其他模块若只 import 本文件，条件为 False，便能复用函数而不自动打印或读取数据文件。
if __name__ == "__main__":
    main()
