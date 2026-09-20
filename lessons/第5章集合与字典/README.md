# 第 5 章：集合与字典

本章目标：不只会写 `set` 和 `dict`，还要能根据“唯一成员关系”与“键值映射”选择数据结构，并判断一次操作会创建新容器还是修改原容器。配套代码：`example.py` / `Chapter05SetsDicts.java`。

建议先阅读本章，再直接运行：

```bash
python3 java_to_python/chapters/05_sets_dicts/example.py
```

示例输出按照 5.1～5.12 分段。学习时先猜输出，再替换技能、权限和商品数据重跑。

## 学习目标

学完本章后，你应该能够：

- 用 `set` 表达“有哪些唯一成员”，用 `dict` 表达“一个键对应一个值”；
- 解释为什么集合元素和字典键必须可哈希；
- 正确选择 `add` / `update`、`remove` / `discard` 和各种集合运算；
- 区分 `mapping[key]`、`get`、`setdefault` 与 `defaultdict` 的行为；
- 理解集合运算、字典合并、别名和原地修改之间的关系；
- 安全遍历字典的动态视图，并避免在遍历时改变字典大小；
- 使用 `Counter` 和 `defaultdict` 完成计数与分组；
- 识别可变默认值共享、缺失键和无可靠集合顺序等常见问题。

## 5.1 两种“按内容查找”的容器

列表把数据放在有顺序的位置中；集合关心某个唯一成员是否存在；字典则通过唯一的键找到值。可以先用下面三个问题选择容器：

| 问题 | 合适的类型 | Java 心智模型 |
| --- | --- | --- |
| 第几个位置是什么？ | `list` / `tuple` | `List<T>` |
| 某个唯一成员在不在？ | `set` | `HashSet<T>` |
| 某个键对应什么值？ | `dict` | `HashMap<K, V>` |

```python
languages = ["Java", "Python", "Java"]
unique_languages = {"Java", "Python"}
scores = {"Alice": 95, "Bob": 88}

print(languages[0])                   # Java：按位置读取
print("Python" in unique_languages)  # True：判断成员
print(scores["Alice"])               # 95：通过键读取值
```

Python 容器保存的是对象引用。把一个集合或字典赋给另一个变量不会自动复制对象；只要两个名字指向同一可变容器，通过任一名字进行原地修改，另一边都能观察到。

## 5.2 创建集合：唯一、无可靠顺序

集合字面量使用花括号，重复元素只保留一份：

```python
skills = {"Java", "Python", "Java"}
print(len(skills))       # 2
print(sorted(skills))    # ['Java', 'Python']
```

`set(iterable)` 会遍历输入并收集唯一元素，因此常用于去重：

```python
raw_tags = ["backend", "python", "backend", "api"]
unique_tags = set(raw_tags)
print(sorted(unique_tags))  # ['api', 'backend', 'python']
```

这里使用 `sorted` 只是为了让展示结果稳定。不要依赖集合的打印或迭代顺序；即使某次运行看起来固定，也不代表它具有可依赖的业务含义。

### 空集合的特殊写法

```python
empty_set = set()
empty_dict = {}

print(type(empty_set).__name__)   # set
print(type(empty_dict).__name__)  # dict
```

`{}` 已经表示空字典，所以空集合必须写成 `set()`。这是本章最容易出现的字面量错误之一。

### 什么是“可哈希”

集合需要快速判断一个元素是否已经存在。Python 会先计算元素的哈希值（hash），把它缩小到可能存放的位置，再用相等性判断确认。因此，集合元素必须是可哈希对象：在参与查找期间，它的哈希值和相等性不能随内部内容任意改变。

常见情况如下：

| 对象 | 通常能否作为集合元素或字典键 | 原因 |
| --- | --- | --- |
| `int`、`float`、`str`、`bytes` | 能 | 这些对象不可变 |
| 只包含可哈希元素的 `tuple` | 能 | 元组及其内部元素都满足要求 |
| `list`、`dict`、`set` | 不能 | 内容可原地改变 |
| `frozenset` | 能 | 它是不可变集合 |

```python
owners = {("service-a", "Alice"), ("service-b", "Bob")}

try:
    invalid = {["Java", "Python"]}
except TypeError as error:
    print(type(error).__name__, error)  # unhashable type: 'list'
```

