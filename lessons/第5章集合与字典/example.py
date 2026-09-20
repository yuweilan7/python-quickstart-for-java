"""第 5 章：集合、字典、计数与分组。

这个文件把每个知识点拆成独立示例。直接运行后，可以对照输出观察：
容器如何保证唯一性、缺失键如何处理，以及一次操作是否修改原对象。
"""

from collections import Counter, defaultdict


def show_section(title: str) -> None:
    """为每组输出增加清晰边界。"""
    print(f"\n{'=' * 12} {title} {'=' * 12}")


def demonstrate_set_creation() -> None:
    """展示集合去重、空集合写法和可哈希边界。"""
    show_section("5.1～5.2 集合创建与可哈希元素")

    raw_skills: list[str] = ["Java", "Python", "Java", "SQL"]
    unique_skills: set[str] = set(raw_skills)
    print("原始列表:", raw_skills)
    print("集合去重:", sorted(unique_skills))

    empty_set: set[Any] = set()
    empty_dict: dict[Any, Any] = {}
    print("set() / {} 的类型:", type(empty_set).__name__, type(empty_dict).__name__)

    # 不可变字符串和只含可哈希元素的元组，都可以放进集合。
    service_owners: set[tuple[str, str]] = {("order-service", "Alice"), ("user-service", "Bob")}
    print("可哈希元组:", sorted(service_owners))

    # 教学反例：list 会原地改变，不能提供稳定哈希值。
    try:
        set([["Java"], ["Python"]])
    except TypeError as error:
        print("列表不能作为集合元素:", type(error).__name__, "-", error)


def demonstrate_membership_and_ordered_deduplication() -> None:
    """展示成员判断，以及是否保留顺序时的不同去重方案。"""
    show_section("5.3 成员判断与去重")

    allowed_roles: set[str] = {"admin", "operator"}
    current_role = "operator"
    print("角色是否允许:", current_role in allowed_roles)
    print("guest 是否允许:", "guest" in allowed_roles)

    languages: list[str] = ["Java", "Python", "Java", "Go", "Python"]
    unordered_unique: set[str] = set(languages)
    ordered_unique: list[str] = list(dict.fromkeys(languages))
    print("只关心唯一值:", sorted(unordered_unique))
    print("保留首次出现顺序:", ordered_unique)


def demonstrate_set_mutation() -> None:
    """对比集合的单项/批量添加，以及严格/宽松删除。"""
    show_section("5.4 修改集合")

    skills: set[str] = {"Java"}
    skills.add("Python")  # add 把字符串作为一个完整元素。
    skills.update(["SQL", "Git"])  # update 遍历输入，逐个添加元素。
    add_result: None = skills.add("Docker")
    print("添加后的技能:", sorted(skills))
    print("add 的返回值:", add_result)

    # 教学反例：字符串可迭代，update 会把它拆成字符。
    wrong_update: set[str] = {"Java"}
    wrong_update.update("SQL")
    print("错误使用 update 添加字符串:", sorted(wrong_update))

    # 缺失是正常情况时，discard 可以直接表达“有就删除”。
    skills.discard("Rust")
    try:
        skills.remove("Rust")
    except KeyError as error:
        print("remove 删除缺失元素:", type(error).__name__, "-", error)

    # pop 删除任意元素，不要把它用于依赖顺序的业务。
    # 这里使用单元素集合，让演示结果保持确定。
    temporary: set[str] = {"temporary"}
    popped: str = temporary.pop()
    print("pop 返回被删元素:", popped, "剩余:", temporary)


