"""第 12 章：使用 requests 调用本地 API，并逐层处理 HTTP 与 JSON 边界。"""

# 建议按 main() 中的调用顺序阅读本文件：先辨认 Python 值、JSON 文本和 bytes，再比较
# params=/json=/data=，最后跟着 fetch_document() 走完请求、响应和失败分类。
# 默认入口只连接 local_http_fixture.py 启动的 127.0.0.1 服务，不会访问公网。

# 标准库中，json 负责 Python 对象与 JSON 文本之间的转换，os 读取进程环境变量；
# Callable 描述“可调用对象”的类型，dataclass 减少数据类样板代码，metadata 读取发行包版本。
# requests 是真正发出 HTTP 请求的第三方客户端；本地 fixture 则提供可自动关闭的教学服务器，
# 以及一个用于稳定制造“拒绝连接”的临时地址。
import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from importlib import metadata

import requests
from local_http_fixture import LocalHttpFixture, refusing_connection_url

# 这两个值都是二元 tuple，依次表示建立连接的等待上限与相邻响应字节的等待上限。
# 它们不是“整个业务最多运行多久”；解析 JSON、校验字段和多次请求都不在总计时器内。
DEFAULT_TIMEOUT = (0.5, 0.3)
SLOW_RESPONSE_TIMEOUT = (0.5, 0.08)
# 这是类型别名：trace 回调接收阶段名 str 和任意 object，不返回有用结果。
# 它只服务于类型检查与阅读，Python 运行时不会像 Java 接口那样自动验证实现签名。
TraceCallback = Callable[[str, object], None]


# @dataclass 会在类定义时参与处理，自动生成 __init__、__repr__、__eq__ 等方法，
# 可类比减少 Java DTO/POJO 的样板代码；frozen=True 还会阻止字段在构造后被重新赋值。
@dataclass(frozen=True)
class Document:
    """通过所有边界校验后，调用者才能得到这个稳定的返回类型。"""

    id: int
    title: str
    content: str


# 下面建立一棵应用异常继承树。调用者既可以捕获 DocumentApiError 统一处理所有预期失败，
# 也可以捕获某个子类，精确区分配置、传输、HTTP、JSON、数据结构和业务结果。
class DocumentApiError(RuntimeError):
    """文档 API 的预期失败基类；layer 明确指出失败发生在哪一层。"""

    # layer 是类属性，所有实例默认共享；子类只需覆盖这个标签，不必重复初始化逻辑。
    layer: str = "未知层"


class ConfigurationError(DocumentApiError):
    layer: str = "调用配置层"


class TransportError(DocumentApiError):
    layer: str = "HTTP 传输层"


class ConnectionFailure(TransportError):
    """DNS、拒绝连接、连接被重置等连接问题。"""


class RequestTimeout(TransportError):
    """connect timeout 或 read timeout。"""


class HttpStatusFailure(DocumentApiError):
    layer: str = "HTTP 状态层"

    # 类型提示说明参数意图，但不负责运行时强制校验。先保存状态码，再把 message 交给
    # RuntimeError 的初始化链，这样 str(error) 和 traceback 都能显示错误消息。
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code: int = status_code
        super().__init__(message)


class ResponseJsonFailure(DocumentApiError):
    layer: str = "JSON 语法层"


class ResponseValidationFailure(DocumentApiError):
    layer: str = "响应数据校验层"


class BusinessFailure(DocumentApiError):
    layer: str = "业务结果层"

    # 业务码作为结构化属性单独保留；调用者无需从自然语言错误消息中反向解析 code。
    def __init__(self, code: str, message: str) -> None:
        self.code: str = code
        super().__init__(message)


def print_section(title: str) -> None:
    """让终端输出与 README 小节一一对应。"""
    # f-string 会先计算花括号中的 title；开头的 \n 让不同实验之间空一行。
    print(f"\n=== {title} ===")


