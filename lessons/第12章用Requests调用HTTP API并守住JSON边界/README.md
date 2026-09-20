# 第 12 章：用 Requests 调用 HTTP API，并守住 JSON 边界

一句话先说透：**调用 HTTP API，不是“发请求后把 JSON 转成字典”这么简单，而是要依次确认传输、HTTP 状态、JSON 语法、数据结构和业务结果都成功。**

如果你熟悉 Spring MVC、`RestClient`、`WebClient`、Jackson 和 DTO 校验，可以先把 Python 的 `requests` 理解成一个轻量的同步 HTTP 客户端。它能帮你发送请求、管理连接和解析 JSON，但不会像完整的 Spring 客户端封装那样，自动替你完成 DTO 映射、Bean Validation、统一异常转换和重试治理。

学完本章，你应该能够：

- 用 `params=`、`json=` 和 `data=` 把数据放到正确的位置；
- 分清 Python `dict`、JSON 文本 `str` 和网络字节 `bytes`；
- 正确判断 HTTP 成功、JSON 可解析、字段合法和业务成功；
- 给请求设置合理的连接超时与读取超时；
- 使用 `Session` 复用连接，并明确资源由谁关闭；
- 把网络、状态码、协议数据和业务错误分开暴露。

本章只讨论同步 Requests。异步 HTTP、Pydantic、自动重试和总 deadline 会在需要这些能力的章节再展开。

## 一、先建立 Java → Python 心智模型

| Java 中熟悉的概念 | Python / Requests 中的对应物 | 必须注意的不等价之处 |
| --- | --- | --- |
| `java.net.http.HttpClient`、Spring `RestClient` | `requests.Session` | 都能发送同步请求并复用连接；Requests 的默认抽象更轻，不自带 Spring 式拦截器和统一 DTO 体系 |
| `UriComponentsBuilder.queryParam(...)` | `params={...}` | 都负责查询参数编码，不要手工拼接中文、空格和 `&` |
| Jackson `ObjectMapper.writeValueAsString(...)` | `json.dumps(...)` | 都把内存对象序列化成 JSON 文本；`requests` 的 `json=` 还能代做这一步 |
| Jackson `readValue(...)` | `response.json()` 或 `json.loads(...)` | Python 默认得到 `dict`、`list` 等通用对象，不会自动变成强类型 DTO |
| `ResponseEntity<T>` | `requests.Response` | Response 同时提供状态、头、字节、文本和 JSON 入口，但 `T` 不会自动校验 |
| DTO / Java `record` | `@dataclass` | `dataclass` 主要减少样板代码，类型注解默认不会在运行时自动校验外部数据 |
| `AutoCloseable` + try-with-resources | `with requests.Session() as session` | 都表达资源生命周期；退出 `with` 时 Session 会关闭连接池资源 |
| `throw new XxxException(..., cause)` | `raise XxxError(...) from error` | 都能在增加业务语义的同时保留底层异常原因 |

最重要的差异是：Java 项目常把 HTTP 客户端、Jackson、DTO 和校验框架组合成一层完整的客户端 SDK；原生 Requests 只提供较底层的构件。边界是否可信，仍然要由你的应用代码判断。

## 二、一次 API 调用到底经过哪些边界

把一次调用拆成下面五层，排错时就不会只剩一句“接口挂了”：

> 构造请求 → 建立连接并传输 → 检查 HTTP 状态 → 解析 JSON → 校验结构与业务结果

| 层次 | 要回答的问题 | 常见失败 |
| --- | --- | --- |
| 请求构造 | 方法、URL、参数、请求头和 body 是否符合接口合同？ | URL 拼错、查询词为空、凭据缺失、body 编码错误 |
| HTTP 传输 | 是否成功连接，并收到服务端响应？ | 连接被拒绝、DNS/TLS 问题、连接或读取超时 |
| HTTP 状态 | 服务端是否在 HTTP 层接受了请求？ | 401、404、429、500，或者合同不接受的 204/3xx |
| JSON 语法 | 响应正文是否为合法 JSON？ | 网关返回 HTML、正文截断、JSON 格式错误 |
| 数据与业务 | 字段、类型、空值和业务码是否符合合同？ | 缺字段、`null`、类型漂移、`code != "OK"` |

这五层不能互相替代。例如：

