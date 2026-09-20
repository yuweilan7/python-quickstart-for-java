# 第 6 章：控制流

一句话先抓住本章：**Python 控制流的重点不是手工维护下标，而是直接说明要判断什么、遍历什么、何时跳过，以及如何产生结果。**

这些能力会出现在几乎所有真实业务中：按订单状态选择处理路径、批量校验输入、重试外部请求、搜索第一个匹配项，以及从原始数据构造新集合。Python 和 Java 解决的是同一类问题，但 Python 更依赖缩进、对象的真值和可迭代协议来表达流程。

学完本章后，你应该能够：

- 用 `if` / `elif` / `else` 写出边界清楚的互斥分支；
- 理解真值、短路求值，以及 `and` / `or` 不一定返回布尔值；
- 根据数据来源选择 `for`、`range`、`enumerate`、`zip` 或 `while`；
- 分清 `break`、`continue`、`pass` 和循环 `else`；
- 把简单的映射、过滤写成推导式，同时知道何时应保留普通循环；
- 识别 Java 工程师迁移时最容易遇到的边界错误。

## 先建立 Java → Python 心智模型

| 要表达的意图 | Java 常见写法 | Python 常见写法 | 不能直接等同的地方 |
| --- | --- | --- | --- |
| 选择一个分支 | `if / else if / else` | `if / elif / else` | Python 用缩进界定代码块，条件也不要求必须是 `bool` |
| 遍历元素 | 增强 `for` | `for item in iterable` | Python 面向可迭代对象，不要求有下标或 `size()` |
| 按整数计数 | C 风格 `for` | `for number in range(...)` | Python 没有 `for (初始化; 条件; 更新)` 这种语法 |
| 同时取得序号和值 | 手工维护 `int i` | `enumerate(iterable)` | `start` 改变的是产生的编号，不是原容器下标 |
| 并行遍历两组值 | 下标 + 长度检查 | `zip(a, b, strict=True)` | 默认 `zip` 会按最短输入静默停止 |
| 条件未知次数的循环 | `while` / `do-while` | `while` | Python 没有内置 `do-while` |
| 简单过滤与转换 | Stream 或循环 + `add` | 推导式 | 列表推导式立即创建列表，不等同于惰性的 Java Stream |
| 遍历完仍未找到 | 标志变量或提前 `return` | 提前 `return` 或循环 `else` | 循环 `else` 的条件是“没有被 `break` 终止” |

先记住三个选择问题：

1. 只是在一批数据中逐个处理吗？优先 `for`。
2. 退出由不断变化的状态决定吗？使用 `while`，并明确终止条件。
3. 只是把输入逐个转换或过滤成新集合吗？逻辑足够简单时使用推导式。

## 6.1 分支怎样决定哪段代码执行

### 缩进就是代码块边界

Java 用 `{}` 包围分支，Python 在条件后写 `:`，再用一致的缩进表示从属关系。惯例是每层使用 4 个空格：

```python
temperature = 29

if temperature >= 35:
    feeling = "高温预警"
elif temperature >= 26:
    feeling = "偏热"
elif temperature >= 18:
    feeling = "舒适"
else:
    feeling = "偏冷"
```

Python 从上到下检查条件，只执行第一个真值分支。`29` 命中 `temperature >= 26` 后，不会再检查 `>= 18`。因此多个区间有包含关系时，要先写更严格、更具体的条件；如果先写 `temperature >= 18`，高温数据也会提前进入“舒适”分支。

`if`、`for` 和 `while` 不会像 Java 的 `{}` 那样创建新的块级作用域。分支里绑定的名字仍属于当前模块或函数作用域，但这不代表它一定存在：

```python
def order_status(paid: bool) -> str:
    if paid:
        status = "已支付"
    return status  # paid 为 False 时，status 从未绑定，会抛 UnboundLocalError
```

如果分支后的代码一定要读取某个名字，就让所有路径都为它赋值，或者在分支前提供合理初值。

### 分支顺序本身就是业务规则

互斥分类通常从异常边界到正常范围，或者从最具体条件到最宽泛条件：

```python
def classify_score(score: int) -> str:
    if not 0 <= score <= 100:
        return "非法分数"
    if score >= 90:
        return "优秀"
    if score >= 60:
        return "及格"
    return "不及格"
```

