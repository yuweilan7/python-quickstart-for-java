"""第 23 章：并发上限、Queue、背压与共享状态。

所有“查询”都是纯内存替身。Event 控制关键阶段，sleep(0) 只提供一次事件循环调度点；
示例不访问真实网络，也不依赖某段墙钟时间内恰好完成多少工作。
"""

# 阅读顺序：先看 Semaphore 如何约束“已经进入查询”的 Task，再看 Queue 如何把“尚未领取”
# 的工作限制在固定容量内，最后用一个确定性实验观察跨 await 的共享状态竞争。
# 数据主线都是 Record -> 替身 query -> QueryResult；assert 负责把并发与清理规则变成可验证结果。

# asyncio 提供协程、Task、TaskGroup 以及只供事件循环 Task 使用的同步原语。
# dataclass 用来生成保存输入、结果和指标的轻量对象；field 在每个实例构造时创建独立的可变字段。
import asyncio
from dataclasses import dataclass, field

# 这三个常量分别约束不同位置：Semaphore 的在途调用、Queue 中等待领取的记录、长期 worker 数。
# 数值相同不代表含义相同；修改其中一个时，不应默认另外两个也要随之变化。
MAX_IN_FLIGHT = 3
QUEUE_CAPACITY = 4
WORKER_COUNT = 3


# 三种异常把“可继续的单项业务失败”与“必须让整个 TaskGroup 失败”的基础设施错误分开。
# worker 只会捕获第一种；后两种保持向外传播，由 TaskGroup 联动取消兄弟 Task。
class RecordNotFound(Exception):
    """一次可预期的业务失败：本批次可以继续处理其他记录。"""


class WorkerCrashed(RuntimeError):
    """worker 内部出现未知程序错误：整个任务组必须失败并清理。"""


class ProducerCrashed(RuntimeError):
    """生产者无法继续枚举输入：整个任务组必须失败并清理。"""


# dataclass 类似减少了 Java record/POJO 的样板代码，会按字段生成初始化和比较等方法。
# frozen=True 禁止普通字段重新赋值，slots=True 不再为每个实例创建任意扩展属性的 __dict__；
# 它们是 Python 运行时的数据模型选项，而下面的 int/str 类型提示主要仍由类型检查器检查。
@dataclass(frozen=True, slots=True)
class Record:
    # record_id 用于稳定匹配结果和故障开关；query 模拟已经准备好的查询文本。
    record_id: int
    query: str


@dataclass(frozen=True, slots=True)
class QueryResult:
    # str | None 表示“字符串或 None”。成功时 value 有值而 error 为 None，失败时正好相反。
    record_id: int
    value: str | None
    error: str | None

    # @property 让调用者写 result.ok，而不是 result.ok()；每次访问都会执行下面的判断。
    @property
    def ok(self) -> bool:
        # None 是单例，用 is 判断身份；不要照搬 Java 写成 error == null。
        return self.error is None


@dataclass(slots=True)
class ConcurrencyProbe:
    """记录替身查询的在途数量，并用 Event 构造可重复的并发阶段。"""

    # expected_peak 是本次实验必须会合的调用数；达到前所有已进入的 query 都会等待。
    expected_peak: int
    # 传入额外 Event 时，即使达到峰值也继续暂停，供“父任务取消”实验精确控制时机。
    hold_after_peak: asyncio.Event | None = None
    # active 是当前尚未离开 query 的数量，max_active 保存整个实验见过的峰值。
    active: int = 0
    max_active: int = 0
    # started_count/cleaned_count 和 ID 列表用于核对开始、清理及实际处理过的记录。
    started_count: int = 0
    cleaned_count: int = 0
    # default_factory=list 会为每个 ConcurrencyProbe 实例调用一次 list()；避免多个实例共享同一列表。
    started_ids: list[int] = field(default_factory=list)
    # asyncio.Event 同理必须逐实例创建。它是“一旦置位即可放行所有等待者”的阶段信号，不是锁。
    peak_reached: asyncio.Event = field(default_factory=asyncio.Event)

    # async def 调用后先得到 coroutine 对象；由 await 或 Task 驱动时，函数体才真正向前执行。
    # record 是本次输入；星号后的两个故障 ID 必须以关键字传入，None 表示不启用该故障。
    async def query(
        self,
        record: Record,
        *,
        business_failure_id: int | None = None,
        crash_id: int | None = None,
    ) -> str:
        # 这些计数更新之间没有 await；在同一个事件循环线程里不会被别的 Task 插入。
        # 这只保证这一小段连续执行，不代表整个 async 函数或任意共享操作天然是原子的。
        self.active += 1
        self.started_count += 1
        self.started_ids.append(record.record_id)
        self.max_active = max(self.max_active, self.active)
        try:
            # 超限立即把测试判为失败；等于目标值时 set()，唤醒所有等待 peak_reached 的 Task。
            if self.active > self.expected_peak:
                raise AssertionError(f"在途查询超过上限：{self.active}")
            if self.active == self.expected_peak:
                self.peak_reached.set()

            # 前 expected_peak 个调用会在这里会合，保证实验确实观察到峰值，而非猜调度顺序。
            # Event 一旦 set，当前和以后调用 wait() 的 Task 都可继续；它不会一次只放行一个。
            await self.peak_reached.wait()
            # is not None 明确区分“没有第二道闸门”和“传入了一个尚未 set 的 Event”。
            if self.hold_after_peak is not None:
                await self.hold_after_peak.wait()

            # 代表一次会把控制权交回事件循环的 I/O；0 表示不用墙钟延迟做正确性判断。
            await asyncio.sleep(0)
            # 两个比较按 record_id 注入确定性结果：业务失败会被上层转成数据，崩溃则向外传播。
            if record.record_id == business_failure_id:
                raise RecordNotFound(f"记录 {record.record_id} 不存在")
            if record.record_id == crash_id:
                raise WorkerCrashed(f"处理记录 {record.record_id} 时解析器崩溃")
            return f"记录 {record.record_id} 的结果"
        finally:
            # 真实客户端应在这里归还连接或关闭响应体；取消和异常也必须经过这里。
            # finally 覆盖正常 return、两类异常以及 await 处收到的 CancelledError。
            self.active -= 1
            self.cleaned_count += 1


