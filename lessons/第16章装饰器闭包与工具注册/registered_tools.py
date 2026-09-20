"""用于证明“模块导入时注册，函数调用时才执行业务”的本地工具模块。"""

from dataclasses import dataclass

from tool_registry import tool

CALL_EVENTS: list[str] = []
LOCAL_DOCUMENTS = (
    "Python 装饰器执行时机",
    "闭包与 nonlocal",
    "用 ParamSpec 保留函数签名",
)


@tool(name="search_docs", description="在确定性的本地标题中搜索关键字。")
def search_documents(query: str, /, *, limit: int = 2) -> list[str]:
    """返回最多 limit 个包含 query 的本地标题。"""

    # 只有真正调用工具时才记录业务事件。模块导入和装饰器注册不会走到这里。
    CALL_EVENTS.append(f"调用 search_documents(query={query!r}, limit={limit})")
    if not query.strip():
        raise ValueError("query 不能为空")
    if limit < 1:
        raise ValueError("limit 必须大于 0")
    return [title for title in LOCAL_DOCUMENTS if query.lower() in title.lower()][
        :limit
    ]


@tool()
def summarize_results(items: list[str], *, prefix: str = "结果") -> str:
    """把本地搜索结果拼成一行；工具名和说明回退到函数名与 docstring。"""

    CALL_EVENTS.append(f"调用 summarize_results(count={len(items)})")
    return f"{prefix}: {'、'.join(items) if items else '（空）'}"


@tool(name="render_card", description="渲染一个在函数定义之后才声明的结果对象。")
def render_card(card: ResultCard) -> str:
    """演示 Python 3.14 延迟注解和未解析前向引用。"""

    CALL_EVENTS.append(f"调用 render_card(title={card.title!r})")
    return f"[{card.score:.1f}] {card.title}"


@dataclass(frozen=True, slots=True)
class ResultCard:
    """故意放在 render_card 后面；Python 3.14 定义函数时不会立即求值该注解。"""

    title: str
    score: float
