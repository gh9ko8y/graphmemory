"""聊天记忆仓储 — memory_state / episodes / memory_fragments 操作。

episodes 是原文账本：raw_text 只可 INSERT 不可 UPDATE（原始证据永不覆盖）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


class RecallRepository:
    def __init__(self, dsn: str | None = None, pool: asyncpg.Pool | None = None):
        self._dsn = dsn
        self._shared_pool = pool
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self._shared_pool is None and self._dsn:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
            logger.info("RecallRepository pool created")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    def attach_pool(self, pool: asyncpg.Pool) -> None:
        self._shared_pool = pool

    @property
    def pool(self) -> asyncpg.Pool:
        p = self._shared_pool or self._pool
        assert p is not None, "RecallRepository not connected"
        return p

    # ── memory_state：六变量 ─────────────────────────────

    async def init_memory_state(
        self,
        fact_id: str,
        significance: float = 0.5,
        emotion: float = 0.0,
        kind: str = "fact",
    ) -> None:
        """新事实登记六变量（D=0.9 刚发生 / R=0.3 靠线索 / C=0.1 起）。"""
        await self.pool.execute(
            """
            INSERT INTO memory_state
              (memory_id, memory_kind, S_significance, D_detail, R_retrieval,
               E_emotion, C_consolidation)
            VALUES ($1, $2, $3, 0.9, 0.3, $4, 0.1)
            ON CONFLICT (memory_id) DO NOTHING
            """,
            fact_id, kind, significance, emotion,
        )

    async def get_states(self, fact_ids: list[str]) -> dict[str, asyncpg.Record]:
        """批量取六变量（检索竞争用）。"""
        if not fact_ids:
            return {}
        rows = await self.pool.fetch(
            "SELECT * FROM memory_state WHERE memory_id = ANY($1::text[])", fact_ids
        )
        return {r["memory_id"]: r for r in rows}

    async def list_active_for_decay(
        self, user_id: str | None = None, limit: int = 2000
    ) -> list[asyncpg.Record]:
        """衰减作业扫描清单：活跃事实 + 六变量。

        注意：建表时 S_significance 等未加引号 → Postgres 存的是小写列名，
        这里显式用小写别名，避免 asyncpg Record 取值时的大小写陷阱。
        """
        return await self.pool.fetch(
            """
            SELECT m.memory_id, f.fact_id,
                   m.s_significance, m.d_detail, m.e_emotion, m.r_retrieval,
                   m.c_consolidation, m.i_interference, m.faded, m.last_decay_at,
                   f.user_id, f.fact_type, f.fact_text, f.episode_id, f.created_at
            FROM memory_state m
            JOIN fact_state f ON f.fact_id = m.memory_id
            WHERE f.archived = FALSE
              AND f.superseded = FALSE
              AND ($1::text IS NULL OR f.user_id = $1)
            ORDER BY m.last_decay_at ASC
            LIMIT $2
            """,
            user_id,
            limit,
        )

    async def interference_counts(self, fact_ids: list[str]) -> dict[str, int]:
        """干扰度：每条事实同 (user, source, target, relationship) 的活跃邻居数。"""
        if not fact_ids:
            return {}
        rows = await self.pool.fetch(
            """
            SELECT f1.fact_id AS me, COUNT(f2.fact_id) AS peers
            FROM fact_state f1
            JOIN fact_state f2
              ON f2.user_id = f1.user_id
             AND f2.source_entity = f1.source_entity
             AND f2.target_entity = f1.target_entity
             AND f2.relationship = f1.relationship
             AND f2.fact_id <> f1.fact_id
             AND f2.archived = FALSE AND f2.superseded = FALSE
            WHERE f1.fact_id = ANY($1::text[])
              AND COALESCE(f1.source_entity,'') NOT IN ('我', '行为', '')
              AND COALESCE(f1.target_entity,'') NOT IN ('我', '行为', '')
            GROUP BY f1.fact_id
            """,
            fact_ids,
        )
        return {r["me"]: int(r["peers"]) for r in rows}

    async def apply_decay(
        self, fact_id: str, s: float, d: float, e: float, i: float, r: float | None = None
    ) -> None:
        await self.pool.execute(
            """
            UPDATE memory_state
            SET s_significance = $2, d_detail = $3, e_emotion = $4,
                i_interference = $5,
                r_retrieval = COALESCE($6, r_retrieval),
                last_decay_at = NOW(), updated_at = NOW()
            WHERE memory_id = $1
            """,
            fact_id, s, d, e, i, r,
        )

    async def mark_archived(self, memory_id: str, fact_id: str | None = None) -> None:
        """低意义记忆：标记 faded + 归档（不删数据）。"""
        await self.pool.execute(
            "UPDATE memory_state SET faded = TRUE, updated_at = NOW() WHERE memory_id = $1",
            memory_id,
        )
        if fact_id:
            await self.pool.execute(
                "UPDATE fact_state SET archived = TRUE, updated_at = NOW() WHERE fact_id = $1",
                fact_id,
            )

    async def mark_faded(self, fact_id: str) -> None:
        await self.pool.execute(
            "UPDATE memory_state SET faded = TRUE, updated_at = NOW() WHERE memory_id = $1",
            fact_id,
        )

    async def touch_recall(self, fact_ids: list[str], boost: float, cap: float) -> None:
        """命中副作用：recall_count+1、C+=boost、last_recall_at=now（回忆巩固记忆）。"""
        if not fact_ids:
            return
        await self.pool.execute(
            """
            UPDATE memory_state
            SET recall_count = recall_count + 1,
                C_consolidation = LEAST($3, C_consolidation + $2::real),
                r_retrieval = GREATEST(0.05, r_retrieval - 0.05),
                last_recall_at = NOW(), updated_at = NOW()
            WHERE memory_id = ANY($1::text[])
            """,
            fact_ids, boost, cap,
        )

    # ── episodes：原文账本 ───────────────────────────────

    async def insert_episode(
        self,
        episode_id: str,
        raw_text: str,
        ts=None,
        chat_id: str | None = None,
        actors: list[str] | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        meta: dict[str, Any] | None = None,
        raw_quote: str | None = None,
    ) -> None:
        """原文入库。唯一真相源，只插入、永不更新。

        raw_quote：用户原话片段（逐字）。有它才算"原文直录"，
        没有就只能是转述（reconstructed_detail）——防止记忆自证。
        """
        import json as _json

        await self.pool.execute(
            """
            INSERT INTO episodes (id, chat_id, ts, actors, user_id, agent_id,
                                  session_id, raw_text, meta, raw_quote)
            VALUES ($1,$2,COALESCE($3, NOW()),$4,$5,$6,$7,$8,$9::jsonb,$10)
            ON CONFLICT (id) DO NOTHING
            """,
            episode_id, chat_id, ts, actors or [], user_id, agent_id,
            session_id, raw_text, _json.dumps(meta or {}, ensure_ascii=False),
            raw_quote,
        )

    # ── action_log：行为账本（她做过什么/失败过什么）─────────

    async def insert_action(
        self,
        tool_name: str,
        outcome: str,
        args_digest: str | None = None,
        detail: str | None = None,
        product: str | None = None,
        chat_id: str | None = None,
    ) -> str:
        """记录一次行为。outcome: ok / failed / queued / unknown。"""
        aid = str(uuid.uuid4())
        await self.pool.execute(
            """
            INSERT INTO action_log (id, tool, outcome, args_digest, detail, product, chat_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            """,
            aid, tool_name, outcome, args_digest, detail, product, chat_id,
        )
        return aid

    async def list_actions(self, limit: int = 20, chat_id: str | None = None) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """
            SELECT * FROM action_log
            WHERE ($2::text IS NULL OR chat_id = $2)
            ORDER BY ts DESC LIMIT $1
            """,
            limit, chat_id,
        )

    async def insert_self_fact(
        self,
        fact_text: str,
        fact_type: str,
        confidence: float = 0.95,
        significance: float = 0.6,
        emotion: float = 0.0,
        episode_id: str | None = None,
        evidence_ids: list[str] | None = None,
        user_id: str = "self",
        chat_id: str | None = None,
    ) -> str:
        """写入一条"我做过/失败过"的行为事实（系统直接观测，可信度高）。

        修复（2026-10-29）：
          - 参数错位：relationship 原先把 fact_type 写进去 → 改为 did/failed
          - 幂等：同一 user + 同文本 10 分钟内只写一条（防重试重复）
          - 原文账本：没有 episode 时自动补一条（可溯源）
        """
        fid = str(uuid.uuid4())
        dup = await self.pool.fetchrow(
            """
            SELECT fact_id FROM fact_state
            WHERE user_id = $1 AND fact_text = $2
              AND created_at > NOW() - INTERVAL '10 minutes'
            ORDER BY created_at DESC LIMIT 1
            """,
            user_id, fact_text,
        )
        if dup:
            logger.info("insert_self_fact dedupe hit: %s", fact_text[:40])
            return dup["fact_id"]

        if episode_id is None:
            episode_id = str(uuid.uuid4())
            await self.insert_episode(
                episode_id=episode_id, raw_text=fact_text, chat_id=chat_id,
                user_id=user_id, meta={"source": "self_action", "fact_type": fact_type},
                raw_quote=fact_text,
            )

        relationship = "failed" if fact_type == "failure" else "did"
        await self.pool.execute(
            """
            INSERT INTO fact_state
              (fact_id, user_id, source_entity, target_entity, relationship,
               fact_text, fact_type, confidence, confirmed_count, valid_from,
               source_type, evidence_ids, episode_id, inference_count, raw_quote)
            VALUES ($1,$2,'我','行为',$3,$4,$5,$6,1,NOW(),'remembered_fact',$7,$8,0,$9)
            """,
            fid, user_id, relationship, fact_text, fact_type, confidence,
            evidence_ids or [], episode_id, fact_text,
        )
        await self.init_memory_state(fid, significance=significance, emotion=emotion)
        return fid

    async def search_facts_pg(
        self, query: str, user_id: str | None = None, limit: int = 8,
        scan: int = 600, prefer_actions: bool = False,
    ) -> list[dict[str, Any]]:
        """PG 侧召回：给行为/失败事实兜底（这些没进 Neo4j）。

        用字符 bigram 重叠打分，只做粗排；与图谱结果合并后由竞争排序处理。
        """
        import re as _re

        def bigrams(t: str) -> set[str]:
            t = _re.sub(r"[\s，。.,!！?？的了吗呢吧]", "", t or "")
            return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) > 1 else ({t} if t else set())

        qg = bigrams(query)
        if not qg:
            return []
        rows = await self.pool.fetch(
            """
            SELECT fact_id, user_id, source_entity, target_entity, relationship,
                   fact_text, fact_type, confidence, valid_from, valid_until,
                   source_type, evidence_ids, episode_id, inference_count, raw_quote
            FROM fact_state
            WHERE archived = FALSE AND superseded = FALSE
              AND (expires_at IS NULL OR expires_at > NOW())
              AND fact_id NOT IN (SELECT fact_id FROM memory_tombstones)
              AND (user_id = $1 OR user_id = 'self' OR $1::text IS NULL)
            ORDER BY created_at DESC
            LIMIT $2
            """,
            user_id, scan,
        )
        scored: list[tuple[float, dict[str, Any]]] = []
        action_rows: list[dict[str, Any]] = []
        for r in rows:
            ft = r["fact_type"] or ""
            is_action = ft in ("action", "failure")
            d = dict(r)
            if is_action:
                action_rows.append(d)
            tg = bigrams(r["fact_text"] or "")
            if not tg:
                continue
            overlap = len(qg & tg) / max(1, len(qg | tg))
            # 行为/失败事实给加成：它们正是"我做过什么"的直接答案
            if is_action:
                overlap *= 1.35
                # 问句带"发/做/成功/失败/了吗"时，即使字面重合低也要把行为事实带出来，
                # 否则她永远搜不到自己翻过车。
                if prefer_actions:
                    overlap = max(overlap, 0.16)
            if overlap >= 0.06:
                d["_score"] = overlap
                scored.append((overlap, d))

        # 兜底：问的是"做没做过"时，最近 3 条行为事实一定带上
        if prefer_actions:
            have = {d["fact_id"] for _, d in scored}
            for d in action_rows[:3]:
                if d["fact_id"] not in have:
                    d["_score"] = 0.12
                    scored.append((0.12, d))

        scored.sort(key=lambda x: -x[0])
        return [d for _, d in scored[:limit]]

    async def get_episode(self, episode_id: str) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT * FROM episodes WHERE id = $1", episode_id
        )

    async def list_episodes(
        self,
        chat_id: str | None = None,
        user_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """
            SELECT * FROM episodes
            WHERE ($1::text IS NULL OR chat_id = $1)
              AND ($2::text IS NULL OR user_id = $2)
            ORDER BY ts DESC LIMIT $3 OFFSET $4
            """,
            chat_id, user_id, limit, offset,
        )

    async def episode_facts(self, episode_id: str, limit: int = 3) -> list[asyncpg.Record]:
        """同一条消息抽出的其他事实（fragment 生成候选）。"""
        return await self.pool.fetch(
            """
            SELECT fact_id, fact_text FROM fact_state
            WHERE episode_id = $1 AND archived = FALSE AND superseded = FALSE
            ORDER BY confidence DESC LIMIT $2
            """,
            episode_id, limit,
        )

    # ── memory_fragments：照片式片段 ─────────────────────

    async def insert_fragment(
        self, fact_id: str, text: str, episode_id: str | None = None,
        user_id: str | None = None,
    ) -> str:
        fid = str(uuid.uuid4())
        await self.pool.execute(
            """
            INSERT INTO memory_fragments (id, fact_id, episode_id, user_id, text)
            VALUES ($1,$2,$3,$4,$5)
            """,
            fid, fact_id, episode_id, user_id, text,
        )
        return fid

    async def list_fragments(
        self, user_id: str | None = None, limit: int = 50
    ) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """
            SELECT * FROM memory_fragments
            WHERE ($1::text IS NULL OR user_id = $1)
            ORDER BY created_at DESC LIMIT $2
            """,
            user_id, limit,
        )

    # ── 统计 ─────────────────────────────────────────────

    async def expire_and_purge(self, grace_days: int = 1) -> dict[str, int]:
        """时效性清理：到期 → 标记 expired；超过宽限期 → 物理删除（原文账本保留）。"""
        # a) 到约定日期 → 进入待确认（不删，等她确认结果）
        await self.pool.execute(
            "UPDATE fact_state SET confirm_state='pending' "
            "WHERE due_at IS NOT NULL AND due_at < NOW() "
            "AND COALESCE(confirm_state,'') = '' AND archived = FALSE"
        )
        # b) 待确认超 14 天仍未确认 → 自动补 7 天时效（之后走删除）
        await self.pool.execute(
            "UPDATE fact_state SET expires_at = NOW() + interval '7 days', ttl_days = 7 "
            "WHERE confirm_state='pending' AND due_at < NOW() - interval '14 days' "
            "AND expires_at IS NULL"
        )
        marked = await self.pool.execute(
            "UPDATE fact_state SET expired_at = NOW() "
            "WHERE expires_at IS NOT NULL AND expires_at < NOW() AND expired_at IS NULL"
        )
        rows = await self.pool.fetch(
            "SELECT fact_id, user_id, fact_text FROM fact_state "
            "WHERE expired_at IS NOT NULL AND expired_at < NOW() - ($1 || ' days')::interval",
            str(grace_days),
        )
        ids = [r["fact_id"] for r in rows]
        if not ids:
            return {"marked": int(str(marked).split()[-1]) if marked else 0, "purged": 0}
        for r in rows:
            await self.pool.execute(
                "INSERT INTO memory_tombstones (fact_id, user_id, fact_text, reason) "
                "VALUES ($1,$2,$3,'expired') ON CONFLICT (fact_id) DO NOTHING",
                r["fact_id"], r["user_id"], r["fact_text"],
            )
        await self.pool.execute("DELETE FROM memory_state WHERE memory_id = ANY($1::text[])", ids)
        await self.pool.execute("DELETE FROM memory_fragments WHERE fact_id = ANY($1::text[])", ids)
        await self.pool.execute("DELETE FROM fact_state WHERE fact_id = ANY($1::text[])", ids)
        return {"marked": int(str(marked).split()[-1]) if marked else 0, "purged": len(ids)}

    async def count_stats(self) -> dict[str, int]:
        row = await self.pool.fetchrow(
            """
            SELECT
              (SELECT COUNT(*) FROM memory_state) AS memory_tracked,
              (SELECT COUNT(*) FROM memory_state WHERE faded) AS faded,
              (SELECT COUNT(*) FROM episodes) AS episodes,
              (SELECT COUNT(*) FROM memory_fragments) AS fragments
            """
        )
        return {k: int(v) for k, v in dict(row).items()} if row else {}
