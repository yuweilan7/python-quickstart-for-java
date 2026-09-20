"""第 2 章：数字、布尔值、None、字符串和多行文本。"""

from decimal import Decimal
from math import isclose
from textwrap import dedent


def print_section(title: str) -> None:
    """输出分段标题，让运行结果能与 README 小节对应。"""
    print(f"\n=== {title} ===")


def demonstrate_types_and_conversion() -> None:
    """演示运行时类型、继承关系以及显式转换的边界。"""
    print_section("2.1-2.2 类型与转换")

    samples = (42, 3.14, True, "Python", None)
    for value in samples:
        print(f"{value!r:<10} -> {type(value).__name__}")

    # isinstance 会考虑继承关系，而 bool 是 int 的子类。
    print("isinstance(True, int):", isinstance(True, int))
    print("type(True) is int:", type(True) is int)

    count = int("42")
    ratio = float("0.75")
    label = str(42)
    print("显式转换:", count, ratio, label, type(label).__name__)

    # 教学反例：int 不会猜测你想如何处理小数，错误被限制在本小节内。
    try:
        int("3.14")
    except ValueError as error:
        print("int('3.14') 转换失败:", type(error).__name__)

    # bool 判断字符串是否为空，并不会解析字符串写的是 true 还是 false。
    print("bool(''):", bool(""), "bool('False'):", bool("False"))


def demonstrate_integers() -> None:
    """演示任意精度整数、进制和 Python 的整除规则。"""
    print_section("2.3 整数与除法")

    huge_number = 10**30
    readable_million = 1_000_000
    print("大整数:", huge_number)
    print("下划线不改变数值:", readable_million == 1000000)
    print("十六进制 ff:", int("ff", 16), "二进制字面量:", 0b1010)

    print("7 / 2 =", 7 / 2)
    print("7 // 2 =", 7 // 2, "7 % 2 =", 7 % 2)
    print("-7 // 2 =", -7 // 2, "-7 % 2 =", -7 % 2)

    quotient, remainder = divmod(-7, 2)
    reconstructed = quotient * 2 + remainder
    print("divmod(-7, 2):", (quotient, remainder))
    print("商和余数可还原被除数:", reconstructed)


def demonstrate_floats() -> None:
    """让浮点近似误差可见，并展示两种合适的处理方式。"""
    print_section("2.4 浮点数与精确小数")

    result = 0.1 + 0.2
    print("0.1 + 0.2:", result)
    print("直接比较 0.3:", result == 0.3)
    print("isclose 比较:", isclose(result, 0.3))

    # 从字符串构造 Decimal，避免先把二进制浮点近似值带进来。
    exact_result = Decimal("0.1") + Decimal("0.2")
    print("Decimal 精确计算:", exact_result)
    print("Decimal 直接比较 0.3:", exact_result == Decimal("0.3"))


def format_total_yuan(unit_price_cents: int, quantity: int) -> str:
    """使用整数分计算非负订单总价，并返回固定两位小数的元字符串。"""
    if unit_price_cents < 0 or quantity < 0:
        raise ValueError("单价和数量必须是非负整数")

    total_cents = unit_price_cents * quantity
    yuan, cents = divmod(total_cents, 100)
    return f"{yuan}.{cents:02d}"


def split_seconds(total_seconds: int) -> tuple[int, int, int]:
    """用两次 divmod 把秒数拆成小时、分钟和秒。"""
    hours, remaining_seconds = divmod(total_seconds, 60 * 60)
    minutes, seconds = divmod(remaining_seconds, 60)
    return hours, minutes, seconds


def parse_switch(text: str) -> bool:
    """只接受忽略大小写与两端空白后的 true/false。"""
    normalized = text.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"开关只能是 true 或 false，实际收到 {text!r}")


def demonstrate_boolean_and_none() -> None:
    """演示真假值、短路默认值与 None 的正确判断。"""
    print_section("2.5-2.6 布尔值与 None")

    values = (None, 0, 0.0, "", [], "0", [0])
    for value in values:
        print(f"bool({value!r}) = {bool(value)}")

    nickname = ""
    display_name = nickname or "匿名用户"
    print("空昵称使用默认值:", display_name)

    # 教学反例：0 是合法的重试次数，却会被 or 当作假值替换。
    retry_count = 0
    wrong_retry_count = retry_count or 3
    correct_retry_count = 3 if retry_count is None else retry_count
    print("or 误伤合法的 0:", wrong_retry_count)
    print("显式区分 None 后:", correct_retry_count)

    missing = None
    print("缺失值使用 is None:", missing is None)


def demonstrate_strings() -> None:
    """演示字符串不可变性以及拆分、连接和替换。"""
    print_section("2.7 字符串")

    raw_language = "  Python  "
    language = raw_language.strip()
    print("原字符串保留空格:", repr(raw_language))
    print("strip 返回新字符串:", repr(language))
    print("首字符仍是 str:", language[0], type(language[0]).__name__)

    lowered = language.lower()
    replaced = language.replace("Python", "Python 3.14")
    print(f"{language} / lower={lowered} / length={len(language)}")
    print("replace 后:", replaced, "; 原值:", language)

    parts = "a,b,c".split(",")
    print("切分结果:", parts)
    print("连接结果:", " | ".join(parts))


def demonstrate_multiline_text() -> None:
    """演示三引号字符串如何保留换行，以及 dedent 的作用。"""
    print_section("2.8 多行字符串")

    message = dedent("""\
        第一行：三引号字符串可以跨行。
        第二行：dedent 可以移除代码的共同缩进。
    """)
    print(message, end="")
    print("末尾是否保留换行:", message.endswith("\n"))


def demonstrate_order_total() -> None:
    """组合整数、divmod 和字符串格式化完成一个小型业务例子。"""
    print_section("2.9 综合示例：订单金额")

    for unit_price_cents, quantity in ((1_990, 3), (5, 1), (0, 8)):
        total_yuan = format_total_yuan(unit_price_cents, quantity)
        print(f"{unit_price_cents} 分 × {quantity} = {total_yuan} 元")


def demonstrate_readme_exercises() -> None:
    """运行 README 末尾的四组练习与边界输入。"""
    print_section("README 小练习")

    print("练习 1：真值与 bool/int 继承关系")
    print("bool('0'):", bool("0"))
    print("bool(0):", bool(0))
    print("isinstance(False, int):", isinstance(False, int))

    print("练习 2：使用 divmod 拆分秒数")
    for total_seconds in (-1, 0, 59, 60, 3661):
        print(f"  {total_seconds:>4} 秒 -> {split_seconds(total_seconds)}")
    print(
        "  -1 秒得到前一天的 23:59:59；"
        "若业务不接受负数，应在入口主动拒绝"
    )

    print("练习 3：显式解析开关")
    for switch_text in (" true ", "FALSE"):
        print(f"  parse_switch({switch_text!r}) -> {parse_switch(switch_text)}")
    try:
        parse_switch("yes")
    except ValueError as error:
        print("  非法开关:", error)

    print("练习 4：只用整数分计算金额")
    order_cases = ((0, 10), (7, 1), (123_456_789_012_345, 3))
    for unit_price_cents, quantity in order_cases:
        result = format_total_yuan(unit_price_cents, quantity)
        print(f"  {unit_price_cents} 分 × {quantity} = {result} 元")


def main() -> None:
    demonstrate_types_and_conversion()
    demonstrate_integers()
    demonstrate_floats()
    demonstrate_boolean_and_none()
    demonstrate_strings()
    demonstrate_multiline_text()
    demonstrate_order_total()
    demonstrate_readme_exercises()


if __name__ == "__main__":
    main()
