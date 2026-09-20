# 第 8 章：面向对象编程

一句话先抓住本章：**Python 的类仍然负责把状态、规则和行为组织在一起，但它依靠公开属性、运行时协议和命名约定协作，不是把 Java 的字段、访问器和接口逐行换一种语法。**

本章面向已经熟悉 Java OOP、但刚开始学习 Python 的开发者。读完后，你应该能够：

- 判断一段逻辑该写成普通函数、类、继承关系还是组合关系；
- 解释类对象、实例、`self` 和绑定方法在运行时是什么关系；
- 分清实例属性、类属性和 `property`，知道一次读写最终落在哪里；
- 用 `__str__`、`__repr__`、`__eq__` 等协议接入 Python 内置行为；
- 正确选择实例方法、类方法和静态方法；
- 理解 `super()`、MRO、鸭子类型以及 Python 的封装约定；
- 避开可变类属性、把类型提示当校验、机械生成 getter/setter 等迁移陷阱。

本章配套材料是 `example.py` 和 `Chapter08Oop.java`。正文可以独立阅读，不需要先打开示例代码。

## 8.1 先完成 Java → Python 心智切换

两种语言都用类描述对象，但默认设计习惯不同：

| 要解决的问题 | Java 常见做法 | Python 常见做法与边界 |
| --- | --- | --- |
| 创建对象 | `new Account(...)`，调用构造器 | 直接调用类 `Account(...)`；`__new__` 创建，`__init__` 初始化 |
| 表示当前对象 | 隐式的 `this` | 定义方法时显式写 `self`，调用绑定方法时自动传入 |
| 暴露状态 | 私有字段配 getter/setter | 简单状态先用公开属性，需要规则时再升级为 `property` |
| 限制访问 | `private` / `protected` 由语言检查 | `_name` 是内部约定；`__name` 主要避免子类意外重名，不是安全边界 |
| 提供多种构造方式 | 重载构造器或静态工厂 | 命名明确的 `@classmethod` 替代构造器 |
| 重载同名方法 | 按参数签名选择版本 | 后定义的同名方法覆盖前一个；通常改用默认参数、关键字参数或不同方法名 |
| 表达多态 | 继承和显式接口 | 既支持继承，也常按对象实际提供的协议进行鸭子类型协作 |
| 定义通用行为 | 覆写 `toString`、`equals` 等 | 实现 `__str__`、`__eq__` 等双下方法，接入语言协议 |

可以把 Python 的 `property` 暂时类比为 getter/setter，把 `classmethod` 类比为静态工厂，把鸭子类型类比为面向接口编程，但这些类比都不能完全等同：Python 的属性访问、方法绑定和类型约束发生在运行时，是否继承某个声明也往往不是协作的必要条件。

## 8.2 什么时候值得创建一个类

### 类维护的是一组长期共同变化的状态和规则

账户对象需要同时维护户主、余额、“余额不能为负”的约束以及存取款行为。这些内容共享同一生命周期，放进一个类能够形成清楚的业务边界：

```python
class Account:
    def __init__(self, owner: str, balance: int = 0) -> None:
        if balance < 0:
            raise ValueError("余额不能为负")
        self.owner = owner
        self.balance = balance

    def deposit(self, amount: int) -> None:
        self.balance += amount
```

如果逻辑只是接收输入、完成计算、返回结果，而且不需要维护持续状态，普通函数通常更直接。Python 支持面向对象，但不要求把所有函数都包装进类。选择类的理由应是封装状态和不变量，而不是“Java 项目里通常有一个同名类”。

### 类本身也是运行时对象

Python 执行 `class Account: ...` 时，会执行类体并创建一个类对象，然后把名字 `Account` 绑定到它。调用这个类对象会创建实例：

```python
account = Account("Alice", 10_000)

print(type(Account))  # <class 'type'>
print(type(account))  # <class '__main__.Account'>（模块名可能不同）
```