- 404 的正文完全可能是合法 JSON；
- HTTP 200 的正文可能是一段 HTML；
- HTTP 200 加合法 JSON，也可能包含 `{"code": "QUOTA_EXCEEDED"}`；
- 字段存在但值为 `null`，与服务端根本没发送这个字段，不是同一种情况。

这和 Java 服务中的分层非常像：TCP 成功不等于 Controller 成功，Controller 返回 200 不等于 Jackson 一定能反序列化，反序列化成功也不等于业务状态有效。

## 三、请求由方法、地址、参数、头和 body 组成

### 1. GET 查询：用 `params=`，不要手拼 URL

```python
import requests

response = requests.get(
    "https://api.example.com/documents",
    params={"query": "Agent 入门", "page": 1},
    headers={"Accept": "application/json"},
    timeout=(3.05, 20.0),
)
```

`params` 接收 Python 字典。Requests 会把它编码到 URL 的 query string 中，因此中文、空格、`&` 和 `=` 都会被正确处理。

可以把它类比为 Spring 的 URI Builder：你提供参数值，客户端负责 percent-encoding。不要写下面这种代码：

```python
# 反例：query 中只要出现空格、中文或 &，就可能生成错误 URL。
url = f"https://api.example.com/documents?query={query}"
```

查询参数经常进入访问日志、代理日志和监控系统，所以 token 不应放在 query string 中。认证信息通常放到 `Authorization` 请求头：

```python
headers = {"Authorization": f"Bearer {token}"}
```

### 2. POST body：先看服务端要求哪种编码

| 写法 | 数据位置 | Requests 会做什么 | 典型场景 |
| --- | --- | --- | --- |
| `params={"q": "Python"}` | URL 查询串 | URL 编码 | 筛选、分页、搜索 |
| `json={"q": "Python"}` | body | JSON 序列化，并设置 JSON `Content-Type` | REST/JSON API |
| `data={"q": "Python"}` | body | 表单编码 | `application/x-www-form-urlencoded` 接口 |
| `data=json_text` | body | 原样发送 `str` 或 `bytes` | 自己已经完成精确编码、签名等特殊协议 |

普通 JSON API 的正确写法是：

```python
payload = {"query": "Agent 入门", "limit": 2}
response = requests.post(
    "https://api.example.com/search",
    json=payload,
    timeout=(3.05, 20.0),
)
```

这里传给 `json=` 的应该是 Python 对象。Requests 会完成 JSON 序列化和请求头设置。

下面是 Java 工程师很容易写出的重复序列化反例：

```python
import json

json_text = json.dumps({"query": "Agent"})
requests.post(url, json=json_text)  # 错：json_text 会被当作普通字符串再编码一次
```

服务端解析一次后得到的是字符串，而不是对象。它相当于先用 Jackson 得到 JSON 字符串，又把这个字符串交给 Jackson 当普通值再序列化一次。

选择方法可以压缩成三句话：

1. 手里是 Python 对象，服务端要 JSON：使用 `json=python_value`。
2. 服务端明确要表单：使用 `data=dict_value`。
3. 协议要求自己控制原始正文：使用 `data=str_or_bytes`，同时自行设置正确的 `Content-Type`。

## 四、`dict`、JSON `str` 和 `bytes` 是三个不同阶段

```python
import json

python_value = {"query": "Agent 入门"}                  # dict
json_text = json.dumps(python_value, ensure_ascii=False)  # str
body_bytes = json_text.encode("utf-8")                   # bytes
```

数据流是：

```text
Python dict/list
    │ json.dumps
    ▼
JSON 文本 str
    │ encode("utf-8")
    ▼
可传输的 bytes
```

反方向则是：

```text
响应 bytes
    │ 按字符编码解码
    ▼
JSON 文本 str
    │ json.loads / response.json()
    ▼
Python dict/list/str/int/bool/None
```

可以类比 Java 中的 `Map<String, Object>`、JSON `String` 和 UTF-8 `byte[]`。外观看起来像 JSON，不代表类型已经是可按字段访问的对象。

几个常见判断：

- `dict` 可以用 `value["query"]` 取字段，但它还不是 JSON 文本；
- `str` 是字符序列，必须先 `json.loads(...)` 才能得到 JSON 对应的 Python 值；
- `bytes` 是字节序列，必须知道字符编码才能安全变成文本；
- JSON 顶层可以是数组、字符串、数字、布尔值或 `null`，所以 `response.json()` 不保证返回 `dict`。

