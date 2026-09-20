# 第 16 章：装饰器、闭包与工具注册

一句话理解本章：**装饰器是在执行 `def` 时，把刚创建的函数交给另一个可调用对象处理，再用处理结果替换原函数名。**

因此，装饰器既可以给每次调用增加计时、日志或鉴权，也可以只在定义函数时把它登记到注册表。闭包让包装函数在以后执行时仍能找到原函数和配置；`functools.wraps` 与 ParamSpec 则分别保住运行时元信息和静态类型关系。

学完本章后，你应该能够：

- 把 `@decorator` 手工展开为函数调用和名字重新绑定；
- 区分模块导入、装饰器应用和业务函数调用三个时刻；
- 解释闭包、`nonlocal`、跨调用共享状态和循环变量晚绑定；
- 写出能正确转发参数、返回值和异常的同步 wrapper；
- 判断 `wraps`、ParamSpec 各自解决了什么问题；
- 理解同步 `@tool(...)` 为什么能在导入时注册，却不会执行工具函数体。

本章只讨论同步函数装饰器。异步函数、生成器、重试、缓存和并发状态管理有不同的生命周期，不应塞进一个所谓的“万能装饰器”。

## 16.1 Java 工程师先建立什么心智模型

装饰器常被类比成 Java 注解或 Spring AOP。这个类比能帮助入门，但不能直接画等号。

| Java 中熟悉的概念 | Python 中可以先类比为 | 不能等同的地方 |
| --- | --- | --- |
| `@Transactional`、拦截器、动态代理 | 用 wrapper 增加横切行为 | Java 注解通常只是元数据，由容器或代理解释；Python 装饰器表达式会真实执行，并直接改变名字绑定 |
| 带属性的 Java 注解 | `@tool(name="search")` 装饰器工厂 | Python 中 `tool(...)` 是普通函数调用，不是被动标签 |
| Lambda / 匿名内部类捕获局部状态 | 闭包捕获外层名字 | Java 有 effectively final 限制；Python 可以读取外层绑定，重新绑定时用 `nonlocal` |
| Spring Bean 注册表 | 模块导入时把函数写入字典 | 这里没有类路径扫描、容器生命周期或依赖注入，只有 Python 顶层代码和进程内对象 |
| 泛型适配器保持函数接口 | ParamSpec + 返回类型变量 | 它只帮助 Pyright 等静态检查器，不会在运行时验证实参 |
| 代理对象暴露目标方法信息 | `functools.wraps` 和 `__wrapped__` | `wraps` 复制常用元信息并建立解包链，不会自动保证业务语义或静态类型正确 |

最关键的差异是：Java 注解通常要等框架读取后才产生效果，而 Python 的 `@...` 本身就是函数定义语句的一部分。解释器执行到它时，装饰逻辑已经发生。

## 16.2 `@decorator` 到底做了什么

### 函数名保存的是函数对象

Python 函数可以像普通对象一样赋给变量、作为参数传入，也可以作为返回值返回：

```python
def greet(name: str) -> str:
    return f"你好，{name}"


operation = greet          # 没有括号：保存函数对象
message = operation("Ada")  # 有括号：执行函数体
```

这与 Java 中把方法引用保存为 `Function<String, String>` 的用途相似。不同之处是，Python 函数对象还带有 `__name__`、`__doc__`、注解和签名等可内省信息。

### 装饰器就是“接收函数，返回替代对象”

先不用 `@`，写一个最小装饰器：

```python
def announce_call(function):
    def wrapper():
        print("准备调用")
        return function()

    return wrapper


def message():
    return "完成"


message = announce_call(message)
```

最后一行发生了三件事：

1. 右侧 `message` 取得原函数对象；
2. `announce_call(message)` 创建并返回 `wrapper`；
3. 左侧 `message` 改为指向这个 `wrapper`。

以后调用 `message()`，实际先进入 `wrapper`，再由闭包中的 `function()` 调用原函数。

`@` 只是把这个关系写在函数定义旁边：

```python
@announce_call
def message():
    return "完成"
```

理解装饰器时，始终先写出这条等式：

```python
message = announce_call(message)
```