@dataclass(slots=True)
class QueueMetrics:
    # 这些字段分别观察容量、背压以及 worker/Queue 的生命周期；默认值使每次运行从 0 开始。
    max_queued: int = 0
    backpressure_waits: int = 0
    workers_started: int = 0
    workers_exited: int = 0
    graceful_shutdowns: int = 0
    immediate_shutdowns: int = 0


@dataclass(slots=True)
class Counter:
    # 可变计数器专门用于最后的竞争实验；两个 Task 会持有同一个 Counter 实例。
    value: int = 0


# 普通 def 会立即执行，不会产生 coroutine；-> None 表示函数只负责输出，没有有意义的返回值。
def print_section(title: str) -> None:
    # f-string 在运行时计算花括号中的 title；开头的 \n 先输出一个空行分隔实验。
    print(f"\n=== {title} ===")


def make_records(count: int) -> list[Record]:
    # 列表推导式遍历 range(0, count)，每轮用关键字参数创建一个 Record，最终返回完整 list。
    # 它更接近 Java Stream 的 map(...).toList()，但这里是立即求值，不是惰性输入流。
    return [Record(record_id=index, query=f"查询-{index}") for index in range(count)]


def explain_independent_limits() -> None:
    # 本段只建立术语边界：在途、单位时间请求、排队和连接占用是四个可独立配置的维度。
    print_section("1. 四种限制解决四个不同问题")
    print("  在途并发上限：同一时刻最多多少次调用尚未结束")
    print("  请求速率上限：某个时间窗口最多允许多少次开始请求")
    print("  Queue 容量：最多允许多少项尚未被 worker 取走")
    print("  HTTP 连接池：客户端最多建立或借出多少连接，并负责连接复用")
    print("  结论：Semaphore 只管同时占用许可的数量，不会等待下一个时间窗口。")


async def hold_one_permit(
    limit: asyncio.Semaphore,
    entered: asyncio.Event,
    cleaned: asyncio.Event,
) -> None:
    # async with 会先 await limit.acquire()，进入后独占这唯一许可，退出时自动 release()。
    async with limit:
        # set() 本身不暂停：它通知父任务“许可已经拿到”，然后本 Task 继续向下运行。
        entered.set()
        try:
            # 这个临时 Event 永远不会被 set，因此 wait() 会一直挂起；实验只通过 cancel() 结束它。
            await asyncio.Event().wait()
        finally:
            # 取消从 wait() 抛入后先执行这里，再离开 async with 并归还 Semaphore 许可。
            cleaned.set()


async def wait_for_one_permit(
    limit: asyncio.Semaphore,
    attempting: asyncio.Event,
    entered: asyncio.Event,
) -> None:
    # 先通知父任务“马上要申请许可”，便于父任务确定取消发生在等待 acquire 的阶段。
    attempting.set()
    async with limit:
        # 只有 acquire 真正成功才会执行；等待期间取消时，这个 Event 应始终未设置。
        entered.set()


