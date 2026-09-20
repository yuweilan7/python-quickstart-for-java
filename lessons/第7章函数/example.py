"""第 7 章：函数参数、内置函数、一等函数、*args 与 **kwargs。

这个文件故意把每个知识点拆成独立示例。直接运行后，可以按照输出中的章节标题，
把调用方式、函数内部收到的值和最终结果对照起来看。
"""

from collections.abc import Callable
from urllib.parse import quote, urlencode


def show_section(title: str) -> None:
    """让不同知识点的运行结果有清晰边界。"""
    print(f"\n{'=' * 12} {title} {'=' * 12}")


# -----------------------------------------------------------------------------
# 7.2 位置参数与默认参数
# -----------------------------------------------------------------------------
def calculate_total(
    prices: list[float],
    discount: float = 0.0,
    *,
    shipping: float = 0.0,
) -> float:
    """计算订单总价。

    prices 和 discount 可以按位置传入；discount 省略时使用 0.0。
    单独的 * 表示它后面的 shipping 只能写成 ``shipping=...``，这样调用处不会
    把“折扣”和“运费”这两个同为 float 的参数弄反。
    """
    subtotal: float | Literal[0] = sum(prices)
    discounted: float = subtotal * (1 - discount)
    return discounted + shipping


def unsafe_collect(
    item: str,
    target: list[str] = [],  # noqa: B006  # 教学反例：故意展示可变默认值
) -> list[str]:
    """错误示范：默认列表只创建一次，之后每次省略 target 都会共享它。"""
    target.append(item)
    return target


def collect(item: str, target: list[str] | None = None) -> list[str]:
    """正确示范：省略 target 时，每次调用都创建一个全新的列表。

    注意：调用者如果明确传入列表，本函数仍会修改那个列表。
    这往往正是调用者想要的；
    如果不希望修改外部列表，应先写 ``actual_target = list(target)`` 创建副本。
    """
    actual_target: list[str] = [] if target is None else target
    actual_target.append(item)
    return actual_target


def add_tag(tag: str, tags: list[str] | None = None) -> list[str]:
    """省略 tags 时每次创建新列表；明确传入时按约定修改原列表。"""
    actual_tags: list[str] = [] if tags is None else tags
    actual_tags.append(tag)
    return actual_tags


# -----------------------------------------------------------------------------
# 7.3 关键字参数、仅位置参数和仅关键字参数
# -----------------------------------------------------------------------------
def price(
    amount: float,
    /,
    tax_rate: float = 0.13,
    *,
    discount: float = 0.0,
) -> float:
    """展示三类参数。

    amount 在 / 前面，只能按位置传递。
    tax_rate 在 / 和 * 之间，可以按位置传递，也可以按名字传递。
    discount 在 * 后面，只能按名字传递。

    以下调用会报 TypeError，因此没有放进 main 执行：
        price(amount=100.0)         # amount 不能用关键字传递
        price(100.0, 0.13, 0.2)    # discount 不能作为第三个位置参数
    """
    discounted: float = amount * (1 - discount)
    return round(discounted * (1 + tax_rate), 2)


def connect(
    host: str,
    port: int = 5432,
    *,
    use_ssl: bool = True,
    timeout: float = 3.0,
) -> str:
    """布尔开关和多个数字参数使用关键字后，调用代码更容易读懂。"""
    protocol: Literal["加密连接"] | Literal["普通连接"] = "加密连接" if use_ssl else "普通连接"
    return f"{protocol} {host}:{port}, 超时 {timeout} 秒"


def create_user(
    username: str,
    /,
    *,
    admin: bool = False,
    active: bool = True,
) -> dict[str, object]:
    """用 / 和 * 让用户名仅限位置传递、开关仅限关键字传递。"""
    return {"username": username, "admin": admin, "active": active}


# -----------------------------------------------------------------------------
# 7.5 函数也是对象
# -----------------------------------------------------------------------------
def double(number: int) -> int:
    return number * 2


def square(number: int) -> int:
    return number**2


def apply(value: int, operation: Callable[[int], int]) -> int:
    """operation 是一个参数：它必须接收 int，并返回 int。"""
    return operation(value)


def score_of(student: tuple[str, int]) -> int:
    """作为 sorted 的 key 函数：每次接收一名学生，返回用于比较的分数。"""
    return student[1]


