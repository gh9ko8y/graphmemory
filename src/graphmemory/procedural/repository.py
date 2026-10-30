"""程序性记忆仓储层 — asyncpg 操作 interaction_traces 与 skills 表。"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


def _steps_json(steps: list) -> str:
    """将 TraceStep/SkillStep 列表序列化为 JSON（pydantic 对象走 model_dump）。"""
    out = []
    for s in steps:
        if hasattr(s, "model_dump"):
            out.append(s.model_dump(mode="json"))
        else:
            out.append(s)
    return json.dumps(out, ensure_ascii=False, default=str)


class ProceduralRepository:
    def __init__(self, dsn: str | None = None, pool: asyncpg.Pool | None = None):
        self._dsn = dsn
        self._shared_pool = pool
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self._shared_pool is None and self._dsn:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
            logger.info("ProceduralRepository pool created")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    def attach_pool(self, pool: asyncpg.Pool) -> None:
        self._shared_pool = pool

    @property
    def pool(self) -> asyncpg.Pool:
        p = self._shared_pool or self._pool
        assert p is not None, "ProceduralRepository not connected"
        return p

    # ── 交互轨迹 ──────────────────────────────────────

    async def insert_trace(self, trace) -> str:
        await self.pool.execute(
            """
            INSERT INTO interaction_traces
              (id, task_type, steps, outcome, duration_ms, token_cost, user_id,
               agent_id, session_id, model)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """,
            trace.id,
            trace.task_type,
            _steps_json(trace.steps),
            trace.outcome,
            trace.duration_ms,
            trace.token_cost,
            getattr(trace, "user_id", None),
            getattr(trace, "agent_id", None),
            getattr(trace, "session_id", None),
            getattr(trace, "model", None),
        )
        return trace.id

    async def get_trace(self, trace_id: str) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT * FROM interaction_traces WHERE id=$1", trace_id
        )

    async def list_traces_by_task(
        self, task_type: str | None = None, user_id: str | None = None, limit: int = 20
    ) -> list[asyncpg.Record]:
        """按任务类型列出轨迹；task_type/user_id 为 None 或空串时不过滤。"""
        return await self.pool.fetch(
            """
            SELECT * FROM interaction_traces
            WHERE (NULLIF($1,'')::text IS NULL OR task_type=$1)
              AND ($2::text IS NULL OR user_id=$2)
            ORDER BY created_at DESC LIMIT $3
            """,
            task_type,
            user_id,
            limit,
        )

    # ── 技能 ──────────────────────────────────────────

    async def insert_skill(self, skill) -> str:
        await self.pool.execute(
            """
            INSERT INTO skills
              (id, name, description, trigger_pattern, steps, success_rate,
               usage_count, avg_duration_ms, source_trace_ids, version, disabled, user_id,
               agent_id, session_id, model)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
            """,
            skill.id,
            skill.name,
            skill.description,
            skill.trigger_pattern,
            _steps_json(skill.steps),
            skill.success_rate,
            skill.usage_count,
            skill.avg_duration_ms,
            skill.source_trace_ids,
            skill.version,
            skill.disabled,
            getattr(skill, "user_id", None),
            getattr(skill, "agent_id", None),
            getattr(skill, "session_id", None),
            getattr(skill, "model", None),
        )
        return skill.id

    async def get_skill(self, skill_id: str) -> asyncpg.Record | None:
        return await self.pool.fetchrow("SELECT * FROM skills WHERE id=$1", skill_id)

    async def list_skills(
        self, user_id: str | None = None, task_type: str | None = None, min_rate: float = 0.0,
        limit: int = 100, offset: int = 0
    ) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """
            SELECT * FROM skills
            WHERE disabled=FALSE AND success_rate >= $1
              AND ($2::text IS NULL OR user_id=$2)
              AND ($3::text IS NULL OR trigger_pattern ILIKE '%' || $3 || '%')
            ORDER BY success_rate DESC, usage_count DESC
            LIMIT $4 OFFSET $5
            """,
            min_rate,
            user_id,
            task_type,
            limit,
            offset,
        )

    async def find_applicable(
        self, task_type: str, user_id: str | None = None, min_rate: float = 0.7
    ) -> asyncpg.Record | None:
        """查找该任务类型下成功率达标的最优技能。"""
        return await self.pool.fetchrow(
            """
            SELECT * FROM skills
            WHERE disabled=FALSE AND success_rate >= $1
              AND ($2::text IS NULL OR user_id=$2)
              AND trigger_pattern ILIKE '%' || $3 || '%'
            ORDER BY success_rate DESC, usage_count DESC
            LIMIT 1
            """,
            min_rate,
            user_id,
            task_type,
        )

    async def update_skill_stats(
        self,
        skill_id: str,
        success_rate: float,
        usage_count: int,
        avg_duration_ms: int,
    ) -> None:
        await self.pool.execute(
            """
            UPDATE skills SET success_rate=$2, usage_count=$3, avg_duration_ms=$4,
                   updated_at=NOW()
            WHERE id=$1
            """,
            skill_id,
            success_rate,
            usage_count,
            avg_duration_ms,
        )

    async def set_skill_disabled(self, skill_id: str, disabled: bool = True) -> None:
        await self.pool.execute(
            "UPDATE skills SET disabled=$2, updated_at=NOW() WHERE id=$1",
            skill_id,
            disabled,
        )

    async def list_low_success(self, threshold: float) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT * FROM skills WHERE disabled=FALSE AND success_rate < $1",
            threshold,
        )

    async def count_stats(self) -> dict:
        row = await self.pool.fetchrow(
            """
            SELECT
              COUNT(*) AS traces
            FROM interaction_traces
            """
        )
        row2 = await self.pool.fetchrow(
            """
            SELECT
              COUNT(*) AS skills,
              COUNT(*) FILTER (WHERE disabled=FALSE) AS active_skills,
              COALESCE(AVG(success_rate) FILTER (WHERE disabled=FALSE),0) AS avg_rate
            FROM skills
            """
        )
        return {"traces": int(row["traces"]), **{k: int(v) if k != "avg_rate" else float(v) for k, v in dict(row2).items()}}
