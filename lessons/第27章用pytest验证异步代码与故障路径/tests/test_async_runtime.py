"""异步故障、取消、截止时间、并发、流和 ContextVar 的稳定测试。"""

import asyncio
from dataclasses import dataclass, field

import pytest
from chapter27.agent_runtime import (
    StreamState,
    ToolBusinessError,
    ToolLifecycle,
    ToolOutcome,
    ToolProgramError,
    classify_failure,
    flatten_exception_group,
    observe_request_context,
    read_first_token,
    request_id_var,
    run_optional_tools,
    run_required_tools,
    run_required_tools_before,
    run_with_limit,
    wait_for_tool,
)
from conftest import LoopBoundResource


async def wait_until_set(*events: asyncio.Event) -> None:
    """一秒只是防悬挂护栏；测试顺序由 Event 建立，不靠这段时间碰运气。"""
    async with asyncio.timeout(1.0):
        for event in events:
            await event.wait()


@dataclass(slots=True)
class ControlledTool:
    value: str
    error: Exception | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    cleaned: asyncio.Event = field(default_factory=asyncio.Event)
    was_cancelled: bool = False

    async def __call__(self) -> str:
        self.started.set()
        try:
            await self.release.wait()
            if self.error is not None:
                raise self.error
            return self.value
        except asyncio.CancelledError:
            self.was_cancelled = True
            raise
        finally:
            self.cleaned.set()


@pytest.mark.asyncio
async def test_pytest_owns_running_loop(loop_resource: LoopBoundResource) -> None:
    running_loop = asyncio.get_running_loop()

    assert loop_resource.created_in is running_loop
    assert not loop_resource.closed


@pytest.mark.asyncio
async def test_cancellation_waits_for_start_then_checks_finally() -> None:
    lifecycle = ToolLifecycle()
    task = asyncio.create_task(wait_for_tool(lifecycle), name="cancel-cleanup-test")

    await wait_until_set(lifecycle.started)
    task.cancel("测试主动取消")

    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(1.0):
            await task

    assert lifecycle.cleaned.is_set()
    assert lifecycle.events == ["started", "cleaned"]
    assert task.cancelled()


@pytest.mark.asyncio
async def test_optional_tools_keep_expected_partial_failure() -> None:
    search = ControlledTool("docs")
    record = ControlledTool("unused", ToolBusinessError("记录不存在"))
    tools: dict[str, ControlledTool] = {"search": search, "record": record}
    request = asyncio.create_task(run_optional_tools(tools))

    await wait_until_set(search.started, record.started)
    search.release.set()
    record.release.set()
    outcomes: list[ToolOutcome] = await request

    assert [outcome.ok for outcome in outcomes] == [True, False]
    assert outcomes[0].value == "docs"
    assert outcomes[1].error == "记录不存在"
    assert search.cleaned.is_set() and record.cleaned.is_set()


@pytest.mark.asyncio
async def test_required_failure_cancels_siblings_and_preserves_group() -> None:
    waiting_a = ControlledTool("never-a")
    failing = ControlledTool("never-failure", ToolProgramError("响应协议损坏"))
    waiting_b = ControlledTool("never-b")
    tools: dict[str, ControlledTool] = {"a": waiting_a, "failure": failing, "b": waiting_b}
    request = asyncio.create_task(run_required_tools(tools))

    await wait_until_set(waiting_a.started, failing.started, waiting_b.started)
    failing.release.set()

    with pytest.raises(ExceptionGroup) as raised:
        async with asyncio.timeout(1.0):
            await request

    leaves: list[BaseException] = flatten_exception_group(raised.value)
    assert len(leaves) == 1
    assert isinstance(leaves[0], ToolProgramError)
    assert str(leaves[0]) == "响应协议损坏"
    assert waiting_a.was_cancelled and waiting_b.was_cancelled
    assert all(tool.cleaned.is_set() for tool in (waiting_a, failing, waiting_b))


@pytest.mark.asyncio
async def test_whole_deadline_cancels_and_cleans_pending_tool() -> None:
    pending = ControlledTool("never")
    loop = asyncio.get_running_loop()
    request = asyncio.create_task(
        run_required_tools_before(
            {"pending": pending},
            deadline=loop.time() + 0.2,
        )
    )

    await wait_until_set(pending.started)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(1.0):
            await request

    assert pending.was_cancelled
    assert pending.cleaned.is_set()


@dataclass(slots=True)
class ConcurrencyProbe:
    expected_peak: int
    active: int = 0
    max_active: int = 0
    peak_reached: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def call(self, item: int) -> int:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.active == self.expected_peak:
            self.peak_reached.set()
        try:
            await self.release.wait()
            return item * 10
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_concurrency_limit_uses_observed_peak_not_elapsed_time() -> None:
    probe = ConcurrencyProbe(expected_peak=2)
    request = asyncio.create_task(run_with_limit(range(6), max_in_flight=2, operation=probe.call))

    await wait_until_set(probe.peak_reached)
    assert probe.active == 2
    assert probe.max_active == 2
    probe.release.set()
    results: list[int] = await request

    assert results == [0, 10, 20, 30, 40, 50]
    assert probe.max_active == 2
    assert probe.active == 0


@pytest.mark.asyncio
async def test_early_stream_exit_closes_owned_generator() -> None:
    state = StreamState()

    first: str = await read_first_token(state)

    assert first == "agent"
    assert state.closed.is_set()
    assert state.close_count == 1
    assert state.events == ["stream-open", "stream-closed"]


@pytest.mark.asyncio
async def test_contextvar_values_do_not_cross_requests() -> None:
    barrier = asyncio.Barrier(2)

    first, second = await asyncio.gather(
        observe_request_context("req-A", barrier),
        observe_request_context("req-B", barrier),
    )

    assert first == ("req-A", "req-A")
    assert second == ("req-B", "req-B")
    assert request_id_var.get() == "-"


def test_exception_group_leaves_are_classified_by_meaning() -> None:
    grouped = BaseExceptionGroup(
        "request failures",
        [
            ToolBusinessError("输入不可用"),
            ExceptionGroup("nested", [TimeoutError("整体超时"), ValueError("程序缺陷")]),
            asyncio.CancelledError("用户停止"),
        ],
    )

    categories: list[str] = [classify_failure(error) for error in flatten_exception_group(grouped)]

    assert categories == ["business", "timeout", "program-defect", "cancelled"]