def mask_secret(secret: str | None) -> str:
    """日志只显示凭据是否存在及极少首尾字符，不输出完整值。"""
    # str | None 相当于 Java 语境下“可能没有值”，但这里只是类型提示。
    # 布尔上下文会把 None 和空字符串都视为 False，因此二者都显示成未提供。
    if not secret:
        return "（未提供）"
    # 短凭据完全替换成同长度星号；字符串乘法会重复 "*"，不会修改原字符串。
    if len(secret) <= 4:
        return "*" * len(secret)
    # [前:后] 是切片；[:2] 取前两个字符，[-2:] 从倒数第二个字符取到末尾。
    return f"{secret[:2]}***{secret[-2:]}"


def trace_to_terminal(stage: str, value: object) -> None:
    """示例专用观察器；真实应用通常改用结构化日志并进一步限制响应正文。"""
    # object 表示入口可以接收任意 Python 对象。若已经校验成 Document，就用 asdict()
    # 新建普通 dict 供 JSON 序列化；它不会把原 Document 原地改成字典。
    if isinstance(value, Document):
        visible_value: object = asdict(value)
    else:
        visible_value = value
    # ensure_ascii=False 让中文保持可读；sort_keys=True 让字典键顺序稳定。
    # default=repr 表示遇到 json 不认识的对象时，退回它的 repr(...) 字符串用于教学输出。
    rendered: str = json.dumps(visible_value, ensure_ascii=False, sort_keys=True, default=repr)
    print(f"  {stage}: {rendered}")


def require_non_null_field(
    mapping: dict[str, object], field_name: str, *, location: str
) -> object:
    """把“缺字段”和“字段明确为 null”分成两个可诊断错误。"""
    # 签名中的 * 使 location 成为仅限关键字参数，调用处必须写 location=...，
    # 读代码时能看出它是错误路径说明，而不是另一个待查询字段。
    # `in` 检查键是否存在，不能用 mapping.get(...) 一步代替，否则会把缺键与 JSON null
    #（解析后是 None）折叠成同一种情况。
    if field_name not in mapping:
        # f-string 中的 !r 使用 repr(field_name)，所以错误里的字段名会带引号。
        raise ResponseValidationFailure(f"{location} 缺少字段 {field_name!r}")
    value: object = mapping[field_name]
    # None 是单例，按身份用 is 判断；不要用 == 套用 Java 的 null 比较经验。
    if value is None:
        raise ResponseValidationFailure(f"{location}.{field_name} 不能为 null")
    return value


def validate_document_payload(payload: object) -> Document:
    """校验 JSON 对应的 Python 对象，并只返回调用者真正需要的数据。"""
    # response.json() 的返回类型取决于 JSON 顶层，可能是 dict、list、str、数字或 None。
    # 因而参数先声明成最宽的 object，再在每一层用 isinstance 缩小为本 API 约定的结构。
    if not isinstance(payload, dict):
        raise ResponseValidationFailure("响应 JSON 顶层必须是 object/dict")

    # 先判断业务码。require_non_null_field 已保证 code 存在且不是 None，下一步再验证类型；
    # “存在”“非 null”“类型正确”和“值表示成功”是四条不同规则。
    code: object = require_non_null_field(payload, "code", location="响应")
    if not isinstance(code, str):
        raise ResponseValidationFailure("响应.code 必须是 str")
    if code != "OK":
        # 错误响应中的 message 不是成功结构的必需字段，所以用 get() 可选读取。
        # 条件表达式 `A if 条件 else B` 根据类型选择服务端消息或本地兜底文本。
        message: Any | None = payload.get("message")
        safe_message: str = message if isinstance(message, str) else "服务端未提供错误说明"
        raise BusinessFailure(code, f"业务码 {code}: {safe_message}")

    # 业务码成功后再逐层下钻：payload["data"] -> data["document"]。
    # 每层先验存在/null，再验 dict，避免让 KeyError 或 TypeError 泄漏成含义不清的失败。
    data: object = require_non_null_field(payload, "data", location="响应")
    if not isinstance(data, dict):
        raise ResponseValidationFailure("响应.data 必须是 object/dict")
    document: object = require_non_null_field(data, "document", location="响应.data")
    if not isinstance(document, dict):
        raise ResponseValidationFailure("响应.data.document 必须是 object/dict")

    # 到这里 document 才能安全按键访问。三个局部变量仍是 object，下面的类型判断会把它们
    # 收窄成构造 Document 所需的 int/str/str。
    document_id: object = require_non_null_field(
        document, "id", location="响应.data.document"
    )
    title: object = require_non_null_field(
        document, "title", location="响应.data.document"
    )
    content: object = require_non_null_field(
        document, "content", location="响应.data.document"
    )

    # Python 的 bool 是 int 的子类，isinstance(True, int) 会得到 True；本合同要求真正的
    # 整数 ID，所以使用 type(...) is int 排除布尔值。其余字段允许 str 的子类，因此用 isinstance。
    if type(document_id) is not int:
        raise ResponseValidationFailure("响应.data.document.id 必须是 int，且不能是 bool")
    if not isinstance(title, str):
        raise ResponseValidationFailure("响应.data.document.title 必须是 str")
    if not isinstance(content, str):
        raise ResponseValidationFailure("响应.data.document.content 必须是 str")

    # 关键字参数按名字绑定到 dataclass 字段。这里返回新的稳定对象，而不把仍含 code/data
    # 包装层的原始响应字典泄漏给调用者。
    return Document(id=document_id, title=title, content=content)


