"""第 8 章：用可运行示例理解 Python 面向对象。

本章不只展示语法，还会打印对象状态、方法绑定和属性查找的结果。建议先猜每一段
输出，再运行文件验证自己的心智模型。

阅读本文件时先抓住四条主线：

1. ``self`` 是当前实例，``self.xxx`` 通常表示这个实例上的状态。
2. ``balance`` 和 ``_balance`` 不是两份余额：前者是公开 property，后者才是实际存储值。
3. ``@classmethod`` 自动接收“实际调用的类” ``cls``；``@staticmethod`` 不自动接收对象。
4. 子类先用 ``super().__init__`` 建好父类部分，再初始化自己新增的部分，通常最容易维护。
"""

from typing import ClassVar, Self


def show_section(title: str) -> None:
    """让每个知识点的终端输出拥有清晰边界。"""
    print(f"\n{'=' * 12} {title} {'=' * 12}")


class Account:
    """一个维护“余额不能为负”这一业务不变量的账户。

    对外公开 ``account.balance``，让调用者像访问普通字段一样读写余额；对内只用
    ``account._balance`` 保存真正的数值。property 位于二者中间，负责拦截公开读写并校验。

    可以把它想成：``balance`` 是带门卫的窗口，``_balance`` 是窗口后面的仓库。窗口和仓库
    指向的是同一份业务状态，并不是两个余额变量。
    """

    bank_name: str = "示例银行"  # 类属性：保存在类上，所有实例默认都能读到

    def __init__(self, owner: str, balance: int = 0) -> None:
        """初始化已经由 Python 创建好的实例。

        类型提示不会在运行时自动检查参数，所以重要的业务约束仍需显式校验。
        初始余额通过 property setter 写入，让初始化和后续赋值共用同一套余额规则。
        """
        if not isinstance(owner, str):
            raise TypeError("户主必须是字符串")

        normalized_owner: str = owner.strip()
        if not normalized_owner:
            raise ValueError("户主不能为空")

        # owner 不需要拦截读写，所以直接保存为普通的公开实例属性。
        self.owner: str = normalized_owner

        # 这行看起来像普通赋值，实际会调用下面的 balance setter：
        #
        #     Account.balance.fset(self, balance)
        #
        # setter 校验成功后才创建 self._balance。实例中不会再额外保存一份名为
        # self.balance 的数据，因此这里没有“两个余额”。
        self.balance = balance

    # 第一步：@property 把这个 getter 函数包装成类属性 Account.balance。
    # 以后读取 account.balance 时，Python 会自动调用 balance(account)。
    @property
    def balance(self) -> int:
        """读取公开属性 ``balance`` 时，返回内部真正保存的 ``_balance``。"""
        return self._balance

    # 第二步：此时 balance 已经是 property 对象；@balance.setter 的意思是
    # “给这个名为 balance 的 property 注册写入函数”。
    # 不能写 @_balance.setter，因为 _balance 只是实例运行时才出现的普通值，
    # 类定义到这里时不存在名为 _balance 的 property 对象。
    @balance.setter
    def balance(self, value: int) -> None:
        """拦截 ``account.balance = value``，校验后写入唯一的存储字段。"""
        # bool 是 int 的子类，因此这里用 type(value) is int 明确拒绝 True/False。
        if type(value) is not int:
            raise TypeError("余额必须是整数（单位：分）")
        if value < 0:
            raise ValueError("余额不能为负")

        # _balance 是真正保存数值的实例属性。这里必须写 _balance，不能再写
        # self.balance = value，否则会再次进入当前 setter，造成无限递归。
        self._balance: int = value

    def deposit(self, amount: int) -> None:
        """向当前 ``self`` 账户存款，并让新余额继续经过 setter。"""
        if not self.is_valid_amount(amount):
            raise ValueError("存款金额必须为正整数")

        # self.balance += amount 可以展开理解成：
        #
        #     new_balance = self.balance + amount  # 先调用 getter
        #     self.balance = new_balance           # 再调用 setter
        #
        # 因而最终仍由 setter 把结果保存到 self._balance。
        self.balance += amount

    def withdraw(self, amount: int) -> None:
        """从当前账户取款；校验失败时不会改变余额。"""
        if not self.is_valid_amount(amount):
            raise ValueError("取款金额必须为正整数")
        if amount > self.balance:
            raise ValueError("余额不足")
        self.balance -= amount

    def __str__(self) -> str:
        """面向最终用户的可读文本，由 str() 和 print() 使用。"""
        return f"{self.owner}: {self.balance} 分"

    def __repr__(self) -> str:
        """面向开发者的调试文本，尽量暴露重建对象所需的关键信息。"""
        return f"Account(owner={self.owner!r}, balance={self.balance})"

    def __eq__(self, other: object) -> bool:
        """定义领域中的“值相等”，而不是比较两个变量是否指向同一对象。"""
        if not isinstance(other, Account):
            # 让 Python 有机会尝试 other 的反向比较；双方都不支持时结果才是 False。
            return NotImplemented
        return (self.owner, self.balance) == (other.owner, other.balance)

    @classmethod
    def from_text(cls, text: str) -> Self:
        """把 ``"户主:余额"`` 文本转换成对象，是一个有名字的替代构造器。

        这不是反射。``Account`` 本来就是可以调用的类对象；@classmethod 只是在调用本方法时
        自动把类传给 ``cls``：

        - ``Account.from_text(...)`` 中，``cls is Account``；
        - ``SavingsAccount.from_text(...)`` 中，``cls is SavingsAccount``。

        Java 可以把它类比为 ``static Account fromText(String text)`` 工厂方法，但 Python 的
        ``cls(...)`` 会保留实际调用的子类。返回类型 ``Self`` 只给 IDE/类型检查器看。
        """
        # str.partition(":") 总是返回三部分：冒号左边、冒号本身、冒号右边。
        # 如果原文本没有冒号，separator 就是空字符串。
        owner: str
        separator: str
        balance_text: str
        owner, separator, balance_text = text.partition(":")
        if not separator:
            raise ValueError("账户文本必须使用“户主:余额”格式")

        # 调用一个类对象就会创建实例。这里等价于：
        # Account.from_text 时调用 Account(owner, ...)，子类调用时则创建子类实例。
        return cls(owner, int(balance_text.strip()))

    @staticmethod
    def is_valid_amount(amount: object) -> bool:
        """检查金额；既不读取实例 ``self``，也不读取类 ``cls``。

        这与 Java 静态方法最接近。它放在 Account 中只是因为规则与账户概念紧密相关；如果很多
        不同类型都要使用这条规则，也可以把它写成模块级普通函数。
        """
        return type(amount) is int and amount > 0