def make_multiplier(factor: int) -> Callable[[int], int]:
    """返回一个新函数；新函数会记住本次调用传入的 factor。"""

    def multiply(value: int) -> int:
        return value * factor

    return multiply


# -----------------------------------------------------------------------------
# 7.6 任意数量的位置参数：*args
# -----------------------------------------------------------------------------
def total(*values: float) -> float:
    """values 在函数内部一定是元组；没有参数时，它就是空元组。"""
    return sum(values)


def describe_args(*values: object) -> str:
    """把 *args 的真实类型和值显示出来，便于观察。"""
    return f"类型={type(values).__name__}, 值={values}"


def format_tags(prefix: str, *tags: str, separator: str = ", ") -> str:
    """固定参数、*args 和仅关键字参数可以组合使用。

    prefix 接收第一个位置参数，剩余位置参数进入 tags；因为 separator 位于 *tags
    后面，所以它是仅关键字参数。
    """
    return f"{prefix}: {separator.join(tags)}"


# -----------------------------------------------------------------------------
# 7.7 任意数量的关键字参数：**kwargs
# -----------------------------------------------------------------------------
def create_profile(name: str, **attributes: object) -> dict[str, object]:
    """attributes 在函数内部是字典，保存调用者额外传入的命名参数。"""
    return {"name": name, **attributes}


def summarize(*values: float, **metadata: object) -> dict[str, object]:
    """同时接收任意位置参数和任意关键字参数。"""
    return {
        "values": values,
        "count": len(values),
        "total": sum(values),
        "metadata": metadata,
    }


def score_report(
    *scores: int,
    pass_line: int = 60,
    **metadata: object,
) -> dict[str, object]:
    """汇总任意数量的成绩；没有成绩时用 None 表示没有平均值。"""
    if not 0 <= pass_line <= 100:
        raise ValueError("及格线必须在 0 到 100 之间")
    if any(not 0 <= score <= 100 for score in scores):
        raise ValueError("成绩必须在 0 到 100 之间")

    total_score: int = sum(scores)
    average_score: float | None = total_score / len(scores) if scores else None
    return {
        "count": len(scores),
        "total": total_score,
        "average": average_score,
        # all([]) 虽然为 True，但业务上“没有学生”不等于“全部及格”。
        "all_passed": bool(scores) and all(score >= pass_line for score in scores),
        "has_perfect_score": any(score == 100 for score in scores),
        "metadata": metadata,
    }


def build_url(
    base: str,
    *paths: str,
    secure: bool = True,
    **query: object,
) -> str:
    """连接 URL 路径，并用标准库正确编码查询参数。"""
    _: str
    separator: str
    host_part: str
    _, separator, host_part = base.partition("://")
    host: str = host_part if separator else base
    scheme: Literal["https"] | Literal["http"] = "https" if secure else "http"
    url: str = f"{scheme}://{host.strip('/')}"

    # 每个 path 实参代表一个路径段；空段忽略，特殊字符进行百分号编码。
    encoded_paths: list[str] = [quote(path.strip("/"), safe="") for path in paths if path.strip("/")]
    if encoded_paths:
        url = f"{url}/{'/'.join(encoded_paths)}"

    # doseq=True 允许值使用列表/元组，表达同名查询参数出现多次。
    encoded_query: str = urlencode(query, doseq=True)
    return f"{url}?{encoded_query}" if encoded_query else url


def inspect_call(
    identifier: int,
    /,
    category: str = "general",
    *tags: str,
    active: bool = True,
    **extra: object,
) -> dict[str, object]:
    """集中展示参数定义的常见完整顺序。

    1. identifier：仅位置参数
    2. category：普通参数（位置或关键字均可）
    3. tags：收集剩余位置参数的元组
    4. active：仅关键字参数
    5. extra：收集剩余关键字参数的字典
    """
    return {
        "identifier": identifier,
        "category": category,
        "tags": tags,
        "active": active,
        "extra": extra,
    }


def request(
    url: str,
    *,
    timeout: float = 3.0,
    verify_ssl: bool = True,
) -> str:
    """稳定接口优先列出参数，让拼写错误和类型问题更容易被 IDE 发现。"""
    return f"GET {url} (timeout={timeout}, verify_ssl={verify_ssl})"