def fetch_document(
    query: str,
    *,
    base_url: str | None = None,
    api_token: str | None = None,
    session: requests.Session | None = None,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    trace: TraceCallback | None = None,
) -> Document:
    """同步查询一份文档；失败时抛出带层次的异常，不伪造空字典。

    ``base_url`` 与 ``api_token`` 的显式参数优先；未传时才读取环境变量。调用者可以传入
    Session 复用连接，也可以省略它，让函数创建并在本次调用结束时关闭自己的 Session。
    """
    # 签名中 query 后的 * 表示后续参数都必须按名字传入，能减少多个 str/timeout 参数错位。
    # `T | None` 表示值可为 T 或 None；tuple[float, float] 表示正好两个浮点数的类型意图。
    # DEFAULT_TIMEOUT 是不可变 tuple，作为默认值不会产生“多个调用共享可变列表”的陷阱。

    # `or` 具有短路行为：query 不是 str 时不会继续调用不存在的 strip()。
    # strip() 返回去掉首尾空白的新字符串，不会原地修改调用者传入的 str。
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 必须是非空 str")
    # HTTP 请求尚未发生，先拒绝形状不对或非正数的超时配置。
    if len(timeout) != 2 or timeout[0] <= 0 or timeout[1] <= 0:
        raise ValueError("timeout 必须是两个正数：(connect timeout, read timeout)")

    # base_url 使用“显式值 or 环境值”：显式传空字符串也会回退到环境变量。
    # token 则用 is not None 区分“没有传”与“明确传入空字符串”；明确的空字符串表示不认证，
    # 不应又从 DOCUMENT_API_TOKEN 捞回一个进程级凭据。
    resolved_base_url: str | None = base_url or os.environ.get("DOCUMENT_API_BASE_URL")
    resolved_token: str | None = (
        api_token if api_token is not None else os.environ.get("DOCUMENT_API_TOKEN")
    )
    if not resolved_base_url:
        raise ConfigurationError(
            "请通过 base_url 参数或 DOCUMENT_API_BASE_URL 配置服务地址"
        )

    # rstrip('/') 只去掉末尾斜杠，再统一拼出 /documents，避免得到双斜杠。
    # params 保持为 Python dict，稍后交给 Requests 做 URL percent-encoding；不要手拼查询字符串。
    url: str = f"{resolved_base_url.rstrip('/')}/documents"
    params: dict[str, str] = {"query": query.strip()}
    # Accept 描述希望收到 JSON；GET 没有 JSON body，所以这里不需要请求 Content-Type。
    headers: dict[str, str] = {"Accept": "application/json"}
    if resolved_token:
        headers["Authorization"] = f"Bearer {resolved_token}"

    # 回调是可选依赖；`is not None` 明确区分“没有观察器”和“传入了可调用对象”。
    if trace is not None:
        # dict(headers) 建立浅拷贝。后面对 Authorization 的替换只影响日志副本，
        # 真正发送用的 headers 仍保留完整凭据。
        safe_headers: dict[str, str] = dict(headers)
        if "Authorization" in safe_headers:
            safe_headers["Authorization"] = f"Bearer {mask_secret(resolved_token)}"
        # trace 接到的是发送前的 Python 数据结构；此刻 Requests 还没有编码 URL 或发出请求。
        trace(
            "发送前（Python 对象）",
            {
                "method": "GET",
                "url": url,
                "params": params,
                "headers": safe_headers,
                "timeout": {"connect": timeout[0], "read": timeout[1]},
            },
        )

    # 调用者传 Session 时复用它的连接池，并继续由调用者拥有；否则本函数创建私有 Session。
    # 两个变量分开保存客户端与所有权，finally 才能只关闭自己创建的资源。
    client: Session = session if session is not None else requests.Session()
    owns_session: bool = session is None
    try:
        # 内层 try 只包围真正的网络发送，负责把 Requests 异常翻译成应用异常；外层 try/finally
        # 则负责 Session 生命周期。client.get(...) 返回后，才拥有可检查的 Response。
        try:
            response: Response = client.get(
                url, params=params, headers=headers, timeout=timeout
            )
        except requests.Timeout as error:
            # except 按从上到下匹配；ConnectTimeout 同时也是 ConnectionError，因此必须先捕获
            # Timeout 才不会被归错类。`raise ... from error` 把底层异常保存在 __cause__ 中。
            raise RequestTimeout(f"请求超时（{type(error).__name__}）") from error
        except requests.ConnectionError as error:
            raise ConnectionFailure(f"连接失败（{type(error).__name__}）") from error
        except requests.RequestException as error:
            # RequestException 是 Requests 请求异常的公共基类，接住前两类以外的传输失败，
            # 但不会吞掉普通编程错误或下面的数据校验异常。
            raise TransportError(
                f"requests 发送请求失败（{type(error).__name__}）"
            ) from error

        if trace is not None:
            # 这里已经收到响应，但尚未宣布 HTTP 成功。content 是 bytes，text 是 Requests
            # 按响应编码解码得到的 str；读取这些属性不等于完成 JSON 或业务校验。
            trace(
                "收到后（还未断言成功）",
                {
                    "status_code": response.status_code,
                    "content_type": response.headers.get("Content-Type"),
                    "content_python_type": type(response.content).__name__,
                    "text_python_type": type(response.text).__name__,
                    "body_byte_count": len(response.content),
                },
            )

        # raise_for_status() 对 4xx/5xx 抛 HTTPError，成功状态返回 None；先做这一层，
        # 避免把 404 的合法 JSON 错误正文误送入成功业务模型。
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            # [:120] 产生正文前 120 个字符的新 str；这里只截取本地演示正文，生产日志
            # 还应按服务的数据分类规则做脱敏。!r 让换行等不可见字符以转义形式展示。
            preview: str = response.text[:120]
            raise HttpStatusFailure(
                response.status_code,
                f"HTTP {response.status_code}，响应预览：{preview!r}",
            ) from error

        # raise_for_status() 只拒绝 4xx/5xx；3xx、204 等不一定抛错。这个查询合同只允许 200，
        # 所以还要显式检查精确状态，不能把“未抛 HTTPError”直接等同于业务成功。
        if response.status_code != 200:
            raise HttpStatusFailure(
                response.status_code,
                f"文档查询预期 HTTP 200，实际为 {response.status_code}",
            )

        # .json() 才把响应正文解析为 Python 对象；它不保证顶层一定是 dict，也不校验字段。
        try:
            payload: Any = response.json()
        except requests.JSONDecodeError as error:
            # 只把 JSON 语法解析失败转换为本层异常，并保留原始 cause。
            raise ResponseJsonFailure("HTTP 成功，但响应正文不是合法 JSON") from error

        if trace is not None:
            # 此处打印的是“可解析但仍不可信”的 Python 值，下一步才做结构和业务规则校验。
            trace(
                "解析后（尚未业务校验）",
                {"python_type": type(payload).__name__, "value": payload},
            )

        # 只有这一调用返回后，payload 才被收窄成调用者可以依赖的 Document。
        document: Document = validate_document_payload(payload)
        if trace is not None:
            trace("校验后（稳定返回值）", document)
        return document
    finally:
        # finally 无论正常 return 还是中途 raise 都执行。仅当本函数创建 Session 时关闭它；
        # 传入的共享 Session 仍由外层调用者继续使用和最终关闭。
        if owns_session:
            client.close()


