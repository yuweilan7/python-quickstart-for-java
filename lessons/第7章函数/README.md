# 第 7 章：函数

Python 函数的核心不只是 `def`，而是两件事：**调用时如何把实参绑定给形参，以及函数本身也能像普通对象一样被传递。**

这两点解释了默认参数、关键字参数、`*args`、`**kwargs`、回调、排序键和闭包。对 Java 工程师来说，可以先把 Python 函数理解成“自带一套灵活绑定规则的可调用对象”，但不能把它完全等同于 Java 方法：Python 默认没有编译期重载选择，类型提示也不会自动做运行时校验。

配套代码：`example.py` / `Chapter07Functions.java`。

## 学习目标

学完本章后，你应该能够：

- 解释执行 `def` 与调用函数分别发生了什么；
- 判断位置参数、关键字参数、默认值、`/` 和 `*` 如何共同决定参数绑定；
- 避免可变默认值共享、重复传参和不受约束的 `**kwargs`；
- 区分函数对象与函数调用结果，并把函数作为参数或返回值；
- 理解闭包捕获、`lambda` 的用途与边界；
- 同时读懂定义侧和调用侧的 `*`、`**`；
- 用常见内置函数替代重复的计数、排序、配对和聚合代码。

## 先建立 Java → Python 心智模型

| 要解决的问题 | Java 中的常见方式 | Python 中的常见方式 | 不能直接等同的地方 |
| --- | --- | --- | --- |
| 定义可复用行为 | 类中的方法、静态方法 | 模块级函数、方法、局部函数 | 函数是运行时对象，可以赋值、传参和返回 |
| 提供缺省配置 | 方法重载、Builder | 默认参数、关键字参数 | 默认值在执行 `def` 时求值，不是每次调用都求值 |
| 限制调用形式 | 方法签名 | `/`、`*` 和明确的参数名 | 参数名可能成为公共 API 的一部分 |
| 接收任意数量的值 | Java varargs | `*args` | 函数内部得到元组；调用侧 `*` 还能拆开任意可迭代对象 |
| 接收命名配置 | 配置对象、Map | 明确的仅关键字参数或 `**kwargs` | `**kwargs` 灵活，但会削弱自动补全和拼写检查 |
| 传递一段行为 | 函数式接口、lambda、method reference | 直接传函数或其他可调用对象 | 不要求先声明专用函数式接口，类型提示也不强制校验 |
| 返回多个结果 | DTO、数组 | 常用元组打包与解包 | `return a, b` 实际只返回一个二元组对象 |

## 7.1 从 `def` 到 `return`：函数调用发生了什么

### `def` 创建函数，括号触发调用

`def` 是一条可执行语句。程序执行到它时，Python 创建一个函数对象，并把函数名绑定到该对象；函数体要等到调用时才执行：

```python
def add(left: int, right: int) -> int:
    result = left + right
    return result


operation = add          # 保存函数对象，没有执行函数体
answer = operation(10, 20)  # 调用函数，得到 30
```

`left`、`right` 是定义中的形参（parameter），`10`、`20` 是调用时提供的实参（argument）。一次普通调用可以用下面的简化过程理解：

1. 先计算调用表达式中的各个实参；
2. 按函数签名把实参绑定给形参；
3. 如果缺少参数、参数重复或出现不允许的参数，抛出 `TypeError`，函数体不会开始执行；
4. 绑定成功后，使用这次调用的局部名字执行函数体；
5. 遇到 `return` 时立即结束本次调用，把结果交给调用者。

没有显式 `return`，或者只写 `return`，都会返回 `None`：

```python
def print_message(message: str) -> None:
    print(message)


result = print_message("hello")
print(result)  # None
```

`return` 后写逗号分隔的多个值时，Python 会先把它们组成一个元组：

```python
def divide(dividend: int, divisor: int) -> tuple[int, int]:
    return dividend // divisor, dividend % divisor


quotient, remainder = divide(17, 5)  # 实际返回 (3, 2)，随后被解包
```

### 类型提示不是运行时门卫

`left: int` 和 `-> int` 是类型提示。它们服务于读者、IDE、Pyright 和 mypy，但 Python 默认不会像 Java 编译器那样阻止错误类型进入函数：