如果输出里突然出现很多 `\"`，先检查 `type(value)`。这通常表示字符串被重复 JSON 序列化了。

## 五、正确读取响应：先看状态，再解析，再校验

一个 `Response` 有几种常用视图，它们回答的问题不同：

| 写法 | 得到什么 | 适合做什么 |
| --- | --- | --- |
| `response.status_code` | `int` 状态码 | 按接口合同判断 200、201、204 等 |
| `response.raise_for_status()` | 4xx/5xx 时抛 `HTTPError` | 快速拒绝 HTTP 错误 |
| `response.content` | `bytes` | 文件、图片或需要自行解码的内容 |
| `response.text` | `str` | 普通文本或受控的错误正文预览 |
| `response.json()` | JSON 对应的 Python 值 | 解析 JSON；之后仍需校验结构 |

一个可靠的 JSON API 处理顺序通常是：

```python
response.raise_for_status()
if response.status_code != 200:  # 本接口合同只接受 200
    raise RuntimeError(f"预期 200，实际 {response.status_code}")

try:
    payload = response.json()
except requests.JSONDecodeError as error:
    raise RuntimeError("响应不是合法 JSON") from error

if not isinstance(payload, dict):
    raise TypeError("响应 JSON 顶层必须是 object")
```

`raise_for_status()` 主要拒绝 4xx 和 5xx。它不负责以下事情：

- 不保证状态恰好是接口合同要求的 200；
- 不保证正文是 JSON；
- 不保证 JSON 顶层是对象；
- 不保证字段存在、类型正确；
- 不保证业务码成功。

### 缺字段和 `null` 要分开

JSON 的 `null` 会被解析成 Python 的 `None`：

```python
if "title" not in document:
    raise ValueError("缺少 title")
if document["title"] is None:
    raise ValueError("title 不能为 null")
```

不要只写 `document.get("title") is None`，因为它会把“缺少字段”和“明确传了 null”合并。是否允许省略或允许 `null`，应由接口合同决定。

### 类型注解不是运行时校验器

即使函数标注返回 `Document`，网络返回的数据也不会自动变成 `Document`。通常需要先验证原始 `dict`，再构造稳定对象：

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Document:
    id: int
    title: str
    content: str
```

`@dataclass` 可以类比 Java `record` 或由 Lombok 生成的 DTO，但不能完全等同：它会生成构造器、比较和展示等样板方法，默认不会检查运行时是否真的是 `int`。本章示例因此在构造 `Document` 前显式检查外部数据。

另一个 Python 特有边界是 `bool` 属于 `int` 的子类：`isinstance(True, int)` 为真。如果协议要求“真正的整数 ID”，可使用 `type(value) is int` 排除布尔值。

## 六、`timeout` 限制的是网络等待，不是整个业务耗时

Requests 默认不会自动给请求设置 `timeout`。生产环境中的请求若不传 `timeout=`，可能让工作线程长时间等待。

```python
response = session.get(url, timeout=(3.05, 20.0))
#                                    │     └─ read timeout
#                                    └─────── connect timeout
```

- **connect timeout**：等待建立连接的上限；
- **read timeout**：连接建立后，等待响应字节的 socket 空闲上限。

`timeout=(3.05, 20.0)` 不表示整个函数一定在 23.05 秒内结束。域名可能解析出多个地址并依次尝试；只要响应持续在 read timeout 内送来新字节，大文件下载也可能远超 20 秒；JSON 解析和后续业务处理更不在这个计时范围内。

可以把它类比为 Java HTTP 客户端的连接/读取超时，但不能把它当成 `CompletableFuture.orTimeout(...)` 或上层请求 deadline。若业务需要一个覆盖多次调用、解析和后续处理的总截止时间，必须在更高层单独设计。

## 七、多次请求使用 `Session`，并明确所有权

一次独立调用可以使用模块函数：

```python
response = requests.get(url, timeout=(3.05, 20.0))
```

同一服务有多次请求时，更适合使用 Session：

```python
with requests.Session() as session:
    session.headers.update({"X-Client": "document-service"})
    first = session.get(first_url, timeout=(3.05, 20.0))
    second = session.get(second_url, timeout=(3.05, 20.0))