这里没有 `new`。`Account` 是类对象，`account` 是指向实例对象的名字。普通赋值只增加一个指向同一对象的名字，不会复制实例：

```python
first = Account("Alice", 100)
second = first
second.deposit(50)

print(first.balance)     # 150
print(first is second)   # True
```

这与列表、字典等可变对象的引用语义相同，并不是 OOP 的特殊规则。

## 8.3 对象怎样创建，`self` 又从哪里来

### `__new__` 创建实例，`__init__` 建立初始状态

调用 `Account("Alice")` 时，可以用下面的简化流程理解：

1. Python 调用 `Account.__new__` 创建实例；
2. 新实例作为 `self` 传给 `Account.__init__`；
3. `__init__` 在实例上建立完整、合法的初始状态；
4. 类调用表达式返回创建好的实例。

因此，`__init__` 严格说是初始化器，不负责分配对象，也必须返回 `None`。下面的类能被定义，但实例化时会失败：

```python
class Wrong:
    def __init__(self) -> None:
        return 1  # TypeError: __init__() should return None
```

普通业务类几乎不需要重写 `__new__`。它主要出现在不可变类型子类、实例缓存或元编程中。初学 OOP 时，更重要的是让 `__init__` 一次建立好合法对象，而不是先返回半成品，再要求调用者按顺序调用多个 setter。

### `self` 是显式写出的接收者

`self` 不是关键字，却是 Python 代码共同遵守的固定命名约定。定义实例方法时必须显式声明它：

```python
class Account:
    def deposit(self, amount: int) -> None:
        self.balance += amount
```

调用时不需要手动传 `self`。下面两种写法的核心效果相同：

```python
account.deposit(100)             # 常规写法
Account.deposit(account, 100)    # 直接从类取函数，并显式传实例
```

读取 `account.deposit` 时，Python 会得到一个绑定方法：它把函数和 `account` 组合在一起，已经记住之后要使用的 `self`。

```python
deposit_later = account.deposit
print(deposit_later.__self__ is account)  # True
deposit_later(100)                         # 只需再传 amount
```

这也解释了为什么方法可以像普通函数一样保存到变量、放进容器或作为回调传递。

## 8.4 对象的状态到底存在哪里

### 实例属性保存独立状态，类属性保存共享默认值

在 `self` 上赋值通常创建实例属性；在类体中赋值创建类属性：

```python
class Account:
    bank_name = "示例银行"  # 类属性：类创建时只执行一次

    def __init__(self, owner: str) -> None:
        self.owner = owner   # 实例属性：每次初始化一个实例时赋值
```

每个账户都有自己的 `owner`。实例自身没有 `bank_name` 时，会从类及其父类读取该名称。给单个实例同名赋值，只会在该实例上创建一个遮蔽值：

```python
alice = Account("Alice")
bob = Account("Bob")

alice.bank_name = "城东分行"
print(alice.bank_name)          # 城东分行：来自 alice 实例
print(bob.bank_name)            # 示例银行：来自 Account 类
print(Account.bank_name)        # 示例银行：类属性没有改变
```

若要修改类级默认值，应显式写 `Account.bank_name = "新名称"`。已经拥有同名实例属性的 `alice` 仍会继续读到自己的遮蔽值。

### 属性查找不只是“先实例、后类”

对 `obj.name`，一个足够准确的入门模型是：

1. 先检查类及 MRO 上是否存在接管读写的数据描述符，`property`（包括只读 property）就属于它；
2. 如果没有，再检查实例自己的属性字典；
3. 再检查类及其父类上的普通属性或方法；
4. 仍找不到时，通常抛出 `AttributeError`。

这个顺序解释了两个看似不同的现象：普通类属性可以被实例属性遮蔽，而 `property` 能优先拦截同名属性的赋值。描述符是 Python 对象模型的通用机制；本章只需要掌握 `property` 带来的实际读写流程，更完整的数据模型会在后续章节继续展开。

### 最危险的类属性是可变容器