async def demonstrate_semaphore_cancellation() -> None:
    print_section("2. Semaphore：等待许可和持有许可时被取消都不泄漏")
    # 许可数为 1，先启动的 holder 会让后启动的 waiter 必然停在 acquire，而不是碰运气竞争。
    limit = asyncio.Semaphore(1)
    # 每个 Event 都表示一个单向阶段事实；名称说明由谁设置、父任务等待观察什么。
    holder_entered = asyncio.Event()
    holder_cleaned = asyncio.Event()
    waiter_attempting = asyncio.Event()
    waiter_entered = asyncio.Event()

    # TaskGroup 是这些 Task 的结构化所有者：离开作用域前会等待它们全部结束。
    async with asyncio.TaskGroup() as group:
        # create_task 把 coroutine 纳入事件循环调度，并返回可取消、可等待的 Task 句柄。
        holder: Task[None] = group.create_task(
            hold_one_permit(limit, holder_entered, holder_cleaned),
            name="permit-holder",
        )
        # 等 holder 明确发出“已进入”信号，下面才创建 waiter，避免依赖创建先后猜调度结果。
        await holder_entered.wait()

        waiter: Task[None] = group.create_task(
            wait_for_one_permit(limit, waiter_attempting, waiter_entered),
            name="permit-waiter",
        )
        # attempting 在 acquire 前设置；当前许可仍由 holder 占用，所以 waiter 随后必定等待。
        await waiter_attempting.wait()
        # cancel() 只是发出取消请求；继续 await 才能观察 CancelledError 并确认清理已经结束。
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            # 本例有意消费已确认的子 Task 取消；生产代码不应无意吞掉当前父 Task 的取消。
            pass

        # waiter 没进入上下文体，就没有获取许可，也不存在需要它自行释放的许可。
        assert not waiter_entered.is_set()
        # 再取消已经持有许可的 holder，验证 async with 的退出路径会自动归还许可。
        holder.cancel()
        try:
            await holder
        except asyncio.CancelledError:
            pass

    # TaskGroup 已等待两个 Task 完整退出；此时检查 finally 的副作用不会再与子 Task 并发变化。
    assert holder_cleaned.is_set()
    # holder 的 async with 已在取消展开时释放许可；这里应能立刻重新进入。
    async with limit:
        reacquired = True

    # 三个输出依次证明：等待者未进入、持有者已清理、同一个 Semaphore 仍可使用。
    print("  等待中的 Task 未误入临界区：", not waiter_entered.is_set())
    print("  持有者取消后执行 finally：", holder_cleaned.is_set())
    print("  取消清理后可以重新获取许可：", reacquired)


async def query_with_limit(
    record: Record,
    limit: asyncio.Semaphore,
    probe: ConcurrencyProbe,
    *,
    business_failure_id: int | None,
) -> QueryResult:
    # 许可尽量贴近真正受限的 I/O；参数准备和结果格式化不必占用稀缺许可。
    # 每个记录 Task 都会调用本函数，但同时最多 MAX_IN_FLIGHT 个 Task 能进入 probe.query。
    try:
        async with limit:
            value: str = await probe.query(record, business_failure_id=business_failure_id)
    except RecordNotFound as error:
        # 只把有恢复约定的业务异常转成 QueryResult；未知异常和取消仍交给 TaskGroup 处理。
        # str(error) 取异常消息，value=None 与 error 字符串共同表示这一项失败。
        return QueryResult(record.record_id, value=None, error=str(error))
    # try 正常完成时 value 已绑定为查询字符串；构造成功结果时 error 明确为 None。
    return QueryResult(record.record_id, value=value, error=None)


async def run_small_semaphore_batch(
    records: list[Record],
) -> tuple[list[QueryResult], ConcurrencyProbe, int]:
    # 返回的是三元组：（按输入 Task 顺序的结果列表，探针对象，实际创建的记录 Task 数）。
    limit = asyncio.Semaphore(MAX_IN_FLIGHT)
    # 非空小批次不足 3 项时，等待实际可达到的峰值，不能让替身自己制造死锁。
    expected_peak: int = max(1, min(MAX_IN_FLIGHT, len(records)))
    probe = ConcurrencyProbe(expected_peak=expected_peak)

    async with asyncio.TaskGroup() as group:
        # TaskGroup 管生命周期；Semaphore 管进入 query 的并发数。
        # 因此 N 条记录仍会在这里创建 N 个 Task。
        # 推导式会立刻为 records 中每条记录调用 create_task；Semaphore 不会推迟 Task 的创建。
        # tasks 保存句柄，是为了等 TaskGroup 成功退出后按创建顺序读取每个 result()。
        tasks: list[Task[QueryResult]] = [
            group.create_task(
                query_with_limit(
                    record,
                    limit,
                    probe,
                    business_failure_id=2,
                ),
                name=f"small-batch-{record.record_id}",
            )
            for record in records
        ]

    # 走到这里表示 TaskGroup 已成功等待所有任务；result() 此时不会阻塞，只取已完成的返回值。
    return [task.result() for task in tasks], probe, len(tasks)