class SavingsAccount(Account):
    """SavingsAccount 是一种 Account，继承户主、余额、存款和取款规则。"""

    def __init__(self, owner: str, balance: int = 0, interest_rate: float = 0.02) -> None:
        # super() 按 MRO 找下一个 __init__，在这里就是 Account.__init__。
        # 它先建立 owner 和 balance，保证父类部分已经是合法、完整的 Account。
        #
        # Python 语法并不强制 super() 必须是第一条语句；在当前简单实现中，先设置
        # interest_rate 也碰巧能工作。但通常应该先初始化父类，因为子类后续代码、property
        # 或父类方法可能依赖 owner/balance。父类初始化失败时，也能避免先留下子类的半成品状态。
        super().__init__(owner, balance)

        # 这会进入下面的 interest_rate setter；校验后由 _interest_rate 保存实际值。
        self.interest_rate = interest_rate

    # 与 balance/_balance 完全相同：interest_rate 是公开入口，_interest_rate 是内部存储。
    @property
    def interest_rate(self) -> float:
        """读取公开利率时，返回内部存储值。"""
        return self._interest_rate

    # 必须写 @interest_rate.setter，因为 setter 要注册到名为 interest_rate 的 property 上。
    @interest_rate.setter
    def interest_rate(self, value: float) -> None:
        """拦截公开利率赋值，把合法的 int/float 统一保存成 float。"""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("利率必须是数字")
        if not 0 <= value <= 1:
            raise ValueError("利率必须在 0 到 1 之间")

        # 不写 self.interest_rate = ...，否则会递归调用当前 setter。
        self._interest_rate = float(value)

    def add_interest(self) -> int:
        """用两个公开 property 计算利息，再通过余额 setter 写回。"""
        # self.balance 会调用父类的 balance getter；self.interest_rate 会调用当前类的
        # interest_rate getter。它们返回的实际值分别来自 _balance 和 _interest_rate。
        interest: int = round(self.balance * self.interest_rate)

        # 这行先通过 getter 读取旧余额，计算新值，然后通过 setter 写回 _balance。
        # 使用公开接口能继续复用 Account 对余额的约束，子类不需要知道父类怎样存储余额。
        self.balance += interest
        return interest

    def __repr__(self) -> str:
        return (
            f"SavingsAccount(owner={self.owner!r}, balance={self.balance}, "
            f"interest_rate={self.interest_rate})"
        )