类体只执行一次，因此类属性中的列表也只创建一次：

```python
class BadShoppingCart:
    items: list[str] = []

first = BadShoppingCart()
second = BadShoppingCart()
first.items.append("Python 书")
print(second.items)  # ['Python 书']：两个实例读到同一个列表
```

每个实例需要独立容器时，必须在 `__init__` 中创建：

```python
class ShoppingCart:
    def __init__(self) -> None:
        self.items: list[str] = []
```

它与“可变默认参数只创建一次”的陷阱根因相同：对象是在定义时创建一次，还是在每次初始化时重新创建，决定了状态是否共享。

### 动态属性和类型提示都不会替你守住业务规则

普通 Python 实例通常允许初始化后新增属性，例如 `account.note = "VIP"`。这种能力适合探索，但稳定的业务对象仍应在 `__init__` 中统一建立主要状态，否则维护者、IDE 和类型检查器都很难判断实例有哪些属性，拼写错误也可能悄悄创建一个错误的新属性。

同样，`balance: int` 是给阅读者、IDE 和静态类型检查器看的提示，Python 默认不会据此插入运行时校验或转换：

```python
class Account:
    def __init__(self, balance: int) -> None:
        self.balance = balance

account = Account("100")  # 运行时可能接受，balance 实际是 str
```

余额非负、订单状态合法等不变量必须由业务代码显式检查。配套示例还特意拒绝 `bool`，因为 Python 中 `bool` 是 `int` 的子类：`isinstance(True, int)` 会得到 `True`。若金额必须是纯整数，可以用 `type(value) is int` 明确排除布尔值。

## 8.5 用 `property` 维护属性不变量

### 先保持自然的访问语法，再在边界集中校验

Java 常从私有字段和 getter/setter 起步。Python 更倾向先公开简单属性；当属性需要校验、计算、只读控制或 API 兼容时，再把它升级为 `property`，调用方仍然使用自然的属性语法：

```python
class Account:
    def __init__(self, balance: int = 0) -> None:
        self.balance = balance

    @property
    def balance(self) -> int:
        return self._balance

    @balance.setter
    def balance(self, value: int) -> None:
        if type(value) is not int:
            raise TypeError("余额必须是整数")
        if value < 0:
            raise ValueError("余额不能为负")
        self._balance = value
```

这里没有保存两份余额：

- `balance` 是类上的 `property` 对象，是公开读写入口；
- `_balance` 是实例上的普通属性，才是实际存储位置；
- `self.balance = balance` 会调用 setter，校验通过后才创建 `self._balance`。

`@property` 先把 getter 包装成名为 `balance` 的 property。随后 `@balance.setter` 在这个 property 上注册写入函数。因此不能写 `@_balance.setter`：`_balance` 只是实例运行时才创建的存储属性，并不是类上的 property。

### 一次读取和一次赋值怎样流动

读取 `account.balance` 时：

1. Python 在 `Account` 上找到 `balance` property；
2. property 调用 getter，并把 `account` 作为 `self`；
3. getter 返回 `account._balance`。

执行 `account.balance = 100` 时：

1. Python 找到同一个 property；
2. property 调用 setter，传入 `account` 和 `100`；
3. setter 先验证类型和范围；
4. 验证通过后，把 `100` 写入 `account._balance`。

setter 内必须写 `self._balance = value`。若再次写 `self.balance = value`，赋值会重新进入同一个 setter，最终因无限递归抛出 `RecursionError`。

`self.balance += amount` 不是绕过 property 的特殊写法，可以展开理解为：

```python
new_balance = self.balance + amount  # getter 读取 _balance
self.balance = new_balance           # setter 校验并写回 _balance
```

初始化、外部赋值、存款和利息等路径都应维护同一套不变量。它们可以统一经过 setter，也可以复用内部校验函数；关键是不能让某条写入路径成为绕过规则的后门。

### 只读属性与不适合 property 的操作

只定义 getter、不定义 setter，就能得到公开只读属性：

