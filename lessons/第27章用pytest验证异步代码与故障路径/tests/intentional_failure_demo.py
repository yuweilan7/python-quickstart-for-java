"""受控失败演示；文件名不匹配 test_*.py，正常测试套件不会收集它。"""

import asyncio

import pytest
from chapter27.agent_runtime import StreamState, buggy_read_first_token


@pytest.mark.asyncio
async def test_buggy_reader_should_close_stream_before_returning() -> None:
    state = StreamState()
    first, leaked_stream = await buggy_read_first_token(state)

    try:
        assert first == "agent"
        # 这里会失败，准确指出真实缺陷：函数已返回，但流仍停在 yield。
        assert state.closed.is_set(), "缺陷：提前返回时没有关闭异步生成器"
    finally:
        # 即使断言故意失败，也显式回收流，不把 pending async generator 留给循环退出。
        await asyncio.wait_for(leaked_stream.aclose(), timeout=1.0)