class BankCard:
    """银行卡“拥有”账户而不是“属于”账户，用组合比继承更贴合关系。"""

    def __init__(self, account: Account, single_payment_limit: int = 5_000) -> None:
        # 静态方法没有 self/cls，可以直接通过类名调用。
        if not Account.is_valid_amount(single_payment_limit):
            raise ValueError("单笔限额必须为正整数")

        # 保存的是 Account 对象的引用，不是复制一份账户。card.pay 最终会修改同一个账户。
        self.account: Account = account
        self.single_payment_limit: int = single_payment_limit

    def pay(self, amount: int) -> None:
        if not Account.is_valid_amount(amount):
            raise ValueError("消费金额必须为正整数")
        if amount > self.single_payment_limit:
            raise ValueError("超过银行卡单笔限额")
        # 把实际扣款委托给账户，继续复用 Account 的余额不足等规则。
        self.account.withdraw(amount)


class BadShoppingCart:
    """教学反例：可变类属性会被所有实例共享。"""

    # 类体只执行一次，因此这个列表也只创建一次，所有没遮蔽它的实例都会读到同一个列表。
    # ClassVar 只是在类型层面明确“这是类属性”，不会为每个实例复制列表。
    items: ClassVar[list[str]] = []


class ShoppingCart:
    """正确示例：需要各实例独立的数据应在 __init__ 中创建。"""

    def __init__(self) -> None:
        # 每次 ShoppingCart() 都会执行 __init__，所以每个实例得到一个新列表。
        self.items: list[str] = []


class Wallet:
    """README 练习：只能通过公开行为修改余额的钱包。

    它与 Account 刻意采用不同设计：Wallet 的 balance 只有 getter，没有 setter，因此外部只能
    读取 ``wallet.balance``，不能写 ``wallet.balance = ...``。余额变化只能通过 deposit 和
    withdraw 这些有明确业务含义的方法完成。
    """

    def __init__(self, owner: str, balance: int = 0) -> None:
        """直接校验并建立钱包状态；这里没有可复用的 balance setter。"""
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("所有者必须是非空字符串")
        if type(balance) is not int:
            raise TypeError("余额必须是整数（单位：分）")
        if balance < 0:
            raise ValueError("余额不能为负")
        self.owner: str = owner.strip()

        # Wallet 的公开 balance 是只读 property，所以类自己的受信任方法直接维护 _balance。
        self._balance: int = balance

    # 只有 @property getter、没有 @balance.setter，便得到公开只读属性。
    @property
    def balance(self) -> int:
        """允许读取 ``wallet.balance``，但不提供 ``wallet.balance = value``。"""
        return self._balance

    @staticmethod
    def _validate_positive_amount(amount: object) -> int:
        """供 Wallet 及子类复用的内部金额校验工具。"""
        if type(amount) is not int or amount <= 0:
            raise ValueError("金额必须是正整数（单位：分）")
        return amount

    def deposit(self, amount: int) -> None:
        """校验后直接修改内部存储；外部调用者仍不能直接赋值 balance。"""
        self._balance += self._validate_positive_amount(amount)

    def withdraw(self, amount: int) -> None:
        """先完成全部校验，最后才修改余额，避免失败路径留下半次修改。"""
        valid_amount: int = self._validate_positive_amount(amount)
        if valid_amount > self._balance:
            raise ValueError("余额不足，不能透支")
        self._balance -= valid_amount

    def __str__(self) -> str:
        return f"{self.owner} 的钱包余额：{self._balance} 分"

    def __repr__(self) -> str:
        return f"Wallet(owner={self.owner!r}, balance={self._balance})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Wallet):
            return NotImplemented
        return (self.owner, self._balance) == (other.owner, other._balance)

    @classmethod
    def from_text(cls, text: str) -> Self:
        """与 Account.from_text 同理：解析文本后通过 ``cls(...)`` 创建实际调用类。"""
        owner: str
        separator: str
        balance_text: str
        owner, separator, balance_text = text.partition(":")
        if not separator:
            raise ValueError("钱包文本必须使用“所有者:余额”格式")
        return cls(owner, int(balance_text.strip()))