每个命中分支都立即 `return`，因此不必再写 `elif`。这种写法可以先排除非法状态，让后面的主路径保持平坦。它和 Java 的 guard clause（守卫式提前返回）思路相同。

### Python 条件使用对象的真值

Java 的 `if` 条件必须产生 `boolean`，Python 会询问对象“放在条件位置时算真还是假”，这叫真值判断。

常见假值如下：

| 类别 | 假值示例 |
| --- | --- |
| 空值 | `None` |
| 布尔值 | `False` |
| 数字零 | `0`、`0.0` |
| 空字符串 | `""` |
| 空容器 | `[]`、`()`、`{}`、`set()` |

其他大多数对象都是真值。因此检查容器是否非空时，通常直接写：

```python
items = []

if items:
    print("有数据")

if not items:
    print("没有数据")
```

这种简写只有在业务语义也允许“所有假值等价”时才安全。`None`、`0` 和空字符串可能代表三种不同状态：

```python
discount: int | None = 0

if discount is None:
    print("调用者没有提供折扣")
elif discount == 0:
    print("调用者明确指定不打折")
```

`None` 是单例，使用 `is None` 或 `is not None` 判断身份；普通值比较使用 `==`。

### `and` 与 `or` 既会短路，也会返回操作数

短路规则和 Java 相似：

- `a and b`：如果 `a` 已是假值，就不计算 `b`；
- `a or b`：如果 `a` 已是真值，就不计算 `b`；
- `not value`：把对象的真值反转成真正的 `bool`。

```python
user = None

if user is not None and user.get("name"):
    print(user["name"])
```

当左侧为假时，`user.get(...)` 不会执行，因此不会对 `None` 调用方法。保护条件必须放在可能出错的表达式前面。

与 Java 的 `&&` / `||` 不同，Python 的 `and` / `or` 返回决定结果的那个操作数，而不一定返回 `True` 或 `False`：

```python
typed_name = ""
display_name = typed_name or "访客"  # 结果是字符串 "访客"
```

这适合提供“假值即缺省”的默认值。如果 `0`、`False` 或空字符串是合法输入，就不要用 `or` 抹平它们，应显式判断 `is None`。

### 区间和二选一可以写得更贴近意图

Python 支持链式比较：

```python
age = 20

if 18 <= age < 60:
    print("年龄在目标区间内")
```

它表达的关系相当于 Java 的 `age >= 18 && age < 60`。链中的中间表达式只求值一次，这不等于先得到 `18 <= age` 的布尔值，再拿布尔值和 `60` 比较。

Java 的三元表达式 `condition ? yes : no` 在 Python 中写成：

```python
amount = 120
label = "大额" if amount >= 100 else "普通"
```

阅读顺序是“条件为真时的值 → 条件 → 条件为假时的值”。它只适合简单的二选一；多层嵌套、日志或状态更新应改回普通分支。

## 6.2 `for` 为什么通常不需要下标

### `for` 直接向可迭代对象取值

Python 的 `for` 更接近 Java 增强 `for`，而不是 C 风格计数循环：

```python
languages = ["Java", "Python", "Go"]

for language in languages:
    print(language)
```

列表、元组、字符串、字典、集合、文件、`range` 和生成器都可以提供一串值，因此都属于可迭代对象。循环每轮取得一个值，把它绑定到 `language`，然后执行循环体；数据源耗尽后自然结束。

只为取元素而写下标会增加无用状态：

```python
# 不推荐：循环真正需要的是元素，而不是下标。
for index in range(len(languages)):
    print(languages[index])
```

需要序号时用 `enumerate`，需要并行取多组值时用 `zip`。只有算法本身依赖位置、相邻元素或原地替换指定位置时，才优先考虑下标。

### 循环变量是名字绑定，不是源容器的槽位

给循环变量重新赋值，只是让这个名字指向新对象，不会替换列表元素：

```python
numbers = [1, 2, 3]

for number in numbers:
    number *= 10

print(numbers)  # [1, 2, 3]
```

要构造转换后的数据，创建新列表通常最清楚。确实要原地改列表时，应按位置赋值，例如 `numbers[index] = ...`。

普通 `for` 也不会创建块级作用域。非空循环结束后，循环变量仍绑定着最后一个值；空数据源则可能让它从未绑定。不要依赖循环外的循环变量表达结果，应显式保存需要的状态或提前返回。