这段异常捕获只是为了观察教学反例。实际代码应在设计数据结构时就选用合适类型，而不是每次添加元素时再捕获 `TypeError`。元组也不是无条件可哈希：`("Alice", ["admin"])` 内部仍有列表，同样不能放进集合。

## 5.3 集合适合成员判断与去重

集合最直观的用途是表达允许名单、已处理编号、标签和权限：

```python
allowed_roles = {"admin", "operator"}
role = "operator"

if role in allowed_roles:
    print("允许操作")
```

`in` 在集合中通常能直接根据哈希值定位，而列表需要从前向后比较。数据量较大且需要反复判断成员时，集合通常比列表更合适；小数据场景仍应优先考虑语义是否清楚，不必为了微小性能差异把所有列表都改成集合。

### 去重时是否需要保留顺序

```python
values = ["Java", "Python", "Java", "Go"]

unordered_unique = set(values)
ordered_unique = list(dict.fromkeys(values))

print(sorted(unordered_unique))  # 只关心唯一值
print(ordered_unique)            # ['Java', 'Python', 'Go']
```

如果只关心有哪些不同值，用集合；如果还要保留第一次出现的顺序，可以利用字典键唯一且保留插入顺序的性质。不要把 `list(set(values))` 当成稳定的顺序去重方案。

## 5.4 修改集合

集合是可变对象。常用方法可以按“添加”和“删除”来记：

| 方法 | 行为 | 关键边界 |
| --- | --- | --- |
| `add(value)` | 添加一个完整元素 | 已存在时不重复添加 |
| `update(iterable)` | 遍历输入并添加多个元素 | 字符串会被拆成字符 |
| `remove(value)` | 删除指定元素 | 不存在时抛 `KeyError` |
| `discard(value)` | 删除指定元素 | 不存在时安静忽略 |
| `pop()` | 删除并返回任意元素 | 不是“最后一个”；空集合抛 `KeyError` |
| `clear()` | 删除全部元素 | 原地修改并返回 `None` |

```python
skills = {"Java"}
skills.add("Python")
skills.update(["SQL", "Git"])
print(sorted(skills))

skills.discard("Rust")  # 不存在也没关系

try:
    skills.remove("Rust")
except KeyError:
    print("remove 认为缺失元素是错误")
```

`add` 把实参当作一个元素，`update` 把实参当作一批可迭代数据。下面是常见误区：

```python
letters = {"Java"}
letters.update("SQL")
print(sorted(letters))  # ['Java', 'L', 'Q', 'S']
```

想添加完整字符串时写 `add("SQL")`；想添加多个字符串时写 `update(["SQL", "Git"])`。

与列表的原地方法类似，`add`、`update`、`discard` 和 `clear` 都返回 `None`。不要写 `skills = skills.add("Python")`，否则变量会重新绑定到 `None`。

## 5.5 集合代数与关系判断

当数据表达权限、标签、共同好友或已完成任务时，集合运算通常比嵌套循环更贴近问题本身：

| 意图 | 运算符 | 方法形式 | Java `Set` 常见做法 |
| --- | --- | --- | --- |
| 并集 | `a \| b` | `a.union(b)` | 复制后 `addAll` |
| 交集 | `a & b` | `a.intersection(b)` | 复制后 `retainAll` |
| 差集 | `a - b` | `a.difference(b)` | 复制后 `removeAll` |
| 对称差 | `a ^ b` | `a.symmetric_difference(b)` | 组合两个方向的差集 |
| 子集 | `a <= b` | `a.issubset(b)` | `b.containsAll(a)` |
| 真子集 | `a < b` | 无需特殊写法 | 包含且不相等 |
| 是否无交集 | — | `a.isdisjoint(b)` | 遍历判断 |

```python
backend = {"Java", "Python", "SQL"}
data = {"Python", "SQL", "Pandas"}

print(sorted(backend | data))  # 并集
print(sorted(backend & data))  # 交集
print(sorted(backend - data))  # 只在 backend 中
print(sorted(backend ^ data))  # 只在其中一边
```

关系运算同样很适合权限校验：

```python
granted = {"order:read", "order:write", "report:read"}
required = {"order:read", "order:write"}

print(required <= granted)  # True：所需权限是已有权限的子集
print(required - granted)   # set()：没有缺失权限
```

### 新集合与原地修改

`a | b`、`a & b`、`a - b` 和 `a ^ b` 都创建新集合，不修改两侧输入。带 `=` 的版本会原地修改左侧集合：