class LimitedWallet(Wallet):
    """LimitedWallet 是一种 Wallet，并增加每日提现上限。"""

    def __init__(self, owner: str, balance: int = 0, daily_limit: int = 1_000) -> None:
        # 先让 Wallet 建立 owner 和 _balance，再创建 LimitedWallet 自己新增的状态。
        super().__init__(owner, balance)
        self.daily_limit: int = self._validate_positive_amount(daily_limit)
        self._withdrawn_today = 0

    def withdraw(self, amount: int) -> None:
        """覆写父类取款：先增加每日限额规则，再复用父类的余额规则。"""
        valid_amount: int = self._validate_positive_amount(amount)
        if self._withdrawn_today + valid_amount > self.daily_limit:
            raise ValueError("超过每日提现上限")

        # 这里的 super().withdraw(...) 按 MRO 调用 Wallet.withdraw(...)。
        # 只有父类取款成功后，才累计今日已取金额；失败不会错误地占用每日额度。
        super().withdraw(valid_amount)
        self._withdrawn_today += valid_amount


class GiftCard:
    """GiftCard 拥有一个 Wallet，并维护自己的单笔消费上限。"""

    def __init__(self, wallet: Wallet, single_payment_limit: int) -> None:
        # 组合：保存传入的钱包引用。GiftCard 不是 Wallet 子类，而是把扣款委托给 Wallet。
        self.wallet: Wallet = wallet
        self.single_payment_limit: int = Wallet._validate_positive_amount(single_payment_limit)

    def pay(self, amount: int) -> None:
        """先检查礼品卡自己的单笔限额，再调用钱包的取款行为。"""
        valid_amount: int = Wallet._validate_positive_amount(amount)
        if valid_amount > self.single_payment_limit:
            raise ValueError("超过礼品卡单笔消费上限")
        self.wallet.withdraw(valid_amount)


class BadWalletHistory:
    """教学反例：类属性交易记录会被所有实例共享。"""

    # 这个列表在 class 语句执行时只创建一次，不会在 BadWalletHistory() 时重新创建。
    # ClassVar 明确标出这是故意共享的类属性，运行时仍然只有这一个列表。
    transactions: ClassVar[list[str]] = []

    def record(self, message: str) -> None:
        # self 上找不到 transactions 时，会回到类上取得所有实例共享的那个列表。
        self.transactions.append(message)


class WalletHistory:
    """正确示例：每次初始化都创建独立的交易记录列表。"""

    def __init__(self) -> None:
        # 在 self 上创建列表，因此每个 WalletHistory 实例都有自己的列表。
        self.transactions: list[str] = []

    def record(self, message: str) -> None:
        self.transactions.append(message)


