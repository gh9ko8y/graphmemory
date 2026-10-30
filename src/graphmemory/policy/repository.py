"""Policy 仓储层 — 基于 asyncpg 操作 PostgreSQL 的 fact_state 与 policy_events。

fact_state 是"Views 层"：Policy 维护的派生状态（置信度/访问/降级/归档/被覆盖）。
policy_events 是 Policy 决策的审计日志（只追加）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


class PolicyRepository:
    def __init__(self, dsn: str | None = None, pool: asyncpg.Pool | None = None):
        self._dsn = dsn
        self._shared_pool = pool
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        # 有共享池则用它，否则自建（向后兼容）
        if self._shared_pool is None and self._dsn:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
            logger.info("PolicyRepository pool created")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    def attach_pool(self, pool: asyncpg.Pool) -> None:
        """接入共享连接池（用于多 Repository 共用一个池）。"""
        self._shared_pool = pool

    @property
    def pool(self) -> asyncpg.Pool:
        p = self._shared_pool or self._pool
        assert p is not None, "PolicyRepository not connected"
        return p

    # ── fact_state 写入 ──────────────────────────────

    async def upsert_fact_state(self, row: dict[str, Any]) -> None:
        """插入一条 fact_state（新 edge）。若同 fact_id 已存在则更新文本/类型。"""
        await self.pool.execute(
            """
            INSERT INTO fact_state
              (fact_id, user_id, agent_id, session_id, model,
               source_entity, target_entity, relationship,
               fact_text, fact_type, confidence, confirmed_count, valid_from, valid_until,
               source_type, evidence_ids, episode_id, inference_count)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
            ON CONFLICT (fact_id) DO UPDATE SET
              fact_text = EXCLUDED.fact_text,
              fact_type = EXCLUDED.fact_type,
              agent_id = EXCLUDED.agent_id,
              session_id = EXCLUDED.session_id,
              model = EXCLUDED.model,
              updated_at = NOW()
            """,
            row["fact_id"],
            row.get("user_id"),
            row.get("agent_id"),
            row.get("session_id"),
            row.get("model"),
            row.get("source_entity"),
            row.get("target_entity"),
            row.get("relationship"),
            row.get("fact_text"),
            row.get("fact_type", "normal"),
            row.get("confidence", 1.0),
            row.get("confirmed_count", 1),
            row.get("valid_from"),
            row.get("valid_until"),
            row.get("source_type", "remembered_fact"),
            row.get("evidence_ids", []),
            row.get("episode_id"),
            row.get("inference_count", 0),
        )

    async def get_active_by_relation(
        self, user_id: str | None, source: str, target: str, rel: str, exclude_fact_id: str
    ) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """
            SELECT * FROM fact_state
            WHERE source_entity = $1 AND target_entity = $2 AND relationship = $3
              AND ($4::text IS NULL OR user_id = $4)
              AND fact_id <> $5
              AND superseded = FALSE AND archived = FALSE
            """,
            source,
            target,
            rel,
            user_id,
            exclude_fact_id,
        )

    async def set_validity(self, fact_id: str, ttl_days, expires_at, due_at=None,
                           confirm_state: str = "") -> None:
        """写入有效期（TTL）。ttl_days=None 表示永不自动删除；due_at 用于"到日期再确认"。"""
        await self.pool.execute(
            "UPDATE fact_state SET ttl_days=$2, expires_at=$3, due_at=$4, confirm_state=$5, "
            "updated_at=NOW() WHERE fact_id=$1",
            fact_id, ttl_days, expires_at, due_at, confirm_state,
        )

    async def set_birth(self, fact_id: str, birth_date=None, birth_year=None) -> None:
        """写入出生锚点（年龄随时间动态计算，不存死值）。"""
        await self.pool.execute(
            "UPDATE fact_state SET birth_date=$2, birth_year=$3, updated_at=NOW() WHERE fact_id=$1",
            fact_id, birth_date, birth_year,
        )

    async def mark_superseded(
        self, fact_id: str, now: datetime, superseded_by: str | None = None
    ) -> None:
        """版本收口：valid_until（现实中不再成立）+ tx_to（这版何时被修正）双轴同关。"""
        await self.pool.execute(
            """
            UPDATE fact_state
            SET superseded = TRUE, valid_until = $2, tx_to = $2,
                superseded_by = COALESCE($3, superseded_by), updated_at = NOW()
            WHERE fact_id = $1
            """,
            fact_id,
            now,
            superseded_by,
        )

    async def set_confidence(
        self, fact_id: str, confidence: float, confirmed_count: int
    ) -> None:
        await self.pool.execute(
            "UPDATE fact_state SET confidence = $2, confirmed_count = $3, updated_at = NOW() "
            "WHERE fact_id = $1",
            fact_id,
            confidence,
            confirmed_count,
        )

    async def mark_demoted(self, fact_id: str, demoted: bool = True) -> None:
        await self.pool.execute(
            "UPDATE fact_state SET demoted = $2, updated_at = NOW() WHERE fact_id = $1",
            fact_id,
            demoted,
        )

    async def mark_archived(self, fact_id: str, archived: bool = True) -> None:
        await self.pool.execute(
            "UPDATE fact_state SET archived = $2, updated_at = NOW() WHERE fact_id = $1",
            fact_id,
            archived,
        )

    # ── 检索期：读取状态 + 记录访问 ──────────────────

    async def get_states(self, fact_ids: list[str]) -> dict[str, asyncpg.Record]:
        if not fact_ids:
            return {}
        rows = await self.pool.fetch(
            "SELECT * FROM fact_state WHERE fact_id = ANY($1::text[])", fact_ids
        )
        return {r["fact_id"]: r for r in rows}

    async def increment_access(self, fact_ids: list[str], now: datetime) -> None:
        if not fact_ids:
            return
        await self.pool.execute(
            "UPDATE fact_state SET access_count = access_count + 1, last_accessed = $2, "
            "updated_at = NOW() WHERE fact_id = ANY($1::text[])",
            fact_ids,
            now,
        )

    # ── 维护期：批量扫描 ─────────────────────────────

    async def list_active_for_user(self, user_id: str | None) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """
            SELECT * FROM fact_state
            WHERE archived = FALSE AND superseded = FALSE
              AND ($1::text IS NULL OR user_id = $1)
            """,
            user_id,
        )

    async def find_similar_groups(
        self, user_id: str | None, threshold: int
    ) -> list[asyncpg.Record]:
        """同 (source,target,relationship) 的活跃事实数 > threshold 的分组。"""
        return await self.pool.fetch(
            """
            SELECT source_entity, target_entity, relationship, COUNT(*) AS cnt
            FROM fact_state
            WHERE archived = FALSE AND superseded = FALSE
              AND ($1::text IS NULL OR user_id = $1)
            GROUP BY source_entity, target_entity, relationship
            HAVING COUNT(*) > $2
            """,
            user_id,
            threshold,
        )

    async def fetch_group_members(
        self, user_id: str | None, source: str, target: str, rel: str
    ) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """
            SELECT * FROM fact_state
            WHERE source_entity=$1 AND target_entity=$2 AND relationship=$3
              AND archived=FALSE AND superseded=FALSE
              AND ($4::text IS NULL OR user_id=$4)
            ORDER BY confidence DESC, created_at DESC
            """,
            source,
            target,
            rel,
            user_id,
        )

    async def count_stats(self) -> dict[str, int]:
        row = await self.pool.fetchrow(
            """
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE archived=FALSE AND superseded=FALSE) AS active,
              COUNT(*) FILTER (WHERE archived=TRUE) AS archived,
              COUNT(*) FILTER (WHERE demoted=TRUE AND archived=FALSE) AS demoted,
              COUNT(*) FILTER (WHERE superseded=TRUE) AS superseded,
              COALESCE(AVG(confidence) FILTER (WHERE archived=FALSE AND superseded=FALSE),0) AS avg_conf
            FROM fact_state
            """
        )
        return dict(row) if row else {}

    # ── 审计日志 ─────────────────────────────────────

    async def log_event(
        self,
        rule_name: str,
        trigger_type: str,  # conflict / forgetting / compression / proactive
        action: str,
        fact_id: str | None = None,
        user_id: str | None = None,
        details: dict | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO policy_events
              (rule_name, trigger_type, fact_id, action_taken, details,
               user_id, agent_id, session_id, model)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            rule_name,
            trigger_type,
            fact_id,
            action,
            json.dumps(details or {}, ensure_ascii=False),
            user_id,
            agent_id,
            session_id,
            model,
        )

    # ── Ledger（只追加事件账本，命题二） ───────────────

    async def append_ledger(
        self,
        action: str,  # add_fact / invalidate / supersede / update_confidence
        fact_id: str | None = None,
        entity_id: str | None = None,
        details: dict | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
    ) -> None:
        """向只追加的 ledger 表写一条事件（原始账本，不可改）。"""
        await self.pool.execute(
            """
            INSERT INTO ledger
              (action, fact_id, entity_id, details, agent_id, session_id, model)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            """,
            action,
            fact_id,
            entity_id,
            json.dumps(details or {}, ensure_ascii=False),
            agent_id,
            session_id,
            model,
        )
