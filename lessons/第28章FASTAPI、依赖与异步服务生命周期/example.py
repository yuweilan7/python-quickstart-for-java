"""第 28 章应用内完整示例。

这里验证模型、状态码、依赖作用域、共享资源、并发请求和 JSONL 事件内容。
HTTPX ASGITransport 不经过真实网络，也不会自动触发 lifespan；真实断连另见
`network_disconnect_demo.py`。
"""

import asyncio
import json
from dataclasses import dataclass, field

import httpx
from app import QueryResponse, ToolName, create_app, current_request_id
from asgi_lifespan import LifespanManager


def print_section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass(slots=True)
class EventBarrierBackend:
    """四个调用全部到达才统一放行，用事件证明两个请求确实重叠。"""

    target: int
    arrivals: list[tuple[str, ToolName]] = field(default_factory=list)
    all_arrived: asyncio.Event = field(default_factory=asyncio.Event)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def call(self, tool: ToolName, query: str) -> str:
        del query
        async with self._lock:
            self.arrivals.append((current_request_id(), tool))
            if len(self.arrivals) == self.target:
                self.all_arrived.set()
        async with asyncio.timeout(2.0):
            await self.all_arrived.wait()
        return f"{tool} 的受控结果"


async def demonstrate_missing_lifespan() -> None:
    print_section("1. ASGITransport 不会自动启动 lifespan")
    application: FastAPI = create_app()
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://in-process") as client:
        response: Response = await client.get(
            "/health",
            headers={"X-Request-ID": "no-lifespan"},
        )
    print("未启动 lifespan 的 /health:", response.status_code, response.json())
    print("生命周期记录:", application.state.lifecycle_events)
    assert response.status_code == 503
    assert application.state.lifecycle_events == []