```python
def double(number: int) -> int:
    return number * 2


double("ha")  # 运行成功，结果是 "haha"，尽管它不符合类型提示
```

Python 是动态类型语言，不是“没有类型”：“ha”仍然是运行时类型为 `str` 的对象，只是形参不会像 Java 变量那样在编译期限制它能绑定的对象。业务入口需要强校验时，要显式校验，或者交给数据校验框架；类型提示本身不负责。

### 参数传递的是对象，重新绑定不等于修改对象

可以把 Python 的参数传递先类比为 Java 的“对象引用按值传递”，但 Python 中整数、函数、类等也都是对象。调用后，形参只是新绑定到调用者提供的对象：

```python
def add_item(items: list[str]) -> None:
    items.append("Python")  # 修改双方共享的列表对象


def replace_items(items: list[str]) -> None:
    items = ["new"]         # 只把局部名字绑定到新列表


courses = ["Java"]
add_item(courses)      # courses 变为 ["Java", "Python"]
replace_items(courses) # courses 不变
```

判断调用是否会影响外部时，不要只看“有没有赋值”，而要看函数是在修改共享对象，还是在重新绑定局部名字。

### 同名 `def` 不是方法重载

Python 不会根据参数数量或类型，在多个同名定义之间选择版本：

```python
def greet(name: str) -> str:
    return f"你好，{name}"


def greet(name: str, greeting: str) -> str:
    return f"{greeting}，{name}"


greet("Alice")  # TypeError：名字 greet 已经指向第二个函数
```

第二次执行 `def` 是把名字 `greet` 重新绑定到新函数对象。Python 通常用默认参数和关键字参数表达多种调用形式；语义不同的操作应使用不同函数名。确实需要按运行时类型分派时，可以了解 `functools.singledispatch`，但它也不是 Java 的通用方法重载。

## 7.2 位置参数与默认参数：先理解绑定，再写调用

### 位置参数按顺序绑定

```python
def introduce(name: str, city: str) -> str:
    return f"我是 {name}，来自 {city}"


introduce("Alice", "上海")
```

第一个实参绑定给 `name`，第二个绑定给 `city`。如果两个参数类型相同，顺序传反通常不会触发技术错误，却会产生业务错误。这也是参数较多时应考虑关键字调用的原因。

参数数量不匹配会在进入函数体之前失败：

```python
introduce("Alice")          # TypeError：缺少 city
introduce("Alice", "上海", 28)  # TypeError：多了一个位置实参
```

### 默认值只在调用者省略参数时使用

```python
def greet(name: str, greeting: str = "你好") -> str:
    return f"{greeting}，{name}"


greet("Alice")                   # "你好，Alice"
greet("Alice", "早上好")         # "早上好，Alice"
greet("Alice", greeting="晚上好")
```

在普通参数区间内，没有默认值的必填参数必须位于有默认值的参数之前：

```python
def valid(required, optional=0): ...
# def invalid(optional=0, required): ...  # SyntaxError
```

这份签名可以覆盖 Java 中常见的两个重载，但它还有一条非常重要的运行时规则。

### 可变默认值为什么会跨调用共享

默认值在执行 `def` 时计算一次，并保存在函数对象上，不会在每次调用时重新计算。因此，把列表、字典或集合作为默认值，会让多次调用共享同一个对象：

```python
def unsafe_collect(item: str, target: list[str] = []) -> list[str]:
    target.append(item)
    return target


first = unsafe_collect("A")
second = unsafe_collect("B")
print(first)           # ['A', 'B']
print(first is second) # True
```

安全写法是用 `None` 表示“调用者没有提供”，再在函数体内创建对象：

```python
def collect(item: str, target: list[str] | None = None) -> list[str]:
    actual_target = [] if target is None else target
    actual_target.append(item)
    return actual_target
```

这份代码有两种明确语义：

- 省略 `target`：每次调用创建新列表，调用之间互不影响；
- 显式传入列表：函数修改调用者的列表，并返回同一个对象。

如果函数承诺不修改调用者的数据，应在收到列表时复制，例如 `actual_target = list(target)`。如果 `None` 本身也是一个合法业务值，可以创建专用哨兵对象来区分“未提供”和“提供了 None”。

同一规则也适用于 `datetime.now()`、随机数和普通函数调用：把它们写成默认值，会在定义函数时执行。需要“本次调用的当前时间”时，应在函数体内计算。