async def demonstrate_small_semaphore_batch() -> None:
    print_section("3. 小批量 + Semaphore：限制在途，但 Task 已全部创建")
    records: list[Record] = make_records(9)
    results: list[QueryResult]
    probe: ConcurrencyProbe
    created_tasks: int
    results, probe, created_tasks = await run_small_semaphore_batch(records)

    # 这组断言同时验证“Task 数没有受限”“查询并发受限”“finally 清零在途计数”。
    assert created_tasks == len(records)
    assert probe.max_active == MAX_IN_FLIGHT
    assert probe.active == 0
    # bool 在求和中按 1/0 计算，因此这里是在统计 ok 为 True 的结果数量。
    assert sum(result.ok for result in results) == 8
    print(f"  输入={len(records)}，创建的记录 Task={created_tasks}")
    print(f"  在途上限={MAX_IN_FLIGHT}，观察到的最大在途={probe.max_active}")
    print("  业务结果：成功 8，失败 1；批次没有因可预期失败而中止")


async def queue_worker(
    worker_id: int,
    queue: asyncio.Queue[Record],
    own_results: list[QueryResult],
    probe: ConcurrencyProbe,
    metrics: QueueMetrics,
    *,
    business_failure_id: int | None,
    crash_id: int | None,
) -> None:
    """一个固定 worker；own_results 只由这个 worker 写，退出后交还父作用域。"""
    # worker_id 只用于日志；queue 传递待处理 Record；own_results 是当前 worker 独占的结果列表。
    # probe/metrics 记录可观测状态；两个可空 ID 决定替身在哪条记录注入业务失败或程序崩溃。
    metrics.workers_started += 1
    try:
        # worker 长期存在，不为每条记录新建 Task；正常和紧急 shutdown 都通过异常结束此循环。
        while True:
            try:
                # Queue 为空时 get() 会暂停；成功返回后 record 已从“排队”转为此 worker 的在途工作。
                record: Record = await queue.get()
            except asyncio.QueueShutDown:
                # 正常 shutdown 后，队列排空；或紧急 shutdown 后，worker 被唤醒。
                # get() 没有交付项目，所以这个分支绝不能调用 task_done()。
                return

            # 外层 try/finally 从成功 get 的这一刻开始，确保无论处理怎样结束都完成一次 Queue 记账。
            try:
                try:
                    value: str = await probe.query(
                        record,
                        business_failure_id=business_failure_id,
                        crash_id=crash_id,
                    )
                except RecordNotFound as error:
                    # 只转换已定义恢复语义的业务错误；WorkerCrashed 和取消继续传播。
                    # append 之前没有 await，且此列表没有其他写入者，因此不需要 Lock。
                    own_results.append(
                        QueryResult(record.record_id, value=None, error=str(error))
                    )
                else:
                    # try 的 else 只在没有异常时执行，可保证 value 已由 await 的返回值成功绑定。
                    own_results.append(
                        QueryResult(record.record_id, value=value, error=None)
                    )
            finally:
                # 每次成功 get 都恰好对应一次 task_done，包括业务失败、程序异常和取消。
                # task_done() 不会删除元素；它递减 put() 建立的“未完成工作”计数，使 join() 可返回。
                queue.task_done()
    finally:
        # 这一层覆盖 shutdown、WorkerCrashed 和 TaskGroup 取消，用于证明每个长期 worker 都已回收。
        metrics.workers_exited += 1
        print(f"  worker-{worker_id} 已退出")


async def produce_then_join(
    queue: asyncio.Queue[Record],
    records: list[Record],
    metrics: QueueMetrics,
    *,
    producer_fail_after: int | None,
) -> None:
    """生产者兼正常关闭协调者；异常路径用紧急关闭解除 Queue 等待。"""
    # False 表示尚未完成“正常关闭 + 所有已入队项目记账归零”的整套协议。
    # 不能只看是否调用过 shutdown，因为等待 join 时仍可能发生 worker 崩溃或用户取消。
    joined_normally = False
    try:
        # enumerate 同时产生从 0 开始的索引和当前 Record，类似带计数器的 Java for-each。
        for index, record in enumerate(records):
            # 这个开关模拟流式输入源在读取下一项前损坏；None 表示不注入故障。
            if producer_fail_after is not None and index == producer_fail_after:
                raise ProducerCrashed(f"读取到第 {index} 项时输入源损坏")

            if queue.full():
                # full() 与紧接着的 put() 之间没有 await；put 会在满队列上形成真实背压。
                # 这里的计数只证明本例观察过满队列；真实正确性仍来自 await put() 自身的等待语义。
                metrics.backpressure_waits += 1
            # Queue 满时生产者在此暂停，直到某个 worker get() 腾出位置；记录不会被静默丢弃。
            await queue.put(record)
            # qsize() 是此刻快照。当前到 max() 之间没有 await，适合更新教学指标；
            # 若先 await 再用这个值做并发协议，其他 Task 可能已改变队列，结论就不再成立。
            metrics.max_queued = max(metrics.max_queued, queue.qsize())

        # 不再接收新工作，但允许 worker 取完已入队项目；空批次也会直接唤醒 worker。
        queue.shutdown()
        metrics.graceful_shutdowns += 1
        # join() 等未完成计数归零，不负责等待 worker 退出；worker 由上面的 shutdown 得到退出信号。
        await queue.join()
        # 只有 join 确实返回才标记正常完成，使 finally 能区分成功与任意中断路径。
        joined_normally = True
    finally:
        if not joined_normally:
            # worker 崩溃、生产者崩溃或用户取消时，不等待“处理完所有业务”。
            # immediate=True 会丢弃尚未领取的项目，所以它是放弃路径，不是成功证明。
            # 紧急关闭还能唤醒正等待 put()/get()/join() 的相关 Task，让 TaskGroup 有限时收尾。
            queue.shutdown(immediate=True)
            metrics.immediate_shutdowns += 1


