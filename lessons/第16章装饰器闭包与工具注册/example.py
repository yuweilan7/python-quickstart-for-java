"""第 16 章：装饰器、闭包与同步工具注册。

所有实验都在本机同步完成。计时值会随机器变化；事件顺序、返回值、异常类型与
注册状态由断言验证，不依赖 sleep 或固定线程调度。
"""

# 建议按文件从上往下读：前半部分先“定义”装饰器和被装饰函数，后半部分的
# demonstrate_* 函数再“调用”它们并用断言验证现象。要始终区分两个时刻：
# 装饰器在 def 执行后立即应用，被装饰函数的函数体则要等到显式调用才运行。

# 标准库导入按本章用途可以分成四类：
# - importlib 用于第二次导入实验，验证普通 import 与 import_module 都会复用模块缓存；
# - annotationlib、inspect 用于读取 Python 3.14 延迟注解与函数签名；
# - Callable、wraps 用于保存静态调用关系与运行时元信息；
# - perf_counter_ns 提供单调高分辨率计时，assert_type 则记录静态类型检查预期。
import importlib
from annotationlib import Format, ForwardRef, get_annotations
from collections.abc import Callable
from functools import wraps
from inspect import signature
from time import perf_counter_ns
from types import ModuleType
from typing import assert_type

# 这些对象来自同目录的最小工具注册表。tool 负责注册；其余函数负责查询、
# 动态调用和观察事件。
from tool_registry import (
    DuplicateToolError,
    ToolMetadata,
    get_tool,
    invoke_tool,
    list_tools,
    registration_events,
    tool,
)


# 这个小函数只统一七组实验的输出标题。-> None 表示它不向调用者返回有意义的结果。
def print_section(title: str) -> None:
    # f-string 会先计算花括号中的 title；开头的 \n 让不同实验之间空一行。
    print(f"\n=== {title} ===")


# -----------------------------------------------------------------------------
# 1. 从普通高阶函数推导 @decorator
# -----------------------------------------------------------------------------

# function 是“函数对象”，不是函数调用的结果；Callable[[], str] 表示它不接收参数，
# 调用后返回 str。返回注解相同，表示 announce_call 最终也交回一个同契约的可调用对象。
def announce_call(function: Callable[[], str]) -> Callable[[], str]:
    """最小无参装饰器：接收函数对象，返回另一个函数对象。"""

    # 执行 announce_call 时只是创建 wrapper，并不进入 plain_message 的函数体。
    # wrapper 引用了外层局部名字 function，因此与该绑定共同组成闭包；外层返回后
    # 仍能找到原函数。
    def wrapper() -> str:
        print(f"  包装层：准备调用 {function.__name__}")
        # 这里的括号才真正调用原函数。return 把原结果继续交给 wrapper 的调用者。
        return function()

    # 返回函数对象时没有括号；若写成 wrapper()，会在装饰阶段过早执行它并返回字符串。
    return wrapper


# 这是待装饰的原函数。执行 def 只创建函数对象，return 语句要等 plain_message() 才运行。
def plain_message() -> str:
    return "普通形式完成"


# 右侧先把原函数对象交给 announce_call，返回的 wrapper 再绑定到新名字。
# 因而手工形式可清楚地看见核心模型：decorated = decorator(original)。
manually_decorated_message: Callable[[], str] = announce_call(plain_message)


# @announce_call 是上面赋值模型的语法糖。解释器执行到这个 def 时先创建原函数对象，
# 随即调用 announce_call，再让 syntax_sugar_message 这个名字指向返回的 wrapper。
@announce_call
def syntax_sugar_message() -> str:
    return "@语法完成"


# -----------------------------------------------------------------------------
# 2. 类型保真的同步计时装饰器
# -----------------------------------------------------------------------------

