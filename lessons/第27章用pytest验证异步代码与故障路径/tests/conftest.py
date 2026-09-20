"""只服务本章的 fixture；默认 function scope 保持测试隔离。"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from chapter27.agent_runtime import initialize_notes


@pytest.fixture
def isolated_events() -> list[str]:
    """每个测试获得新列表，避免共享可变状态造成顺序依赖。"""
    return []


@dataclass(slots=True)
class LoopBoundResource:
    created_in: asyncio.AbstractEventLoop
    closed: bool = False

    async def aclose(self) -> None:
        await asyncio.sleep(0)
        self.closed = True


@pytest_asyncio.fixture
async def loop_resource() -> AsyncIterator[LoopBoundResource]:
    """strict 模式下，async fixture 明确使用 pytest_asyncio.fixture。"""
    resource = LoopBoundResource(asyncio.get_running_loop())
    try:
        yield resource
    finally:
        # 测试成功、失败或被跳过，fixture 都会从 yield 后继续做清理。
        await resource.aclose()
        assert resource.closed


@pytest_asyncio.fixture
async def model_client() -> AsyncIterator[httpx.AsyncClient]:
    """进程内 HTTP transport：验证请求/响应代码，不创建 socket。"""

    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.params["query"] == "asyncio"
        assert request.url.params["limit"] == "2"
        return httpx.Response(200, json={"answer": "使用结构化并发"})

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://model.test",
    ) as client:
        yield client
    assert client.is_closed


@pytest.fixture
def note_database(tmp_path: Path) -> Iterator[Path]:
    """同步 yield fixture：测试借用文件，yield 后由拥有者删除。"""
    database_path: Path = tmp_path / "agent-notes.sqlite3"
    initialize_notes(database_path)
    try:
        yield database_path
    finally:
        database_path.unlink(missing_ok=True)
        assert not database_path.exists()