async def run_worker_queue(
    records: list[Record],
    *,
    business_failure_id: int | None = None,
    crash_id: int | None = None,
    producer_fail_after: int | None = None,
    probe: ConcurrencyProbe | None = None,
    metrics: QueueMetrics | None = None,
) -> tuple[list[QueryResult], ConcurrencyProbe, QueueMetrics, int]:
    """拥有 Queue、TaskGroup 和结果聚合；成功退出后才把结果交给调用者。"""
    # Queue[Record] 是静态类型信息；maxsize=4 才是运行时容量。默认 maxsize=0 反而代表无限容量。
    queue: asyncio.Queue[Record] = asyncio.Queue(maxsize=QUEUE_CAPACITY)
    # 空批次没有查询，仍给探针一个不会被使用的最小正目标；非空批次目标不超过 worker 或记录数。
    expected_peak: int = max(1, min(WORKER_COUNT, len(records)))
    # 条件表达式“已有对象 if 非空 else 新建对象”支持测试注入，并避免把可变实例写成默认参数。
    # 判断必须用 is not None：对象是否存在是身份语义，不应依赖对象未来可能定义的真假规则。
    run_probe: ConcurrencyProbe = (
        probe if probe is not None else ConcurrencyProbe(expected_peak)
    )
    run_metrics: QueueMetrics = metrics if metrics is not None else QueueMetrics()
    # 外层列表按 worker 编号保存结果；内层 [] 在每轮重新创建，三个 worker 不会共享同一个列表。
    per_worker_results: list[list[QueryResult]] = [[] for _ in range(WORKER_COUNT)]

    # TaskGroup 同时拥有生产/协调者与全部 worker。任一成员抛非取消异常时，它会取消其余成员，
    # 等所有成员的 finally 执行完，再向本函数调用者抛出 ExceptionGroup。
    async with asyncio.TaskGroup() as group:
        # 只有 1 个生产/协调 Task 和固定数量 worker，不随 records 数量增长。
        # 这里不保存生产者句柄，因为生命周期和异常都由 TaskGroup 管理，不需要单独读取其结果。
        group.create_task(
            produce_then_join(
                queue,
                records,
                run_metrics,
                producer_fail_after=producer_fail_after,
            ),
            name="producer-and-joiner",
        )
        # enumerate 把（索引，当前内层列表）解包到两个变量；每个 worker 获得不同的 own_results。
        for worker_id, own_results in enumerate(per_worker_results):
            group.create_task(
                queue_worker(
                    worker_id,
                    queue,
                    own_results,
                    run_probe,
                    run_metrics,
                    business_failure_id=business_failure_id,
                    crash_id=crash_id,
                ),
                name=f"queue-worker-{worker_id}",
            )

    # TaskGroup 成功退出后，不再有 worker 修改这些列表；父作用域此时取得聚合所有权。
    # 双层生成器先依次展开每个 worker 的结果，再由 sorted 按 record_id 恢复输入顺序；
    # lambda 是只含一个表达式的匿名函数，这里告诉 sorted 从每个 QueryResult 取哪个排序键。
    results: list[QueryResult] = sorted(
        (result for own_results in per_worker_results for result in own_results),
        key=lambda result: result.record_id,
    )
    # 基础设施 Task 始终为 1 个生产者加固定 worker 数，不随 len(records) 增长。
    infrastructure_tasks: Literal[4] = 1 + WORKER_COUNT
    # 返回结果之外也交回探针和指标，调用者可在所有 Task 停止后检查完整生命周期。
    return results, run_probe, run_metrics, infrastructure_tasks