# Python 3.14 的 [**P, R] 在本函数上声明两个类型参数：P 是整组参数规格，R 是返回类型。
# 输入、输出都写 Callable[P, R]，静态检查器便知道装饰前后的参数规则与结果类型应保持一致。
# 这些类型提示不会在运行时自动检查实参；真正的参数绑定仍由被调用函数完成。
def timed[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """记录同步调用耗时，并原样转发参数、返回值和异常。"""

    # timed(function) 执行时会定义 wrapper，并立即把原函数交给 wraps。
    # wraps 不负责计时；它只让返回的包装函数对日志、帮助系统和 inspect 更像原函数。
    @wraps(function)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        # 定义参数时，*args 把位置参数收集为 tuple，**kwargs 把关键字参数收集为 dict。
        # 每次调用 wrapper 都取一个新的起点；计时状态不会跨调用累计。
        started_ns: int = perf_counter_ns()
        try:
            # 调用位置上的 * 与 ** 做相反动作：把 tuple 和 dict 展开后交给原函数绑定。
            # return 很关键，否则原函数虽已执行，调用者却只能得到 wrapper 默认返回的 None。
            return function(*args, **kwargs)
        finally:
            # finally 在正常 return 和异常退出前都会运行。这里没有 except，所以不会吞掉
            # 或替换原异常；计时打印完后，正常结果或异常会沿原来的控制流继续向上传递。
            elapsed_ms: float = (perf_counter_ns() - started_ns) / 1_000_000
            # :.3f 把浮点毫秒数格式化为小数点后三位；具体数值随机器负载变化，不作断言。
            print(f"    [计时] {function.__name__}: {elapsed_ms:.3f} ms")

    # timed 必须返回 wrapper，外部函数名才会被重新绑定到带计时行为的新函数。
    return wrapper


# 装饰器在模块加载、执行这个 def 后立刻应用；此时还不会构造问候语。
@timed
def build_greeting(
    prefix: str, name: str, /, *, punctuation: str = "!"
) -> str:
    """构造一条问候语。"""

    # 签名中的 / 表示它左侧的 prefix、name 只能按位置传入；单独的 * 表示其后的
    # punctuation 只能按关键字传入。wrapper 会原样转发，最终仍由这个签名执行约束。
    return f"{prefix}，{name}{punctuation}"


# 这个函数提供稳定的失败路径，用来证明 timed 的 finally 会执行，但 ValueError 不会被改写。
@timed
def require_positive(value: int) -> int:
    """用受控失败证明装饰器没有把异常改成其他结果。"""

    # 条件成立时 raise 创建并抛出 ValueError，当前函数不会继续执行下面的 return。
    if value <= 0:
        raise ValueError("value 必须大于 0")
    return value


# -----------------------------------------------------------------------------
# 3. 闭包状态、nonlocal 与晚绑定
# -----------------------------------------------------------------------------

# 每应用一次 @count_calls，都会重新调用此函数，创建一份 call_count 和一个新 wrapper。
# 所以同一 wrapper 的多次调用共享计数，不同被装饰函数之间却不会共享这一整数绑定。
def count_calls[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """每个 wrapper 拥有一个跨调用共享、但与其他 wrapper 独立的计数。"""

    # int 是不可变对象；后面的 += 不是修改原整数，而是计算新整数并重新绑定名字。
    call_count = 0

    @wraps(function)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        # nonlocal 指明这里使用最近一层外部函数中的 call_count，而不是创建 wrapper 的
        # 局部变量。若省略它，Python 会因下方存在赋值而把 call_count 判为局部名，
        # 读取旧值时就会报 UnboundLocalError。
        nonlocal call_count
        call_count += 1
        print(f"  {function.__name__} 第 {call_count} 次调用")
        return function(*args, **kwargs)

    return wrapper


# 下面两次应用装饰器各自创建独立闭包。uppercase 调两次会显示 1、2，
# text_length 第一次调用仍显示自己的 1。
@count_calls
def uppercase(text: str) -> str:
    return text.upper()


@count_calls
def text_length(text: str) -> int:
    return len(text)


def make_bad_name_readers(names: list[str]) -> list[Callable[[], str]]:
    """教学反例：所有闭包在调用时读取同一个循环变量 name。"""

    # 列表元素是稍后才调用的无参函数，不是已经算好的字符串。
    readers: list[Callable[[], str]] = []
    # Python 的 for 循环不会像调用函数那样为每轮创建新局部作用域；所有轮次复用同一个 name。
    for name in names:
        # 每轮确实创建了不同的 read_name 函数对象，但它们闭包里引用的是同一个 name 绑定。
        # 调用发生在循环结束后，届时该绑定保存的是 names 的最后一个元素。
        def read_name() -> str:
            # 行尾抑制指令只忽略这个刻意保留的晚绑定告警，不应扩散到其他代码。
            return name  # noqa: B023

        # 这里只保存函数对象，没有 ()，因此循环期间不会读取 name。
        readers.append(read_name)
    return readers


# 工厂函数用一次真实的函数调用制造一层新作用域；每次调用的参数 name 都是独立局部绑定。
def make_name_reader(name: str) -> Callable[[], str]:
    """每调用一次工厂就创建一个新作用域和独立绑定。"""

    # 返回的闭包只引用本次 make_name_reader 调用中的 name，所以之后循环变量再变化也不影响它。
    def read_name() -> str:
        return name

    return read_name


def make_good_name_readers(names: list[str]) -> list[Callable[[], str]]:
    # 推导式从左到右取出每个 name，并立即调用一次工厂；结果列表仍保存函数对象。
    # 等价于循环中反复执行 readers.append(make_name_reader(name))。
    return [make_name_reader(name) for name in names]


# -----------------------------------------------------------------------------
# 4. 装饰器工厂与多层顺序
# -----------------------------------------------------------------------------

# layer 本身是“装饰器工厂”：先接收 label 和共享事件列表，再返回真正接收 function 的 decorate。
# 最外层返回类型可读作 (Callable[P, R]) -> Callable[P, R]，即一个类型保真的装饰器。
def layer[**P, R](
    label: str,
    events: list[str],
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """同时记录工厂求值、装饰器应用以及包装函数调用。"""

    # 这行位于工厂函数体内，所以求值 @layer(...) 表达式时立刻记录，
    # 早于装饰器应用和业务调用。
    events.append(f"求值装饰器表达式：{label}")

    # Python 创建完被装饰函数后，才把它传给 decorate；多层装饰器在此阶段从下往上应用。
    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        events.append(f"应用装饰器：{label} -> {function.__name__}")

        # label、events 和 function 都来自外层作用域，wrapper 以后调用时仍能通过闭包访问它们。
        @wraps(function)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            events.append(f"进入包装层：{label}")
            try:
                return function(*args, **kwargs)
            finally:
                # 多层调用返回时由内向外退出；finally 也保证原函数抛异常时仍留下离开事件。
                events.append(f"离开包装层：{label}")

        return wrapper

    # 工厂返回装饰器本身，不在这里应用它。
    return decorate


# -----------------------------------------------------------------------------
# 5. 两个可控反例
# -----------------------------------------------------------------------------

# 这个反例正确转发了调用，却故意省略 @wraps(function)。运行结果仍对，运行时元信息却
# 属于 wrapper，说明“调用语义正确”和“内省元信息完整”是两件需要分别保证的事。
def without_wraps[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """教学反例：调用关系保留了，但运行时元信息没有复制。"""

    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        return function(*args, **kwargs)

    return wrapper


def drops_return[**P, R](function: Callable[P, R]) -> Callable[P, None]:
    """教学反例：原函数结果被调用了，却没有从 wrapper 返回。"""

    # 返回类型明确写成 None，是为了诚实描述这个错误 wrapper 的实际契约。
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        # 原函数会执行并算出结果，但结果没有被 return；到达函数末尾会隐式返回 None。
        function(*args, **kwargs)

    return wrapper


# 应用装饰器后，opaque_add 这个名字实际指向 without_wraps 内创建的 wrapper。
@without_wraps
def opaque_add(left: int, right: int = 1) -> int:
    """本应能被帮助系统看到的说明。"""

    return left + right


# broken_add 的原函数体会计算 left + right，但公开调用经过 wrapper 后收不到该整数。
@drops_return
def broken_add(left: int, right: int) -> int:
    return left + right


def annotation_label(value: object) -> str:
    """把普通类型、泛型别名和 ForwardRef 变成稳定的教学输出。"""

    # 定义 render_card 时 ResultCard 尚不存在，FORWARDREF 模式会先得到这种占位对象。
    if isinstance(value, ForwardRef):
        # !r 使用 repr 风格，把字符串引号也放进稳定输出；__forward_arg__ 是其保存的类型名。
        return f"ForwardRef({value.__forward_arg__!r})"
    # 普通类对象统一只显示 __name__，避免默认字符串包含长的模块路径。
    if isinstance(value, type):
        return value.__name__
    # list[str] 等泛型别名不属于 type 分支，用 str 得到适合教学展示的文本。
    return str(value)


def demonstrate_equivalence() -> None:
    print_section("1. @decorator 就是重新绑定函数名")

    # 两个公开名字现在都指向 wrapper。调用它们时，先打印包装层信息，再通过闭包调用原函数。
    manual_result: str = manually_decorated_message()
    sugar_result: str = syntax_sugar_message()
    # 两种写法的包装过程相同，差别只在“手工调用并赋值”还是使用 @ 语法表达。
    print("  手工展开结果：", manual_result)
    print("  @ 语法结果：", sugar_result)

    # assert 条件为 False 时会抛 AssertionError，因此这里既校验返回值，也校验重新绑定后的身份。
    assert manual_result == "普通形式完成"
    assert sugar_result == "@语法完成"
    # is not 比较是否为同一个对象，而不是内容是否相等；装饰后对象确实已换成 wrapper。
    assert manually_decorated_message is not plain_message


# 本组从静态类型、运行结果、内省元信息和异常路径四个角度检查 timed 是否“透明”。
def demonstrate_timing_and_metadata() -> None:
    print_section("2. 参数、返回值、异常与元信息都要保留")

    # 两个位置参数和一个关键字参数先被 wrapper 收集，再展开给 build_greeting 的原函数。
    # assert_type 在运行时原样返回问候语，同时要求 Pyright 推断装饰后的调用仍然得到 str。
    result: str = assert_type(
        build_greeting("你好", "Ada", punctuation="。"), str
    )
    # wraps 复制 __name__、__doc__ 并设置 __wrapped__，所以公开对象仍能展示业务函数信息。
    # signature 默认沿 __wrapped__ 链还原业务签名；follow_wrapped=False 才显示 wrapper 的
    # *args: P.args, **kwargs: P.kwargs 入口。
    print("  返回值：", result)
    print("  函数名：", build_greeting.__name__)
    print("  docstring：", build_greeting.__doc__)
    print("  对外签名：", signature(build_greeting))
    print(
        "  wrapper 自身签名：",
        signature(build_greeting, follow_wrapped=False),
    )
    print(
        "  __wrapped__ 指向原函数：",
        build_greeting.__wrapped__ is not build_greeting,
    )

    # 这些断言把“看起来正确”变成明确契约：结果、名字、位置/关键字规则都必须保持。
    assert result == "你好，Ada。"
    assert build_greeting.__name__ == "build_greeting"
    assert str(signature(build_greeting)) == (
        "(prefix: str, name: str, /, *, punctuation: str = '!') -> str"
    )
    # __wrapped__ 指向未包装的原函数。直接调用它会绕过 timed，因此不会输出计时信息。
    assert build_greeting.__wrapped__("你好", "Lin") == "你好，Lin!"

    # require_positive(0) 先进入计时 wrapper，再由原函数抛出 ValueError；finally 打印计时后，
    # 同一个异常继续来到这里的 except。这样能证明装饰器没有把失败悄悄改成 None。
    try:
        require_positive(0)
    except ValueError as error:
        # as error 把捕获到的异常对象绑定到局部变量；type(...).__name__ 取得异常类名。
        print("  异常原样传播：", type(error).__name__, str(error))
        assert str(error) == "value 必须大于 0"
    else:
        # try 没抛异常时才执行 else；走到这里反而说明实验失败，所以主动抛 AssertionError。
        raise AssertionError("require_positive(0) 本应抛出 ValueError")


# 本组把两类“被闭包记住的值”放在一起比较：计数器需要跨调用共享，循环变量却不应共享。
def demonstrate_closures() -> None:
    print_section("3. 闭包捕获与共享状态")

    # uppercase 的两次调用进入同一个 wrapper，所以计数从 1 累加到 2；text_length 来自
    # 另一次 @count_calls 应用，拥有另一份闭包状态，第一次调用从 1 开始。
    print("  uppercase 返回：", uppercase("agent"))
    print("  uppercase 返回：", uppercase("tool"))
    print("  text_length 有自己的计数：", text_length("MCP"))

    names: list[str] = ["search", "weather", "database"]
    # 两个列表推导式都会逐个取出 reader、并立即调用 reader()，把返回字符串收集成新列表。
    # 坏版本的三个函数都在此刻读取循环结束后的同一 name；好版本各读自己工厂作用域中的 name。
    bad_results: list[str] = [
        reader() for reader in make_bad_name_readers(names)
    ]
    good_results: list[str] = [
        reader() for reader in make_good_name_readers(names)
    ]
    print("  晚绑定反例：", bad_results)
    print("  工厂创建独立绑定：", good_results)

    # 第一条稳定复现晚绑定，第二条证明工厂按输入顺序保留了三个独立值。
    assert bad_results == ["database", "database", "database"]
    assert good_results == names


# 这个实验中的装饰器写在函数内部，因此每次调用本函数，都会重新定义 layered_work，
# 也会重新经历工厂求值、装饰器应用和 wrapper 调用三个阶段。
def demonstrate_decorator_order() -> None:
    print_section("4. 多个装饰器：先猜顺序，再看事件")

    # 同一个可变列表被两层装饰器和原函数闭包共同引用，用它记录确定的执行轨迹。
    events: list[str] = []

    # 装饰器表达式先按源码从上到下求值：先 layer("外层")，再 layer("内层")。
    # 原函数对象创建后，则按 layer("外层")(layer("内层")(layered_work)) 从内向外应用。
    @layer("外层", events)
    @layer("内层", events)
    def layered_work() -> str:
        events.append("执行原函数体")
        return "完成"

    # 到 def 语句执行完，列表只包含“求值”和“应用”事件。copy 创建独立列表快照，
    # 避免后续调用继续 append 时也改变 definition_events。
    definition_events: list[str] = events.copy()
    print("  定义阶段：", definition_events)
    # 最终公开名字指向外层 wrapper：调用时先进入外层，再进入内层，最后才执行原函数体。
    result: str = layered_work()
    # 切片从定义期事件数量处取到末尾，产生只含调用期事件的新列表。
    call_events: list[str] = events[len(definition_events) :]
    print("  调用阶段：", call_events)

    # 定义期是“表达式从上到下求值、装饰器从下到上应用”。
    assert definition_events == [
        "求值装饰器表达式：外层",
        "求值装饰器表达式：内层",
        "应用装饰器：内层 -> layered_work",
        "应用装饰器：外层 -> layered_work",
    ]
    # 调用期从最外层向内进入，return 或异常退出时再从最内层向外经过各层 finally。
    assert call_events == [
        "进入包装层：外层",
        "进入包装层：内层",
        "执行原函数体",
        "离开包装层：内层",
        "离开包装层：外层",
    ]
    assert result == "完成"


# 这里故意调用两个有缺陷但范围可控的装饰器，直接观察它们各自破坏了哪一部分契约。
def demonstrate_controlled_bugs() -> None:
    print_section("5. 受控反例：忘记 wraps 或 return")

    # opaque_add 的加法仍可用，但内省看到的是 wrapper 的名字、docstring 和宽泛参数签名。
    print("  无 wraps 的名字：", opaque_add.__name__)
    print("  无 wraps 的说明：", opaque_add.__doc__)
    print("  无 wraps 的签名：", signature(opaque_add))
    # broken_add 的原函数计算出 5，wrapper 却丢弃它并隐式返回 None。
    print("  丢失 return 后：", broken_add(2, 3))

    # 第一组断言证明“业务调用成功”不代表元信息正确；第二组锁定丢失 return 的后果。
    assert opaque_add(2, right=3) == 5
    assert opaque_add.__name__ == "wrapper"
    assert not hasattr(opaque_add, "__wrapped__")
    assert broken_add(2, 3) is None


# 本组把工具注册模块延迟到函数内部导入，便于在导入前后分别观察注册表和业务事件。
def demonstrate_import_time_registration() -> None:
    print_section("6. 导入时注册，调用时才执行业务")

    # 程序此前只导入了 tool_registry，它只定义注册能力，尚未导入任何带 @tool 的工具模块。
    # list_tools 返回元信息元组；列表推导式这里只取每项的 name 用于展示。
    print("  导入前工具：", [item.name for item in list_tools()])
    assert list_tools() == ()

    # import 是可执行语句。第一次到达这里时，解释器从上到下执行 registered_tools 顶层代码：
    # 每个 def 创建函数，紧邻的 @tool(...) 立即提取元信息并注册，但函数体业务代码不运行。
    import registered_tools

    # 导入完成后注册表已有三个工具；CALL_EVENTS 仍为空则证明搜索、汇总、渲染都未被调用。
    names_after_import: list[str] = [item.name for item in list_tools()]
    print("  导入后已注册：", names_after_import)
    print("  导入后业务调用事件：", registered_tools.CALL_EVENTS)
    print("  注册事件数：", len(registration_events()))
    assert names_after_import == ["search_docs", "summarize_results", "render_card"]
    assert registered_tools.CALL_EVENTS == []

    # 同一进程再次 import 命中 sys.modules 缓存，不会重跑模块顶层代码、装饰器或注册逻辑。
    # import_module 返回模块对象，因此可以用 is 验证它与第一次 import 绑定的是同一对象。
    same_module: ModuleType = importlib.import_module("registered_tools")
    print("  再次导入得到同一模块：", same_module is registered_tools)
    assert same_module is registered_tools
    assert len(registration_events()) == 3

    # 每轮 metadata 都是一个 ToolMetadata。内部字典推导式把注解映射中的键和值逐项转换成
    # 稳定展示文本；它创建新 dict，不会修改注册表保存的只读注解映射。
    for metadata in list_tools():
        annotation_text: dict[str, str] = {
            key: annotation_label(value)
            for key, value in metadata.annotations.items()
        }
        # 相邻 f-string 会在圆括号内自动拼接成一条字符串；!r 使用 repr，明确显示引号。
        print(
            f"  工具 {metadata.name!r}: python_name={metadata.python_name!r}, "
            f"signature={metadata.signature}, description={metadata.description!r}"
        )
        print("    注册时注解：", annotation_text)

    # @tool 的 decorate 只把原函数存进元信息并原样 return，没有创建 wrapper。
    search_metadata: ToolMetadata = get_tool("search_docs")
    print(
        "  @tool 返回原函数：",
        search_metadata.function is registered_tools.search_documents,
    )
    assert search_metadata.function is registered_tools.search_documents
    # 没有包装层也就没有 wraps 设置的 __wrapped__ 属性；这里不是“忘了 wraps”的缺陷。
    assert not hasattr(registered_tools.search_documents, "__wrapped__")

    # render_card 注册时 ResultCard 尚未定义，因此 Format.FORWARDREF 把 card 注解保存成占位对象。
    # 模块完成导入后类已经存在，再请求 Format.VALUE 就能把同一注解解析为真正的类对象。
    render_metadata: ToolMetadata = get_tool("render_card")
    # annotations 是“参数名 -> 注解值”的映射；用 ["card"] 取得注册时的注解。
    registered_card_annotation: object = render_metadata.annotations["card"]
    resolved_annotations: dict[str, object] = get_annotations(
        registered_tools.render_card,
        format=Format.VALUE,
    )
    print("  注册时 card 注解：", annotation_label(registered_card_annotation))
    print("  模块导入后 card 注解：", annotation_label(resolved_annotations["card"]))
    assert isinstance(registered_card_annotation, ForwardRef)
    # 类对象用 is 比较身份；这里只接受 registered_tools 中实际定义的 ResultCard 类。
    assert resolved_annotations["card"] is registered_tools.ResultCard

    # 到这里才真正调用三个工具函数，CALL_EVENTS 也将从空列表增长为三项。
    # assert_type 同时记录静态预期，不会在运行时替我们检查 list[str] 或 str。
    results: list[str] = assert_type(
        registered_tools.search_documents("装饰器", limit=2), list[str]
    )
    summary: str = assert_type(
        registered_tools.summarize_results(results, prefix="命中"), str
    )
    card_text: str = assert_type(
        registered_tools.render_card(
            registered_tools.ResultCard("装饰器", 9.5)
        ),
        str,
    )
    print("  调用后结果：", results, summary, card_text)
    print("  调用后业务事件：", registered_tools.CALL_EVENTS)
    assert results == ["Python 装饰器执行时机"]
    assert summary == "命中: Python 装饰器执行时机"
    assert card_text == "[9.5] 装饰器"
    assert len(registered_tools.CALL_EVENTS) == 3

    # 注册表能按运行时字符串找到并调用异构函数，但静态检查器无法从任意 str 推断该项的
    # 精确参数和返回类型，所以统一入口只能诚实承诺 object；具体函数直接调用仍保留精确类型。
    dynamic_result: object = assert_type(
        invoke_tool("summarize_results", results, prefix="动态调用"), object
    )
    print("  动态注册表调用：", dynamic_result, f"（{type(dynamic_result).__name__}）")
    assert dynamic_result == "动态调用: Python 装饰器执行时机"


# 最后一组先验证失败注册不会破坏旧状态，再展示“只注册”和“包装执行”两种装饰器如何组合。
def demonstrate_registration_conflict_and_composition() -> None:
    print_section("7. 重名拒绝与注册/包装组合")

    # 先保存当前已注册函数对象，稍后用身份比较确认重名失败后它没有被覆盖。
    original = get_tool("search_docs").function
    try:
        # 执行到这个局部 def 时，tool(...) 先生成 decorate，随后 decorate 尝试注册新函数并
        # 抛出 DuplicateToolError。conflicting_search 的函数体不会运行，公开名字绑定也不会完成。
        @tool(name="search_docs", description="这个定义故意制造重名。")
        def conflicting_search(query: str) -> str:
            return query

    except DuplicateToolError as error:
        print("  重名注册已拒绝：", str(error))
    else:
        # 没有异常才会进入 else；这意味着注册器意外接受了冲突，应让实验明确失败。
        raise AssertionError("重名工具本应被拒绝")
    # tool_registry 会先检查重名再写入，因此失败后 search_docs 仍指向原对象。
    assert get_tool("search_docs").function is original

    # 上方 tool 再读取这个 wrapper 的元信息、注册它并原样返回。wraps 让注册器看到业务签名。
    @tool(name="timed_local_sum", description="展示注册装饰器与执行包装器的组合。")
    @timed
    def local_sum(left: int, right: int) -> int:
        return left + right

    # 公开名字与注册表保存的是同一个 timed wrapper；该 wrapper 的 __wrapped__ 才是原 local_sum。
    metadata: ToolMetadata = get_tool("timed_local_sum")
    print("  公开函数就是已注册函数：", local_sum is metadata.function)
    print("  但 timed 创建了 wrapper：", local_sum is not local_sum.__wrapped__)
    print("  组合后签名：", metadata.signature)
    assert local_sum is metadata.function
    assert local_sum is not local_sum.__wrapped__
    # 因为注册的是 wrapper，从公开名字或注册表调用都会经过 timed 并打印耗时。
    assert local_sum(2, 3) == 5


# main 只负责编排实验；函数定义顺序不等于执行顺序，真正调用从这里按列出的次序发生。
def main() -> None:
    demonstrate_equivalence()
    demonstrate_timing_and_metadata()
    demonstrate_closures()
    demonstrate_decorator_order()
    demonstrate_controlled_bugs()
    demonstrate_import_time_registration()
    demonstrate_registration_conflict_and_composition()
    print("\n全部第 16 章实验与断言通过。")


# 模块被直接当脚本运行时 __name__ 等于 "__main__"，于是调用入口；若被别的模块导入，
# 上面的定义期代码和模块级装饰器仍会执行，但不会自动跑七组 demonstrate_* 实验。
if __name__ == "__main__":
    main()