```

Session 的作用包括：

- 通过底层连接池复用连接，减少重复建连；
- 在多次请求之间保留 cookie；
- 保存公共 headers、认证等默认配置。

它可以类比为复用的 Java `HttpClient`，而 `with` 可以类比 try-with-resources。连接池允许连接复用，但服务端、代理或响应头仍可能关闭连接，所以不能假定所有请求永远使用同一个 socket。

如果函数接受调用者传入的 Session，推荐遵守“谁创建，谁关闭”：

- 调用者传入 Session：函数只借用，不关闭；
- 函数内部创建 Session：函数必须在正常返回和异常路径都关闭它。

## 八、错误要按层分类，并保留 cause

不要这样处理所有失败：

```python
# 反例：404、连接失败、非法 JSON 和真实空结果都被伪装成同一个值。
try:
    return response.json()
except Exception:
    return {}
```

更可靠的客户端会区分：

| 失败层 | Requests / 应用中的例子 | 调用方通常怎么处理 |
| --- | --- | --- |
| 配置与输入 | 缺服务地址、空查询词 | 修正配置或立即拒绝调用 |
| 传输 | `ConnectionError`、`Timeout` | 检查网络、服务状态和超时预算 |
| HTTP 状态 | `HTTPError`，或应用的 `HttpStatusFailure` | 按 401/404/429/5xx 制定策略 |
| JSON 语法 | `JSONDecodeError` | 记录受控正文预览，检查网关或服务输出 |
| 数据结构 | 缺字段、`null`、类型错误 | 视为协议不兼容，不能假装没有结果 |
| 业务结果 | `code != "OK"` | 按稳定业务码处理配额、状态或用户提示 |

转换底层异常时使用异常链：

```python
try:
    response = session.get(url, timeout=(3.05, 20.0))
except requests.Timeout as error:
    raise RequestTimeout("文档服务请求超时") from error
except requests.ConnectionError as error:
    raise ConnectionFailure("无法连接文档服务") from error
