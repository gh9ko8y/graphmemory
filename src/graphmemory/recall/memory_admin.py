# -*- coding: utf-8 -*-
"""记忆管理：去重扫描/删除、遗忘、孤立实体名单。

删除 = 墓碑 + PG 三表 + Neo4j 边（原文账本 episodes 保留）。
"""
from __future__ import annotations

import math
import re
from typing import Any

from ..config import settings

NORM_RE = re.compile(r"[\s，。.,!！?？:：;；“”\"'()（）\[\]【】]+")


def _norm(t: str) -> str:
    return NORM_RE.sub("", (t or "")).lower()


def _bigrams(t: str) -> set[str]:
    s = _norm(t)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else ({s} if s else set())


def _jaccard(a: str, b: str) -> float:
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _cos(a, b) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _score(row: dict) -> float:
    """保留哪条：有证据/原文 > 置信度高 > 更长 > 更新。"""
    s = 0.0
    if row.get("raw_quote"):
        s += 3.0
    if row.get("episode_id"):
        s += 2.0
    s += float(row.get("confidence") or 0) * 2
    s += min(1.5, len(row.get("fact_text") or "") / 60)
    if row.get("created_at"):
        s += 0.5
    return s


async def scan(pool, embed_fn=None, threshold: float = 0.86, user_id: str | None = None) -> list[dict]:
    """扫重复组。embed_fn(texts)->list[vector] 可选，有就用语义相似度，否则 bigram。"""
    sql = """
        SELECT fact_id, user_id, source_entity, target_entity, relationship, fact_text,
               confidence, confirmed_count, episode_id, raw_quote, created_at
        FROM fact_state
        WHERE archived = FALSE AND superseded = FALSE
    """
    args = []
    if user_id:
        sql += " AND user_id = $1"
        args.append(user_id)
    rows = [dict(r) for r in await pool.fetch(sql, *args)]
    if len(rows) < 2:
        return []

    vectors = None
    if embed_fn is not None:
        try:
            vectors = await embed_fn([r["fact_text"] or "" for r in rows])
        except Exception:
            vectors = None

    used = set()
    groups = []
    for i, a in enumerate(rows):
        if i in used:
            continue
        members = [i]
        for j in range(i + 1, len(rows)):
            if j in used:
                continue
            b = rows[j]
            same_rel = (a["source_entity"] == b["source_entity"]
                        and a["target_entity"] == b["target_entity"]
                        and a["relationship"] == b["relationship"])
            if vectors is not None:
                sim = _cos(vectors[i], vectors[j])
            else:
                sim = _jaccard(a["fact_text"], b["fact_text"])
            if sim >= threshold and (same_rel or sim >= threshold + 0.05):
                members.append(j)
        if len(members) > 1:
            for j in members:
                used.add(j)
            best = max(members, key=lambda k: _score(rows[k]))
            groups.append({
                "keep": rows[best]["fact_id"],
                "keep_text": rows[best]["fact_text"],
                "drop": [rows[k]["fact_id"] for k in members if k != best],
                "drop_texts": [rows[k]["fact_text"] for k in members if k != best],
                "sim_kind": "embedding" if vectors is not None else "bigram",
            })
    return groups


async def forget(pool, fact_ids: list[str], reason: str = "manual") -> dict:
    """删除事实：墓碑 + PG 三表 + Neo4j 边（episodes 保留）。"""
    if not fact_ids:
        return {"deleted": 0}
    rows = await pool.fetch(
        "SELECT fact_id, user_id, fact_text FROM fact_state WHERE fact_id = ANY($1::text[])", fact_ids)
    for r in rows:
        await pool.execute(
            "INSERT INTO memory_tombstones (fact_id, user_id, fact_text, reason) "
            "VALUES ($1,$2,$3,$4) ON CONFLICT (fact_id) DO NOTHING",
            r["fact_id"], r["user_id"], r["fact_text"], reason)
    await pool.execute("DELETE FROM memory_state WHERE memory_id = ANY($1::text[])", fact_ids)
    await pool.execute("DELETE FROM memory_fragments WHERE fact_id = ANY($1::text[])", fact_ids)
    await pool.execute("DELETE FROM fact_state WHERE fact_id = ANY($1::text[])", fact_ids)
    neo = await _delete_edges(fact_ids)
    return {"deleted": len(rows), "neo4j_edges_deleted": neo}


