"""第 3 章：名字绑定、对象别名、运算符、增强赋值和解包。"""

import keyword


def print_section(title: str) -> None:
    """打印统一的小节标题，让运行结果能和 README 对照。"""
    print(f"\n=== {title} ===")


def normalize_page_size(value: int | None) -> int:
    """只把缺失值替换为 20，合法的 0 必须原样保留。"""
    return 20 if value is None else value


def show_names_and_types() -> None:
    """观察名字如何绑定对象，以及类型提示和运行时类型的区别。"""
    print_section("1. 名字绑定、类型与关键字")

    print("部分 Python 关键字:", ", ".join(keyword.kwlist[:]))

    score: int = 95
    score_alias: Literal[95] = score
    print("score 的值/类型:", score, type(score).__name__)
    print("直接赋值后指向同一对象:", score is score_alias)

    # 类型提示描述设计意图，但 CPython 不会像 Java 编译器那样强制它。
    # 教学反例（保持注释）：把 score 改成“优秀”可以运行，
    # 却会被静态检查器警告。
    print("推荐让 score 始终表达整数分数，避免随意重新绑定为字符串")

    # 教学反例（保持注释）：不要遮蔽 list、str、sum 等内置名称。
    # list = "Python"
    # values = list(range(3))  # 此时会得到 TypeError: 'str' object is not callable
    course_name = "Python"
    print("有业务含义的名字:", course_name)


def show_aliases_and_copies() -> None:
    """用修改前后结果展示别名、浅复制和嵌套对象共享。"""
    print_section("2. 别名与浅复制")

    original: list[str] = ["Java", "Python"]
    alias: list[str] = original
    copied: list[str] = original.copy()

    alias.append("Go")
    print("通过 alias 修改后 original:", original)
    print("外层已复制的 copied:", copied)
    print("original is alias:", original is alias)
    print("original is copied:", original is copied)

    nested: list[list[str]] = [["Java"], ["Python"]]
    shallow: list[list[str]] = nested.copy()
    nested.append(["Go"])
    nested[0].append("Kotlin")

    # 新增外层元素不会进入 shallow，但共享的第一个内层列表会同时变化。
    print("nested 新增了外层元素:", nested)
    print("shallow 仍共享内层元素:", shallow)
    print("外层是否相同:", nested is shallow)
    print("第一个内层列表是否相同:", nested[0] is shallow[0])