def post_echo(
    session: requests.Session,
    base_url: str,
    *,
    params: dict[str, str] | None = None,
    json_value: dict[str, str | int] | str | None = None,
    data_value: dict[str, str | int] | str | None = None,
) -> tuple[requests.Response, dict[str, object]]:
    """调用无副作用回显端点，让不同参数生成的真实 HTTP 请求可观察。"""
    # `dict[...] | str | None` 表示示例允许传 Python 字典、已经编码的文本或不传该参数。
    # 函数返回二元 tuple：原始 Response 用来观察最终 URL，dict 用来观察服务端收到的内容。
    # params/json/data 被原样交给 Requests，它们分别控制查询串与两种 body 编码入口。
    response: Response = session.post(
        f"{base_url}/echo",
        params=params,
        json=json_value,
        data=data_value,
        timeout=DEFAULT_TIMEOUT,
    )
    # 回显端点也先检查 HTTP，再解析 JSON；不能因为它是教学服务就颠倒边界顺序。
    response.raise_for_status()
    payload: Any = response.json()
    # JSON 顶层理论上可以是任意值，本地 fixture 合同则明确要求 object/dict。
    if not isinstance(payload, dict):
        raise TypeError("fixture 的 echo 响应应为 JSON object")
    return response, payload


def demonstrate_json_shapes() -> None:
    """观察 dict、JSON 文本、UTF-8 bytes 和重复序列化。"""
    print_section("1. dict、JSON str 与 UTF-8 bytes")
    # 起点是可按键访问的 Python dict；它只是进程内对象，还不是线上传输的 JSON。
    python_value: dict[str, Any] = {"query": "Agent 入门", "limit": 2}
    # dumps 的 s 表示 string：返回 JSON str。ensure_ascii=False 让中文保留原字符，
    # 随后的 encode 才按 UTF-8 把字符变成可传输的 bytes。
    json_text: str = json.dumps(python_value, ensure_ascii=False)
    utf8_bytes: bytes = json_text.encode("utf-8")
    # 反例：json_text 已经是 str，再 dumps 会把“这段字符串”编码成 JSON string，
    # 而不是保留原先 JSON object 的形状，因此内部引号会出现反斜杠转义。
    double_dumped_text: str = json.dumps(json_text, ensure_ascii=False)

    # type(...).__name__ 给出类型名；repr(...) 把引号和反斜杠显式显示出来，便于辨认层次。
    print("Python 数据:", type(python_value).__name__, python_value)
    print("JSON 文本:", type(json_text).__name__, repr(json_text))
    print("UTF-8 载荷:", type(utf8_bytes).__name__, utf8_bytes)
    print("重复 dumps 后:", repr(double_dumped_text))
    # 一次 loads 只撤销最外层序列化，所以先得到 str；对该 str 再 loads 才回到 dict。
    print("一次 loads 的类型:", type(json.loads(double_dumped_text)).__name__)
    print(
        "两次 loads 才回到:",
        type(json.loads(json.loads(double_dumped_text))).__name__,
    )