```python
current = {"read"}
alias = current

combined = current | {"write"}
print(alias)              # {'read'}：原集合没变

current |= {"delete"}
print(alias)              # alias 也能看到 delete
print(current is alias)   # True
```

如果调用者仍持有左侧集合，是否使用原地运算就是可观察行为。拿不准时，明确创建新集合通常更容易控制副作用。

## 5.6 `frozenset`：不可变集合

`frozenset` 支持成员判断和集合代数，但没有 `add`、`remove` 等修改方法。由于内容创建后不变，它本身可以成为字典键或另一个集合的元素：

```python
role_by_permissions = {
    frozenset({"read"}): "viewer",
    frozenset({"read", "write"}): "editor",
}

current_permissions = frozenset({"write", "read"})
print(role_by_permissions[current_permissions])  # editor
```

普通 `set` 适合“这组成员之后还会变化”；`frozenset` 适合“这组成员本身要作为一个稳定值”。它只冻结集合结构，并不会神奇地把任意不可哈希对象变得可哈希。

## 5.7 创建字典：唯一键对应一个值

字典可以想成一排带唯一标签的抽屉：键是标签，值是抽屉里的对象。字面量写作 `键: 值`：

```python
scores: dict[str, int] = {"Alice": 95, "Bob": 88}
scores["Cara"] = 91

print(scores)  # {'Alice': 95, 'Bob': 88, 'Cara': 91}
```

还可以从键值对构造：

```python
pairs = [("host", "localhost"), ("port", 8080)]
config = dict(pairs)
print(config["port"])  # 8080
```

字典键与集合元素遵守相同的可哈希规则；值可以是列表、集合、其他字典等任意对象。一个键只能对应一个当前值，再次赋值会覆盖旧值：

```python
scores["Alice"] = 96
print(scores["Alice"])  # 96
```

现代 Python 字典保留插入顺序。更新已有键不会把它移到末尾；删除后重新插入才会成为最新的键。保留插入顺序不等于按键自动排序，需要排序时仍应显式使用 `sorted`。

## 5.8 字典取值：缺失键是不是错误

取值方式没有绝对优劣，关键是先决定“缺少这个键”在业务上代表什么：

| 写法 | 键存在时 | 键不存在时 | 是否插入默认值 |
| --- | --- | --- | --- |
| `mapping[key]` | 返回值 | 抛 `KeyError` | 否 |
| `mapping.get(key)` | 返回值 | 返回 `None` | 否 |
| `mapping.get(key, default)` | 返回值 | 返回 `default` | 否 |
| `mapping.setdefault(key, default)` | 返回当前值 | 插入并返回 `default` | 是 |

```python
config = {"timeout": 30, "proxy": None}

print(config["timeout"])          # 30
print(config.get("retries", 3))   # 3
print("retries" in config)        # False：get 没有插入默认值
```

如果缺键说明配置损坏，使用 `mapping[key]` 让错误尽早暴露；如果缺键是正常情况，使用 `get` 表达回退规则。不要无条件用 `get` 把拼写错误也变成 `None`。

### `None` 可能是合法值

```python
print(config.get("proxy"))          # None：键存在，值为 None
print(config.get("missing_proxy"))  # None：键不存在

print("proxy" in config)          # True
print("missing_proxy" in config)  # False
```

当 `None` 本身有业务含义时，先用 `key in mapping` 区分“存在但值为空”和“键根本不存在”。`value in mapping` 默认检查的是键；检查值需要显式写 `value in mapping.values()`。

函数调用的实参会在调用前求值，所以 `get(key, build_default())` 和 `setdefault(key, build_default())` 即使最终用不到默认值，也会先执行 `build_default()`。默认值构造很昂贵或带副作用时，应先做显式成员判断，或使用合适的延迟创建逻辑。

## 5.9 修改、合并与删除字典

给不存在的键赋值会新增条目，给已有键赋值会覆盖值：

```python
profile = {"name": "Alice", "city": "Shanghai"}
profile["level"] = 3
profile["city"] = "Hangzhou"
```

`update` 和 `|=` 原地合并，右侧的同名键覆盖左侧；`|` 创建一个新字典：

```python
defaults = {"theme": "light", "page_size": 20}
user_settings = {"theme": "dark"}

effective = defaults | user_settings
print(effective)  # {'theme': 'dark', 'page_size': 20}
print(defaults)   # 原字典不变

defaults.update({"language": "zh-CN"})
defaults |= {"page_size": 50}
```