def demonstrate_defaults() -> None:
    show_section("7.2 位置参数与默认参数")

    # discount 被省略，使用默认值 0.0。
    print("使用默认折扣:", calculate_total([100.0, 50.0]))

    # 前两个参数按位置匹配；shipping 被 * 限制为只能按名字传递。
    print("位置参数 + 关键字参数:", calculate_total([100.0, 50.0], 0.1, shipping=8.0))

    # 错误版本的两次调用没有传 target，因此拿到的是同一个默认列表。
    unsafe_first: list[str] = unsafe_collect("A")
    unsafe_second: list[str] = unsafe_collect("B")
    print("可变默认值第一次结果也被改变:", unsafe_first)
    print("两次返回同一个列表:", unsafe_first is unsafe_second)

    # 正确版本会在函数体内分别创建列表。
    safe_first: list[str] = collect("A")
    safe_second: list[str] = collect("B")
    print("None 哨兵得到独立列表:", safe_first, safe_second)
    print("两次返回不同列表:", safe_first is not safe_second)

    # 明确传入列表时，collect 会按设计修改调用者的列表。
    shopping_cart: list[str] = ["苹果"]
    returned_cart: list[str] = collect("香蕉", shopping_cart)
    print("传入已有列表:", shopping_cart)
    print("返回的正是传入列表:", returned_cart is shopping_cart)


def demonstrate_keyword_arguments() -> None:
    show_section("7.3 关键字参数与 /、*")

    print("全部使用默认选项:", price(100.0))
    print("tax_rate 按位置传入:", price(100.0, 0.10, discount=0.20))
    print("tax_rate 按名字传入:", price(100.0, tax_rate=0.10, discount=0.20))

    # 如果只看到 connect("db.example.com", 5432, False, 10)，很难立刻知道 False
    # 和 10 的含义。仅关键字参数把含义直接写在调用处。
    print(
        "清晰的配置调用:",
        connect("db.example.com", use_ssl=False, timeout=10.0),
    )


def demonstrate_builtins() -> None:
    show_section("7.4 常用内置函数")

    # len 面向“有长度”协议工作，不局限于某一种集合。
    print("len 字符串/列表/字典:", len("Python"), len([10, 20, 30]), len({"name": "Alice"}))

    scores: list[int] = [88, 95, 72, 100]
    print("sum/min/max:", sum(scores), min(scores), max(scores))

    # sorted 返回新列表，不修改原数据；list.sort() 才会原地修改列表。
    sorted_scores: list[int] = sorted(scores, reverse=True)
    print("sorted 降序结果:", sorted_scores)
    print("原列表保持不变:", scores)

    words: list[str] = ["pear", "fig", "banana", "kiwi"]
    print("按长度排序:", sorted(words, key=len))

    # enumerate 同时产生序号和值，start=1 适合面向用户的编号。
    for index, word in enumerate(words, start=1):
        print(f"enumerate -> {index}. {word}")

    names: list[str] = ["Alice", "Bob", "Cara"]
    points: list[int] = [95, 88, 91]
    # strict=True 能在两边长度不一致时抛出 ValueError，避免数据被静默截断。
    print("zip 配对:", list(zip(names, points, strict=True)))

    print("是否有人满分 any:", any(score == 100 for score in scores))
    print("是否全部及格 all:", all(score >= 60 for score in scores))
    print("空序列 any/all:", any([]), all([]))


def demonstrate_function_objects() -> None:
    show_section("7.5 函数也是对象")

    # 赋值时没有括号：保存的是函数本身；operation(6) 才真正调用函数。
    operation: Callable[..., int] = double
    print("函数赋给变量:", operation(6))

    # 函数可以像字符串或数字一样放进容器。
    operations: dict[str, Callable[[int], int]] = {
        "double": double,
        "square": square,
    }
    for name, current_operation in operations.items():
        print(f"函数放入字典 {name}:", current_operation(5))

    # apply 不知道具体运算，只负责调用传入的函数。
    # 这就是“把行为作为参数”。
    print("函数作为参数:", apply(-4, abs), apply(5, square))

    students: list[tuple[str, int]] = [("Alice", 88), ("Bob", 95), ("Cara", 91)]
    print("函数作为排序规则:", sorted(students, key=score_of, reverse=True))

    # make_multiplier 返回函数；triple 会记住 factor=3。
    # 这种记忆外层变量的函数叫闭包。
    triple: Callable[[int], int] = make_multiplier(3)
    print("函数作为返回值:", triple(10))

    # lambda 适合非常短的一次性表达式。
    # 有分支、循环或异常处理时应改用 def。
    print("简单 lambda:", apply(5, lambda number: number + 1))

    # 列表推导式自己负责遍历并创建列表；它不是 lambda。
    student_names_by_comprehension: list[str] = [student[0] for student in students]
    # 下面才出现 lambda：map 负责遍历，lambda 只描述“怎样处理一个 student”。
    student_names_by_map: list[str] = list(  # noqa: C417  # 教学对照：故意展示 lambda + map
        map(lambda student: student[0], students)
    )
    print("列表推导式取姓名:", student_names_by_comprehension)
    print("lambda + map 取姓名:", student_names_by_map)


