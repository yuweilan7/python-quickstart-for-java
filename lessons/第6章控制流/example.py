"""第 6 章：控制流。

直接运行本文件，观察条件分支、循环控制、range、enumerate、zip 和推导式。
每组输出都对应 README.md 中的一个小节。
"""

from dataclasses import Field, dataclass, fields
from typing import Any, Literal


@dataclass
class LoopDemoRunState:
    """只为演示 fields(...) 返回的字段怎样被 for 遍历。"""

    run_id: str
    phase: str = "created"
    attempts: int = 0


def show_section(title: str) -> None:
    """为每组示例打印醒目的标题，方便把代码与输出对应起来。"""
    print(f"\n{'=' * 12} {title} {'=' * 12}")


def classify_temperature(temperature: int) -> str:
    """按从严格到宽松的顺序判断温度所属区间。"""
    if temperature >= 35:
        return "高温预警"
    if temperature >= 26:
        return "偏热"
    if temperature >= 18:
        return "舒适"
    return "偏冷"


def classify_score(score: int) -> str:
    """按边界完整、互不重叠的区间给成绩分类。"""
    if not 0 <= score <= 100:
        return "非法分数"
    if score >= 90:
        return "优秀"
    if score >= 60:
        return "及格"
    return "不及格"


def find_first_even(values: list[int]) -> int | None:
    """返回第一个偶数；遍历结束仍未返回时，结果为 None。"""
    for value in values:
        if value % 2 == 0:
            return value
    return None


def find_product(products: list[str], target: str) -> int | None:
    """用 for...else 查找商品，并返回位置。

    只有循环正常耗尽、从未执行 break，else 才会运行。
    """
    for index, product in enumerate(products):
        if product == target:
            print(f"找到 {target}，准备结束搜索")
            break
    else:
        print(f"没有找到 {target}；循环是正常耗尽的")
        return None

    # 只有 break 路径能走到这里，所以 index 一定已经绑定。
    return index


def find_first_overlong_username(usernames: list[str]) -> str | None:
    """跳过空名字，找到第一个长度大于 12 的名字后停止。"""
    found: str | None = None
    for username in usernames:
        if not username:
            continue
        if len(username) > 12:
            found = username
            break
    else:
        print("  没有过长用户名")
    return found


def demonstrate_conditionals() -> None:
    show_section("6.1 条件分支与真值")

    for temperature in [38, 29, 22, 10]:
        print(temperature, "度 ->", classify_temperature(temperature))

    # Python 会把空容器、空字符串、0 和 None 当作假值。
    cart: list[str] = []
    if not cart:
        print("购物车为空")

    # and 和 or 会短路：前半部分已能确定结果时，后半部分不会执行。
    user: dict[str, str] | None = None
    if user is not None and user.get("name"):
        print("用户名:", user["name"])
    else:
        print("没有可显示的用户名，且没有对 None 调用 get")

    age = 20
    print("18 <= age < 60:", 18 <= age < 60)

    temperature = 27
    feeling: Literal["热"] | Literal["舒适"] = "热" if temperature >= 26 else "舒适"
    print("条件表达式:", feeling)


def demonstrate_for_and_range() -> None:
    show_section("6.2 for 与 range")

    languages: list[str] = ["Java", "Python", "Go"]
    print("只需要元素时，直接迭代:")
    for language in languages:
        print("-", language)

    odd_numbers = range(1, 8, 2)
    print("range 对象:", odd_numbers)
    print("起点包含、终点不包含:", list(odd_numbers))
    print("反向计数:", list(range(5, 0, -1)))
    print("方向与步长不匹配会得到空序列:", list(range(1, 5, -1)))


