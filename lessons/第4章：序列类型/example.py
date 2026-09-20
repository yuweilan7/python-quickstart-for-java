"""第 4 章：列表、切片、元组与字符串序列。

这个文件把每个知识点拆成独立示例。直接运行后，可以对照输出观察：
一次操作是读取原对象、创建新对象，还是原地修改对象。
"""


def show_section(title: str) -> None:
    """为每组输出增加清晰边界。"""
    print(f"\n{'=' * 12} {title} {'=' * 12}")


def paginate(items: list[str], page_number: int, page_size: int) -> list[str]:
    """把从 1 开始的页码转换为半开切片边界。"""
    if page_number < 1 or page_size < 1:
        raise ValueError("页码和每页数量必须是正整数")
    start = (page_number - 1) * page_size
    stop = start + page_size
    return items[start:stop]


def demonstrate_indexing() -> None:
    """展示正负索引，以及单个索引和切片的越界差异。"""
    show_section("4.1～4.2 序列与列表索引")

    languages = ["Java", "Python", "Go", "Rust"]
    print("完整列表:", languages)
    print("长度:", len(languages))
    print("首尾元素:", languages[0], languages[-1])
    print("倒数第二项:", languages[-2])
    print("是否包含 Python:", "Python" in languages)
    print("是否包含 Ruby:", "Ruby" in languages)

    # 教学反例：索引必须对应一个真实元素。这里只捕获明确的 IndexError，
    # 让后面的教学示例仍能继续运行；异常处理会在后续章节系统学习。
    try:
        print(languages[len(languages)])
    except IndexError as error:
        print("索引越界:", type(error).__name__, "-", error)

    empty = []
    print("空列表切片是安全的:", empty[:1])


def demonstrate_slicing_and_copying() -> None:
    """展示半开切片、步长，以及浅拷贝的边界。"""
    show_section("4.3 切片与浅拷贝")

    numbers = [10, 20, 30, 40, 50, 60]
    print("切片 [1:4]:", numbers[1:4])
    print("前三项 / 其余项:", numbers[:3], numbers[3:])
    print("每隔一项:", numbers[::2])
    print("反转副本:", numbers[::-1])
    print("超出边界会收缩:", numbers[:100], numbers[100:])

    copied = numbers[:]
    print("刚复制时内容相等 / 对象相同:", numbers == copied, numbers is copied)
    copied.append(70)
    print("外层副本可独立增删:", copied)
    print("原列表保持不变:", numbers)

    # 浅拷贝只创建新的外层列表，里面的列表仍是同一个对象。
    nested = [["Java"], ["Python"]]
    shallow_copy = nested[:]
    shallow_copy[0].append("Kotlin")
    print("浅拷贝后修改内层:", nested, shallow_copy)
    print("内层对象仍共享:", nested[0] is shallow_copy[0])

    # 已知结构时，逐个复制内层列表往往比盲目深拷贝更明确。
    independent_copy = [skills.copy() for skills in nested]
    independent_copy[0].append("Go")
    print("逐层复制后原数据:", nested)
    print("逐层复制后的新数据:", independent_copy)


def demonstrate_list_mutation() -> None:
    """对比添加、删除、切片赋值和原地方法的返回值。"""
    show_section("4.4 修改列表")

    skills = ["Java"]
    skills.append("Python")  # 把字符串作为一个完整元素加入。
    skills.extend(["SQL", "Git"])  # 遍历右侧列表，逐个加入其中的元素。
    skills.insert(1, "Linux")
    print("append / extend / insert:", skills)

    # 教学反例：字符串也是可迭代对象，extend 会逐个添加字符。
    wrong_extend = ["Java"]
    wrong_extend.extend("SQL")
    print("错误使用 extend 添加字符串:", wrong_extend)

    removed_last = skills.pop()
    skills.remove("SQL")
    del skills[0]
    print("删除后的列表:", skills, "pop 返回:", removed_last)

    queue = ["A", "B", "C", "D"]
    queue[1:3] = ["X", "Y", "Z"]
    print("切片赋值可改变长度:", queue)

    numbers = [3, 1, 2]
    sort_result = numbers.sort()
    print("sort 后的原列表:", numbers)
    print("sort 的返回值:", sort_result)


def demonstrate_new_object_or_in_place() -> None:
    """对比 +、+=、sort 和 sorted 对对象身份的影响。"""
    show_section("4.5 新对象与原地修改")

    added = [1, 2]
    added_alias = added
    added = added + [3]  # 先创建新列表，再让 added 指向它。
    print("使用 + 后:", added, "旧别名:", added_alias)
    print("使用 + 后仍是同一对象:", added is added_alias)

    extended = [1, 2]
    extended_alias = extended
    extended += [3]  # list 的 += 原地扩展，别名也能看到变化。
    print("使用 += 后:", extended, "别名:", extended_alias)
    print("使用 += 后仍是同一对象:", extended is extended_alias)

    scores = [88, 95, 91]
    ranking = sorted(scores, reverse=True)
    print("sorted 产生的新列表:", ranking)
    print("原列表不变:", scores)

    students = [("Alice", 88), ("Bob", 95), ("Cara", 91), ("Dan", 91)]
    # key 对每个元组取索引 1，也就是分数；reverse=True 表示降序。
    student_ranking = sorted(students, key=lambda student: student[1], reverse=True)
    champion, *others = student_ranking
    print("学生排名:", student_ranking)
    print("冠军:", champion, "其余选手:", others)
    print("同分者保持原相对顺序:", student_ranking[1:3])