准确地说，真实语义不会先把原函数临时绑定给公开名字再赋值，但这条“近似等价”能正确表达最终的对象关系。装饰器的返回值不一定非得是函数，不过它会成为公开名字的新值；如果装饰器意外返回 `None`，以后执行 `message()` 就会抛出 `TypeError`。

## 16.3 定义时和调用时是两条时间线

假设工具模块中有：

```python
@tool(name="search", description="搜索文档")
def search(query: str) -> list[str]:
    return send_http_request(query)
```

不要把“注册工具”和“调用工具”理解成同一件事：

| 时刻 | 会发生什么 | 不会发生什么 |
| --- | --- | --- |
| 首次导入模块 | Python 从上到下执行模块顶层语句 | 不会自动调用所有函数体 |
| 到达带装饰器的 `def` | 先从上到下求值装饰器表达式；`tool(...)` 返回真正的装饰器 | `search` 的函数体仍未执行 |
| 准备并创建 `search` 函数对象 | 计算默认参数等定义期表达式，然后构造函数对象 | `return` 和 HTTP 请求还没有执行 |
| 应用装饰器 | 把函数交给装饰器；多层时从下到上应用，最后绑定公开名字 | 仍未发送业务请求 |
| 以后调用 `search(...)` | wrapper 和原函数体开始执行 | 此时通常不再做首次注册 |

“定义时”不是保存源码的时刻，而是解释器真正执行这条 `def` 语句的时刻。模块级 `def` 通常在首次导入时执行；若 `def` 位于另一个函数内部，每次控制流走到那里，都可能重新创建函数并再次应用装饰器。

普通导入会使用 `sys.modules` 缓存。同一进程再次 `import` 已加载模块，通常不会重跑顶层代码；但新进程、显式重载或用另一个模块名加载同一文件仍可能再次注册，所以注册表仍要主动拒绝重名。

工程上的安全边界也很清楚：模块导入时可以做轻量、确定性的声明与注册，但不要顺便建立远程连接、发 HTTP 请求或调用模型。导入应该能发现工具，不应该执行业务。

## 16.4 闭包为什么能记住原函数和配置

内层函数引用了外层函数的局部名字，外层返回后这些绑定仍可被内层函数访问，这就是闭包：

```python
def make_prefixer(prefix: str):
    def add_prefix(text: str) -> str:
        return f"{prefix}: {text}"

    return add_prefix


warning = make_prefixer("WARN")
print(warning("磁盘空间不足"))  # WARN: 磁盘空间不足
```

`make_prefixer()` 已经结束，但 `warning` 仍能读取那次调用中的 `prefix`。装饰器的 wrapper 也是同样机制：它通常捕获原函数、装饰器配置以及需要跨调用共享的状态。

### 重新绑定外层名字时要用 `nonlocal`

下面的计数器让同一个 wrapper 的多次调用共享计数：

```python
def count_calls(function):
    count = 0

    def wrapper(*args, **kwargs):
        nonlocal count
        count += 1
        print(f"第 {count} 次调用")
        return function(*args, **kwargs)

    return wrapper
```

整数不可变，`count += 1` 会计算新整数并把名字 `count` 重新绑定。没有 `nonlocal` 时，Python 会把 wrapper 中被赋值的 `count` 判定为局部变量；右侧先读取尚未赋值的局部变量，于是抛出 `UnboundLocalError`。

如果捕获的是列表，`events.append(...)` 只是修改同一个对象，不需要 `nonlocal`；如果写成 `events = [...]`，那又是重新绑定，仍需要 `nonlocal`。

每次应用装饰器都会创建一个新 wrapper 和一份新闭包。因此，一个被装饰函数的多次调用共享状态，两个分别被装饰的函数默认不共享同一个计数。共享状态也不会自动拥有线程安全、请求隔离或清理机制，生产代码必须单独设计这些边界。

### 闭包捕获绑定，不会自动拍快照

循环中最常见的错误是晚绑定：

```python
readers = []
for name in ["search", "weather", "database"]:
    def read_name():
        return name

    readers.append(read_name)

print([reader() for reader in readers])
# ['database', 'database', 'database']
```

循环不会为每轮创建新的函数作用域。三个函数对象都在调用时读取同一个 `name` 绑定，而循环结束后它的值是 `"database"`。

更清楚的修复是用工厂函数为每个值创建独立作用域：