def demonstrate_how_to_read_for_forms() -> None:
    show_section("6.2.1 普通 for、列表推导式与 lambda")

    # fields(...) 返回一组字段对象。先不用关心 dataclass 的细节，只把它当成
    # “能逐个取出元素的数据源”；第 14 章会专门解释 fields。
    state_fields: tuple[Field[Any], ...] = fields(LoopDemoRunState)

    # 写法 1：普通 for 语句。它可以在每轮执行多行代码，也可以打印日志或提前停止。
    field_names_by_loop: list[str] = []
    for item in state_fields:
        field_name: str = item.name
        field_names_by_loop.append(field_name)

    # 写法 2：列表推导式。阅读时先看中间的 for，再看最左边要放入列表的值：
    # for item in state_fields -> 每次取一个字段
    # item.name                -> 把这个字段的 name 放入新列表
    field_names_by_comprehension: list[str] = [item.name for item in state_fields]
    print("普通 for 得到字段名:", field_names_by_loop)
    print("列表推导式得到字段名:", field_names_by_comprehension)
    print("两种写法结果相同:", field_names_by_loop == field_names_by_comprehension)

    # 写法 3：lambda 只定义“给我一个参数，我返回什么”，它本身不会遍历。
    # 真正逐个遍历 state_fields 的是 map；list 再把 map 的惰性结果收集起来。
    field_names_by_map: list[str] = list(  # noqa: C417  # 教学对照：这里故意展示 lambda + map
        map(lambda item: item.name, state_fields)
    )
    print("lambda + map 得到字段名:", field_names_by_map)

    # for 左边可以一次接住多个值，这叫解包，不是另一种循环。
    course_results: list[tuple[str, int]] = [("控制流", 90), ("函数", 95)]
    for course_name, score in course_results:
        print(f"元组解包 -> {course_name}: {score}")

    # 遍历字典本身只能得到键；items() 每轮提供一个（键，值）元组，再由左边解包。
    settings: dict[str, int] = {"timeout": 3, "retries": 2}
    for setting_name, setting_value in settings.items():
        print(f"字典 items() -> {setting_name}={setting_value}")


def demonstrate_enumerate_and_zip() -> None:
    show_section("6.3 enumerate 与 zip")

    languages: list[str] = ["Java", "Python", "Go"]
    scores: list[int] = [90, 95, 88]

    # start=1 只改变产生的编号，不会改变原列表。
    for display_number, language in enumerate(languages, start=1):
        print(f"{display_number}. {language}")

    for language, score in zip(languages, scores, strict=True):
        print(f"{language} -> {score}")

    # 默认 zip 会在较短输入耗尽时结束，末尾的 Python 会被静默丢掉。
    default_pairs: list[tuple[str, int]] = list(zip(["Java", "Python"], [90]))
    print("默认 zip 的静默截断:", default_pairs)

    # 教学反例放在窄范围异常捕获中，不会中断后续示例。
    try:
        list(zip(["Java", "Python"], [90], strict=True))
    except ValueError as error:
        print("strict=True 发现长度不一致:", error)


def demonstrate_while() -> None:
    show_section("6.4 while 与终止条件")

    # 用确定性的布尔列表模拟三次请求，不依赖网络或随机数。
    request_results: list[bool] = [False, False, True]
    attempt = 0
    max_attempts: int = len(request_results)

    while attempt < max_attempts:
        succeeded: bool = request_results[attempt]
        attempt += 1  # 每轮推进状态；漏掉它可能造成死循环。
        print(f"第 {attempt} 次尝试:", "成功" if succeeded else "失败")
        if succeeded:
            break
    else:
        # 如果所有尝试都失败，循环会正常耗尽并进入这里。
        print("已达到最大尝试次数")


def demonstrate_loop_control() -> None:
    show_section("6.5 break、continue、pass 与循环 else")

    raw_scores: list[int] = [95, -1, 82, 101, 60]
    valid_scores: list[int] = []
    for score in raw_scores:
        if not 0 <= score <= 100:
            print("跳过非法分数:", score)
            continue  # 直接进入下一轮，下面的 append 不会执行。
        valid_scores.append(score)
    print("合法分数:", valid_scores)

    products: list[str] = ["键盘", "鼠标", "显示器"]
    print("搜索鼠标的位置:", find_product(products, "鼠标"))
    print("搜索音箱的位置:", find_product(products, "音箱"))

    # pass 只是什么都不做，并不会跳过本轮；它常用作暂时的语法占位符。
    for note in ["待处理", "", "已完成"]:
        if not note:
            pass
        print("pass 后仍会执行本轮后续代码:", repr(note))


def demonstrate_comprehensions() -> None:
    show_section("6.6 推导式与生成器表达式")

    numbers = range(1, 11)
    even_squares: list[int] = [number**2 for number in numbers if number % 2 == 0]
    print("偶数平方列表:", even_squares)

    # 字典的键重复时，后计算出的值会覆盖旧值。
    parity: dict[int, str] = {number: "偶数" if number % 2 == 0 else "奇数" for number in range(1, 6)}
    print("奇偶字典:", parity)

    languages: list[str] = ["Java", "Python", "JavaScript", "Go"]
    unique_initials: set[str] = {language[0] for language in languages}
    print("首字母集合:", sorted(unique_initials))

    # 生成器表达式不会先创建完整列表；sum 取值时才逐个计算。
    total: int = sum(number**2 for number in range(1, 5))
    print("生成器表达式求平方和:", total)

    # 有多步业务规则或需要日志时，普通循环通常比推导式更清楚。
    taxable_orders: list[float] = [120.0, -10.0, 80.0, 200.0]
    taxed_orders: list[float] = []
    for amount in taxable_orders:
        if amount < 0:
            print("忽略非法订单金额:", amount)
            continue
        if amount >= 100:
            taxed_orders.append(round(amount * 1.13, 2))
    print("大额订单含税金额:", taxed_orders)


