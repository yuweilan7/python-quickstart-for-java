"""从同步数据校验开始，再比较 fixture、Mock 与 monkeypatch。"""

from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from chapter27 import agent_runtime
from chapter27.agent_runtime import AsyncTextCall, SearchArgs, ask_model
from pydantic import ValidationError


def test_model_accepts_valid_search_args() -> None:
    args = SearchArgs.model_validate({"query": "  asyncio  ", "limit": 2})

    assert args.query == "asyncio"
    assert args.limit == 2


@pytest.mark.parametrize(
    ("raw_args", "bad_field"),
    [
        pytest.param({"query": "", "limit": 2}, "query", id="empty-query"),
        pytest.param({"query": "  ", "limit": 2}, "query", id="blank-query"),
        pytest.param({"query": "ok", "limit": 0}, "limit", id="limit-too-small"),
        pytest.param({"query": "ok", "limit": "2"}, "limit", id="strict-integer"),
        pytest.param({"query": "ok", "limit": 2, "extra": True}, "extra", id="extra-field"),
    ],
)
def test_model_rejects_bad_search_args(raw_args: object, bad_field: str) -> None:
    with pytest.raises(ValidationError) as raised:
        SearchArgs.model_validate(raw_args)

    error_locations = {str(error["loc"][0]) for error in raised.value.errors()}
    assert bad_field in error_locations


def test_function_fixture_starts_empty_a(isolated_events: list[str]) -> None:
    assert isolated_events == []
    isolated_events.append("只属于测试 A")


def test_function_fixture_starts_empty_b(isolated_events: list[str]) -> None:
    # 即使 A 先运行，这里仍是新对象；测试不依赖收集或执行顺序。
    assert isolated_events == []


@pytest.mark.asyncio
async def test_asyncmock_is_awaited() -> None:
    model_call = AsyncMock(return_value="离线模型结果")

    result = await ask_model(model_call, "解释 TaskGroup")

    assert result == "离线模型结果"
    model_call.assert_awaited_once_with("解释 TaskGroup")


@pytest.mark.asyncio
async def test_plain_mock_return_value_is_not_awaitable() -> None:
    plain_mock = Mock(return_value="普通字符串")
    # cast 只能说服类型检查器，不能把 str 在运行时变成 awaitable。
    claimed_async_call = cast(AsyncTextCall, plain_mock)

    with pytest.raises(TypeError, match="await"):
        await ask_model(claimed_async_call, "这个调用必须可等待")

    plain_mock.assert_called_once_with("这个调用必须可等待")


@pytest.mark.asyncio
async def test_monkeypatch_replaces_dependency_at_use_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_api = AsyncMock(return_value="patched-result")
    monkeypatch.setattr(agent_runtime, "call_model_api", fake_api)

    result = await agent_runtime.answer_with_default_model("离线请求")

    assert result == "patched-result"
    fake_api.assert_awaited_once_with("离线请求")