def demonstrate_request_arguments(
    session: requests.Session, base_url: str
) -> None:
    """比较 params=、json= 和 data= 在服务端真正看到的结果。"""
    print_section("2. params=、json=、data= 的实际区别")
    # 两个变量内容相近但类型不同：python_value 是 dict，json_text 是已编码的 str。
    python_value: dict[str, Any] = {"query": "Agent 入门", "limit": 2}
    json_text: str = json.dumps(python_value, ensure_ascii=False)

    # tuple 返回值按位置解包：json_response 接 Response，json_echo 接回显 dict。
    # params= 被编码到 URL；json=dict 把 dict 序列化进 body，并设置 JSON Content-Type。
    json_response, json_echo = post_echo(
        session,
        base_url,
        params={"source": "第 12 章"},
        json_value=python_value,
    )
    print("params= 编码后的 URL:", json_response.url)
    print("json=dict 的 Content-Type:", json_echo["content_type"])
    print(
        "json=dict 的服务端类型/值:",
        json_echo["json_value_type"],
        json_echo["json_value"],
    )

    # `_` 只是“此值不打算使用”的命名惯例，并不是特殊关键字。data=dict 会走表单编码，
    # 服务端看到 application/x-www-form-urlencoded，而不是 JSON object。
    _, form_echo = post_echo(session, base_url, data_value=python_value)
    print("data=dict 的 Content-Type:", form_echo["content_type"])
    print("data=dict 的表单值:", form_echo["form_value"])

    # data=str 直接把现成文本作为 body 发送，不会自动声明 application/json；调用者若真要
    # 使用这种低层写法，还必须按协议自行管理 Content-Type。
    _, raw_echo = post_echo(session, base_url, data_value=json_text)
    print("data=JSON str 的 Content-Type:", raw_echo["content_type"])
    print("data=JSON str 的原始 body:", repr(raw_echo["raw_body"]))

    # 教学反例：json= 期望“待序列化的 Python 值”，传入已经 dumps 的 str 会再次序列化。
    # 服务端解析一次只得到 str，原始 body 也会出现额外引号和转义符。
    _, wrong_echo = post_echo(session, base_url, json_value=json_text)
    print(
        "教学反例 json=JSON str 的服务端类型:",
        wrong_echo["json_value_type"],
    )
    print("教学反例的原始 body:", repr(wrong_echo["raw_body"]))