```python
class Wallet:
    def __init__(self, balance: int) -> None:
        self._balance = balance

    @property
    def balance(self) -> int:
        return self._balance
```

读取 `wallet.balance` 正常，执行 `wallet.balance = 100` 会抛出 `AttributeError`。但这仍不是安全隔离：外部技术上可以修改 `_balance`，只是明确违反了类的公开契约。

`property` 适合表现“读取起来像数据”的轻量行为。若操作昂贵，包含网络或磁盘 I/O，或者调用者需要意识到它会改变系统状态，显式方法如 `refresh_balance()`、`load_profile()` 更清楚。

## 8.6 三种方法分别绑定什么

判断方法种类时，不要先问“Java 中是不是 static”，先问实现需要哪一个隐式对象：

| 写法 | 自动绑定的首个参数 | 典型用途 |
| --- | --- | --- |
| 实例方法 | `self`，当前实例 | 读取或修改实例状态 |
| `@classmethod` | `cls`，实际调用该方法的类 | 可继承的替代构造器、类级行为 |
| `@staticmethod` | 无 | 与类概念紧密相关，但不需要实例或类状态的规则 |

### 类方法适合命名明确的替代构造器

Python 不按参数签名重载构造器，常用类方法表达“从文本创建”“从配置创建”等输入来源：

```python
from typing import Self

class Account:
    def __init__(self, owner: str, balance: int = 0) -> None:
        self.owner = owner
        self.balance = balance

    @classmethod
    def from_text(cls, text: str) -> Self:
        owner, balance = text.split(":", maxsplit=1)
        return cls(owner.strip(), int(balance))
```

执行 `Account.from_text("Alice:100")` 时，`cls` 是 `Account`。若子类继承该方法并调用 `SavingsAccount.from_text(...)`，`cls` 就是 `SavingsAccount`。因此替代构造器应调用 `cls(...)`，而不是写死 `Account(...)`；前提是子类构造签名仍兼容这里传入的参数。

`Self` 是类型提示，表示返回值与实际调用方法的类一致，不会改变运行时行为。可以先把类方法类比为 Java 的命名静态工厂，但不能完全等同：类方法自动接收实际调用的类，并能保留可继承性。

### 静态方法只是没有自动绑定参数的函数

```python
class Account:
    @staticmethod
    def is_valid_amount(amount: object) -> bool:
        return type(amount) is int and amount > 0
```

该规则与账户概念紧密相关，却不读取某个账户或类状态，所以可以放在 `Account` 中作为静态方法。若很多不同类型都要使用它，模块级普通函数通常更自然。

### 重复定义同名方法不会形成重载

```python
class Parser:
    def parse(self, text: str) -> str:
        return text

    def parse(self, number: int) -> str:
        return str(number)  # 直接覆盖前一个 parse
```

Python 通常用默认参数、仅关键字参数、不同且有业务含义的方法名，或者 `from_text` 这样的替代构造器表达多种调用方式。确实需要按类型分派时，再考虑 `functools.singledispatchmethod`。

## 8.7 用双下方法接入 Python 协议

`__str__`、`__repr__`、`__eq__`、`__len__` 等特殊方法规定对象如何参与通用语言操作。业务代码应写 `str(obj)`、`left == right`、`len(obj)`，而不是直接调用双下方法。

| 用户代码 | Python 使用的协议 | 设计目标 |
| --- | --- | --- |
| `print(obj)` / `str(obj)` | `__str__` | 面向最终用户的可读文本 |
| `repr(obj)` / 调试容器 | `__repr__` | 面向开发者的明确表示 |
| `left == right` | `__eq__` | 领域意义上的值相等 |
| `len(obj)` | `__len__` | 对象包含的元素数量 |

这些名字由 Python 数据模型保留。不要自创 `__save_data__` 一类双下名字；普通业务方法应该使用正常名称。

### `str` 与 `repr` 面向不同读者