def show_arithmetic_and_comparisons() -> None:
    """展示最容易和 Java 产生语义差异的常用运算符。"""
    print_section("3. 算术、比较、成员与身份")

    quotient: int
    remainder: int
    quotient, remainder = divmod(17, 5)
    print("真除法 17 / 5:", 17 / 5)
    print("整除与余数:", 17 // 5, 17 % 5)
    print("divmod 同时返回:", quotient, remainder)
    print("负数整除 -7 // 2:", -7 // 2)
    print("幂 2 ** 8:", 2**8)

    age = 30
    print("链式比较 18 <= age < 65:", 18 <= age < 65)

    roles: list[str] = ["editor", "reviewer"]
    print("成员判断 'reviewer' in roles:", "reviewer" in roles)
    print("成员判断 'admin' not in roles:", "admin" not in roles)

    first: list[int] = [1, 2]
    second: list[int] = [1, 2]
    print("first == second（值相等）:", first == second)
    print("first is second（不是同一对象）:", first is second)

    missing = None
    print("None 使用身份判断:", missing is None)


def show_boolean_operators() -> None:
    """观察短路求值，以及 and/or 返回对象而非固定返回 bool。"""
    print_section("4. 真假值、逻辑运算与短路")

    denominator = 0
    # 左侧为 False 后，右侧除法不会执行，因此不会出现 ZeroDivisionError。
    is_large: Literal[False] = denominator != 0 and 100 / denominator > 10
    print("短路避免除零:", is_large)

    nickname: Literal[""] = ""
    display_name: Literal["匿名用户"] = nickname or "匿名用户"
    print("or 返回选中的对象:", display_name, type(display_name).__name__)

    # 0 是有效配置，不能用 configured_timeout or 30，否则会错误地得到 30。
    configured_timeout: int | None = 0
    timeout: Literal[30] | Literal[0] = 30 if configured_timeout is None else configured_timeout
    print("显式判断 None，保留有效的 0:", timeout)
    print("not 一定返回 bool:", not [], type(not []).__name__)


def append_in_place(values: list[int]) -> None:
    """列表的 += 通常原地扩展，因此调用方能看到变化。"""
    before: list[int] = values
    values += [99]
    print("函数内 += 后仍是同一列表:", values is before)


def append_by_rebinding(values: list[int]) -> None:
    """列表的 + 创建新列表；这里只重新绑定了函数内的局部名字。"""
    before: list[int] = values
    values = values + [100]
    print("函数内 + 后是新列表:", values is not before)
    print("函数内局部值:", values)


def show_augmented_assignment() -> None:
    """对比不可变整数和可变列表执行增强赋值时的身份变化。"""
    print_section("5. 增强赋值与重新绑定")

    points = 10
    old_points: Literal[10] = points
    points += 5
    print("整数 += 后的新旧值:", points, old_points)
    print("整数 += 后是否仍是同一对象:", points is old_points)

    numbers: list[int] = [1, 2]
    alias: list[int] = numbers
    append_in_place(numbers)
    print("调用方看到 += 的修改:", numbers)
    print("原别名也看到 += 的修改:", alias)

    append_by_rebinding(numbers)
    print("调用方看不到函数内 + 的局部重绑定:", numbers)


def show_unpacking_and_assignment_expression() -> None:
    """展示交换、普通解包、扩展解包和适度使用赋值表达式。"""
    print_section("6. 交换、解包与赋值表达式")

    left, right = "L", "R"
    left: Literal["R"]
    right: Literal["L"]
    left, right = right, left
    print("先计算右侧再赋值，因此可直接交换:", left, right)

    username, _, city = ("alice", 30, "Shanghai")
    print("普通解包并忽略不需要的年龄:", username, city)

    first: int
    last: int
    first, *middle, last = [10, 20, 30, 40]
    print("扩展解包:", first, middle, last)

    # 教学反例（保持注释）：数量不匹配会中断脚本。
    # only_one, only_two = [1, 2, 3]

    course_name = "Python"
    if (name_length := len(course_name)) >= 6:
        print("赋值表达式避免重复计算长度:", name_length)


def show_bitwise_operators() -> None:
    """用权限掩码展示位运算的典型业务用途。"""
    print_section("7. 位运算：权限掩码")

    read_permission = 0b001
    write_permission = 0b010
    admin_permission = 0b100

    permissions: Literal[3] = read_permission | write_permission
    print("合并后的权限（二进制）:", f"{permissions:03b}")
    print("允许读取:", bool(permissions & read_permission))
    print("允许写入:", bool(permissions & write_permission))
    print("允许管理:", bool(permissions & admin_permission))


def demonstrate_readme_exercises() -> None:
    """运行 README 中关于默认值、权限、身份与解包的练习。"""
    print_section("README 小练习")

    print("练习 1:别名、浅复制和增强赋值的逐行结果见上方第 2、5 节")

    print("练习 2:只为 None 提供分页默认值")
    for page_size in (None, 0, 50):
        print(f"  normalize_page_size({page_size!r}) -> {normalize_page_size(page_size)}")

    print("练习 3:权限掩码")
    read_permission = 0b001
    write_permission = 0b010
    admin_permission = 0b100
    permissions = 0b101
    print("  初始权限:", f"{permissions:03b}")
    print("  读/写/管理:", *(bool(permissions & bit) for bit in (
        read_permission,
        write_permission,
        admin_permission,
    )))
    permissions |= write_permission
    print("  添加写权限后:", f"{permissions:03b}")

    print("练习 4:函数内原地修改与局部重绑定")
    values: list[int] = [1, 2]
    caller_alias: list[int] = values
    append_in_place(values)
    print("  += 返回后调用方:", values, "身份不变:", values is caller_alias)
    append_by_rebinding(values)
    print("  + 返回后调用方:", values, "身份不变:", values is caller_alias)

    print("练习 5:扩展解包与数量不匹配")
    first: str
    last: str
    first, *middle, last = ["Java", "Python", "Go", "Rust"]
    print("  第一项/中间项/最后一项:", first, middle, last)
    try:
        one: str
        two: str
        one, two = ["Java", "Python", "Go"]
        print(one, two)  # 变量数量不匹配，因此不会执行到这里。
    except ValueError as error:
        print("  普通解包数量不匹配:", type(error).__name__, "-", error)


def main() -> None:
    show_names_and_types()
    show_aliases_and_copies()
    show_arithmetic_and_comparisons()
    show_boolean_operators()
    show_augmented_assignment()
    show_unpacking_and_assignment_expression()
    show_bitwise_operators()
    demonstrate_readme_exercises()


if __name__ == "__main__":
    main()