def demonstrate_args() -> None:
    show_section("7.6 任意数量的位置参数 *args")

    print("零个位置参数:", total())
    print("多个位置参数:", total(10.0, 20.0, 30.0))
    print("函数内部看到的 args:", describe_args("A", 10, True))

    numbers: list[float] = [10.0, 20.0, 30.0]
    # 调用侧的 * 做“拆包”：等价于 total(10.0, 20.0, 30.0)。
    print("把列表拆成位置参数:", total(*numbers))
    print("固定参数后继续拆包:", total(5.0, *numbers))

    print("固定参数 + args:", format_tags("课程", "Python", "函数", "参数"))
    print(
        "args 后的仅关键字参数:",
        format_tags("课程", "Python", "函数", separator=" | "),
    )


def demonstrate_kwargs() -> None:
    show_section("7.7 任意数量的关键字参数 **kwargs")

    print("直接传关键字参数:", create_profile("Alice", age=28, city="Shanghai"))

    profile_options: dict[str, object] = {"age": 30, "city": "Beijing", "verified": True}
    # 调用侧的 ** 做“拆包”：字典的键变成参数名，值变成对应的参数值。
    print("把字典拆成关键字参数:", create_profile("Bob", **profile_options))

    numbers: list[float] = [10.0, 20.0, 30.0]
    metadata: dict[str, object] = {"author": "Cara", "unit": "分"}
    print("同时使用 * 和 **:", summarize(*numbers, **metadata))

    print(
        "完整参数顺序:",
        inspect_call(
            1001,
            "course",
            "Python",
            "beginner",
            active=True,
            owner="Alice",
            lessons=10,
        ),
    )

    # 对稳定接口，明确参数通常优于把所有配置都塞进 **kwargs。
    print("明确的业务参数:", request("https://example.com", timeout=10.0, verify_ssl=False))


def demonstrate_readme_exercises() -> None:
    """运行 README 中的四组函数设计练习。"""
    show_section("README 小练习")

    print("练习 1：安全的默认值")
    python_tags: list[str] = add_tag("Python")
    java_tags: list[str] = add_tag("Java")
    print("  两次省略参数:", python_tags, java_tags)
    print("  两个结果互不影响:", python_tags is not java_tags)
    existing_tags: list[str] = ["backend"]
    returned_tags: list[str] = add_tag("api", existing_tags)
    print("  明确传入的原列表被修改:", existing_tags)
    print("  返回值就是原列表:", returned_tags is existing_tags)

    print("练习 2：清晰的调用边界")
    print("  合法调用:", create_user("Alice", admin=True, active=False))
    try:
        create_user(username="Bob")  # type: ignore[call-arg]
    except TypeError as error:
        print("  username= 非法:", error)
    try:
        create_user("Bob", True, False)  # type: ignore[call-arg]
    except TypeError as error:
        print("  布尔值按位置传递非法:", error)
    try:
        create_user("Bob", actve=True)  # type: ignore[call-arg]
    except TypeError as error:
        print("  拼错 active 非法:", error)

    print("练习 3：成绩报告")
    print(
        "  有成绩:",
        score_report(88, 60, 100, pass_line=60, course="Python", teacher="Alice"),
    )
    print("  零个成绩:", score_report(course="Python"))

    print("练习 4：URL 构建器")
    print("  空路径:", build_url("api.example.com"))
    print(
        "  带路径和空格查询值:",
        build_url("api.example.com", "users", "search", keyword="python 文件"),
    )
    print(
        "  同名查询参数使用列表:",
        build_url("api.example.com", "courses", tag=["python", "java"]),
    )


def main() -> None:
    demonstrate_defaults()
    demonstrate_keyword_arguments()
    demonstrate_builtins()
    demonstrate_function_objects()
    demonstrate_args()
    demonstrate_kwargs()
    demonstrate_readme_exercises()


if __name__ == "__main__":
    main()