```python
class Account:
    def __str__(self) -> str:
        return f"{self.owner}: {self.balance} 分"

    def __repr__(self) -> str:
        return f"Account(owner={self.owner!r}, balance={self.balance})"
```

`str` 优先让用户读懂，类似 Java 中面向展示的格式化结果。`repr` 优先帮助开发者定位对象，理想情况下接近一个可重建对象的表达式。`!r` 会调用字段自身的 `repr`，因此字符串的引号和转义不会丢失。密码、令牌等敏感字段必须隐藏，不能为了“完整调试信息”而泄露秘密。

### `==` 比较领域值，`is` 比较对象身份

```python
class Account:
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Account):
            return NotImplemented
        return (self.owner, self.balance) == (other.owner, other.balance)
```

- `first is second` 判断两个名字是否指向同一个对象，不能重载；
- `first == second` 询问两个对象在领域中是否值相等，可以由 `__eq__` 定义；
- 不知道如何比较某种类型时返回 `NotImplemented`，让 Python 尝试另一侧的实现；
- `NotImplemented` 是一个特殊值，不是表示“抽象方法未实现”的 `NotImplementedError` 异常。

相等规则还会影响哈希。若两个对象 `==`，它们的哈希值也必须相同。账户余额会变化，把这种对象作为 `set` 元素或字典键很危险；Python 在类只定义 `__eq__` 而没有合理的 `__hash__` 时，通常会让实例不可哈希。不要随手补一个与相等规则不一致的哈希实现。

## 8.8 继承复用契约，组合拆分职责

### `super()` 按 MRO 找下一个实现

`SavingsAccount` 是一种 `Account`，子类先复用账户初始化规则，再创建自己的利率状态：

```python
class SavingsAccount(Account):
    def __init__(
        self,
        owner: str,
        balance: int = 0,
        interest_rate: float = 0.02,
    ) -> None:
        super().__init__(owner, balance)
        self.interest_rate = interest_rate
```

`super()` 不能简单理解为“固定调用直接父类”。它会按照方法解析顺序（MRO）找到当前类之后的下一个实现；`SavingsAccount.mro()` 可以查看具体顺序。这个机制使协作式多继承成为可能，也要求参与协作的方法使用兼容的调用约定。

Python 语法不强制 `super().__init__()` 是第一条语句，但通常应先让父类建立完整、合法的基础状态。子类后续逻辑就可以安全依赖这些状态；父类初始化失败时，也不会先执行无意义的子类修改。

覆写方法不仅是复用实现，还要遵守父类公开契约。任何能接收 `Account` 的代码拿到 `SavingsAccount` 后，仍应能按相同语义存款、取款和读取余额。这就是替换关系，而不只是语法上写了一个父类名。

### Python 也常按行为完成多态

继承自 `Account` 的对象可以放入 `list[Account]`，由同一段代码调用共同方法。除此之外，Python 还大量采用鸭子类型：调用者关注对象能不能完成需要的行为，不一定要求它继承某个共同父类。

可以先把它类比为 Java 的面向接口编程，但不能完全等同。Java 通常要求显式 `implements`；Python 运行时往往直接尝试调用所需方法。大型项目可以用 `typing.Protocol` 把这种结构契约告诉静态类型检查器，协议的系统用法会在后续章节展开。

### “拥有一个”通常应该使用组合

银行卡不是一种账户，而是拥有并使用一个账户。用组合可以把支付限额和余额规则放在不同职责中：

```python
class BankCard:
    def __init__(self, account: Account, limit: int) -> None:
        self.account = account
        self.limit = limit

    def pay(self, amount: int) -> None:
        if amount > self.limit:
            raise ValueError("超过银行卡单笔限额")
        self.account.withdraw(amount)
```

`BankCard` 保存的是传入账户的引用，并没有复制余额。`pay()` 把扣款委托给账户，因此账户继续负责“金额有效、余额不能透支”等规则，银行卡只维护自己的单笔限额。

选择关系时可以先问两个问题：