def demonstrate_readme_exercises() -> None:
    """运行 README 中控制流相关的五组独立练习。"""
    show_section("README 小练习 1～5")

    print("练习 1：成绩边界")
    for score in (-1, 0, 59, 60, 89, 90, 100, 101):
        print(f"  {score:>3} -> {classify_score(score)}")

    print("练习 2：range 的起点、终点和步长")
    print("  0～10 偶数 range(0, 11, 2):", list(range(0, 11, 2)))
    print("  10～1 倒计时 range(10, 0, -1):", list(range(10, 0, -1)))

    print("练习 3：enumerate + zip")
    products: list[str] = ["键盘", "鼠标", "显示器"]
    prices: list[int] = [299, 99, 1_299]
    for number, (product, price) in enumerate(
        zip(products, prices, strict=True),
        start=1,
    ):
        print(f"  {number}. {product}: ¥{price}")

    short_prices: list[int] = [299, 99]
    print("  默认 zip 静默截断:", list(zip(products, short_prices)))
    try:
        list(zip(products, short_prices, strict=True))
    except ValueError as error:
        print("  strict=True 拒绝长度不一致:", error)

    print("练习 4：continue、break 与循环 else")
    first_result: str | None = find_first_overlong_username(["Alice", "", "very_long_username", "Bob"])
    print("  第一个过长用户名:", first_result)
    second_result: str | None = find_first_overlong_username(["Alice", "", "Bob"])
    print("  正常耗尽后的结果:", second_result)

    print("练习 5：含税金额的推导式与普通循环")
    amounts: list[float] = [120.0, -10.0, 80.0, 200.0]
    comprehension_result: list[float] = [round(amount * 1.13, 2) for amount in amounts if amount >= 100]
    print("  推导式结果:", comprehension_result)

    loop_result: list[float] = []
    for amount in amounts:
        if amount < 0:
            print("  普通循环记录非法负数:", amount)
            continue
        if amount >= 100:
            loop_result.append(round(amount * 1.13, 2))
    print("  普通循环结果:", loop_result)


def demonstrate_combined_case() -> None:
    show_section("6.7 与 README 练习 6：处理订单")

    # 每条元组依次表示订单号、金额、是否已支付、是否已取消。
    orders: list[tuple[str, float, bool, bool]] = [
        ("A100", 120.0, True, False),
        ("A101", -20.0, True, False),
        ("A102", 80.0, False, False),
        ("A103", 200.0, True, False),
        ("A104", -50.0, True, True),
    ]
    completed: list[tuple[str, float]] = []

    for order_id, amount, paid, cancelled in orders:
        # 取消状态优先判断，因此 A104 会记录“已取消”，而不是“金额非法”。
        if cancelled:
            print(f"{order_id}: 已取消，跳过")
            continue
        if amount <= 0:
            print(f"{order_id}: 金额非法，跳过")
            continue
        if not paid:
            print(f"{order_id}: 尚未支付，跳过")
            continue

        level: Literal["大额"] | Literal["普通"] = "大额" if amount >= 100 else "普通"
        completed.append((order_id, amount))
        print(f"{order_id}: {level}订单处理完成")

    completed_ids: list[str] = [order_id for order_id, _ in completed]
    total_amount: float | Literal[0] = sum(amount for _, amount in completed)
    completed_count: int = len(completed)
    average_amount: float | None = total_amount / completed_count if completed else None
    print("完成的订单:", completed_ids)
    print("完成订单数:", completed_count)
    print("完成订单总额:", total_amount)
    print("完成订单平均金额:", average_amount)

    no_completed_orders: list[tuple[str, float]] = []
    empty_average: float | None = (
        sum(amount for _, amount in no_completed_orders) / len(no_completed_orders)
        if no_completed_orders
        else None
    )
    print("没有成功订单时的平均金额:", empty_average)


def main() -> None:
    demonstrate_conditionals()
    demonstrate_for_and_range()
    demonstrate_how_to_read_for_forms()
    demonstrate_enumerate_and_zip()
    demonstrate_while()
    demonstrate_loop_control()
    demonstrate_comprehensions()
    demonstrate_readme_exercises()
    demonstrate_combined_case()
    print("\n找到的首个偶数:", find_first_even([1, 3, 8, 10]))
    print("没有偶数时:", find_first_even([1, 3, 5]))


if __name__ == "__main__":
    main()