## 7.3 关键字参数、`/` 与 `*`：主动设计调用边界

### 关键字参数把业务含义写在调用处

比较两种调用：

```python
connect("db.example.com", 5432, False, 10.0)
connect("db.example.com", port=5432, use_ssl=False, timeout=10.0)
```

第二种写法让布尔值和数字的含义一目了然。布尔开关、多个同类型参数和非必填配置尤其适合关键字调用。

同一个形参不能同时按位置和名称获得两个值：

```python
connect("db.example.com", host="localhost")
# TypeError: host 同时收到了位置实参和关键字实参
```

调用者一旦写下 `timeout=10.0`，`timeout` 就成为公共 API 契约的一部分。以后把它改名为 `timeout_seconds`，会破坏已有调用；公共函数的参数名需要像方法名一样认真设计。

### `/` 和 `*` 把参数分成三个区域

观察这个签名：

```python
def price(amount, /, tax_rate=0.13, *, discount=0.0):
    discounted = amount * (1 - discount)
    return round(discounted * (1 + tax_rate), 2)
```

`/` 和裸 `*` 只负责划分边界，它们不是形参：

| 参数所在位置 | 参数种类 | 合法传法 |
| --- | --- | --- |
| `/` 之前的 `amount` | 仅位置参数 | 只能写 `price(100.0)` |
| `/` 与 `*` 之间的 `tax_rate` | 位置或关键字参数 | `price(100.0, 0.1)` 或 `tax_rate=0.1` |
| `*` 之后的 `discount` | 仅关键字参数 | 必须写 `discount=0.2` |

```python
price(100.0)
price(100.0, 0.10, discount=0.20)
price(100.0, tax_rate=0.10, discount=0.20)

price(amount=100.0)       # TypeError：amount 只能按位置传递
price(100.0, 0.10, 0.20) # TypeError：discount 只能按名称传递
```

设计接口时可以这样判断：

- 参数像 `len(obj)` 中的 `obj` 一样自然，而且参数名不应成为契约时，可以使用仅位置参数；
- 参数名能解释值的业务含义，特别是开关和配置项时，优先使用仅关键字参数；
- 简短的内部函数不必为了展示语法而强行加入 `/`；
- 稳定公共接口可以主动限制调用方式，让误用尽早变成 `TypeError`。

### 用一张表看完整绑定模型

Python 的参数按以下顺序出现在定义中：

```python
def inspect_call(
    identifier,          # 仅位置参数
    /,
    category="general", # 位置或关键字参数
    *tags,               # 收集剩余位置实参
    active=True,         # 仅关键字参数
    **extra,             # 收集剩余关键字实参
):
    return identifier, category, tags, active, extra
```

调用：

```python
result = inspect_call(
    1001,
    "course",
    "Python",
    "beginner",
    active=True,
    owner="Alice",
)
```

绑定结果是：

| 形参 | 得到的值 | 原因 |
| --- | --- | --- |
| `identifier` | `1001` | 第一个位置实参，只能按位置绑定 |
| `category` | `"course"` | 下一个位置实参覆盖默认值 |
| `tags` | `("Python", "beginner")` | 剩余位置实参被收集为元组 |
| `active` | `True` | 通过名称绑定仅关键字参数 |
| `extra` | `{"owner": "Alice"}` | 未被明确形参接收的关键字实参进入字典 |

只要记住“先按签名找到归属，再补默认值，最后检查缺失、重复和多余参数”，复杂签名就不必靠猜。

## 7.4 常用内置函数：优先复用 Python 的数据协议

许多 Python 内置函数面向协议工作，而不是绑定到某个具体类。例如，只要对象能报告长度，`len(obj)` 就能使用；字符串、列表、元组、集合和字典都支持它。

| 内置函数 | 作用 | 关键边界 |
| --- | --- | --- |
| `len(items)` | 返回元素数量 | 字典计算键值对数量，字符串计算字符数量 |
| `sum(numbers)` | 数值求和 | 空输入结果为 `0`；不要用来拼字符串 |
| `min` / `max` | 找最小值或最大值 | 空输入抛出 `ValueError`；可用 `key` 提取比较依据 |
| `sorted(items)` | 返回排序后的新列表 | 不修改原数据；`list.sort()` 才原地修改列表 |
| `enumerate(items)` | 同时产生序号和值 | `start=1` 可生成面向用户的编号 |
| `zip(a, b)` | 按位置配对多个可迭代对象 | 默认到最短输入为止；等长是约束时用 `strict=True` |
| `any(items)` | 是否至少一个元素为真 | `any([])` 是 `False` |
| `all(items)` | 是否所有元素都为真 | `all([])` 是 `True`，业务上可能需要额外处理空输入 |

