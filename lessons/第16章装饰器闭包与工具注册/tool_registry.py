"""第 16 章使用的最小同步工具注册表。

这个模块只做三件事：构造元信息、拒绝重名、保存同步函数。它不发送网络请求，
也不尝试验证任意外部参数；参数边界验证属于后续 Agent 运行时的职责。
"""

from annotationlib import Format, get_annotations
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from inspect import Signature, getdoc, signature
from types import MappingProxyType

type AnySyncFunction = Callable[..., object]


class DuplicateToolError(ValueError):
    """新工具试图占用已有名称时抛出，避免悄悄覆盖。"""


@dataclass(frozen=True, slots=True)
class ToolMetadata:
    """注册时保存的、只读的工具描述。"""

    name: str
    python_name: str
    description: str
    signature: Signature
    annotations: Mapping[str, object]
    function: AnySyncFunction


_REGISTRY: dict[str, ToolMetadata] = {}
_REGISTRATION_EVENTS: list[str] = []


def tool[**P, R](
    *,
    name: str | None = None,
    description: str | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """返回同步工具装饰器，同时保留原函数的参数与返回类型关系。

    这个装饰器只注册，不包装执行，所以 decorate 最后直接返回 function。
    `Callable[P, R] -> Callable[P, R]` 告诉类型检查器：装饰前后的调用契约相同。
    """

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        resolved_name: str = function.__name__ if name is None else name.strip()
        resolved_description: str | None = (
            getdoc(function) if description is None else description.strip()
        )

        if not resolved_name:
            raise ValueError("工具名称不能为空")
        if not resolved_description:
            raise ValueError(f"工具 {resolved_name!r} 缺少 description 或 docstring")
        if resolved_name in _REGISTRY:
            # 先检查再写入，失败时不会留下半注册状态。
            raise DuplicateToolError(f"工具名称已注册：{resolved_name}")

        # Python 3.14 的注解默认延迟求值。FORWARDREF 能保留尚未定义的名称，
        # 不要求注册时所有前向引用都已经可以解析。
        annotations = get_annotations(function, format=Format.FORWARDREF)
        metadata = ToolMetadata(
            name=resolved_name,
            python_name=function.__name__,
            description=resolved_description,
            # STRING 格式适合面向人的工具描述；不依赖 __annotations__ 的内部形态。
            signature=signature(function, annotation_format=Format.STRING),
            annotations=MappingProxyType(dict(annotations)),
            function=function,
        )
        _REGISTRY[resolved_name] = metadata

        event: str = f"注册 {resolved_name!r} <- {function.__name__}{metadata.signature}"
        _REGISTRATION_EVENTS.append(event)
        print(f"[导入期] {event}")

        # 注册与包装是两件事。这里不创建 wrapper，因此函数身份也保持不变。
        return function

    return decorate


def get_tool(name: str) -> ToolMetadata:
    """按名称取得元信息；未知名称通过 LookupError 明确失败。"""

    try:
        return _REGISTRY[name]
    except KeyError:
        raise LookupError(f"未注册工具：{name}") from None


def list_tools() -> tuple[ToolMetadata, ...]:
    """按注册顺序返回不可变快照，调用者不能借此修改内部字典。"""

    return tuple(_REGISTRY.values())


def registration_events() -> tuple[str, ...]:
    """返回注册事件快照，供导入时机实验观察。"""

    return tuple(_REGISTRATION_EVENTS)


def invoke_tool(name: str, /, *args: object, **kwargs: object) -> object:
    """演示按运行时字符串动态调用时，静态签名关系会在边界处被擦除。

    单个被装饰函数仍保留精确类型；但普通 dict 无法让类型检查器根据任意字符串，
    自动关联每个异构工具各自不同的参数和返回类型。因此这个动态入口返回 object。
    """

    return get_tool(name).function(*args, **kwargs)