### 解包让一条记录直接落到多个名字

当每个元素本身包含多个部分时，`for` 左侧可以直接解包：

```python
records = [("Alice", 95), ("Bob", 88)]

for name, score in records:
    print(f"{name}: {score}")
```

这仍是一个循环：每轮先取出一个二元组，再把其中两个值分别绑定到 `name` 和 `score`。元素数量与左侧名字数量不一致时会抛 `ValueError`，不会默默补齐。

遍历字典本身默认得到键；同时需要键和值时使用 `.items()`：

```python
settings = {"timeout": 3, "retries": 2}

for key, value in settings.items():
    print(key, value)
```

不要在遍历列表时增删元素，也不要在遍历字典时改变键的数量。前者容易跳过数据，后者会触发运行时错误。需要筛选时通常构造新容器；确需删除时则遍历副本或待删除键列表。

### `range` 提供整数，但不预先创建整数列表

Python 没有 C 风格 `for`，固定次数或整数区间用 `range`：

| 写法 | 产生的整数 | 说明 |
| --- | --- | --- |
| `range(4)` | `0, 1, 2, 3` | 起点默认为 `0`，步长默认为 `1` |
| `range(2, 6)` | `2, 3, 4, 5` | 包含起点，不包含终点 |
| `range(1, 8, 2)` | `1, 3, 5, 7` | 第三个参数是步长 |
| `range(5, 0, -1)` | `5, 4, 3, 2, 1` | 反向遍历必须使用负步长 |

左闭右开的规则与切片一致。`range` 是轻量、惰性的范围对象，不会先把全部整数装进列表：

```python
numbers = range(1_000_000)
print(numbers)         # range(0, 1000000)
print(list(range(4)))  # 仅在确实需要列表时转换
```

三个常见边界要单独记住：

- `range(1, 5, -1)` 的方向与步长矛盾，结果为空，不会自动纠正；
- 步长不能是 `0`，否则抛 `ValueError`；
- 无论正向还是反向，`stop` 都不包含在结果中。

## 6.3 需要序号或并行数据时怎么遍历

### `enumerate` 同时产生编号和值

```python
languages = ["Java", "Python", "Go"]

for display_number, language in enumerate(languages, start=1):
    print(f"{display_number}. {language}")
```

`enumerate` 每轮产生一个 `(编号, 元素)` 二元组，左侧再把它解包。`start=1` 只让编号从 `1` 开始，适合用户可见的序号；它不会改变原列表，也不意味着第一个元素的真实下标变成了 `1`。

相比手工执行 `index += 1`，`enumerate` 不会因为某个 `continue` 分支漏掉更新，也可以直接包装没有下标的可迭代对象。

### `zip` 按位置配对，长度策略要明确

```python
names = ["Alice", "Bob", "Cara"]
scores = [95, 88, 91]

for name, score in zip(names, scores, strict=True):
    print(name, score)
```

`zip` 每轮从各输入中各取一个值，再组成元组。它和 `enumerate` 都是惰性的：创建对象时不会把全部结果算完，只有循环、`list(...)` 等消费者取值时才真正推进。

默认 `zip` 在最短输入耗尽时停止：

```python
pairs = list(zip(["Alice", "Bob"], [95]))
print(pairs)  # [('Alice', 95)]，Bob 被静默丢弃
```

如果业务要求输入等长，本项目使用的 Python 3.14 可以明确写 `strict=True`：

```python
pairs = list(zip(["Alice", "Bob"], [95], strict=True))
# 实际消费到长度不一致的位置时抛 ValueError
```

只创建 `zip(..., strict=True)` 还没有完成校验，因为惰性对象尚未走到不一致的位置。必须真正迭代它，异常才会出现。

序号和多组数据可以组合：

```python
for number, (name, score) in enumerate(
    zip(names, scores, strict=True),
    start=1,
):
    print(f"{number}. {name}: {score}")
```

如果多个平行列表长期共同描述同一业务实体，应考虑把字段放进元组、`dataclass` 或类。`zip` 适合临时配对，不应代替清晰的数据模型。

## 6.4 次数不确定时如何安全使用 `while`

