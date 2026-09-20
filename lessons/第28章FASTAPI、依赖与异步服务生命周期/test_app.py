"""第 28 章应用内测试。

这些测试显式启动 lifespan，但仍不经过真实 socket；断连只由独立集成脚本验证。
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
import pytest
from app import QueryResponse, ToolName, create_app, current_request_id
from asgi_lifespan import LifespanManager
from fastapi import FastAPI


@asynccontextmanager
async def running_client(application: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(application) as manager:
        transport = httpx.ASGITransport(app=manager.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://in-process",
        ) as client:
            yield client


@pytest.mark.asyncio
async def test_asgi_transport_needs_explicit_lifespan_and_client_closes() -> None:
    application: FastAPI = create_app()
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://in-process") as client:
        response: Response = await client.get("/health")
    assert response.status_code == 503
    assert application.state.lifecycle_events == []

    async with running_client(application) as client:
        response: Response = await client.get("/health")
        assert response.json() == {"status": "ok", "shared_client_closed": False}
        assert not application.state.last_http_client.is_closed

    assert application.state.last_http_client.is_closed
    assert application.state.lifecycle_events[-1] == "shutdown:client-closed=True"


@pytest.mark.asyncio
async def test_query_status_and_error_boundaries() -> None:
    application: FastAPI = create_app()
    async with running_client(application) as client:
        success: Response = await client.post(
            "/query",
            json={"query": "生命周期", "tools": ["search", "weather"]},
            headers={"X-Request-ID": "test-success"},
        )
        parsed: QueryResponse = QueryResponse.model_validate(success.json())
        assert success.status_code == 200
        assert parsed.request_id == success.headers["x-request-id"] == "test-success"

        invalid: Response = await client.post(
            "/query",
            json={"query": " ", "timeout_ms": 1, "extra": "forbidden"},
        )
        locations: set[tuple[Any, ...]] = {tuple(error["loc"]) for error in invalid.json()["detail"]}
        assert invalid.status_code == 422
        assert locations == {
            ("body", "query"),
            ("body", "timeout_ms"),
            ("body", "extra"),
        }

        missing: Response = await client.post("/query", json={"query": "missing"})
        assert missing.status_code == 404

        timed_out: Response = await client.post(
            "/query",
            json={"query": "slow", "tools": ["search"], "timeout_ms": 50},
        )
        assert timed_out.status_code == 504

        internal: Response = await client.post(
            "/query",
            json={
                "query": "strict",
                "tools": ["unstable", "search"],
                "failure_mode": "strict",
            },
            headers={"X-Request-ID": "test-internal"},
        )
        assert internal.status_code == 500
        assert internal.json()["detail"] == {
            "code": "internal_service_error",
            "message": "工具编排发生内部故障",
            "request_id": "test-internal",
        }

        partial: Response = await client.post(
            "/query",
            json={
                "query": "partial",
                "tools": ["unstable", "search"],
                "failure_mode": "best_effort",
            },
        )
        assert partial.status_code == 200
        assert [result["ok"] for result in partial.json()["results"]] == [False, True]


@pytest.mark.asyncio
async def test_dependency_scope_background_order_and_stream_error_contract() -> None:
    application: FastAPI = create_app()
    async with running_client(application) as client:
        response: Response = await client.post(
            "/query",
            json={"query": "scope"},
            headers={"X-Request-ID": "function-scope"},
        )
        assert response.status_code == 200
        function_steps: Any = await application.state.trace.steps_for("function-scope")
        assert function_steps == [
            "dependency:acquired",
            "dependency:released",
            "response:body-sent",
            "background:audit-finished",
        ]

        stream: Response = await client.post(
            "/query/stream",
            json={"query": "scope", "tools": ["search", "clock"]},
            headers={"X-Request-ID": "request-scope"},
        )
        events: list[Any] = [json.loads(line) for line in stream.text.splitlines()]
        assert stream.status_code == 200
        assert [event["event"] for event in events] == [
            "start",
            "tool_result",
            "tool_result",
            "answer",
            "done",
        ]
        request_steps: Any = await application.state.trace.steps_for("request-scope")
        assert request_steps == [
            "dependency:acquired",
            "response:body-sent",
            "dependency:released",
        ]

        failed: Response = await client.post(
            "/query/stream",
            json={
                "query": "stream failure",
                "tools": ["unstable", "search"],
                "failure_mode": "strict",
            },
        )
        failed_events: list[Any] = [json.loads(line)["event"] for line in failed.text.splitlines()]
        assert failed.status_code == 200
        assert failed_events == ["start", "error", "done"]


@dataclass(slots=True)
class BarrierBackend:
    target: int = 4
    arrivals: list[tuple[str, ToolName]] = field(default_factory=list)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def call(self, tool: ToolName, query: str) -> str:
        del query
        async with self.lock:
            self.arrivals.append((current_request_id(), tool))
            if len(self.arrivals) == self.target:
                self.ready.set()
        async with asyncio.timeout(2.0):
            await self.ready.wait()
        return f"{tool}:ok"


@pytest.mark.asyncio
async def test_concurrent_requests_have_capacity_and_context_boundaries() -> None:
    backend = BarrierBackend()
    application: FastAPI = create_app(backend_factory=lambda client: backend)
    async with running_client(application) as client:
        payload: dict[str, Any] = {
            "query": "concurrent",
            "tools": ["search", "weather"],
            "max_concurrency": 2,
        }
        first_call: CoroutineType[Any, Any, Response] = client.post(
            "/query",
            json=payload,
            headers={"X-Request-ID": "test-a"},
        )
        second_call: CoroutineType[Any, Any, Response] = client.post(
            "/query",
            json=payload,
            headers={"X-Request-ID": "test-b"},
        )
        first_response: Response
        second_response: Response
        first_response, second_response = await asyncio.gather(first_call, second_call)

        first: QueryResponse = QueryResponse.model_validate(first_response.json())
        second: QueryResponse = QueryResponse.model_validate(second_response.json())
        assert backend.ready.is_set()
        assert {request_id for request_id, _ in backend.arrivals} == {"test-a", "test-b"}
        assert {first.request_id, second.request_id} == {"test-a", "test-b"}
        assert application.state.services.global_probe.max_active == 4
        assert first.max_parallel_tools == second.max_parallel_tools == 2


@pytest.mark.asyncio
async def test_def_route_and_direct_helper_use_same_worker_thread() -> None:
    application: FastAPI = create_app()
    async with running_client(application) as client:
        sync_info: Any = (await client.get("/execution/sync")).json()
        async_info: Any = (await client.get("/execution/async")).json()

        assert sync_info["route_thread"] == sync_info["helper_thread"]
        assert async_info["route_thread"] == async_info["helper_thread"]
        assert sync_info["route_thread"] != async_info["route_thread"]
