"""只供 Pyright 分析的第 15 章反例；不要执行本文件。

这里故意保留类型错误和 reveal_type()，用来对照运行时成员检查与静态签名检查。
"""

from collections.abc import Sequence
from typing import reveal_type

from example import (
    Box,
    PrefixedFormatter,
    Result,
    SearchBackend,
    SearchHit,
    SearchRequest,
    SignatureMismatchBackend,
    Success,
    Tool,
    append_debug_marker,
    combine_text,
    first,
    first_titled,
    identity,
    passthrough_union,
)

# 诊断 1：runtime_checkable 只看 search 成员是否存在；静态协议会比较完整签名。
bad_backend: SearchBackend = SignatureMismatchBackend()


# 诊断 2：identity 把参数类型和返回类型绑定为同一个 T，不会“允许任意后再变成 str”。
wrong_identity_result: str = identity(15)


# 诊断 3：first 的返回类型来自 Sequence 的元素类型。
numbers: tuple[int, ...] = (1, 2, 3)
wrong_first_result: str = first(numbers)


def wrong_output_tool(request: SearchRequest, /) -> Result[str]:
    return Success(request.query)


# 诊断 4：Tool 的 Output 同时约束 __call__ 返回的 Result[Output]。
search_tool: Tool[SearchRequest, list[SearchHit]] = wrong_output_tool


def wrong_keyword_name(hits: Sequence[SearchHit], label: str) -> str:
    return label + str(len(hits))


# 诊断 5：协议调用会传 prefix=，但实现只声明了另一个关键字名 label。
bad_formatter: PrefixedFormatter = wrong_keyword_name


# 诊断 6：list 可变；若允许这次赋值，函数就能把 str 插入 list[SearchHit]。
hit_list: list[SearchHit] = []
append_debug_marker(hit_list)


# 诊断 7：约束 TypeVar 要求多个位置选择同一个允许类型，不能混用 str 与 bytes。
mixed_text: str = combine_text("tool-", b"result")


class SpecialQuery(str):
    pass


special_query = SpecialQuery("Protocol")
reveal_type(combine_text(special_query, special_query))  # Pyright: str
reveal_type(passthrough_union(special_query))  # Pyright: str | bytes


special_hit = SearchHit("special", "特殊命中", "bound 示例", "local")
reveal_type(first_titled([special_hit]))  # Pyright: SearchHit
reveal_type(Box(special_hit).get())  # Pyright: SearchHit