`for` 的终点来自数据源耗尽，`while` 的终点来自条件变为假。因此 `while` 适合重试、轮询、等待状态变化等“事先不知道准确次数”的流程：

```python
attempt = 0
max_attempts = 3

while attempt < max_attempts:
    attempt += 1
    print(f"第 {attempt} 次尝试")
```

阅读或编写 `while` 时，必须回答三个问题：

1. 初始状态是什么？这里是 `attempt = 0`。
2. 什么条件允许继续？这里是 `attempt < max_attempts`。
3. 每轮如何靠近终点？这里是 `attempt += 1`。

第三点缺失时，循环条件可能永远为真。真实重试通常还需要最大次数、超时、异常分类和取消机制，不能只靠无限循环。

Python 没有内置 `do-while`。必须至少执行一次时，可以先执行一次再进入 `while`，或者使用带明确退出条件的 `while True`：

```python
number = 0

while True:
    number += 1
    print(number)
    if number >= 3:
        break
```

`while True` 本身不是错误，但读者应能快速找到可靠的 `break`、`return` 或异常退出路径。

## 6.5 一轮循环和整个循环分别怎样停止

| 语句或结构 | 对流程的影响 | 典型用途 |
| --- | --- | --- |
| `continue` | 跳过本轮余下语句，开始下一轮 | 排除非法或不相关数据 |
| `break` | 立即结束最近一层循环 | 找到目标后停止搜索 |
| `pass` | 什么都不做，然后继续执行下一条语句 | 暂时提供语法要求的空代码块 |
| 循环 `else` | 循环未被 `break` 终止时执行 | 表达“遍历完仍未找到”或“重试已耗尽” |

### `continue` 适合先排除异常输入

```python
valid_scores = []

for score in [95, -1, 82, 101, 60]:
    if not 0 <= score <= 100:
        continue
    valid_scores.append(score)
```

这种守卫式写法先结束无效数据的本轮处理，主路径不需要包含在多层 `if` 里。

### `pass` 不会跳过本轮

```python
for item in ["ready", "", "done"]:
    if not item:
        pass
    print(repr(item))  # 空字符串仍会打印
```

`pass` 只是一条合法的空语句。它常用于尚未实现的函数体、类体或分支占位；要跳过本轮后续代码必须使用 `continue`。生产代码里长期保留 `pass`，通常意味着遗漏了处理或留下了无用分支。

### 循环 `else` 应读成“没有遇到 `break`”

```python
products = ["键盘", "鼠标", "显示器"]
target = "音箱"

for index, product in enumerate(products):
    if product == target:
        print("找到了，位置是", index)
        break
else:
    print("遍历完仍未找到")
```

准确规则是：

- 数据源正常耗尽，执行 `else`；
- `while` 的条件正常变为假，执行 `else`；
- 循环中执行过任意次数的 `continue`，最后正常耗尽，仍执行 `else`；
- 通过 `break`、`return` 或异常离开，不执行 `else`。

它不是“循环一次也没执行才进入的分支”。如果团队对这个语法不熟，封装成函数并在找到时提前 `return`，通常也很清楚。

`break` 和 `continue` 只作用于最近一层循环。Python 没有 Java 的带标签 `break outer;`；多层搜索可以提取成函数后提前 `return`，或重新设计数据流，避免用复杂标志位穿透多层循环。

## 6.6 什么时候用推导式，什么时候保留普通循环

### 列表推导式表达“遍历 → 过滤 → 转换 → 收集”

```python
even_squares = [
    number**2
    for number in range(1, 11)
    if number % 2 == 0
]
```

不要强迫自己从左到右硬读。按数据流拆解更容易：

1. `for number in range(1, 11)` 依次提供 `1` 到 `10`；
2. `if number % 2 == 0` 丢弃奇数；
3. `number**2` 把保留值转换成平方；
4. 外层 `[...]` 立即创建并返回新列表。

它等价于：

```python
even_squares = []

for number in range(1, 11):
    if number % 2 == 0:
        even_squares.append(number**2)
```

可以先把列表推导式类比为简单的 Java Stream `filter + map + collect`，但不能完全等同：推导式自身不是可复用流水线，方括号版本会立即算完并持有整个结果。

推导式中的两个 `if` 位置表示不同意图：