def demonstrate_set_algebra() -> None:
    """展示集合代数、权限关系，以及新对象和原地修改的区别。"""
    show_section("5.5 集合代数与权限关系")

    backend: set[str] = {"Java", "Python", "SQL"}
    data: set[str] = {"Python", "SQL", "Pandas"}
    print("并集:", sorted(backend | data))
    print("交集:", sorted(backend & data))
    print("仅后端:", sorted(backend - data))
    print("对称差:", sorted(backend ^ data))
    print("两组是否无交集:", backend.isdisjoint(data))

    granted: set[str] = {"order:read", "order:write", "report:read"}
    required: set[str] = {"order:read", "order:write"}
    print("是否满足必需权限:", required <= granted)
    print("缺失权限:", sorted(required - granted))

    original: set[str] = {"read"}
    alias: set[str] = original
    combined: set[str] = original | {"write"}  # 创建新集合，不影响 original 和 alias。
    print("使用 | 后的新集合:", sorted(combined))
    print(
        "使用 | 后原集合:",
        sorted(original),
        "别名仍指向原集合:",
        alias is original,
    )

    original |= {"delete"}  # 原地修改，因此 alias 也能观察到 delete。
    print("使用 |= 后原集合 / 别名:", sorted(original), sorted(alias))


def demonstrate_frozenset() -> None:
    """展示不可变集合如何作为字典键。"""
    show_section("5.6 frozenset")

    role_by_permissions: dict[frozenset[str], str] = {
        frozenset({"read"}): "viewer",
        frozenset({"read", "write"}): "editor",
    }
    current_permissions: frozenset[str] = frozenset({"write", "read"})
    print("权限组合对应角色:", role_by_permissions[current_permissions])
    print("frozenset 仍支持交集:", sorted(current_permissions & {"write", "delete"}))


def demonstrate_dict_creation_and_access() -> None:
    """展示字典创建、覆盖、插入顺序与四种缺失键策略。"""
    show_section("5.7～5.8 字典创建与取值")

    scores: dict[str, int] = {"Alice": 95, "Bob": 88}
    scores["Cara"] = 91
    scores["Alice"] = 96  # 已有键只覆盖值，不会产生第二个 Alice。
    print("成绩字典:", scores)
    print("Alice 分数:", scores["Alice"])
    print("键的插入顺序:", list(scores))
    print("Alice 是键:", "Alice" in scores)
    print("96 是键 / 是值:", 96 in scores, 96 in scores.values())

    config: dict[str, int | None] = {"timeout": 30, "proxy": None}
    print("缺失键的默认值:", config.get("retries", 3))
    print("get 不会插入默认键:", "retries" in config)
    print("值为 None / 缺键:", config.get("proxy"), config.get("missing_proxy"))
    print("用 in 区分两种 None:", "proxy" in config, "missing_proxy" in config)

    # 缺键代表程序或数据错误时，[] 能让问题立即暴露。
    try:
        print(config["missing"])
    except KeyError as error:
        print("方括号访问缺失键:", type(error).__name__, "-", error)


def demonstrate_dict_mutation_and_merging() -> None:
    """展示字典覆盖、合并、删除、别名和浅拷贝。"""
    show_section("5.9 修改、合并与删除字典")

    defaults: dict[str, Any] = {"theme": "light", "page_size": 20}
    user_settings: dict[str, str] = {"theme": "dark"}
    effective: dict[str, Any | str] = defaults | user_settings  # 新字典中由右侧同名键获胜。
    print("合并后的有效配置:", effective)
    print("使用 | 后默认配置不变:", defaults)

    defaults.update({"language": "zh-CN"})
    defaults |= {"page_size": 50}
    print("update / |= 原地修改:", defaults)

    status: dict[str, str] = {"status": "pending"}
    status_alias: dict[str, str] = status
    status_copy: dict[str, str] = status.copy()
    status["status"] = "paid"
    print("别名看到外层修改:", status_alias["status"])
    print("浅拷贝保留旧状态:", status_copy["status"])

    nested: dict[str, list[str]] = {"events": ["created"]}
    nested_copy: dict[str, list[str]] = nested.copy()  # 只复制外层字典，内部 events 列表仍共享。
    nested["events"].append("paid")
    print(
        "浅拷贝仍共享内层列表:",
        nested_copy["events"],
        nested_copy["events"] is nested["events"],
    )

    removable: dict[str, int] = {"first": 1, "second": 2, "third": 3}
    removed_value: int = removable.pop("second")
    missing_value: int = removable.pop("missing", 0)
    last_pair: tuple[str, int] = removable.popitem()
    print("pop 的值 / 缺键默认值:", removed_value, missing_value)
    print("popitem 的最后插入项:", last_pair, "剩余:", removable)