async def demonstrate_worker_queue() -> None:
    print_section("4. 有界 Queue + 固定 worker：24 条批量查询与背压")
    # 24 条输入远大于 4 个排队槽和 3 个 worker，足以稳定触发生产者背压。
    records: list[Record] = make_records(24)
    # record_id=7 的预期失败会变成 QueryResult.error，不会让 TaskGroup 失败。
    results: list[QueryResult]
    probe: ConcurrencyProbe
    metrics: QueueMetrics
    task_count: int
    results, probe, metrics, task_count = await run_worker_queue(
        records,
        business_failure_id=7,
    )

    # 断言分四组核对：结果完整性、在途上限、Queue 容量/背压、正常关闭与 worker 回收。
    assert len(results) == len(records)
    assert sum(result.ok for result in results) == 23
    assert probe.max_active == WORKER_COUNT
    assert probe.active == 0
    assert metrics.max_queued <= QUEUE_CAPACITY
    assert metrics.backpressure_waits > 0
    assert metrics.workers_started == metrics.workers_exited == WORKER_COUNT
    assert metrics.graceful_shutdowns == 1
    assert metrics.immediate_shutdowns == 0
    print(f"  输入={len(records)}，长期存在的基础设施 Task={task_count}")
    print(f"  Queue maxsize={QUEUE_CAPACITY}，观察到的最大排队={metrics.max_queued}")
    print(f"  生产者因满队列等待的次数={metrics.backpressure_waits}")
    print(f"  worker={WORKER_COUNT}，观察到的最大在途={probe.max_active}")
    print("  业务结果：成功 23，失败 1；三个 worker 均正常退出")


async def demonstrate_empty_batch() -> None:
    print_section("5A. 空批次：关闭空 Queue，所有 worker 都会退出")
    # 空列表仍启动固定基础设施，但生产者立即 shutdown；下划线表示不使用返回的 Task 数。
    results: list[QueryResult]
    probe: ConcurrencyProbe
    metrics: QueueMetrics
    results, probe, metrics, _ = await run_worker_queue([])
    # 没有任何 Record 进入 query，三个阻塞在 get() 的 worker 仍应被正常关闭并退出。
    assert results == []
    assert probe.started_count == 0
    assert metrics.workers_exited == WORKER_COUNT
    print("  结果为空，实际查询次数=0，退出 worker 数=3")


async def demonstrate_worker_crash() -> None:
    print_section("5B. worker 意外异常：TaskGroup 取消同伴，紧急关闭 Queue")
    # 显式创建并注入状态对象，即使 run_worker_queue 抛异常，调用者仍可在 except* 后检查清理结果。
    metrics = QueueMetrics()
    probe = ConcurrencyProbe(WORKER_COUNT)
    caught = 0
    try:
        await run_worker_queue(
            make_records(12),
            crash_id=1,
            probe=probe,
            metrics=metrics,
        )
    # TaskGroup 以 ExceptionGroup 抛出成员失败；except* 只匹配其中 WorkerCrashed 类型的叶子异常。
    # group.exceptions 是本次匹配到的子异常序列，这里预期只有 crash_id=1 产生的一项。
    except* WorkerCrashed as group:
        caught: int = len(group.exceptions)

    # active 回到 0 证明在途 query 都跑过 finally；worker 退出数和紧急关闭数证明基础设施已清理。
    assert caught == 1
    assert probe.active == 0
    assert metrics.workers_exited == WORKER_COUNT
    assert metrics.immediate_shutdowns == 1
    print("  捕获 WorkerCrashed 叶子数：", caught)
    print("  取消清理后 active=0，紧急关闭次数=1")


async def demonstrate_producer_crash() -> None:
    print_section("5C. 生产者异常：不等待哨兵或 join，整体有限时退出")
    # 与 worker 崩溃相反，这次错误来自生产者；TaskGroup 会取消仍在 get/query 中的 worker。
    metrics = QueueMetrics()
    probe = ConcurrencyProbe(WORKER_COUNT)
    caught = 0
    try:
        await run_worker_queue(
            make_records(12),
            producer_fail_after=4,
            probe=probe,
            metrics=metrics,
        )
    # except* 从异常组提取生产者故障；未匹配的异常若存在仍会继续向外传播。
    except* ProducerCrashed as group:
        caught: int = len(group.exceptions)

    # 不完整输入不能形成成功结果；这里只验证异常传播和所有清理路径已经收束。
    assert caught == 1
    assert probe.active == 0
    assert metrics.workers_exited == WORKER_COUNT
    assert metrics.immediate_shutdowns == 1
    print("  捕获 ProducerCrashed 叶子数：", caught)
    print("  未要求生产者在失败后继续投递哨兵，所有 worker 已回收")