```python
scores = [95, 52, 88]

# for 后的 if 是过滤：不及格的分数不进入结果。
passed = [score for score in scores if score >= 60]

# for 前的 if...else 是转换：每个分数都产生一个标签。
labels = ["及格" if score >= 60 else "不及格" for score in scores]
```

Python 3 的推导式拥有自己的局部作用域，`number` 等临时名字不会泄漏到外部；这与普通 `for` 的循环变量在循环后仍可见不同。

### 外层符号决定收集结果的类型

```python
languages = ["Java", "Python", "JavaScript", "Go"]

initials = {language[0] for language in languages}
parity = {
    number: "偶数" if number % 2 == 0 else "奇数"
    for number in range(1, 6)
}
```

- `[value for ...]` 创建列表，保留遍历顺序和重复值；
- `{value for ...}` 创建集合，自动去重，不应用来表达业务顺序；
- `{key: value for ...}` 创建字典，重复键以后出现的值为准。

空集合仍写作 `set()`，因为 `{}` 表示空字典。

### 生成器表达式按需产出，而且通常只能消费一次

把方括号换成圆括号，会得到生成器表达式：

```python
total = sum(number**2 for number in range(1, 5))
```

它不会先创建完整平方列表。`sum` 每请求一个值，生成器才计算一个值并交出。只需顺序消费一次、数据量较大，或直接传给 `sum` / `any` / `all` 时，这种方式避免了中间列表。

生成器被消费后不会自动倒带，也不能像列表一样随机按下标访问。如果需要反复遍历、查看长度或随机访问，应创建列表，或者每次重新创建生成器。

### 推导式与 `lambda` 不是同一种东西

```python
fields = [{"name": "run_id"}, {"name": "phase"}]
names = [item["name"] for item in fields]
getter = lambda item: item["name"]
```

第二行会遍历 `fields` 并立即创建字段名列表。第三行只创建一个匿名函数：以后传入一个 `item` 时，它返回 `item["name"]`；它自己不会遍历任何数据。简单批量转换通常优先推导式，传递一个短小回调规则时才考虑 `lambda`。

### 出现业务过程时，普通循环更清楚

推导式适合无副作用的简单映射和过滤。出现下面任一情况，优先使用普通循环：

- 每个元素需要多步状态更新；
- 需要日志、指标或细分跳过原因；
- 需要 `break`、`continue` 或局部异常处理；
- 出现多层 `for`、多个难懂条件或嵌套条件表达式。

不要用 `[print(order) for order in orders]` 代替循环。它会为了副作用创建一份全是 `None` 的无用列表。Pythonic 的含义是意图清楚，不是行数最少。

## 6.7 把控制流放进一个订单处理过程

假设每条订单依次包含订单号、金额、是否支付、是否取消。业务要求取消状态优先于金额校验，只有未取消、金额有效且已支付的订单才能完成：

```python
orders = [
    ("A100", 120.0, True, False),
    ("A101", -20.0, True, False),
    ("A102", 80.0, False, False),
]
completed = []

for order_id, amount, paid, cancelled in orders:
    if cancelled:
        print(f"{order_id}: 已取消，跳过")
        continue
    if amount <= 0:
        print(f"{order_id}: 金额非法，跳过")
        continue
    if not paid:
        print(f"{order_id}: 尚未支付，跳过")
        continue
    completed.append((order_id, amount))
```

这里的控制流可以按数据变化来理解：

1. 元组解包把一条订单拆成四个有业务含义的名字；
2. 三个守卫条件按优先级排除订单，`continue` 保证失败订单到此为止；
3. 只有通过所有守卫的订单才能进入 `completed`；
4. 因为流程需要输出不同失败原因，普通循环比复杂推导式更合适。

过滤完成后，简单的派生结果再交给推导式和生成器表达式：

```python
completed_ids = [order_id for order_id, _ in completed]
total_amount = sum(amount for _, amount in completed)
average_amount = total_amount / len(completed) if completed else None
```

`_` 是“这个位置的值当前不使用”的命名惯例，不是特殊关键字。平均值先检查 `completed` 的真值，避免空列表导致除零错误。

条件顺序在这里也是业务规则。如果一条订单既已取消又是负金额，因为 `cancelled` 先判断，它只会记录“已取消”。修改分支顺序就会改变可观察结果，不能只把这种调整看成格式重排。