def demonstrate_dict_views_and_safe_iteration() -> None:
    """展示动态视图，以及遍历时修改字典大小的错误与修复。"""
    show_section("5.10 字典视图与安全遍历")

    scores: dict[str, int] = {"Alice": 95, "Bob": 88}
    names_view: dict_keys = scores.keys()
    print("添加前的动态键视图:", list(names_view))
    scores["Cara"] = 91
    print("添加后的同一个键视图:", list(names_view))

    print("使用 items 遍历:")
    for name, score in scores.items():
        print(f"  {name} -> {score}")

    unsafe_scores: dict[str, int] = {"Alice": 95, "Bob": 88, "Cara": 91}
    try:
        for name in unsafe_scores:
            if unsafe_scores[name] < 90:
                del unsafe_scores[name]
    except RuntimeError as error:
        print("遍历时改变字典大小:", type(error).__name__, "-", error)

    safe_scores: dict[str, int] = {"Alice": 95, "Bob": 88, "Cara": 91}
    for name in list(safe_scores):  # list 创建键快照，原字典可安全删除。
        if safe_scores[name] < 90:
            del safe_scores[name]
    print("通过键快照安全删除:", safe_scores)


def demonstrate_counting_and_grouping() -> None:
    """用普通字典、Counter、setdefault 和 defaultdict 完成聚合。"""
    show_section("5.11 计数与分组")

    words: list[str] = ["apple", "apricot", "banana", "blueberry", "apple"]

    manual_counts: dict[str, int] = {}
    for word in words:
        # 右侧先取得旧次数，第一次出现时从 0 开始，
        # 再把结果写回同一个键。
        manual_counts[word] = manual_counts.get(word, 0) + 1
    print("普通字典计数:", manual_counts)

    counts: Counter[str] = Counter(words)
    print("Counter 计数:", dict(counts))
    print("Counter 的缺失项:", counts["missing"])

    setdefault_groups: dict[str, list[str]] = {}
    for word in words:
        initial: str = word[0]
        bucket: list[str] = setdefault_groups.setdefault(initial, [])
        bucket.append(word)  # bucket 就是字典中保存的那个列表。
    print("setdefault 分组:", setdefault_groups)

    grouped: defaultdict[str, list[str]] = defaultdict(list)
    for word in words:
        grouped[word[0]].append(word)
    print("defaultdict 分组:", dict(grouped))

    # defaultdict 的 [] 访问会创建缺失项；get 只读取，不会触发默认工厂。
    print("get 读取缺失组:", grouped.get("z"), "z 是否已插入:", "z" in grouped)
    empty_group: list[str] = grouped["z"]
    print("[] 读取缺失组:", empty_group, "z 是否已插入:", "z" in grouped)


def demonstrate_shared_mutable_defaults() -> None:
    """让 dict.fromkeys 共享同一个可变值的问题变得可观察。"""
    show_section("5.12 嵌套可变默认值")

    # 教学反例：两个键都引用传给 fromkeys 的同一个列表。
    shared: dict[str, list[Any]] = dict.fromkeys(["backend", "data"], [])
    shared["backend"].append("Python")
    print("fromkeys 共享列表:", shared)
    print("两个值是否同一对象:", shared["backend"] is shared["data"])

    # 正确示范：每次循环都执行一次 []，为每个键创建独立列表。
    independent: dict[str, list[str]] = {}
    for team in ["backend", "data"]:
        independent[team] = []
    independent["backend"].append("Python")
    print("逐个创建独立列表:", independent)
    print("两个值是否同一对象:", independent["backend"] is independent["data"])


