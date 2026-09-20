"""HTTP transport 与临时 SQLite 文件分别验证不同边界。"""

from pathlib import Path

import httpx
import pytest
from chapter27.agent_runtime import fetch_model_reply, read_note


@pytest.mark.asyncio
async def test_http_mock_transport_checks_request_and_response_model(
    model_client: httpx.AsyncClient,
) -> None:
    reply = await fetch_model_reply(
        model_client,
        {"query": "asyncio", "limit": 2},
    )

    assert reply.answer == "使用结构化并发"


def test_sqlite_uses_a_per_test_temporary_file(note_database: Path, tmp_path: Path) -> None:
    assert note_database.parent == tmp_path
    assert note_database.is_file()
    assert read_note(note_database, 1) == "pytest fixture"
    assert read_note(note_database, 999) is None