def demonstrate_response_views(
    session: requests.Session, base_url: str
) -> None:
    """同一响应的状态、字节、文本和 JSON 视图回答不同问题。"""
    print_section("3. Response 的五个观察入口")
    # GET 的查询条件放 params，不手工处理中文转义；返回值是尚未做业务判断的 Response。
    response: Response = session.get(
        f"{base_url}/documents",
        params={"query": "响应视图"},
        timeout=DEFAULT_TIMEOUT,
    )
    # 五个入口各回答一个问题：状态码是 int；成功时 raise_for_status() 的返回值是 None；
    # content 是 bytes；text 是解码后的 str；json() 再解析为 Python 值。
    print("status_code:", response.status_code, type(response.status_code).__name__)
    print("raise_for_status() 返回:", response.raise_for_status())
    print("content:", type(response.content).__name__, response.content[:45])
    print("text:", type(response.text).__name__, response.text[:35])
    parsed: Any = response.json()
    # fixture 的成功响应顶层是 dict；sorted(parsed) 迭代并排序它的键，不会排序嵌套值。
    print("json():", type(parsed).__name__, "顶层字段", sorted(parsed))

    # 这个反例故意先解析 404 的合法 JSON，再检查状态：前一步能得到业务码 NOT_FOUND，
    # 后一步仍会抛 HTTPError，证明“JSON 可解析”和“HTTP 成功”彼此独立。
    error_response: Response = session.get(
        f"{base_url}/documents",
        params={"query": "http-error"},
        timeout=DEFAULT_TIMEOUT,
    )
    parsed_error: Any = error_response.json()
    print("404 正文仍能解析 JSON:", parsed_error["code"])
    try:
        error_response.raise_for_status()
    except requests.HTTPError as error:
        # type(error).__name__ 只打印类名，让受控演示继续运行；生产代码通常还需保留详情。
        print("但 raise_for_status() 拒绝它:", type(error).__name__)