async def demonstrate_routes_models_and_scopes() -> None:
    print_section("2. lifespan、路由线程、模型与错误边界")
    application: FastAPI = create_app()

    async with LifespanManager(application) as manager:
        transport = httpx.ASGITransport(app=manager.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://in-process") as client:
            health: Response = await client.get(
                "/health",
                headers={"X-Request-ID": "health-1"},
            )
            print("lifespan 内共享客户端:", health.json())
            assert health.json() == {"status": "ok", "shared_client_closed": False}

            sync_info: Any = (
                await client.get(
                    "/execution/sync",
                    headers={"X-Request-ID": "thread-sync"},
                )
            ).json()
            async_info: Any = (
                await client.get(
                    "/execution/async",
                    headers={"X-Request-ID": "thread-async"},
                )
            ).json()
            print("def 路由:", sync_info)
            print("async def 路由:", async_info)
            assert sync_info["route_thread"] == sync_info["helper_thread"]
            assert async_info["route_thread"] == async_info["helper_thread"]
            assert sync_info["route_thread"] != async_info["route_thread"]

            request_payload: dict[str, Any] = {
                "query": "Agent 生命周期",
                "tools": ["search", "weather", "clock"],
                "max_concurrency": 2,
                "timeout_ms": 500,
                "failure_mode": "best_effort",
            }
            success: Response = await client.post(
                "/query",
                json=request_payload,
                headers={"X-Request-ID": "query-ok"},
            )
            parsed: QueryResponse = QueryResponse.model_validate(success.json())
            print("普通查询:", success.status_code, compact_json(success.json()))
            print("响应头 request_id:", success.headers["x-request-id"])
            assert success.status_code == 200
            assert parsed.request_id == success.headers["x-request-id"] == "query-ok"
            assert parsed.max_parallel_tools == 2

            invalid: Response = await client.post(
                "/query",
                json={"query": "  ", "timeout_ms": 20, "unknown": True},
                headers={"X-Request-ID": "query-invalid"},
            )
            print("无效输入:", invalid.status_code, compact_json(invalid.json()["detail"]))
            assert invalid.status_code == 422
            assert {tuple(error["loc"]) for error in invalid.json()["detail"]} == {
                ("body", "query"),
                ("body", "timeout_ms"),
                ("body", "unknown"),
            }

            missing: Response = await client.post(
                "/query",
                json={"query": "missing"},
                headers={"X-Request-ID": "query-missing"},
            )
            print("业务上不存在:", missing.status_code, missing.json())
            assert missing.status_code == 404

            timeout_response: Response = await client.post(
                "/query",
                json={"query": "slow", "tools": ["search"], "timeout_ms": 50},
                headers={"X-Request-ID": "query-timeout"},
            )
            print("总预算耗尽:", timeout_response.status_code, timeout_response.json())
            assert timeout_response.status_code == 504

            internal: Response = await client.post(
                "/query",
                json={
                    "query": "严格失败",
                    "tools": ["unstable", "search"],
                    "failure_mode": "strict",
                },
                headers={"X-Request-ID": "query-internal"},
            )
            print("内部故障:", internal.status_code, compact_json(internal.json()))
            assert internal.status_code == 500
            assert internal.json()["detail"]["request_id"] == "query-internal"

            partial: Response = await client.post(
                "/query",
                json={
                    "query": "允许部分结果",
                    "tools": ["unstable", "search"],
                    "failure_mode": "best_effort",
                },
                headers={"X-Request-ID": "query-partial"},
            )
            print("部分成功:", partial.status_code, compact_json(partial.json()["results"]))
            assert partial.status_code == 200
            assert [result["ok"] for result in partial.json()["results"]] == [False, True]

            services: Any = application.state.services
            assert "query-ok:Agent 生命周期" in services.audit_log
            query_steps: Any = await services.trace.steps_for("query-ok")
            print("普通响应作用域顺序:", query_steps)
            assert query_steps.index("dependency:released") < query_steps.index(
                "response:body-sent"
            )
            assert query_steps.index("response:body-sent") < query_steps.index(
                "background:audit-finished"
            )
            assert not application.state.last_http_client.is_closed

    print("lifespan 退出记录:", application.state.lifecycle_events)
    print("共享 HTTPX 客户端已关闭:", application.state.last_http_client.is_closed)
    assert application.state.last_http_client.is_closed


async def demonstrate_concurrent_requests() -> None:
    print_section("3. 两个并发请求：共享容量，不共享业务结果或 request_id")
    backend = EventBarrierBackend(target=4)
    application: FastAPI = create_app(backend_factory=lambda client: backend)

    async with LifespanManager(application) as manager:
        transport = httpx.ASGITransport(app=manager.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://in-process") as client:
            payload: dict[str, Any] = {
                "query": "并发请求",
                "tools": ["search", "weather"],
                "max_concurrency": 2,
            }
            async with asyncio.TaskGroup() as group:
                first_task: Task[Response] = group.create_task(
                    client.post(
                        "/query",
                        json=payload,
                        headers={"X-Request-ID": "concurrent-a"},
                    )
                )
                second_task: Task[Response] = group.create_task(
                    client.post(
                        "/query",
                        json=payload,
                        headers={"X-Request-ID": "concurrent-b"},
                    )
                )

            first: QueryResponse = QueryResponse.model_validate(first_task.result().json())
            second: QueryResponse = QueryResponse.model_validate(second_task.result().json())
            observed_request_ids: set[str] = {
                request_id for request_id, _ in backend.arrivals
            }
            print("Event 屏障观察到:", backend.arrivals)
            print("全局最大在途工具数:", application.state.services.global_probe.max_active)
            print("两个响应的 request_id:", first.request_id, second.request_id)
            assert backend.all_arrived.is_set()
            assert observed_request_ids == {"concurrent-a", "concurrent-b"}
            assert application.state.services.global_probe.max_active == 4
            assert {first.request_id, second.request_id} == observed_request_ids
            assert first.results is not second.results


async def demonstrate_streaming_contract() -> None:
    print_section("4. 有限 JSONL 流与响应开始后的错误事件")
    application: FastAPI = create_app()

    async with LifespanManager(application) as manager:
        transport = httpx.ASGITransport(app=manager.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://in-process") as client:
            async with client.stream(
                "POST",
                "/query/stream",
                json={"query": "流式 Agent", "tools": ["search", "clock"]},
                headers={"X-Request-ID": "stream-ok"},
            ) as response:
                events: list[Any] = [json.loads(line) async for line in response.aiter_lines()]

            print("正常流 HTTP 状态:", response.status_code)
            for event in events:
                print("  ", compact_json(event))
            assert response.status_code == 200
            assert [event["event"] for event in events] == [
                "start",
                "tool_result",
                "tool_result",
                "answer",
                "done",
            ]

            failed_stream: Response = await client.post(
                "/query/stream",
                json={
                    "query": "流开始后失败",
                    "tools": ["unstable", "search"],
                    "failure_mode": "strict",
                },
                headers={"X-Request-ID": "stream-failed"},
            )
            failed_events: list[Any] = [
                json.loads(line) for line in failed_stream.text.splitlines()
            ]
            print(
                "流内失败仍保持 HTTP 状态:",
                failed_stream.status_code,
                [event["event"] for event in failed_events],
            )
            assert failed_stream.status_code == 200
            assert [event["event"] for event in failed_events] == ["start", "error", "done"]

            stream_steps: Any = await application.state.services.trace.steps_for("stream-ok")
            print("流式响应作用域顺序:", stream_steps)
            assert stream_steps.index("response:body-sent") < stream_steps.index(
                "dependency:released"
            )
            print("说明：ASGITransport 汇集了应用输出；本段没有证明 TCP 分块或断连取消。")


async def run_examples() -> None:
    await demonstrate_missing_lifespan()
    await demonstrate_routes_models_and_scopes()
    await demonstrate_concurrent_requests()
    await demonstrate_streaming_contract()
    print("\n全部应用内实验完成；真实网络边界请运行 network_disconnect_demo.py。")


def main() -> None:
    asyncio.run(run_examples())


if __name__ == "__main__":
    main()