```python
scores = [88, 95, 72, 100]

sum(scores)                              # 355
sorted(scores, reverse=True)             # [100, 95, 88, 72]
any(score == 100 for score in scores)    # True
all(score >= 60 for score in scores)     # True
```

### `key` 是“提取比较键”，不是两两比较器

```python
words = ["pear", "fig", "banana"]
sorted(words, key=len)  # ['fig', 'pear', 'banana']
```

`sorted` 会把每个元素交给 `key` 函数，得到用于排序的值，这里比较的是 `3、4、6`，而不是让 `len` 比较两个单词。这和 Java `Comparator` 的二元比较模型不同，更接近先执行一次 `map` 提取排序键，再按键排序。

需要多个排序条件时，可以返回元组：

```python
sorted(words, key=lambda word: (len(word), word))
```

Python 按元组元素从左到右比较，所以先按长度，再按单词本身排序。

### `enumerate` 和严格 `zip` 让结构错误更早暴露

```python
for number, name in enumerate(["Alice", "Bob"], start=1):
    print(number, name)

names = ["Alice", "Bob"]
scores = [95, 88]
pairs = list(zip(names, scores, strict=True))
```

普通 `zip` 在最短输入耗尽时停止，可能静默丢掉较长一侧的数据。两边本应严格等长时，`strict=True` 会在长度不一致时抛出 `ValueError`。

## 7.5 函数也是对象：把行为放进数据流

“函数是一等对象”意味着函数可以绑定给变量、放进容器、作为实参传入，也可以从另一个函数返回。区别首先在有没有调用括号：

```python
def double(number: int) -> int:
    return number * 2


operation = double   # 函数对象
result = operation(5) # 调用函数，结果是 10
```

### 传入函数：不先声明函数式接口

```python
from collections.abc import Callable


def apply(value: int, operation: Callable[[int], int]) -> int:
    return operation(value)


apply(-4, abs)    # 4
apply(5, double)  # 10
```

`Callable[[int], int]` 表示“可以用一个 `int` 调用，并预期返回 `int` 的对象”。它可以先类比为 Java 的函数式接口类型，但 Python 不要求为每种函数签名单独声明接口，而且这个类型提示仍然不会自动在运行时校验。

`sorted(students, key=score_of)` 是同一种高阶函数模式：调用者注入“怎样提取分数”，`sorted` 负责遍历和排序。函数的职责因此可以保持分离。

### 返回函数：闭包把行为和外部环境放在一起

```python
from collections.abc import Callable


def make_multiplier(factor: int) -> Callable[[int], int]:
    def multiply(value: int) -> int:
        return value * factor

    return multiply


triple = make_multiplier(3)
triple(10)  # 30
```

外层调用结束后，`multiply` 仍然能访问当时所在作用域中的 `factor`。这种“函数与它所引用的外部环境”称为闭包。它适合生成小型策略或回调；当状态、生命周期和行为逐渐复杂时，类通常更容易维护。

闭包捕获的是名字所在的绑定环境，不是每次创建时自动复制当前值。下面三个函数最终都会读到循环结束后的 `factor=4`：

```python
def make_operations():
    operations = []
    for factor in (2, 3, 4):
        operations.append(lambda value: value * factor)
    return operations


[operation(10) for operation in make_operations()]  # [40, 40, 40]
```

需要固定每轮的值时，优先调用 `make_multiplier(factor)` 这样的工厂函数。也可以利用默认值在函数创建时求值的规则写 `lambda value, factor=factor: value * factor`，但工厂函数通常更清楚。

### `lambda` 只适合一条短规则

`lambda` 创建匿名函数，函数体只能是一个表达式：

```python
sorted(words, key=lambda word: (len(word), word))
```

它适合只用一次、含义清楚的转换或排序键。需要多步计算、异常处理、注释、复用或独立测试时，应改用有名字的 `def`。

