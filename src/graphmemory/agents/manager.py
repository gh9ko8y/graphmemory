"""多 agent 归属管理 — 注册房间、开箱、换模型、关箱。"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..models.memory import Agent, Session
from .repository import AgentRepository

logger = logging.getLogger(__name__)


class AgentManager:
    def __init__(self, repo: AgentRepository):
        self.repo = repo

    # ── 房间（agent） ────────────────────────────────

    async def register_agent(self, agent_id: str, name: str, description: str = "") -> Agent:
        """登记一个 agent（换 agent = 登记新房间，各记各的）。"""
        await self.repo.register_agent(agent_id, name, description)
        return Agent(agent_id=agent_id, name=name, description=description)

    async def get_agent(self, agent_id: str) -> Optional[Agent]:
        row = await self.repo.get_agent(agent_id)
        if row is None:
            return None
        return Agent(agent_id=row["agent_id"], name=row["name"], description=row.get("description") or "")

    async def list_agents(self, limit: int = 100, offset: int = 0) -> list[Agent]:
        rows = await self.repo.list_agents(limit, offset)
        return [
            Agent(agent_id=r["agent_id"], name=r["name"], description=r.get("description") or "")
            for r in rows
        ]

    # ── 箱子（session） ──────────────────────────────

    async def open_session(
        self,
        agent_id: str,
        title: str = "",
        topic: str = "",
        model: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        """开一个会话箱子。agent 必须已注册。"""
        if await self.repo.get_agent(agent_id) is None:
            raise ValueError(f"agent not found: {agent_id}")
        s = Session(
            session_id=session_id or "",
            agent_id=agent_id,
            title=title,
            topic=topic,
            current_model=model,
        )
        if not s.session_id:
            import uuid

            s.session_id = str(uuid.uuid4())
        await self.repo.open_session(s.session_id, agent_id, title, topic, model)
        return s

    async def switch_model(self, session_id: str, model: str) -> dict[str, Any]:
        """换大模型：记忆继承（箱子不变），只标记当前模型。"""
        row = await self.repo.get_session(session_id)
        if row is None:
            raise ValueError(f"session not found: {session_id}")
        await self.repo.switch_model(session_id, model)
        return {"session_id": session_id, "agent_id": row["agent_id"], "current_model": model}

    async def close_session(self, session_id: str) -> None:
        await self.repo.close_session(session_id)

    async def list_sessions(self, agent_id: str, limit: int = 100, offset: int = 0) -> list[Session]:
        rows = await self.repo.list_sessions(agent_id, limit, offset)
        return [
            Session(
                session_id=r["session_id"],
                agent_id=r["agent_id"],
                title=r.get("title") or "",
                topic=r.get("topic") or "",
                current_model=r.get("current_model"),
                started_at=r["started_at"],
                ended_at=r.get("ended_at"),
            )
            for r in rows
        ]