def demonstrate_repeated_nested_lists() -> None:
    """让二维列表的引用共享变得可观察。"""
    show_section("4.5 嵌套列表与 *")

    # 教学反例：* 只把同一个内层列表的引用复制了 3 份。
    shared_matrix = [[0] * 3] * 3
    shared_matrix[0][0] = 1
    print("共享内层列表:", shared_matrix)
    print("各行是否和第 1 行相同:", [row is shared_matrix[0] for row in shared_matrix])
    print("第 1、2 行是同一对象:", shared_matrix[0] is shared_matrix[1])

    # 正确示范：外层每次循环都会执行 []，创建新的内层列表。
    independent_matrix = [[0 for _ in range(3)] for _ in range(3)]
    independent_matrix[0][0] = 1
    print("独立内层列表:", independent_matrix)
    same_as_first = [row is independent_matrix[0] for row in independent_matrix]
    print("各行是否和第 1 行相同:", same_as_first)
    print("第 1、2 行是同一对象:", independent_matrix[0] is independent_matrix[1])


def demonstrate_tuples() -> None:
    """展示元组创建、解包，以及元组内部可变对象。"""
    show_section("4.6 元组与解包")

    point = (120, 30)
    x, y = point
    print("元组解包:", x, y)

    singleton = (42,)
    not_a_tuple = (42)
    print("单元素元组:", singleton, type(singleton).__name__)
    print("没有逗号:", not_a_tuple, type(not_a_tuple).__name__)

    order = "ORD-001", "paid", 299
    order_id, status, amount = order
    print("打包和解包:", order_id, status, amount)

    first, *middle, last = [10, 20, 30, 40]
    print("扩展解包:", first, middle, last)

    # 元组槽位不能替换，但槽位所指向的列表仍然可以修改。
    profile = ("Alice", ["Java"])
    profile[1].append("Python")
    print("元组中的可变列表:", profile)


def demonstrate_strings() -> None:
    """展示字符串的序列行为、不可变性、格式化和转义。"""
    show_section("4.7～4.8 字符串序列与格式化")

    text = "Python"
    print("字符串索引/切片:", text[0], text[1:4], text[::-1])
    print("子串成员判断:", "tho" in text)

    upper_text = text.upper()
    replaced_text = text.replace("Py", "Jy")
    print("字符串方法返回新对象:", upper_text, replaced_text)
    print("原字符串不变:", text)

    raw_tags = "  java,python,sql  "
    tags = raw_tags.strip().upper().split(",")
    summary = " / ".join(tags)
    print("清理并连接标签:", summary)

    product = "机械键盘"
    price = 12_345.6
    discount_rate = 0.15
    print(f"格式化: {product} ¥{price:,.2f}, 折扣率 {discount_rate:.1%}")
    print(f"调试表示 !r 会显示引号: {text!r}")
    print("转义字符串: 第一行\n第二行\t制表符")
    print(r"原始字符串: C:\temp\new")


def demonstrate_readme_exercises() -> None:
    """运行 README 中的分页、复制、排名和标签整理练习。"""
    show_section("README 小练习")

    print("练习 1: 分页切片")
    order_ids = [f"ORD-{number:03d}" for number in range(1, 11)]
    for page_number in (1, 2, 4):
        page = paginate(order_ids, page_number, page_size=3)
        print(f"  第 {page_number} 页:", page)

    print("练习 2: 安全更新嵌套技能")
    shallow_profile = ["Alice", ["Java", "SQL"]]
    shallow_updated = shallow_profile[:]
    shallow_skills = shallow_updated[1]
    assert isinstance(shallow_skills, list)
    shallow_skills.append("Python")
    print("  普通切片仍共享技能列表:", shallow_profile)
    print("  内层身份相同:", shallow_profile[1] is shallow_updated[1])

    original_profile = ["Alice", ["Java", "SQL"]]
    original_skills = original_profile[1]
    assert isinstance(original_skills, list)
    safe_skills = original_skills.copy()
    safe_profile = [original_profile[0], safe_skills]
    safe_skills.append("Python")
    print("  手工复制后原结构:", original_profile)
    print("  手工复制后的新结构:", safe_profile)
    print("  内层身份不同:", original_profile[1] is not safe_profile[1])

    print("练习 3: 成绩排名")
    students = [("Alice", 88), ("Bob", 95), ("Cara", 91)]
    ranking = sorted(students, key=lambda student: student[1], reverse=True)
    champion, *other_students = ranking
    print("  原列表:", students)
    print("  新排名:", ranking)
    print("  冠军/其余:", champion, other_students)

    print("练习 4: 整理标签文本")
    raw_tags = "  java,python,sql  "
    stripped_tags = raw_tags.strip()
    split_tags = stripped_tags.split(",")
    upper_tags = stripped_tags.upper().split(",")
    joined_tags = " / ".join(upper_tags)
    print("  原始文本:", repr(raw_tags))
    print("  strip 后:", repr(stripped_tags))
    print("  split 后:", split_tags)
    print("  upper 后:", upper_tags)
    print("  join 后:", joined_tags)


def main() -> None:
    demonstrate_indexing()
    demonstrate_slicing_and_copying()
    demonstrate_list_mutation()
    demonstrate_new_object_or_in_place()
    demonstrate_repeated_nested_lists()
    demonstrate_tuples()
    demonstrate_strings()
    demonstrate_readme_exercises()


if __name__ == "__main__":
    main()