async def demonstrate_user_cancellation() -> None:
    print_section("5D. 用户取消：取消父任务，清理 worker 和在途调用")
    # hold_queries 永远不 set：三个 worker 达到峰值后会统一停在第二道 Event，等待外部取消。
    hold_queries = asyncio.Event()
    # 第二个位置参数绑定 hold_after_peak；expected_peak=3 确保取消前恰好有三个在途查询。
    probe = ConcurrencyProbe(WORKER_COUNT, hold_after_peak=hold_queries)
    metrics = QueueMetrics()
    # 把整个 worker 池包装成一个“父 Task”，模拟调用方取消一次请求。
    # Python 3.14 的 eager_start=False 明确让新 Task 先进入事件循环待调度，而非在 create_task
    # 调用中立刻执行 coroutine；后面的 Event 同样保证实验不依赖具体调度顺序。
    parent: Task[tuple[list[QueryResult], ConcurrencyProbe, QueueMetrics, int]] = (
        asyncio.create_task(
            run_worker_queue(
                make_records(12),
                probe=probe,
                metrics=metrics,
            ),
            name="cancelled-queue-request",
            eager_start=False,
        )
    )

    # 等三个 query 都已登记 active 后再取消，才能验证“持有在途资源时取消”的清理路径。
    await probe.peak_reached.wait()
    parent.cancel()
    try:
        # cancel() 不等于已经取消完成；await parent 会等待其 TaskGroup 取消 worker 并运行所有 finally。
        await parent
    except asyncio.CancelledError:
        # 记录调用方确实观察到取消。这里捕获是为了继续做断言，而不是把请求伪装成成功结果。
        cancelled_propagated = True
    else:
        raise AssertionError("用户取消不应被转换成一次成功的空结果")

    # cancelled() 核对最终 Task 状态；其余计数证明三个在途 query 和三个 worker 都已完成清理。
    assert parent.cancelled()
    assert cancelled_propagated
    assert probe.active == 0
    assert probe.cleaned_count == WORKER_COUNT
    assert metrics.workers_exited == WORKER_COUNT
    assert metrics.immediate_shutdowns == 1
    print("  调用方仍收到 CancelledError：", cancelled_propagated)
    print("  在途清理数=3，退出 worker 数=3，active=0")


async def one_item_worker(
    queue: asyncio.Queue[str],
    started: asyncio.Event,
    processed: asyncio.Event,
) -> None:
    # 这个最小 worker 专门演示：处理一项并正确 task_done 后，循环仍会回到下一次 get() 等待。
    started.set()
    while True:
        try:
            item: str = await queue.get()
        except asyncio.QueueShutDown:
            # shutdown 才是生命周期退出信号；join() 返回不会自动走到这个分支。
            return
        try:
            # assert 固定本实验只处理预期字符串；set() 通知父任务业务代码已经执行。
            assert item == "work"
            processed.set()
        finally:
            # 只要 get 成功，每项都在 finally 中恰好记账一次。
            queue.task_done()


async def demonstrate_queue_accounting_bugs() -> None:
    print_section("6. task_done/join 记账，以及 join 不负责关闭 worker")

    # 不传 maxsize 等价于 maxsize=0，表示无界 Queue；此处只放一项，容量不是实验重点。
    forgotten: asyncio.Queue[str] = asyncio.Queue()
    # put 让 Queue 的未完成计数加 1；get 只取走项目，并不会把这个计数减回 0。
    await forgotten.put("work")
    assert await forgotten.get() == "work"
    async with asyncio.TaskGroup() as group:
        # join coroutine 被包装为 Task，父任务才能在它等待期间检查 done() 状态并补上遗漏记账。
        stuck_join: Task[None] = group.create_task(
            forgotten.join(), name="stuck-join-demo"
        )
        # 显式让 join Task 运行到等待点；不是等待某个毫秒数或猜测调度先后。
        await asyncio.sleep(0)
        assert not stuck_join.done()
        print("  教学反例：get 后漏掉 task_done，join 仍在等待：", not stuck_join.done())
        # 这次 task_done 与前面的唯一一次 get 配对，未完成计数归零后 stuck_join 会被唤醒。
        forgotten.task_done()  # 修复记账，让受控反例可以继续退出。
    # TaskGroup 退出意味着它已等待 stuck_join 真正结束，不会把教学反例泄漏给下一段。
    assert stuck_join.done()

    # 第二个实验的 Queue 记账完全正确，专门证明“记账结束”和“worker 生命周期结束”不是一回事。
    queue: asyncio.Queue[str] = asyncio.Queue()
    worker_started = asyncio.Event()
    processed = asyncio.Event()
    async with asyncio.TaskGroup() as group:
        worker: Task[None] = group.create_task(
            one_item_worker(queue, worker_started, processed),
            name="one-item-worker",
        )
        # 先确认 worker 已启动，再投递唯一工作；processed 则确认 worker 已执行这一项。
        await worker_started.wait()
        await queue.put("work")
        await processed.wait()
        # 等对应 task_done 后 join 返回，但 worker 已循环到下一次 queue.get()，仍是 pending 状态。
        await queue.join()
        assert not worker.done()
        print("  join 已返回，但等待下一项的 worker 仍未退出：", not worker.done())
        # 正常关闭空 Queue 会使等待 get() 的 worker 收到 QueueShutDown 并 return。
        queue.shutdown()

    # TaskGroup 已等待 worker 的 return 完成，所以此处 done() 必须为 True。
    assert worker.done()
    print("  显式 shutdown 后 worker 已退出：", worker.done())