列表推导式与 `lambda` 不是同一种语法：

```python
[student.name for student in students] # 推导式负责遍历并创建列表
lambda student: student.name           # 匿名函数一次只处理一个实参
```

只有把后者交给 `map`、`sorted` 等负责遍历的函数，它才会依次作用于一批数据。

## 7.6 `*args`：收集或拆开位置实参

星号出现在定义侧和调用侧时，方向正好相反：

| 出现位置 | 含义 |
| --- | --- |
| `def total(*values)` | 收集：把剩余位置实参装进元组 `values` |
| `total(*numbers)` | 拆开：把可迭代对象中的元素作为多个位置实参传入 |

### 定义侧：收集为元组

```python
def total(*values: float) -> float:
    print(type(values), values)
    return sum(values)


total()                 # values == ()
total(10.0, 20.0, 30.0) # values == (10.0, 20.0, 30.0)
```

`args` 只是社区惯用名，真正起作用的是 `*`。有明确业务含义时，`values`、`paths`、`tags` 通常比 `args` 更容易读懂。

固定位置参数可以放在 `*args` 前面；`*args` 后面的参数自动成为仅关键字参数：

```python
def format_tags(prefix: str, *tags: str, separator: str = ", ") -> str:
    return f"{prefix}: {separator.join(tags)}"


format_tags("课程", "Python", "函数")
format_tags("课程", "Python", "函数", separator=" | ")
```

### 调用侧：拆开可迭代对象

```python
numbers = [10.0, 20.0, 30.0]
total(*numbers)      # 等价于 total(10.0, 20.0, 30.0)
total(5.0, *numbers) # 等价于 total(5.0, 10.0, 20.0, 30.0)
```

这与 Java varargs 有相似用途，但 Python 的 `*` 还可以在调用任意函数时拆开列表、元组、`range` 或生成器。即使语法允许，也不要用 `*args` 隐藏数量固定、含义不同的业务参数。

## 7.7 `**kwargs`：收集或拆开关键字实参

双星号同样需要区分定义侧和调用侧：

| 出现位置 | 含义 |
| --- | --- |
| `def create_profile(**attributes)` | 收集：把额外关键字实参装进字典 |
| `create_profile(**options)` | 拆开：把映射的键和值作为参数名和实参值 |

### 定义侧：收集为字典

```python
def create_profile(name: str, **attributes: object) -> dict[str, object]:
    return {"name": name, **attributes}


profile = create_profile("Alice", age=28, city="Shanghai")
# attributes == {"age": 28, "city": "Shanghai"}
```

`kwargs` 也只是惯用名。使用 `attributes`、`metadata` 或 `options`，通常能更准确地表达这些额外参数的用途。

### 调用侧：拆开映射

```python
options = {"age": 30, "city": "Beijing"}
create_profile("Bob", **options)
```

作为关键字参数展开时，映射的键必须是字符串。展开后仍然遵守普通绑定规则；同一参数收到两个值会抛出 `TypeError`：

```python
options = {"name": "Bob"}
create_profile("Alice", **options) # TypeError: name 得到了两个值
```

`*` 和 `**` 可以同时用于转发数据：

```python
def summarize(*values: float, **metadata: object) -> dict[str, object]:
    return {
        "values": values,
        "total": sum(values),
        "metadata": metadata,
    }


numbers = [10.0, 20.0, 30.0]
options = {"author": "Alice", "unit": "分"}
summarize(*numbers, **options)
```

### `**kwargs` 是开放扩展点，不是默认接口设计

`**kwargs` 适合装饰器转发、轻量适配层和真正开放的扩展点。稳定业务接口如果全靠它接收配置，拼写错误可能被藏进字典，IDE 也很难提供准确补全：

```python
def request(url: str, **options: object): ...
request("https://example.com", tiemout=10) # timeout 拼错，函数仍可能先接收下来
```

参数集合稳定时，明确列出仅关键字参数更可靠：

```python
def request(
    url: str,
    *,
    timeout: float = 3.0,
    verify_ssl: bool = True,
) -> str:
    ...
```

配置字段很多、需要复用并且具有明确结构时，可以在后续章节使用数据类；如果必须保留字典形态，可以用 `TypedDict` 描述结构。两者都比无边界地扩张 `**kwargs` 更能表达契约。

