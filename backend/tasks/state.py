"""任务状态机、会话级并发控制与取消登记。

状态流转（规格书 1.1.4）：
    queued ─┬─> running ─┬─> success
            │            ├─> failed
            │            └─> cancelled
            ├─> cancelled        （排队阶段直接取消）
            └─> failed           （启动阶段异常）

并发约束：同一会话同时只允许一个 queued/running 任务 → PerConversationLocks。
"""
from __future__ import annotations

import asyncio
from enum import StrEnum


class TaskCancelled(Exception):
    """任务被取消：引擎在检查点抛出的协作式退出信号。"""


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED}
)
ACTIVE_STATUSES: frozenset[TaskStatus] = frozenset({TaskStatus.QUEUED, TaskStatus.RUNNING})

ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.RUNNING: frozenset({TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.SUCCESS: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

# 当前步骤文案（task_status 事件与前端步骤条共用）
STEP_QUEUED = "排队中"
STEP_CONTEXT = "上下文准备"
STEP_ANALYZING = "执行分析"
STEP_RESULT = "生成结果"
STEP_DONE = "完成"
STEP_CANCELLED = "已取消"
STEP_FAILED = "已失败"


def can_transition(current: str, target: str) -> bool:
    try:
        return TaskStatus(target) in ALLOWED_TRANSITIONS[TaskStatus(current)]
    except (KeyError, ValueError):
        return False


class PerConversationLocks:
    """会话级互斥锁：保证「同一会话同一时间只有一个分析任务」。"""

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get(self, conversation_id: int) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(conversation_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[conversation_id] = lock
            return lock

    async def discard(self, conversation_id: int) -> None:
        """会话删除时清理锁条目（仅在没有持锁者时移除）。"""
        async with self._guard:
            lock = self._locks.get(conversation_id)
            if lock is not None and not lock.locked():
                self._locks.pop(conversation_id, None)

    def busy(self, conversation_id: int) -> bool:
        lock = self._locks.get(conversation_id)
        return bool(lock and lock.locked())


class CancelRegistry:
    """运行中任务的取消标记（协作式取消：引擎在检查点查询）。"""

    def __init__(self) -> None:
        self._flags: set[int] = set()

    def request(self, task_id: int) -> None:
        self._flags.add(task_id)

    def is_requested(self, task_id: int) -> bool:
        return task_id in self._flags

    def clear(self, task_id: int) -> None:
        self._flags.discard(task_id)

    def snapshot(self) -> list[int]:
        return sorted(self._flags)


conversation_locks = PerConversationLocks()
cancel_registry = CancelRegistry()