async def racy_increment(
    counter: Counter,
    read_finished: asyncio.Event,
    may_write: asyncio.Event,
) -> None:
    # 先把共享字段复制到局部不可变 int；两个 Task 都可能读到同一个旧值 0。
    snapshot: int = counter.value
    # 每个 Task 设置自己的 Event，父任务据此知道两次读取都已发生。
    read_finished.set()
    # 两个 Task 都在读完旧值后停住，父任务再统一放行写入。
    await may_write.wait()
    # 两个局部 snapshot 都是 0，所以随后两次赋值都写 1，后写者覆盖前写者而丢失一次增量。
    counter.value = snapshot + 1


async def locked_increment(counter: Counter, lock: asyncio.Lock) -> None:
    # async with lock 会等待互斥所有权，并在正常、异常或取消退出时释放；同一时刻仅一个 Task 进入。
    async with lock:
        snapshot: int = counter.value
        # 仅为教学故意在读写之间让出控制权；Lock 阻止另一个 Task 进入。
        # 真实代码不应无故持锁等待慢 I/O；这里的 await 是为了证明锁覆盖了完整“读—写”不变量。
        await asyncio.sleep(0)
        counter.value = snapshot + 1


async def count_owned_items(items: list[int]) -> int:
    # 模拟异步工作后只返回本 Task 的局部结果，函数不读取或写入共享 Counter。
    await asyncio.sleep(0)
    return len(items)


async def demonstrate_shared_state() -> None:
    print_section("7. 单线程仍会逻辑竞争：Lock 或改变数据所有权")
    # 两个 Task 共享 counter，却各有一个“已读完”信号；may_write 是父任务统一放行写入的广播信号。
    counter = Counter()
    first_read = asyncio.Event()
    second_read = asyncio.Event()
    may_write = asyncio.Event()

    async with asyncio.TaskGroup() as group:
        # 两个 coroutine 参数里的 counter/may_write 指向同一对象，而 read_finished 各不相同。
        group.create_task(racy_increment(counter, first_read, may_write))
        group.create_task(racy_increment(counter, second_read, may_write))
        # 等两个独立信号都 set，证明两个 snapshot 都已读完旧值；此时再同时放行写入。
        await first_read.wait()
        await second_read.wait()
        may_write.set()

    # TaskGroup 已等待两次写入结束；确定性结果 1 证明单线程事件循环仍有跨 await 的逻辑竞争。
    assert counter.value == 1
    print("  两次‘读—await—写’后的错误结果：", counter.value)

    # 修复一：两个 Task 仍共享对象，但也共享同一把 Lock，把读取和写回作为一个完整临界区。
    protected = Counter()
    lock = asyncio.Lock()
    async with asyncio.TaskGroup() as group:
        group.create_task(locked_increment(protected, lock))
        group.create_task(locked_increment(protected, lock))
    # 第二个 Task 必须在第一个写完 1 后才能读，所以最终稳定得到 2。
    assert protected.value == 2
    print("  Lock 保护同一不变量后的结果：", protected.value)

    # 修复二通常更清楚：每个 Task 只算自己的值，父任务在作用域退出后合并，不再共享可变计数器。
    async with asyncio.TaskGroup() as group:
        left: Task[int] = group.create_task(count_owned_items([1, 2]))
        right: Task[int] = group.create_task(count_owned_items([3, 4, 5]))
    # 离开 TaskGroup 后两个 Task 都已成功完成，result() 取回 2 和 3；父任务是唯一聚合者。
    owned_total: int = left.result() + right.result()
    assert owned_total == 5
    print("  子任务拥有局部结果、父任务最终合并：", owned_total)


async def run_examples() -> None:
    # 同步函数直接调用；各 async 实验按顺序 await，因此上一段完成清理后才进入下一段。
    explain_independent_limits()
    await demonstrate_semaphore_cancellation()
    await demonstrate_small_semaphore_batch()
    await demonstrate_worker_queue()
    await demonstrate_empty_batch()
    await demonstrate_worker_crash()
    await demonstrate_producer_crash()
    await demonstrate_user_cancellation()
    await demonstrate_queue_accounting_bugs()
    await demonstrate_shared_state()
    # all_tasks() 返回当前事件循环中尚未结束的 Task 集合，其中必然包含正在执行本函数的 Task。
    # 推导式排除 current 后应为空，用作整章最后一道“没有遗留后台 Task”的断言。
    current: Task[Any] | None = asyncio.current_task()
    leaked: list[Task[Any]] = [
        task for task in asyncio.all_tasks() if task is not current
    ]
    assert leaked == [], f"发现未回收 Task：{leaked}"
    print("\n所有第 23 章示例与运行时断言均已完成。")


def main() -> None:
    """只创建一个事件循环；每个示例离开前都回收自己创建的 Task。"""
    # asyncio.run 接受 run_examples() 产生的 coroutine，创建事件循环并运行到完成，最后关闭循环。
    # Java 可把它类比为应用最外层的异步运行入口，但这里的事件循环和 Task 由 asyncio 管理。
    asyncio.run(run_examples())


# 模块被直接执行时 __name__ 为 "__main__"；被测试或别的模块 import 时只定义对象，不自动运行。
if __name__ == "__main__":
    main()
