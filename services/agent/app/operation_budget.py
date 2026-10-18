"""请求局部的绝对截止：跨 Service/Adapter 传递预算，不改变 DataAdapter 接口。"""
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
import asyncio

_deadline: ContextVar[float | None] = ContextVar("schema_operation_deadline", default=None)


@contextmanager
def operation_budget(seconds: float) -> Iterator[float]:
    """在锁等待前设定单调时钟截止；嵌套时不放宽，退出时恢复调用者上下文。"""
    deadline = asyncio.get_running_loop().time() + seconds
    parent = _deadline.get()
    if parent is not None:
        deadline = min(parent, deadline)
    token = _deadline.set(deadline)
    try:
        yield deadline
    finally:
        _deadline.reset(token)


def remaining_wait(limit: float) -> float:
    """返回该阶段可等待秒数；已取消或总预算耗尽时为零，不再启动异步清理。"""
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        return 0.0
    deadline = _deadline.get()
    if deadline is None:
        return limit
    return max(0.0, min(limit, deadline - asyncio.get_running_loop().time()))