```python
def make_reader(name: str):
    def read_name() -> str:
        return name

    return read_name


names = ["search", "weather", "database"]
readers = [make_reader(name) for name in names]
```

也可以用 `def read_name(name=name)` 把当前值放进默认参数，但这会把本不应公开的实现细节变成可传参数。小工厂通常更容易表达意图。

## 16.5 无参装饰器、装饰器工厂和多层顺序

无参装饰器直接接收函数：

```python
@timed
def work(): ...

# 近似等价于
work = timed(work)
```

带参形式先调用一个工厂，再由工厂返回的装饰器接收函数：

```python
@tool(name="search", description="搜索文档")
def search(query: str): ...

# 近似等价于
search = tool(name="search", description="搜索文档")(search)
```

这里最多有三层调用，不要混在一起：

1. `tool(...)` 接收配置，在定义时返回 `decorate`；
2. `decorate(function)` 接收刚创建的函数，在定义时注册或包装；
3. `wrapper(*args, **kwargs)` 在以后每次业务调用时执行。

多个装饰器还要区分三种顺序：

```python
@outer("A")
@inner
def work():
    print("body")
```

先展开成：

```python
work = outer("A")(inner(work))
```

于是：

```text
定义阶段：求值 outer("A") → 求值 inner → 应用 inner → 应用 outer
调用阶段：进入 outer wrapper → 进入 inner wrapper → 函数体 → 退出 inner → 退出 outer
```

最靠近函数的装饰器先应用，最终返回的最外层 wrapper 却最先接收调用。装饰器顺序不仅影响日志，还会决定注册表保存哪个对象：

```python
@tool(name="sum")
@timed
def add(left: int, right: int) -> int:
    return left + right
```

它展开为 `add = tool(name="sum")(timed(add))`，所以注册表保存计时 wrapper，通过注册表调用也会计时。若交换顺序，`tool` 可能先保存原函数，注册表调用便会绕过后来添加的计时层。遇到多层装饰器，不要凭直觉猜，先展开括号。

## 16.6 怎样写一个尽量透明的同步 wrapper

所谓“透明”，是指增加横切行为后，原函数的参数规则、返回结果和异常语义仍然成立，并尽量保留原来的元信息与静态类型。

### 参数、返回值和异常必须原样通过

```python
def wrapper(*args, **kwargs):
    return function(*args, **kwargs)
```

定义参数时，`*args` 把额外位置参数收集成元组，`**kwargs` 把关键字参数收集成字典；调用函数时，`*args` 和 `**kwargs` 又把它们展开。

wrapper 表面上能接收任意参数，不代表原函数真的接受任意调用。转发后，仍由原函数检查仅位置参数 `/`、仅关键字参数 `*`、默认值、缺失参数和重复传值。

`return` 不能漏。下面的原函数虽然执行了，调用方却只能收到 `None`：

```python
def wrapper(*args, **kwargs):
    function(*args, **kwargs)  # 错误：丢掉原返回值
```

也不要为了打印和返回结果而调用两次 `function(...)`。对于写数据库、发请求或执行工具的函数，这会让副作用重复发生。正确做法是调用一次，把结果存起来再使用。

计时逻辑希望成功和失败都被记录，可以使用 `try/finally`：

```python
started = clock()
try:
    return function(*args, **kwargs)
finally:
    report(clock() - started)
```

`finally` 执行后，正常返回值或原异常会继续向上传递。若随手写成 `except Exception: return None`，就把“调用失败”和“合法返回空值”混成同一种结果，已经改变了原函数契约。

### `wraps` 保留运行时元信息

包装后公开名字实际指向 wrapper。若不处理，日志、帮助系统和注册器看到的往往是 `wrapper`、空 docstring 和宽泛的 `*args, **kwargs` 签名。

```python
from functools import wraps


def decorate(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return function(*args, **kwargs)

    return wrapper
```

`@wraps(function)` 会复制常用元信息，并设置 `wrapper.__wrapped__ = function`：

| 检查方式 | 没有 `wraps` | 使用 `wraps` |
| --- | --- | --- |
| `decorated.__name__` | 通常是 `wrapper` | 原函数名 |
| `decorated.__doc__` | wrapper 的说明或 `None` | 原 docstring |
| `decorated.__wrapped__` | 不存在 | 指向下一层被包装对象 |
| `inspect.signature(decorated)` | 常见为 `*args, **kwargs` | 默认沿 `__wrapped__` 展示原签名 |