```

`raise ... from error` 类似 Java 构造异常时传入 cause：上层看到的是稳定的应用异常，排障时仍能沿 `__cause__` 找到底层 Requests/urllib3 原因。

捕获顺序也有意义。Requests 的 `ConnectTimeout` 同时属于 `Timeout` 和 `ConnectionError`，因此本章先捕获 `requests.Timeout`，再捕获 `requests.ConnectionError`，避免把连接超时归入普通连接失败。

至于重试，不能看到网络异常就无脑重发。GET 通常设计为幂等，但 POST 可能创建订单、扣费或发消息。只有确认方法语义、幂等键、最大次数、退避和总时间预算后，才应加入自动重试。

## 九、一个可维护的 API 函数应该承诺什么

本章项目提供 `fetch_document(query)`，它体现的是一份客户端合同，而不是需要先背下来的代码流程：

- 输入 `query` 必须是非空字符串，请求发出前先校验；
- `base_url` 和 `token` 可以显式传入，未传时才从环境变量读取；
- 查询参数交给 `params=` 编码，认证信息放请求头；
- 调用方可以传入共享 Session，也可以让函数管理私有 Session；
- 只有 HTTP、JSON、结构和业务规则全部通过，才返回 `Document`；
- 每一层失败都有不同异常，不用空字典伪装成功；
- 转换异常时保留底层 cause；
- 诊断日志中的 token 必须脱敏。

显式参数优先，最利于单元测试：

```python
document = fetch_document(
    "Agent",
    base_url="https://api.example.com",
    api_token=token_from_secret_store,
)
```

应用入口也可以通过环境变量注入：

```bash
export DOCUMENT_API_BASE_URL='https://api.example.com'
export DOCUMENT_API_TOKEN='由部署平台注入的真实值'
```

不要把真实 token 写进源码、README、完整 URL 或异常消息。日志中除了凭据，查询词、响应正文、prompt 和模型输出也可能含敏感数据，应按数据等级决定是否记录。

## 十、运行本章的本地实验

### 1. 环境与依赖

仓库根目录的 `pyproject.toml` 要求 Python `>= 3.14`。本章直接依赖记录在 `requirements.txt`：

```text
requests==2.34.2
```

在仓库根目录执行：

```bash
.venv/bin/python -V
.venv/bin/python -m pip install -r java_to_python/chapters/12_http_json/requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -B java_to_python/chapters/12_http_json/example.py
```

`pip install` 会修改当前 `.venv` 的依赖；请确认它就是你准备作用于本仓库的虚拟环境。`-B` 只表示运行时不生成 `.pyc` 文件。

当前 `java_to_python/run_all.py` 只登记了前 10 章，所以本章要用上面的独立命令运行，不能使用 `--chapter 12`。

### 2. 本章文件分别负责什么

| 文件 | 作用 |
| --- | --- |
| `README.md` | 建立 HTTP、JSON、Session、timeout 和异常边界的心智模型 |
| `example.py` | 可运行的 Requests 客户端与 `fetch_document()` |
| `local_http_fixture.py` | 自动启动和关闭本机 HTTP 服务，稳定制造成功与失败响应 |
| `requirements.txt` | 本章直接运行依赖 |

默认入口只访问 `127.0.0.1` 上临时启动的服务，不访问公网，也不需要真实 API Key。fixture 使用随机空闲端口，退出上下文后会关闭监听 socket 和服务线程。

### 3. 运行时重点观察什么

不需要先逐行阅读代码。先运行并寻找这些现象：

1. 同一个值在 `dict`、JSON `str` 和 UTF-8 `bytes` 三种形态下类型不同；
2. `params=` 进入 URL，`json=dict` 生成 JSON body，`data=dict` 生成表单；
3. 404 正文虽然能解析成 JSON，`raise_for_status()` 仍会拒绝它；
4. 两次查询通过同一个 Session 复用本地连接；
5. 连接失败、read timeout、HTTP 错误、非法 JSON、缺字段、`null`、错类型和业务失败落入不同异常；
6. 最后 Session、监听端口和服务线程都被关闭。

慢响应场景是故意触发的受控 `ReadTimeout`。客户端超时后，fixture 会释放等待事件并完成清理，所以这条异常输出属于实验结果。

## 十一、Java 工程师最容易踩的坑

1. 把 `response.json()` 当成 DTO 反序列化加校验。它只负责 JSON 语法解析，返回值甚至不一定是字典。
2. 先 `json.dumps`，又把结果传给 `json=`。这会发生重复序列化，服务端得到字符串而不是对象。
3. 用 `data=dict` 发送 JSON。Requests 会按表单编码；JSON API 应使用 `json=dict`。
4. 只依赖 `raise_for_status()` 判断成功。它不检查精确成功状态、JSON、字段或业务码。
5. 认为类型注解会在运行时拦截脏数据。Python 注解主要服务于阅读和静态检查，外部输入仍需显式校验或校验库。
6. 省略 `timeout`。Requests 没有默认请求超时，线程可能长时间等待。
7. 把 `(connect, read)` 当成总超时。它不是覆盖整个业务流程的 deadline。
8. 每次请求都新建客户端。同一服务的连续调用应考虑复用 Session 和连接池。
9. 捕获 `Exception` 后返回 `{}`。这会把故障伪装成正常空结果，并丢失排障证据。
10. 把 token 和完整响应写进日志。HTTP 客户端日志往往会进入长期保存的集中系统。

## 十二、练习

### 练习 1：验证四种发送方式

把同一个 `{"query": "Agent", "limit": 2}` 分别传给：

- `json=dict_value`
- `data=dict_value`
- `data=json.dumps(dict_value)`
- `json=json.dumps(dict_value)`

记录每次请求的 `Content-Type`、原始 body 和服务端解析后的类型。目标是能亲眼区分 JSON 对象、表单、原始 JSON 文本和重复编码后的 JSON 字符串。

### 练习 2：增加顶层数组响应

让本地 fixture 返回 HTTP 200 和 JSON 数组。保持 `fetch_document()` 的返回合同不变，使它在数据校验层抛出明确异常，而不是泄漏 `TypeError`、`KeyError` 或返回空对象。

### 练习 3：为 204 定义合同

给 fixture 增加 HTTP 204 响应。先决定“查询文档”是否允许 204：

- 不允许：在 HTTP 状态层明确拒绝；
- 允许：设计一个能明确表达“无正文”的返回类型。

不要直接调用 `.json()`，再把解码失败解释成正常空结果。

## 本章小结

以后看到一个 API 调用，只要按下面顺序提问即可：

1. 请求是否构造正确？
2. 连接和传输是否完成？
3. HTTP 状态是否符合接口合同？
4. 正文是否为合法 JSON？
5. JSON 的结构、字段类型和业务码是否合格？

Requests 的 API 不多，真正重要的是建立这套边界意识。它会直接复用到后续数据库客户端、LLM API、MCP 工具调用和微服务集成中。

延伸阅读：

- Requests Quickstart
- Requests Advanced Usage：Session 与 timeout
- Requests API 与异常参考