def demonstrate_fetch_and_session(
    session: requests.Session,
    fixture: LocalHttpFixture,
) -> None:
    """运行完整五层查询，并用服务端记录证明连接被 Session 复用。"""
    print_section("4. fetch_document：从发送到校验")
    # fixture 会累计记录此前实验的请求；先保存当前长度，稍后用切片只检查本函数新增的记录。
    start_index: int = len(fixture.state.records_snapshot())
    # 两次调用都借用同一个 Session。第一次传 trace 展示四个边界，第二次保持安静，
    # 但两次都必须走完相同的状态、JSON、字段和业务校验才能返回 Document。
    first: Document = fetch_document(
        "Agent",
        base_url=fixture.base_url,
        api_token="local-demo-token-1234",
        session=session,
        trace=trace_to_terminal,
    )
    second: Document = fetch_document(
        "MCP",
        base_url=fixture.base_url,
        api_token="local-demo-token-1234",
        session=session,
    )

    # [start_index:] 取快照尾部；列表推导式只收集 GET 记录的客户端端口，等价于一个紧凑的
    # for + if + append。快照和结果都是新 list，不会修改 fixture 内部记录。
    new_records: list[RequestRecord] = fixture.state.records_snapshot()[start_index:]
    document_ports: list[int] = [
        record.client_port for record in new_records if record.method == "GET"
    ]
    # `and` 从左到右短路：只有长度确实为 2 时才访问下标 0、1，因此不会因空列表越界。
    connection_reused: bool = (
        len(document_ports) == 2 and document_ports[0] == document_ports[1]
    )
    if not connection_reused:
        # AssertionError 表示教学实验的前提没有被观察到，不属于 Document API 的业务失败。
        raise AssertionError(
            f"支持 keep-alive 的本地服务未观察到 Session 连接复用：{document_ports}"
        )
    # dataclass 自动生成可读的 repr；字段也能像普通对象一样用 second.title 访问。
    print("返回值:", first)
    print("第二次查询:", second.title)
    print("服务端观察到两次查询复用客户端端口:", document_ports, connection_reused)


def expect_failure(
    label: str,
    expected_type: type[DocumentApiError],
    *,
    query: str,
    session: requests.Session,
    base_url: str,
) -> None:
    """只捕获该场景预期的 API 异常；其他缺陷继续中断程序。"""
    # type[DocumentApiError] 表示参数本身是一种异常“类”，不是异常实例。
    # Python 允许 except 后使用这个参数，因此同一个小函数可以验证多种预期异常。
    try:
        fetch_document(query, base_url=base_url, session=session)
    except expected_type as error:
        print(f"{label}: {error.layer} -> {type(error).__name__}: {error}")
    else:
        # try 的 else 只在没有异常时执行；这里没有失败反而说明反例未命中预期路径。
        raise AssertionError(f"{label} 没有产生预期的 {expected_type.__name__}")