`inspect.signature(decorated, follow_wrapped=False)` 可以查看 wrapper 自身的入口。直接调用 `decorated.__wrapped__` 则会绕过计时、鉴权等包装行为，只应在明确需要调试、内省或重新包装时使用。

`wraps` 只处理运行时元信息，不会证明 wrapper 的类型注解正确。完整规则可查阅 `functools.wraps` 和 `inspect.signature`。

### ParamSpec 保留静态调用关系

对于“接收任意函数并保持原签名”的装饰器，真正想表达的不是“什么都可以”，而是：包装前后必须接受同一组参数并返回同一种结果。

本项目使用 Python 3.14 类型参数语法：

```python
from collections.abc import Callable
from functools import wraps


def timed[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        return function(*args, **kwargs)

    return wrapper
```

- `P` 是 ParamSpec，表示整组参数规格，不是某个参数值；
- `P.args` 和 `P.kwargs` 分别描述这组参数的位置与关键字部分；
- `R` 表示返回类型；
- 输入和输出都是 `Callable[P, R]`，因此 Pyright 能保留参数名、位置规则和返回值关系。

需要兼容 Python 3.11 及更早版本的项目，会看到模块级 `P = ParamSpec("P")` 和 `R = TypeVar("R")` 写法。两种形式表达的是同一个类型关系。

ParamSpec 服务于静态检查，不会在运行时验证参数；`wraps` 服务于运行时内省，不会建立静态类型关系。一个工程化 wrapper 通常两者都需要。

### 一个完整的同步计时器

```python
from collections.abc import Callable
from functools import wraps
from time import perf_counter_ns


def timed[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        started_ns = perf_counter_ns()
        try:
            return function(*args, **kwargs)
        finally:
            elapsed_ms = (perf_counter_ns() - started_ns) / 1_000_000
            print(f"{function.__name__}: {elapsed_ms:.3f} ms")

    return wrapper
```

`perf_counter_ns()` 适合测量进程内时间间隔。具体毫秒值受机器负载影响，不应写进固定断言。这个实现只包围一次同步函数调用；若传入协程函数，它测到的主要是“创建协程对象”的时间，而不是真正 `await` 完成的时间，因此不能当异步计时器使用。

## 16.7 装饰器也可以只注册、不包装

装饰器只要求接收函数并返回用于重新绑定的对象，并不要求一定创建 wrapper。最小注册器可以这样写：

```python
registry = {}


def tool(*, name: str):
    def decorate(function):
        if name in registry:
            raise ValueError(f"工具名称已注册：{name}")
        registry[name] = function
        return function

    return decorate
```

这个 `@tool(...)` 在定义时完成注册，然后直接返回原函数，所以：

- 函数对象身份不变；
- 调用时没有额外 wrapper；
- 装饰器不能在每次调用前后插入行为；
- 注册表保存的函数与公开名字是同一个对象。

如果需要统一鉴权、验证或日志，就应创建 wrapper，并让公开名字与注册表都保存同一个 wrapper。代价是必须正确处理参数、结果、异常、元信息和类型关系。选择标准只有一个：是否必须拦截每次调用。不需要拦截时，不要为了形式统一增加空 wrapper。

### 本章注册表保存什么

`tool_registry.py` 中的 `ToolMetadata` 保存：

| 字段 | 来源 | 用途 |
| --- | --- | --- |
| `name` | 装饰器参数，缺省时用函数名 | 注册表查找和模型选择工具 |
| `python_name` | `function.__name__` | 日志和排错 |
| `description` | 显式参数，缺省时用 docstring | 描述工具用途 |
| `signature` | `inspect.signature()` | 参数名、参数种类、默认值和返回注解 |
| `annotations` | `annotationlib.get_annotations()` | 生成参数描述或 schema 的原料 |
| `function` | 被注册的同步函数 | 真正执行调用 |

注册器先校验名称和说明，再检查重名，最后才写入字典。这样失败不会覆盖旧函数，也不会留下半注册状态。真实系统若允许替换，应设计显式的 `replace=True` 和来源记录，而不是依赖普通字典覆盖。

