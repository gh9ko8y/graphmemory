"""交互轨迹记录器 — 在任务执行过程中逐步记录步骤，结束后保存完整轨迹。

用法：
    async with TraceRecorder(repo, task_type="deploy", user_id="u1") as rec:
        rec.step("docker build", {"tag":"v1"}, {"image":"v1"}, True, 3200)
        rec.step("docker push",  {"tag":"v1"}, {"ok":True},   True, 5100)
    # 退出 with 时自动保存轨迹；outcome 由最后一步/显式设置决定
"""

from __future__ import annotations

import logging
from typing import Any

from ..models.memory import InteractionTrace, TraceStep

logger = logging.getLogger(__name__)


class TraceRecorder:
    """同步上下文管理器：累积步骤，退出时落库。"""

    def __init__(self, repo, task_type: str, user_id: str | None = None):
        self._repo = repo
        self._task_type = task_type
        self._user_id = user_id
        self._steps: list[TraceStep] = []
        self._outcome: str = "SUCCESS"
        self._token_cost = 0
        self._trace: InteractionTrace | None = None

    # 累积步骤
    def step(
        self,
        action: str,
        input_data: dict | None = None,
        output_data: dict | None = None,
        success: bool = True,
        duration_ms: int = 0,
    ) -> TraceStep:
        s = TraceStep(
            step_index=len(self._steps),
            action=action,
            input_data=input_data or {},
            output_data=output_data or {},
            success=success,
            duration_ms=duration_ms,
        )
        self._steps.append(s)
        if not success:
            self._outcome = "FAILURE"
        return s

    def set_outcome(self, outcome: str) -> None:
        self._outcome = outcome

    def add_token_cost(self, n: int) -> None:
        self._token_cost += n

    @property
    def steps(self) -> list[TraceStep]:
        return list(self._steps)

    def build(self, duration_ms: int = 0) -> InteractionTrace:
        return InteractionTrace(
            task_type=self._task_type,
            steps=list(self._steps),
            outcome=self._outcome,
            duration_ms=duration_ms,
            token_cost=self._token_cost,
        )

    async def save(self, duration_ms: int = 0) -> InteractionTrace:
        trace = self.build(duration_ms)
        trace.user_id = self._user_id  # type: ignore[attr-defined]
        await self._repo.insert_trace(trace)
        self._trace = trace
        logger.info(f"Trace saved: {trace.id} ({trace.task_type}, {trace.outcome}, {len(trace.steps)} steps)")
        return trace

    async def __aenter__(self) -> "TraceRecorder":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            self._outcome = "FAILURE"
        await self.save()