常用删除方式如下：

| 写法 | 返回值 | 缺键行为 |
| --- | --- | --- |
| `del mapping[key]` | 无 | 抛 `KeyError` |
| `mapping.pop(key)` | 被删的值 | 抛 `KeyError` |
| `mapping.pop(key, default)` | 被删的值或默认值 | 返回默认值 |
| `mapping.popitem()` | 最后插入的 `(key, value)` | 空字典抛 `KeyError` |
| `mapping.clear()` | `None` | 原地清空 |

`popitem()` 的“最后插入”依赖字典顺序，但它不是“按最大键删除”，也不能传入指定键。

和集合一样，简单赋值只会创建别名：

```python
original = {"status": "pending"}
alias = original
copied = original.copy()

original["status"] = "paid"
print(alias["status"])   # paid：同一个字典
print(copied["status"])  # pending：不同的外层字典
```

`copy()` 是浅拷贝。如果值是列表或字典，新旧外层字典仍会共享内部可变对象；这与第 4 章的列表浅拷贝遵循同一规则。

## 5.10 遍历字典与动态视图

直接遍历字典得到键；需要值或键值对时分别使用 `values()` 和 `items()`：

```python
scores = {"Alice": 95, "Bob": 88}

for name in scores:
    print(name)

for name, score in scores.items():
    print(name, score)
```

`keys()`、`values()` 和 `items()` 返回的是动态视图，不是当时内容的列表副本：

```python
names = scores.keys()
scores["Cara"] = 91
print(list(names))  # ['Alice', 'Bob', 'Cara']
```

视图会反映后续修改，而且通常不复制整份数据。如果确实需要某一刻的快照，再显式转换成 `list(...)` 或其他合适容器。

### 不要在遍历时改变字典大小

```python
# 教学反例：运行时会抛 RuntimeError
# for name in scores:
#     if scores[name] < 90:
#         del scores[name]
```

遍历过程中新增或删除键会改变字典大小，使当前迭代器失效。可以先复制键，再修改原字典：

```python
for name in list(scores):
    if scores[name] < 90:
        del scores[name]
```

也可以构造一个只包含目标条目的新字典。选择标准仍然是：调用者是否应该观察到原对象被修改。

## 5.11 计数与分组

### 使用普通字典计数

每次遇到一个单词，就把当前次数取出并加一：

```python
words = ["apple", "banana", "apple"]
counts: dict[str, int] = {}

for word in words:
    counts[word] = counts.get(word, 0) + 1

print(counts)  # {'apple': 2, 'banana': 1}
```

右侧先读取旧值，左侧再写回新值。第一次遇到单词时，`get` 提供初始值 `0`。

标准库 `collections.Counter` 直接表达“计数器”意图：

```python
from collections import Counter

counts = Counter(words)
print(counts["apple"])    # 2
print(counts["missing"])  # 0
```

### 使用字典分组

分组算法的核心是“根据记录算出键，再把记录追加到对应列表”。普通字典可使用 `setdefault`：

```python
groups: dict[str, list[str]] = {}

for word in words:
    initial = word[0]
    bucket = groups.setdefault(initial, [])
    bucket.append(word)
```

第一次遇到键时，`setdefault` 把新列表存入字典；之后返回已经存在的列表。`bucket.append` 修改的正是字典里保存的列表。

当整个算法都遵守“缺键时创建空列表”这条规则时，`defaultdict(list)` 更直接：

```python
from collections import defaultdict

groups: defaultdict[str, list[str]] = defaultdict(list)
for word in words:
    groups[word[0]].append(word)
```

需要留意：读取 `groups[missing_key]` 会调用默认工厂并把新条目插入 `defaultdict`。如果只是想检查而不修改它，使用 `get` 或 `in`。偶尔才需要一个默认值时，普通 `dict` 往往更清楚。

## 5.12 嵌套可变默认值的共享陷阱

`dict.fromkeys(keys, value)` 会让所有键引用同一个 `value`，不会为每个键分别调用或复制它：

```python
wrong = dict.fromkeys(["backend", "data"], [])
wrong["backend"].append("Python")
print(wrong)
# {'backend': ['Python'], 'data': ['Python']}
```