def demonstrate_creation_and_methods() -> None:
    show_section("8.1～8.4 对象创建与实例方法")

    # Python 不写 new。调用 Account 类对象后，Python 创建实例并调用 Account.__init__。
    account = Account(" Alice ", 10_000)
    print("实例类型：", type(account).__name__)
    print("初始化后的状态：", repr(account))
    print("户主已被规范化：", account.owner)

    # account.deposit 是绑定方法：Python 已经把 account 记作它的 self。
    bound_deposit: Callable[..., None] = account.deposit
    print("绑定方法记住当前实例：", bound_deposit.__self__ is account)
    bound_deposit(500)  # 等价于 Account.deposit(account, 500)
    print("调用绑定方法后：", account)


def demonstrate_class_and_instance_attributes() -> None:
    show_section("8.3 类属性与实例属性")

    alice = Account("Alice")
    bob = Account("Bob")
    print("实例默认从类上读取：", alice.bank_name, bob.bank_name)

    # 给 alice 同名赋值会创建实例属性，只遮蔽 alice 对类属性的读取。
    alice.bank_name = "城东分行"
    print("实例属性遮蔽类属性：", alice.bank_name)
    print("其他实例仍读取类属性：", bob.bank_name)
    print("类本身的值没有改变：", Account.bank_name)

    first_bad_cart = BadShoppingCart()
    second_bad_cart = BadShoppingCart()
    first_bad_cart.items.append("Python 书")
    print("反例中的两个购物车共享列表：", first_bad_cart.items is second_bad_cart.items)
    print("第二个购物车被意外污染：", second_bad_cart.items)

    first_cart = ShoppingCart()
    second_cart = ShoppingCart()
    first_cart.items.append("Python 书")
    print("正确示例的列表彼此独立：", first_cart.items is not second_cart.items)
    print("第二个购物车保持为空：", second_cart.items)


def demonstrate_property() -> None:
    show_section("8.7～8.10 封装约定与 property")

    account = Account("Alice", 1_000)
    print("类上的 balance 类型：", type(Account.balance).__name__)
    print("实例真正保存的属性：", vars(account))
    print("实例中没有第二份 balance：", "balance" not in vars(account))
    print("读取 property：", account.balance)

    account.balance = 800  # 看似普通赋值，实际会调用 balance setter。
    print("setter 写入后的余额：", account.balance)

    try:
        account.balance = -1
    except ValueError as error:
        print("负数被 setter 拒绝：", error)

    try:
        Account("Bob", "100")  # type: ignore[arg-type]  # 教学反例：类型提示不做运行时转换
    except TypeError as error:
        print("字符串余额被运行时校验拒绝：", error)

    print("True 虽是 int 子类，但不是合法金额：", Account.is_valid_amount(True))


def demonstrate_protocols() -> None:
    show_section("8.5 Python 数据模型与魔术方法")

    first = Account("Alice", 1_500)
    same_value = Account("Alice", 1_500)
    print("str 面向用户：", str(first))
    print("repr 面向开发者：", repr(first))
    print("== 比较领域值：", first == same_value)
    print("is 比较对象身份：", first is same_value)
    print("与不支持的类型比较：", first == "Alice:1500")
    print("可变值对象默认不可哈希：", Account.__hash__ is None)


def demonstrate_constructors_and_inheritance() -> None:
    show_section("8.6 与 8.11 继承、类方法和静态方法")

    # 调用类方法时不手动传 cls；Python 会把点号左边的实际类自动绑定进去。
    account: Account = Account.from_text("Alice:10000")
    savings: SavingsAccount = SavingsAccount.from_text("Bob:20000")
    print("Account.from_text 创建：", repr(account))
    print("cls 保留实际调用的子类：", type(savings).__name__)

    # 绑定后的 classmethod 会通过 __self__ 暴露它记住的类，用来观察机制即可。
    print("Account.from_text 绑定的 cls：", Account.from_text.__self__.__name__)
    print("子类 from_text 绑定的 cls：", SavingsAccount.from_text.__self__.__name__)

    # 静态方法没有自动绑定的 self 或 cls，调用形式与 Java 静态方法最接近。
    print("静态方法校验金额：", Account.is_valid_amount(100))
    print("SavingsAccount 的 MRO：", [item.__name__ for item in SavingsAccount.mro()])

    assert isinstance(savings, SavingsAccount)
    savings.interest_rate = 0.05
    added: int = savings.add_interest()
    print("super 初始化的子类：", repr(savings))
    print("本次增加的利息：", added)

    # 子类对象可以出现在需要 Account 的位置；循环只依赖共同的公开接口。
    accounts: list[Account] = [account, savings]
    for current in accounts:
        print("多态处理：", current)