## Java 工程师最容易误判的地方

| 容易带过来的判断 | 实际行为 | 推荐做法 |
| --- | --- | --- |
| 同名函数会像 Java 一样重载 | 后一次 `def` 重新绑定同一个名字 | 默认参数、明确命名，必要时使用专门分派机制 |
| 类型提示会阻止错误实参 | 默认只供静态分析，运行时仍会调用 | 使用类型检查器；业务边界显式校验 |
| 默认列表每次调用都会新建 | 默认对象在执行 `def` 时只创建一次 | 用 `None` 或专用哨兵，在函数体内创建 |
| 给形参赋新值会替换调用者变量 | 只重新绑定当前调用的局部名字 | 明确区分重新绑定和修改共享对象 |
| `key` 像 Java `Comparator` 一样接收两个值 | `key` 每次接收一个元素并返回比较键 | 写提取函数，例如 `key=lambda user: user.age` |
| 闭包自动保存每轮循环值 | 闭包稍后按绑定环境读取名字 | 使用工厂函数创建闭包，或显式固定默认值 |
| `*args` / `**kwargs` 越多越灵活越好 | 契约变模糊，错误更晚暴露 | 稳定接口优先明确参数和仅关键字参数 |

设计一个函数接口时，可以快速检查四件事：哪些参数必须提供，哪些值容易传反，函数是否修改传入的可变对象，扩展点是否真的需要接收任意参数。灵活语法的目标是让契约更清楚，而不是让所有调用都能“先传进来再说”。

## 小练习

### 练习 1：安全的默认值

实现 `add_tag(tag, tags=None)`。连续调用 `add_tag("Python")` 和 `add_tag("Java")`，确认得到两个互不影响的列表；再传入已有列表，观察原列表是否被修改。

### 练习 2：清晰的调用边界

实现：

```python
def create_user(username, /, *, admin=False, active=True):
    ...
```

分别尝试合法调用与三种错误调用：使用 `username=`；把两个布尔值按位置传递；把 `active` 拼错。记录每次 `TypeError` 是在进入函数体前还是进入后发生。

### 练习 3：成绩报告

实现 `score_report(*scores, pass_line=60, **metadata)`，返回总分、平均分、是否全部及格、是否有人满分和附加信息。特别决定零个分数时平均分和“全部及格”应该是什么，不要直接让 `all([]) == True` 替业务做决定。

### 练习 4：URL 构建器

实现 `build_url(base, *paths, secure=True, **query)`：连接任意数量的路径，并使用标准库 `urllib.parse.urlencode` 编码查询参数。测试空路径、包含空格的值，以及同名查询参数需要多个值时应使用的数据结构。

## 运行本章示例

项目当前使用 Python 3.14；本章语法需要 Python 3.10+，并且只使用标准库，不需要安装第三方依赖。请在项目根目录运行：

```bash
python3 java_to_python/chapters/07_functions/example.py
```

输出按 7.2～7.7 和 README 练习分段。运行时重点观察：

- 可变默认列表的两次返回是否指向同一个对象；
- `/`、`*` 如何限制 `price` 和 `connect` 的调用方式；
- 函数放进变量、字典、参数和返回值后何时真正执行；
- `*args` 在函数内是元组，`**kwargs` 在函数内是字典；
- 错误参数名、错误位置调用如何在函数体执行前抛出 `TypeError`。

若本机同时安装了 JDK，可以通过统一运行器对照 Python 与 Java 版本：

```bash
python3 java_to_python/run_all.py --chapter 7
```

统一运行器会把 Java 编译结果放进临时目录，不会在源码目录留下 `.class` 文件。想只运行 Python 时可追加 `--python-only`。

## 本章小结

- 调用函数时，Python 先计算实参并按签名完成绑定，成功后才执行函数体；
- 默认值在执行 `def` 时求值一次，可变默认对象会被多次调用共享；
- `/`、`*`、明确参数名和默认值共同构成函数的调用契约；
- 函数是对象，因此能赋值、传入、返回并形成闭包；
- 定义侧的 `*`、`**` 负责收集，调用侧的 `*`、`**` 负责拆开；
- `*args` 和 `**kwargs` 提供扩展能力，但稳定业务接口仍应优先表达明确契约。