def demonstrate_readme_exercises() -> None:
    """运行 README 中的权限、去重、配置、分组和共享状态练习。"""
    show_section("README 小练习")

    print("练习 1：权限差异")
    granted: set[str] = {"order:read", "report:read", "profile:edit"}
    required: set[str] = {"order:read", "order:write"}
    print("  共同权限:", sorted(granted & required))
    print("  缺失权限:", sorted(required - granted))
    print("  多余权限:", sorted(granted - required))
    print("  是否可以访问:", required <= granted)

    print("练习 2：保序去重")
    languages: list[str] = ["java", "python", "java", "go", "python"]
    unique_languages: set[str] = set(languages)
    ordered_languages: list[str] = list(dict.fromkeys(languages))
    print("  只关心唯一值:", sorted(unique_languages))
    print("  保留首次出现顺序:", ordered_languages)

    print("练习 3：配置合并")
    defaults: dict[str, Any] = {"theme": "light", "page_size": 20, "debug": False}
    environment: dict[str, Any] = {"debug": True, "region": "cn-east"}
    user: dict[str, Any] = {"theme": "dark", "page_size": 50}
    effective: dict[str, Any] = defaults | environment | user
    print("  创建新配置:", effective)
    print("  三个输入保持不变:", defaults, environment, user)

    mutable_defaults: dict[str, Any] = defaults.copy()
    defaults_alias: dict[str, Any] = mutable_defaults
    mutable_defaults.update(environment)
    mutable_defaults.update(user)
    print("  原地合并结果:", mutable_defaults)
    print("  别名仍是同一个字典:", defaults_alias is mutable_defaults)

    print("练习 4：订单分组与计数")
    orders: list[tuple[str, str]] = [
        ("backend", "O-101"),
        ("data", "O-102"),
        ("backend", "O-103"),
        ("frontend", "O-104"),
    ]

    plain_groups: dict[str, list[str]] = {}
    for department, order_id in orders:
        if department not in plain_groups:
            plain_groups[department] = []
        plain_groups[department].append(order_id)

    setdefault_groups: dict[str, list[str]] = {}
    for department, order_id in orders:
        setdefault_groups.setdefault(department, []).append(order_id)

    default_groups: defaultdict[str, list[str]] = defaultdict(list)
    for department, order_id in orders:
        default_groups[department].append(order_id)

    order_counts: dict[str, int] = {department: len(order_ids) for department, order_ids in plain_groups.items()}
    print("  普通字典:", plain_groups)
    print("  setdefault:", setdefault_groups)
    print("  defaultdict:", dict(default_groups))
    print("  各部门订单数:", order_counts)

    print("练习 5：修复共享列表")
    shared: dict[str, list[Any]] = dict.fromkeys(["A", "B", "C"], [])
    shared["A"].append("value")
    print("  fromkeys 的联动结果:", shared)
    print("  A/B 共享对象:", shared["A"] is shared["B"])

    independent: dict[str, list[Any]] = {key: [] for key in ("A", "B", "C")}
    independent["A"].append("value")
    print("  独立列表结果:", independent)
    print("  A/B 是不同对象:", independent["A"] is not independent["B"])


def main() -> None:
    demonstrate_set_creation()
    demonstrate_membership_and_ordered_deduplication()
    demonstrate_set_mutation()
    demonstrate_set_algebra()
    demonstrate_frozenset()
    demonstrate_dict_creation_and_access()
    demonstrate_dict_mutation_and_merging()
    demonstrate_dict_views_and_safe_iteration()
    demonstrate_counting_and_grouping()
    demonstrate_shared_mutable_defaults()
    demonstrate_readme_exercises()


if __name__ == "__main__":
    main()