- 子类能否在所有需要父类的位置保持父类契约？能，才可能是“是一种”的继承；
- 新对象只是需要另一个对象提供服务吗？是，通常应使用“拥有一个”的组合。

不要为了复用几行代码建立不自然的父子关系。多继承和复杂 MRO 是 Python 提供的能力，并不是默认设计目标。

## 8.9 Python 的封装依靠公开契约

### 单下划线表示内部实现，不阻止访问

`_balance` 表示“这是内部细节，外部调用者不要依赖”。类本身和受信任的子类仍可以使用它，IDE、自动补全和文档工具也会把它视为非公开名称。但 Python 不会因此阻止访问：

```python
account._balance = -1  # 技术上可能成功，但主动绕过了 Account 的公开契约
```

Python 的封装主要依靠清楚的文档、稳定的公开接口、`property` 或业务方法维护不变量，以及测试保护公开行为。“技术上能访问”不等于“调用方可以依赖”。

### 双下划线主要用于避免子类意外重名

类体中的 `self.__state` 会触发名称改写（name mangling），大致改为 `self._ClassName__state`：

```python
class Base:
    def __init__(self) -> None:
        self.__state = "base"
```

它的主要作用是避免子类无意间定义同名属性，不是制造真正的 Java `private` 字段。知道改写后名称的调用者仍可能访问它，所以更不能用它保护密钥或令牌。普通内部状态优先使用单下划线；只有设计可继承的基类、确实要避免子类命名冲突时，才考虑双下划线。

Python 也没有 Java 式 `protected`。设计给别人继承的类时，应明确哪些方法是稳定扩展点，避免子类依赖偶然的内部布局。配套代码中的 `SavingsAccount.add_interest()` 通过公开的 `balance` property 更新余额，而不是直接写父类 `_balance`，因此父类以后加强校验时，子类仍会经过规则。

## 8.10 Java 工程师最容易踩的坑

| 错误判断 | 失败表现 | 更合适的做法 |
| --- | --- | --- |
| 把每个实例的 `items = []` 写在类体中 | 不同实例意外共享同一个列表 | 在 `__init__` 中创建 `self.items` |
| 认为 `balance: int` 会拦截字符串 | 错误类型进入对象，稍后才在运算中失败 | 对业务不变量做显式运行时校验 |
| 为每个字段预先生成 getter/setter | 类充满没有规则的样板访问器 | 先用公开属性，需要规则时升级为 `property` |
| 在 property setter 中给同一公开属性赋值 | setter 无限递归，最终 `RecursionError` | 校验后写内部存储属性，如 `_balance` |
| 重复定义同名方法模拟 Java 重载 | 前面的定义被后面的覆盖 | 使用默认参数、关键字参数、不同方法名或类方法 |
| 用 `==` 判断是不是同一个实例 | 值相等的两个对象被误判成同一对象 | 身份比较用 `is`，领域值比较用 `==` |
| 写死 `Account(...)` 实现类方法构造器 | 子类调用后仍得到父类实例 | 使用自动绑定的 `cls(...)` |
| 为复用代码强行继承 | 子类无法维持父类语义，层级越来越脆弱 | 先验证 “is-a”，否则优先组合 |
| 用 `_name` 或 `__name` 保存秘密 | 调试、序列化或主动访问仍可能暴露数据 | 敏感信息不进入公开表示，并单独设计安全边界 |
| `__eq__` 遇到陌生类型就抛异常 | 正常的跨类型比较意外中断 | 返回 `NotImplemented` |

最后记住一个设计顺序：先找对象必须长期维护的状态和不变量，再确定公开协议，最后才选择普通属性、`property`、继承或组合。不要从“怎样把 Java 类翻译成更短的 Python”开始。

## 8.11 运行本章项目验证心智模型

本章只使用标准库，不需要安装第三方依赖。原仓库配置要求 Python 3.14 或更高版本。请在包含 `pyproject.toml` 的项目根目录执行：