## Java 工程师常见迁移陷阱

| 容易写错或误判的地方 | 实际表现 | 更稳妥的做法 |
| --- | --- | --- |
| 更宽泛的条件放在前面 | 后面的具体分支永远无法到达 | 从非法或具体条件逐步走向一般条件 |
| 用 `if value:` 区分 `None` 和 `0` | 两者都会进入假值分支 | 业务含义不同时显式写 `is None` |
| 只为取值使用 `range(len(items))` | 多维护一层下标并增加越界风险 | 直接 `for item in items` |
| 给循环变量重新赋值，期待修改列表 | 只改变名字绑定，原列表不变 | 构造新列表或按下标回写 |
| 忘记 `range` 不包含终点 | 少处理一个边界值 | 先手算起点、终点和步长 |
| `while` 中没有推进状态 | 条件可能永远为真 | 明确初值、继续条件和推进动作 |
| 把 `pass` 当成 `continue` | 本轮后续代码照常执行 | 跳过本轮必须写 `continue` |
| 默认 `zip` 会检查长度 | 较长输入的尾部被静默丢弃 | 业务要求等长时用 `strict=True` 并实际消费 |
| 以为循环 `else` 表示“零次循环” | 正常耗尽都会执行 `else` | 把它读成“没有 `break`” |
| 在推导式中写日志和状态更新 | 产生无用结果且难以调试 | 有副作用或多步流程就用普通循环 |
| 遍历时增删原列表或字典键 | 跳过数据或抛运行时错误 | 构造新容器，或遍历明确的副本 |

## 小练习

1. **条件边界**：实现 `classify_score(score)`。`90～100` 为“优秀”，`60～89` 为“及格”，`0～59` 为“不及格”，区间外返回“非法分数”。测试 `-1、0、59、60、89、90、100、101`。
2. **整数序列**：分别用 `range` 生成 `0～10` 的偶数和从 `10` 到 `1` 的倒计时。解释每个起点、终点和步长为什么这样填写。
3. **组合遍历**：并行遍历商品名和价格，打印 `1. 商品名: 价格`。故意让两组数据长度不同，比较默认 `zip` 与 `strict=True` 的行为。
4. **搜索流程**：跳过空用户名；遇到第一个长度大于 `12` 的名字就停止，否则在循环正常结束后打印“没有过长用户名”。
5. **选择表达方式**：从订单金额中筛出 `>= 100` 的合法金额并计算含税金额。先写推导式，再加入非法负数日志并改成普通循环，比较哪种形式更能表达需求。
6. **综合练习**：为订单加入“已取消”状态。取消订单应优先跳过，金额非法时记录原因；最后输出成功订单数、总额和平均金额，并处理没有成功订单的情况。

做练习时先列边界输入，再写正常路径。控制流是否可靠，往往由空输入、临界值、找不到目标和长度不一致这些情况决定。

## 运行本章示例

本章只使用 Python 标准库，不需要安装额外依赖。仓库要求 Python 3.14 或更高版本。

在仓库根目录运行：

```bash
.venv/bin/python -B java_to_python/chapters/06_control_flow/example.py
```

如果已经激活仓库虚拟环境，也可以运行：

```bash
python3 -B java_to_python/chapters/06_control_flow/example.py
```

配套文件的职责是：

- `example.py`：可直接运行的 Python 示例，包含边界数据与练习结果；
- `Chapter06ControlFlow.java`：用 Java 展示相近问题，便于比较循环、下标和集合构造方式。

运行 Python 示例时重点观察：宽泛和严格分支的匹配顺序、`range` 的终点、默认 `zip` 的截断、`strict=True` 的异常、`pass` 后仍执行的语句、循环 `else` 的进入条件，以及取消订单优先于金额校验的结果。

## 本章小结

Python 控制流的核心可以压缩成四句话：

1. 分支按顺序匹配，缩进决定归属，真值规则决定条件是否成立。
2. `for` 直接消费可迭代对象；只有确实需要整数或位置时才引入 `range` 和下标。
3. `continue` 结束本轮，`break` 结束最近一层循环，`pass` 什么也不结束，循环 `else` 表示没有通过 `break` 离开。
4. 推导式适合简单、无副作用的结果构造；一旦流程包含状态、日志或多种退出原因，普通循环通常更可维护。