async def _delete_edges(fact_ids: list[str]) -> int:
    try:
        from neo4j import AsyncGraphDatabase
    except Exception:
        return 0
    driver = AsyncGraphDatabase.driver(settings.neo4j_uri,
                                       auth=(settings.neo4j_user, settings.neo4j_password))
    n = 0
    try:
        async with driver.session() as s:
            for fid in fact_ids:
                r = await s.run(
                    "MATCH ()-[r]->() WHERE r.uuid = $uuid OR r.uuid = $uuid2 DELETE r",
                    uuid=fid, uuid2=str(fid))
                summary = await r.consume()
                n += int(summary.counters.relationships_deleted or 0)
    except Exception:
        pass
    finally:
        await driver.close()
    return n


async def isolated_entities(limit: int = 12) -> list[str]:
    """没有连边的实体名（给管理台/她看）。"""
    try:
        from neo4j import AsyncGraphDatabase
    except Exception:
        return []
    driver = AsyncGraphDatabase.driver(settings.neo4j_uri,
                                       auth=(settings.neo4j_user, settings.neo4j_password))
    try:
        async with driver.session() as s:
            r = await s.run(
                "MATCH (n:Entity) WHERE NOT (n)-[:RELATES_TO]-() RETURN n.name AS name LIMIT $lim",
                lim=limit)
            return [row["name"] for row in await r.data() if row.get("name")]
    except Exception:
        return []
    finally:
        await driver.close()


async def scan_neo4j(threshold: float = 0.86, embed_fn=None) -> list[dict]:
    """扫 Neo4j RELATES_TO 边的语义重复（真正重复多在这里）。"""
    try:
        from neo4j import AsyncGraphDatabase
    except Exception:
        return []
    driver = AsyncGraphDatabase.driver(settings.neo4j_uri,
                                       auth=(settings.neo4j_user, settings.neo4j_password))
    rows = []
    try:
        async with driver.session() as s:
            r = await s.run(
                "MATCH (a)-[e:RELATES_TO]->(b) "
                "RETURN e.uuid AS uuid, coalesce(e.fact,'') AS fact, "
                "a.name AS src, b.name AS dst, coalesce(e.created_at, '') AS created_at"
            )
            rows = [dict(x) for x in await r.data()]
    except Exception:
        rows = []
    finally:
        await driver.close()
    rows = [r for r in rows if r.get("uuid") and len(r.get("fact") or "") >= 4]
    if len(rows) < 2:
        return []
    vectors = None
    if embed_fn is not None:
        try:
            vectors = await embed_fn([r["fact"] for r in rows])
        except Exception:
            vectors = None
    used, groups = set(), []
    for i, a in enumerate(rows):
        if i in used:
            continue
        members = [i]
        for j in range(i + 1, len(rows)):
            if j in used:
                continue
            b = rows[j]
            same_rel = (a["src"] == b["src"] and a["dst"] == b["dst"])
            same_src = a["src"] == b["src"]
            sim = _cos(vectors[i], vectors[j]) if vectors is not None else _jaccard(a["fact"], b["fact"])
            # 判定：同源且很像(>=0.88) / 同关系且 >=阈值 / 极像(>=0.93)
            if sim >= threshold and (same_rel or (same_src and sim >= 0.88) or sim >= 0.93):
                members.append(j)
        if len(members) > 1:
            for j in members:
                used.add(j)
            best = max(members, key=lambda k: (len(rows[k]["fact"] or ""), str(rows[k]["created_at"])))
            groups.append({
                "keep": rows[best]["uuid"], "keep_text": rows[best]["fact"],
                "drop": [rows[k]["uuid"] for k in members if k != best],
                "drop_texts": [rows[k]["fact"] for k in members if k != best],
                "sim_kind": "embedding" if vectors is not None else "bigram",
            })
    return groups


async def _embed(texts: list[str]):
    """用 provider 的 embedding 接口（与记忆检索同一模型）。"""
    import json as _json
    import urllib.request as _rq

    base = (settings.openai_base_url or "").rstrip("/")
    key = settings.openai_api_key
    model = settings.default_embedding_model
    out = []
    for i in range(0, len(texts), 32):
        body = _json.dumps({"model": model, "input": texts[i:i + 32]}, ensure_ascii=False).encode()
        req = _rq.Request(base + "/embeddings", data=body,
                          headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                          method="POST")
        with _rq.urlopen(req, timeout=120) as r:
            d = _json.loads(r.read().decode())
        out.extend([it["embedding"] for it in d.get("data", [])])
    return out