这相当于给同一个列表贴了两个键标签。只要默认值是可变对象，通常都不应这样初始化。可以显式地为每个键创建新列表：

```python
correct: dict[str, list[str]] = {}
for team in ["backend", "data"]:
    correct[team] = []

correct["backend"].append("Python")
print(correct)
# {'backend': ['Python'], 'data': []}
```

下一章学习字典推导式后，也可以写成 `{team: [] for team in teams}`。关键不在语法长短，而在每次循环都真正执行一次 `[]`，创建独立列表。

## 数据结构选择速查

| 需求 | 推荐类型或工具 | 不推荐的绕路 |
| --- | --- | --- |
| 保留重复值和位置顺序 | `list` | 用集合后再猜原顺序 |
| 唯一成员与高频成员判断 | `set` | 每次在线性列表中查找 |
| 不可变的一组唯一成员 | `frozenset` | 把普通集合当字典键 |
| 唯一键到值的映射 | `dict` | 用两个平行列表维护对应关系 |
| 按出现次数统计 | `Counter` | 到处重复手写初始化分支 |
| 按键收集多个值 | `defaultdict(list)` 或 `setdefault` | 多层重复的缺键判断 |

## 常见错误速查

| 错误写法或假设 | 实际问题 | 建议 |
| --- | --- | --- |
| `empty = {}` 创建空集合 | 实际创建的是字典 | 使用 `set()` |
| 依赖集合输出顺序 | 集合没有可靠业务顺序 | 展示时 `sorted`，业务有序时换结构 |
| 把列表放进集合或作为字典键 | 列表不可哈希 | 重新设计键，或在合适时用元组 |
| `skills.update("SQL")` | 字符串会被拆成三个字符 | 单项用 `add`，多项传字符串集合 |
| 把 `set.pop()` 当成删除末项 | 集合没有“末项”语义 | 删除指定值用 `remove` / `discard` |
| `skills = skills.add(value)` | `add` 返回 `None` | 单独调用原地方法 |
| 用 `get` 后以为默认键已插入 | `get` 只读取，不修改 | 需要插入时明确赋值或用 `setdefault` |
| 用 `get` 无法区分缺键与值为 `None` | 两种情况都可能得到 `None` | 使用 `key in mapping` |
| 遍历字典时新增或删除键 | 迭代期间大小变化会报错 | 遍历键快照或构造新字典 |
| `dict.fromkeys(keys, [])` 创建多份列表 | 所有键共享同一个列表 | 循环或推导式逐个创建 |

## Java 心智切换

Java 开发者常从 `HashSet`、`HashMap` 的方法名出发思考；Python 更鼓励先辨认数据之间的关系，再直接写出集合代数、成员判断或键值访问。迁移时重点回答四个问题：

1. 数据表达位置顺序、唯一成员，还是键值关系？
2. 缺少某个成员或键是正常分支，还是应该立即报错？
3. 当前操作创建新容器，还是会让所有别名看到原地修改？
4. 作为集合元素或字典键的对象，在查找期间是否稳定可哈希？

Python 的 `set` / `dict` 接近 Java 的 `HashSet` / `HashMap`，但语法更紧凑。紧凑不是目的；让数据结构准确表达业务约束，才是这些写法更容易维护的原因。

## 小练习

### 练习 1：权限差异

给定用户已有权限和接口必需权限，输出共同权限、缺失权限、多余权限，以及是否可以访问。要求输出稳定可读，不依赖集合自身顺序。

### 练习 2：保序去重

给定 `['java', 'python', 'java', 'go', 'python']`，分别得到“只关心唯一值”的集合和“保留首次出现顺序”的列表。解释为什么两种结果适用于不同需求。

### 练习 3：配置合并

合并默认配置、环境配置和用户配置，后面的同名键覆盖前面。分别写出“保留所有输入不变”和“允许原地更新默认配置”的版本，并用 `is` 或修改后的内容验证区别。

### 练习 4：订单分组与计数

给定多条（部门，订单号）记录，构造 `dict[str, list[str]]`，并统计每个部门的订单数。分别尝试普通字典、`setdefault` 和 `defaultdict(list)`，比较哪种意图最清楚。

### 练习 5：修复共享列表

运行 `dict.fromkeys(["A", "B", "C"], [])`，向 `A` 对应列表添加一个值，观察其他键。改写为每个键拥有独立列表，并用 `is` 验证任意两个列表不是同一个对象。