```bash
python3 --version
python3 java_to_python/chapters/08_oop/example.py
```

运行 Python 示例时，重点观察：

1. 绑定方法的 `__self__` 是否就是创建方法的账户实例；
2. 修改一个实例的 `bank_name` 后，类属性和另一个实例是否保持不变；
3. 错误购物车的列表为何共享，正确购物车的列表为何独立；
4. `vars(account)` 中为何有 `_balance`，却没有第二份 `balance`；
5. 负余额、字符串余额和布尔金额分别在哪一层被拒绝；
6. 两个账户为何可以满足 `first == second`，同时 `first is not second`；
7. `SavingsAccount.from_text(...)` 为何创建子类，而不是固定创建 `Account`；
8. 银行卡支付后，传入的原账户余额为何也发生变化。

也可以使用统一运行器同时对照 Python 与 Java 版本：

```bash
python3 java_to_python/run_all.py --chapter 8
```

Java 对照文件展示传统私有字段、getter、静态工厂、`equals`、`hashCode` 和继承写法。它用于帮助迁移心智模型，不表示两份实现的每个设计都严格一一对应。统一运行器会把 Java 编译产物放入临时目录，不会在源码目录留下 `.class` 文件。

> 本次从单课 PDF 独立还原后，可在当前课程目录直接执行 `python example.py`。

## 本章总结

- 类适合共同维护状态、业务不变量和行为；无持续状态的计算通常优先用普通函数。
- 类本身是对象；调用类创建实例，`__init__` 初始化实例，普通赋值不会复制对象。
- `self` 是显式声明、调用绑定方法时自动传入的当前实例。
- 实例属性保存独立状态；类属性保存共享状态或默认值，可变类属性尤其危险。
- `property` 是公开访问入口，内部属性才保存数据；所有写入路径都应维护同一套不变量。
- `classmethod` 接收实际调用的类，适合可继承的替代构造器；`staticmethod` 不接收隐式对象。
- 双下方法让对象参与 Python 协议；`==` 是领域值相等，`is` 是对象身份。
- 继承表达可替换的“是一种”，组合表达“拥有一个”；复用几行代码本身不是继承理由。
- `_name` 和 `__name` 都不是访问控制或安全机制，稳定封装来自清楚的公开契约。

## 小练习

### 练习 1：基础对象与不变量

创建 `Wallet` 类：

- 初始化时接收所有者和以“分”为单位的余额；
- 余额必须是非负整数，且 `True` 不能作为金额；
- 余额只允许通过 `deposit()` 和 `withdraw()` 修改，金额必须为正且不能透支；
- `balance` 对调用方只读；
- 分别实现适合用户查看的 `__str__` 和适合调试的 `__repr__`。

验证零余额、负数、字符串金额、刚好取完和透支五种边界。

### 练习 2：替代构造器与相等

为 `Wallet` 增加 `from_text("Alice:100")` 类方法和 `__eq__`：

- 输入缺少冒号时抛出含义清楚的异常；
- 比较另一个 `Wallet` 时按所有者和余额判断；
- 比较不支持的类型时返回 `NotImplemented`；
- 验证两个值相等的钱包为什么仍可能满足 `first is not second`。

### 练习 3：比较继承与组合

先实现 `LimitedWallet` 子类，增加每日提现上限。再实现 `GiftCard`，让它拥有一个 `Wallet` 并增加单笔消费上限。说明哪个关系是“是一种”，哪个关系是“拥有一个”，以及失败路径为什么不能提前修改余额或已用额度。

### 练习 4：定位共享状态 bug

故意把交易记录列表写成类属性，创建两个钱包历史对象，只向其中一个添加记录，观察另一个对象为何也出现记录。然后把列表移入 `__init__` 修复，并用 `is` 验证两个列表已经是不同对象。

配套 `example.py` 包含这些练习的可运行参考实现。建议先独立完成，再对照实现并修改余额、利率和限额，分别观察成功路径与异常路径。
