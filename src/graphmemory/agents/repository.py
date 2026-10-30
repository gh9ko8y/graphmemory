"""多 agent 归属仓储层 — agents（房间）+ sessions（箱子）表操作。"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


class AgentRepository:
    def __init__(self, dsn: str | None = None, pool: asyncpg.Pool | None = None):
        self._dsn = dsn
        self._shared_pool = pool
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self._shared_pool is None and self._dsn:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
            logger.info("AgentRepository pool created")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    def attach_pool(self, pool: asyncpg.Pool) -> None:
        self._shared_pool = pool

    @property
    def pool(self) -> asyncpg.Pool:
        p = self._shared_pool or self._pool
        assert p is not None, "AgentRepository not connected"
        return p

    # ── agents（房间） ────────────────────────────────

    async def register_agent(self, agent_id: str, name: str, description: str = "") -> None:
        await self.pool.execute(
            """
            INSERT INTO agents (agent_id, name, description)
            VALUES ($1,$2,$3)
            ON CONFLICT (agent_id) DO UPDATE SET name=EXCLUDED.name,
                description=EXCLUDED.description
            """,
            agent_id,
            name,
            description,
        )

    async def get_agent(self, agent_id: str) -> asyncpg.Record | None:
        return await self.pool.fetchrow("SELECT * FROM agents WHERE agent_id=$1", agent_id)

    async def list_agents(self, limit: int = 100, offset: int = 0) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT * FROM agents ORDER BY created_at LIMIT $1 OFFSET $2", limit, offset
        )

    # ── sessions（箱子） ──────────────────────────────

    async def open_session(
        self,
        session_id: str,
        agent_id: str,
        title: str = "",
        topic: str = "",
        model: str | None = None,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO sessions (session_id, agent_id, title, topic, current_model)
            VALUES ($1,$2,$3,$4,$5)
            """,
            session_id,
            agent_id,
            title,
            topic,
            model,
        )

    async def get_session(self, session_id: str) -> asyncpg.Record | None:
        return await self.pool.fetchrow("SELECT * FROM sessions WHERE session_id=$1", session_id)

    async def list_sessions(self, agent_id: str, limit: int = 100, offset: int = 0) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT * FROM sessions WHERE agent_id=$1 ORDER BY started_at DESC LIMIT $2 OFFSET $3",
            agent_id,
            limit,
            offset,
        )

    async def switch_model(self, session_id: str, model: str) -> None:
        await self.pool.execute(
            "UPDATE sessions SET current_model=$2 WHERE session_id=$1", session_id, model
        )

    async def close_session(self, session_id: str) -> None:
        await self.pool.execute(
            "UPDATE sessions SET ended_at=NOW() WHERE session_id=$1", session_id
        )