这个注册表仍不是完整的 Agent 工具运行时：类型注解不会自动验证模型传入的 JSON，任意 Python 类型也不一定能转换成 JSON Schema。外部输入仍需要明确的 schema、反序列化和运行时校验。

### 动态注册会遇到静态类型边界

直接调用具体函数时，Pyright 知道它的精确签名：

```python
results: list[str] = search_documents("装饰器", limit=2)
```

但注册表把多种签名放进同一个字典，再通过运行时字符串选择：

```python
result = invoke_tool("summarize_results", items, prefix="命中")
```

普通静态检查器无法根据任意 `str` 推导对应函数的参数和返回类型，因此本章的统一动态入口诚实地返回 `object`。不要用 `Any` 把检查关闭后，再声称类型关系仍然存在。动态边界应由运行时 schema 验证兜底。

## 16.8 Python 3.14 如何读取延迟注解

本项目要求 Python >= 3.14。从 Python 3.14 开始，注解默认延迟求值：执行 `def` 时不必马上把所有注解解析成真实对象，访问注解时才求值。

这使下面的前向引用合法：

```python
@tool(name="render_card", description="渲染结果")
def render_card(card: ResultCard) -> str: ...


class ResultCard: ...
```

应用 `@tool` 时，`ResultCard` 还没有定义。本章使用公开 API 明确选择所需格式：

```python
from annotationlib import Format, get_annotations
from inspect import signature


annotations = get_annotations(function, format=Format.FORWARDREF)
display_signature = signature(function, annotation_format=Format.STRING)
```

| 格式 | 得到什么 | 适用场景 |
| --- | --- | --- |
| `Format.VALUE` | 尽量求值得到真实对象；名字未定义时可能失败 | 模块加载完成后确实需要类型对象 |
| `Format.FORWARDREF` | 已知项为真值，未知项为 `ForwardRef` | 定义期注册并允许前向引用 |
| `Format.STRING` | 接近源码的文本表示 | 文档、日志和面向人的签名 |

在 `registered_tools.py` 中，`render_card` 注册时得到 `ForwardRef('ResultCard')`；模块完成导入后，再用 `Format.VALUE` 读取便能得到真正的 `ResultCard` 类。这说明“延迟求值”既不等于“永远是字符串”，也不等于“注解永远不会执行”。

不要让框架代码依赖 `__annotations__` 在某个 Python 版本中的具体内部形式。Python 3.14 应优先使用 `annotationlib.get_annotations()`；需要递归解析复杂类型时，再评估 `typing.get_type_hints()`。

注解内省还有安全边界：求值注解表达式可能执行代码，`Format.STRING` 也不是加载不可信 Python 源码的沙箱。本章只导入并内省仓库内受信任的模块。

## 16.9 Java 工程师最容易踩的坑

| 错误判断 | 直接后果 | 正确做法 |
| --- | --- | --- |
| 把 `@tool` 当成不执行的 Java 元数据 | 看不懂为何导入时已注册 | 记住装饰器表达式和装饰器都会在执行 `def` 时调用 |
| wrapper 少写 `return` | 原函数可能已产生副作用，但调用方只收到 `None` | `return function(*args, **kwargs)` |
| 忘记 `wraps` | 名字、docstring、签名和注册元信息属于 wrapper | 在 wrapper 上使用 `@wraps(function)` |
| 用 `Callable[..., Any]` 宣称类型已保留 | 错参数和错返回值更容易漏检 | 用 ParamSpec 与返回类型变量建立对应关系 |
| 宽泛捕获异常后返回 `None` | 失败与合法空值无法区分 | 默认传播异常；若要转换，明确设计新契约 |
| wrapper 调用原函数两次 | I/O、副作用和费用翻倍 | 只调用一次，保存结果后再记录或返回 |
| 认为闭包会复制当前值 | 循环生成的函数全部读到最后一个配置 | 用工厂函数创建独立作用域 |
| 认为闭包状态按请求重建 | 计数和列表跨调用、跨请求共享 | 明确状态生命周期、隔离和并发策略 |
| 认为类型注解会校验模型 JSON | 错类型仍可能到达函数体 | 在动态入口做运行时 schema 校验 |
| 重名时直接覆盖 | 导入顺序改变实际实现 | 写入前检查并明确失败 |

