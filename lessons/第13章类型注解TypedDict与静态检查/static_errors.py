"""只供 Pyright 分析的教学反例；不要把这个文件当作程序执行。

本文件故意保留类型错误。命令和预期诊断见 README.md。
"""

from collections.abc import Callable, Mapping
from typing import Any, Literal, NotRequired, TypedDict, cast, reveal_type


def normalize_query(query: str) -> str:
    return query.strip()


# 诊断 1：参数需要 str，不能直接传 int。
bad_query: str = normalize_query(13)


# 这一行故意没有静态诊断：Any 让错误操作穿过检查器。
payload_any: Any = {"limit": "3"}
hidden_by_any: int = payload_any["limit"] + 1
reveal_type(payload_any)  # Pyright: Any


def load_dynamic_payload() -> object:
    """模拟只承诺返回 object 的外部边界。"""
    return {"limit": "3"}


# 诊断 2：object 很安全，但不能在没有收窄时假定它可按键访问。
payload_object: object = load_dynamic_payload()
bad_access: Any = payload_object["limit"]


if isinstance(payload_object, dict) and all(isinstance(key, str) for key in payload_object):
    # 先用运行时分支证明它是字符串键映射，再用 cast 把已证明的事实告诉检查器。
    narrowed_mapping: Mapping[str, object] = cast(Mapping[str, object], payload_object)
    possible_limit: object = narrowed_mapping.get("limit")
    reveal_type(possible_limit)  # Pyright: object | None (Mapping.get 的默认结果为 None)
    if type(possible_limit) is int:
        reveal_type(possible_limit)  # Pyright: int
        checked_limit: int = possible_limit + 1


class Request(TypedDict):
    query: str
    # 必需键，只是值允许 None。
    category: str | None
    # 可缺少键；存在时必须是 int。
    limit: NotRequired[int]


# 诊断 3：category: str | None 不代表 category 键可以缺少。
missing_required_key: Request = {"query": "typing"}


# 诊断 4：NotRequired 只允许缺键，不会放宽已存在值的类型。
wrong_optional_value: Request = {
    "query": "typing",
    "category": None,
    "limit": "3",
}


type Status = Literal["ok", "not_found"]


# 诊断 5：Literal 集合中没有 "timeout"。
unsupported_status: Status = "timeout"


type Formatter = Callable[[str, int], str]


def one_argument_formatter(text: str) -> str:
    return text


# 诊断 6：回调签名少了一个 int 参数。
wrong_callback: Formatter = one_argument_formatter


# cast 只改静态视角，不验证运行时值，因此这行没有诊断。
claimed_text: str = cast(str, payload_object)
reveal_type(claimed_text)  # Pyright: str