def demonstrate_composition() -> None:
    show_section("组合优先于勉强继承")

    account = Account("Cara", 8_000)
    card = BankCard(account, single_payment_limit=3_000)
    card.pay(2_500)
    print("银行卡委托账户扣款：", account.balance)

    try:
        card.pay(3_500)
    except ValueError as error:
        print("组合对象维护自己的限制：", error)
    print("失败后余额没有变化：", account.balance)


def demonstrate_readme_exercises() -> None:
    """运行 README 中 Wallet、继承、组合与共享状态练习。"""
    show_section("README 小练习")

    print("练习 1：Wallet 基础对象与不变量")
    empty_wallet = Wallet("Alice")
    print("  零余额：", str(empty_wallet), "/", repr(empty_wallet))
    print("  Wallet.balance 没有 setter：", Wallet.balance.fset is None)

    invalid_balances: tuple[tuple[str, object], ...] = (
        ("负数", -1),
        ("字符串", "100"),
        ("布尔值", True),
    )
    for label, balance in invalid_balances:
        try:
            Wallet("Bad", balance)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            print(f"  {label}余额被拒绝：", error)

    exact_wallet = Wallet("Bob", 500)
    exact_wallet.withdraw(500)
    print("  刚好取完：", exact_wallet.balance)
    try:
        exact_wallet.withdraw(1)
    except ValueError as error:
        print("  透支被拒绝：", error)
    try:
        exact_wallet.deposit(0)
    except ValueError as error:
        print("  非正数存款被拒绝：", error)

    print("练习 2：替代构造器与相等")
    first: Wallet = Wallet.from_text("Alice:100")
    second = Wallet("Alice", 100)
    print("  from_text 结果：", repr(first))
    print("  值相等/身份不同：", first == second, first is not second)
    print("  与不支持类型比较：", first == "Alice:100")
    try:
        Wallet.from_text("Alice 100")
    except ValueError as error:
        print("  缺少冒号：", error)

    print("练习 3：继承与组合")
    limited = LimitedWallet("Cara", 2_000, daily_limit=1_000)
    limited.withdraw(600)
    try:
        limited.withdraw(500)
    except ValueError as error:
        print("  每日限额：", error)
    print("  限额钱包余额：", limited.balance)

    gift_wallet = Wallet("Dan", 2_000)
    gift_card = GiftCard(gift_wallet, single_payment_limit=500)
    gift_card.pay(300)
    try:
        gift_card.pay(600)
    except ValueError as error:
        print("  单笔限额：", error)
    print("  礼品卡委托钱包后的余额：", gift_wallet.balance)
    print("  关系：LimitedWallet 是一种 Wallet；GiftCard 拥有一个 Wallet")

    print("练习 4：定位共享状态 bug")
    BadWalletHistory.transactions.clear()  # 重跑示例前先清理类级教学状态。
    bad_first = BadWalletHistory()
    bad_second = BadWalletHistory()
    bad_first.record("存款 100 分")
    print("  错误版本第二个实例也有记录：", bad_second.transactions)
    print("  错误版本共享同一列表：", bad_first.transactions is bad_second.transactions)

    good_first = WalletHistory()
    good_second = WalletHistory()
    good_first.record("存款 100 分")
    print("  修复后第二个实例为空：", good_second.transactions)
    print("  修复后列表相互独立：", good_first.transactions is not good_second.transactions)


def main() -> None:
    demonstrate_creation_and_methods()
    demonstrate_class_and_instance_attributes()
    demonstrate_property()
    demonstrate_protocols()
    demonstrate_constructors_and_inheritance()
    demonstrate_composition()
    demonstrate_readme_exercises()


if __name__ == "__main__":
    main()