还有一个设计原则：普通函数或类已经能清楚表达的业务流程，不必为了“框架感”改成装饰器。装饰器适合稳定、横切、声明式的行为；复杂分支和大量隐式共享状态放在普通对象中通常更易维护。

## 16.10 运行本章示例

本课目录有四个文件：

| 文件 | 职责 |
| --- | --- |
| `README.md` | 独立教程和运行说明 |
| `example.py` | 七组可观察实验与断言 |
| `tool_registry.py` | 类型保真的同步 `@tool(...)` 和最小注册表 |
| `registered_tools.py` | 首次导入时执行注册的本地工具模块 |

本章只使用标准库，不需要新增运行依赖。需要 Python 3.14 或更高版本。

在本课目录执行：

```powershell
py -3.14 -B example.py
```

`-B` 只阻止生成 `__pycache__`，不改变程序语义。

运行时重点观察：

1. 手工赋值和 `@` 写法都先进入包装层，再得到原结果；
2. 计时 wrapper 保留返回值、异常、原函数名、docstring 和对外签名；
3. 同一 wrapper 的计数累积，不同 wrapper 的计数彼此独立；
4. 多层装饰器在定义期从内向外应用，调用期从外向内进入；
5. 忘记 `wraps` 与忘记 `return` 分别破坏元信息和返回值；
6. 导入工具模块后注册表已有三项，但业务调用事件仍为空；
7. 重名注册被拒绝，`@tool` 与 `@timed` 的顺序决定注册哪个对象。

最后应看到：

```text
全部第 16 章实验与断言通过。
```

计时毫秒值每次都可能不同，不要把某次数字当成固定结果。

如需检查语法，可在本课目录执行：

```powershell
py -3.14 -m py_compile example.py tool_registry.py registered_tools.py
```

## 16.11 递进练习

### 练习 1：手工展开装饰器

实现 `uppercase_result`，把无参函数返回的字符串转成大写。分别用普通赋值和 `@` 语法装饰两个函数，并写出各自的 `f = decorator(f)` 等价式。

验收：两个函数分别返回 `"AGENT"` 和 `"TOOL"`，并能说明装饰器应用和函数体调用各发生在何时。

### 练习 2：实现透明的 `@logged`

装饰 `divide(left: float, right: float, *, precision: int = 2) -> float`，调用前打印函数名。要求使用 ParamSpec、返回类型变量和 `wraps`，且不吞掉 `ZeroDivisionError`。

验收：`inspect.signature(divide)` 仍展示原签名，正常结果和异常语义都不变。

### 练习 3：修复晚绑定并管理独立预算

为 `search`、`weather`、`database` 创建带独立标签和调用预算的 wrapper。每项预算从 2 开始，耗尽时抛出 `RuntimeError`。

验收：所有 wrapper 使用自己的标签和预算，并能解释哪里需要 `nonlocal`。

### 练习 4：比较注册与包装顺序

分别定义 `@tool @logged` 和 `@logged @tool` 两个函数，从公开名字和注册表各调用一次，用事件列表记录经过的层。

验收：先写出两组嵌套等价式，再解释注册表保存的是原函数还是 wrapper。

## 16.12 本章小结

- `@decorator` 的核心是 `name = decorator(original_function)`；
- 装饰器在执行 `def` 时应用，wrapper 在以后调用函数时执行；
- 闭包让 wrapper 记住原函数、配置和状态，重新绑定外层名字需要 `nonlocal`；
- 循环变量是晚绑定，想保存每轮值应创建独立作用域；
- 多层装饰器从内向外应用，最终调用从外向内进入；
- 透明 wrapper 必须正确转发参数、返回值和异常，并且只调用原函数一次；
- `wraps` 维护运行时元信息，ParamSpec 维护静态参数关系，两者不能互相替代；
- 注册型装饰器可以直接返回原函数，只有需要拦截每次调用时才创建 wrapper；
- 导入期可以完成轻量注册，但不应顺便执行业务 I/O；
- Python 3.14 应使用 `annotationlib` / `inspect` 的公开 API 读取延迟注解。

如果你能看到 `@tool(...)` 后立即说清“什么时候调用工厂、什么时候注册、什么时候才执行函数体，以及注册表里最终保存哪个对象”，就已经掌握了本章最重要的执行模型。