def demonstrate_failure_layers(
    session: requests.Session,
    fixture: LocalHttpFixture,
) -> None:
    """让每种失败落在唯一、可说明的边界，不用空 dict 掩盖原因。"""
    print_section("5. 失败发生在哪一层")
    # 每个场景是三元 tuple：（展示标签，预期异常类，发给 fixture 的查询词）。
    # 用数据描述场景后，下面一段循环就能统一验证 HTTP、JSON、字段和业务层失败。
    cases: list[Any] = [
        ("HTTP 404", HttpStatusFailure, "http-error"),
        ("200 但非法 JSON", ResponseJsonFailure, "invalid-json"),
        ("缺字段", ResponseValidationFailure, "missing-field"),
        ("字段为 null", ResponseValidationFailure, "null-field"),
        ("字段类型不符", ResponseValidationFailure, "wrong-type"),
        ("HTTP/JSON 都成功但业务码失败", BusinessFailure, "business-fail"),
    ]
    # for 会把每个三元 tuple 按位置解包到三个名字；expected_type 仍是类对象。
    for label, expected_type, query in cases:
        expect_failure(
            label,
            expected_type,
            query=query,
            session=session,
            base_url=fixture.base_url,
        )

    # refusing_connection_url() 是上下文管理器：进入时 yield 一个刚释放端口的 URL，
    # 离开时完成其清理。请求该地址会落到 ConnectionFailure，而不是依赖公网制造故障。
    with refusing_connection_url() as unavailable_base_url:
        expect_failure(
            "本机拒绝连接",
            ConnectionFailure,
            query="Agent",
            session=session,
            base_url=unavailable_base_url,
        )

    # slow 场景已建立连接并到达服务端，但在很短的 read timeout 内收不到首字节。
    # 这里单独验证 RequestTimeout，并用 finally 确保任何离开路径都会释放服务端等待事件。
    try:
        fetch_document(
            "slow",
            base_url=fixture.base_url,
            session=session,
            timeout=SLOW_RESPONSE_TIMEOUT,
        )
    except RequestTimeout as error:
        print(f"慢响应: {error.layer} -> {type(error).__name__}: {error}")
    else:
        raise AssertionError("慢响应没有产生预期的 RequestTimeout")
    finally:
        # 客户端已经完成超时分类，现在明确解除服务端事件；set() 会把事件置为已触发，
        # 在有人调用 clear() 前保持该状态，正在 wait() 的服务线程因此可以继续收尾。
        fixture.state.release_slow_response.set()

    # is_set() 证明请求确实到达服务端；wait(timeout=...) 返回 bool，True 表示清理事件按时发生。
    # 这比固定 sleep 后猜测线程顺序更可靠，也避免反例永久挂住程序。
    if not fixture.state.slow_request_started.is_set():
        raise AssertionError("慢请求没有到达服务端，不能把结果解释成 read timeout")
    if not fixture.state.slow_request_finished.wait(timeout=1.0):
        raise AssertionError("慢请求服务线程没有清理完成")
    print("慢请求顺序证据：服务端已进入等待 -> 客户端 read timeout -> 事件释放并清理")
    print("所有失败都保留异常类型与原因，没有 except Exception 后返回 {}。")


def main() -> None:
    """默认只访问自动管理的 127.0.0.1 服务，不访问公网、不需要真实 API Key。"""
    print("Python HTTP/JSON 边界实验：仅访问自动启动的本地 fixture")
    # 安装版本属于发行包元数据；metadata.version 按发行名 requests 查询当前环境，
    # 不依赖库是否把 __version__ 作为公开类型接口导出。
    print("requests 版本:", metadata.version("requests"))
    # 这一段纯本地转换不需要服务器，因此先运行。
    demonstrate_json_shapes()

    # `with LocalHttpFixture() as fixture` 可类比 Java try-with-resources：进入时启动本地服务，
    # 无论块内正常结束还是抛异常，离开时都会关闭监听 socket 并等待服务线程结束。
    with LocalHttpFixture() as fixture:
        print("本地服务:", fixture.base_url)
        # Session 自己也是上下文管理器。嵌套 with 明确表示连接池的生命周期短于 fixture，
        # 因此先关闭客户端连接池，再关闭它所连接的本地服务。
        with requests.Session() as session:
            # update 原地修改 Session 的公共 headers；每次调用传入的 headers 会与之合并。
            session.headers.update({"X-Demo-Client": "chapter-12"})
            # 本地 fixture 不应受机器上的 HTTP_PROXY / .netrc 等环境配置影响；False 只为保持
            # 本机实验输入可控，不代表真实项目应无条件禁用组织代理或环境认证。
            session.trust_env = False
            # 同一个 Session 顺序执行后续真实 HTTP 实验，允许 urllib3 连接池复用连接。
            demonstrate_request_arguments(session, fixture.base_url)
            demonstrate_response_views(session, fixture.base_url)
            demonstrate_fetch_and_session(session, fixture)
            demonstrate_failure_layers(session, fixture)
        print_section("6. 资源关闭")
        print("已退出 Session 的 with 块；连接池已关闭。")

    print("已退出 fixture 的 with 块；监听端口和服务线程已关闭:", fixture.closed)
    print("完整示例执行完毕。")


# 导入本文件时，函数和类只会被定义，不会自动跑实验。只有把它作为脚本直接执行时，
# Python 才把模块名设为 "__main__" 并调用 main()；这相当于显式声明程序入口。
if __name__ == "__main__":
    main()
