"""System 2 工具集 — 给 LLM 的"机器臂"，主动查询记忆（命题三）。

8 个工具，复用前三层后端：
  - 图谱层（GraphitiClient）：search_facts / record_fact / timeline_query
  - Policy 视图层（fact_state）：get_user_profile / get_recent_context /
    get_emotion_trend / get_relationship_graph
  - 程序性记忆（ProceduralMemory）：get_skill

ToolBackend 注入便于测试：graphiti 可换 fake，policy_repo/procedural 用真实 PG。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

EMOTION_KEYWORDS = (
    "开心", "快乐", "高兴", "兴奋", "满意",  # positive
    "难过", "伤心", "悲伤", "郁闷", "焦虑", "生气", "愤怒", "沮丧", "孤独",  # negative
    "平静", "疲惫", "累", "压力",  # neutral
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ToolBackend:
    """工具执行所需的后端依赖（注入，便于测试）。"""

    graphiti: Any  # GraphitiClient 或 fake
    policy_repo: Any  # PolicyRepository
    procedural: Any  # ProceduralMemory
    cache: Any = None  # CacheStore（可选，读路径加速）


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON-schema-ish {name: type_desc}
    required: list[str]
    run: Callable[[dict, ToolBackend], Any]  # async (args, backend) -> result


# ── 工具实现 ───────────────────────────────────────────


async def _get_user_profile(args: dict, be: ToolBackend) -> dict:
    """用户档案卡：核心身份 + 用户偏好（永不遗忘的事实）。"""
    uid = args.get("user_id")
    cache_key = f"graphmemory:profile:{uid}"
    # 缓存命中直接返回
    if be.cache is not None:
        cached = await be.cache.get_json(cache_key)
        if cached is not None:
            return cached

    rows = await be.policy_repo.pool.fetch(
        """
        SELECT fact_text, fact_type, source_entity, target_entity, relationship
        FROM fact_state
        WHERE ($1::text IS NULL OR user_id=$1)
          AND archived=FALSE AND superseded=FALSE
          AND fact_type IN ('core_identity','user_preference')
        ORDER BY fact_type, created_at DESC
        """,
        uid,
    )
    identity = [r["fact_text"] for r in rows if r["fact_type"] == "core_identity"]
    preferences = [r["fact_text"] for r in rows if r["fact_type"] == "user_preference"]
    result = {"user_id": uid, "identity": identity, "preferences": preferences}
    if be.cache is not None:
        await be.cache.set_json(cache_key, result, ttl=300)
    return result


async def _get_recent_context(args: dict, be: ToolBackend) -> dict:
    """最近 N 轮上下文：近期交互轨迹 + 近期事实。"""
    uid = args.get("user_id")
    n = int(args.get("n", 10))
    cache_key = f"graphmemory:context:{uid}:{n}"
    if be.cache is not None:
        cached = await be.cache.get_json(cache_key)
        if cached is not None:
            return cached

    traces = await be.procedural.repo.pool.fetch(
        """
        SELECT task_type, outcome, duration_ms, created_at
        FROM interaction_traces
        WHERE ($1::text IS NULL OR user_id=$1)
        ORDER BY created_at DESC LIMIT $2
        """,
        uid,
        n,
    )
    facts = await be.policy_repo.pool.fetch(
        """
        SELECT fact_text, created_at
        FROM fact_state
        WHERE ($1::text IS NULL OR user_id=$1)
          AND archived=FALSE AND superseded=FALSE
        ORDER BY created_at DESC LIMIT $2
        """,
        uid,
        n,
    )
    result = {
        "recent_traces": [
            {"task_type": t["task_type"], "outcome": t["outcome"]} for t in traces
        ],
        "recent_facts": [f["fact_text"] for f in facts],
    }
    if be.cache is not None:
        await be.cache.set_json(cache_key, result, ttl=120)
    return result


async def _search_facts(args: dict, be: ToolBackend) -> dict:
    """按时间切片搜索事实（双时态强制）。"""
    from ..models.memory import TimeSlice

    ts = None
    if args.get("time_start") or args.get("time_end"):
        ts = TimeSlice(
            start=args["time_start"] or _now(),
            end=args.get("time_end"),
        )
    else:
        ts = TimeSlice.now()
    result = await be.graphiti.search(
        query=args["query"],
        time_slice=ts,
        user_id=args.get("user_id"),
        top_k=int(args.get("top_k", 10)),
    )
    # 富化字段（检索竞争挂载后 Fact 带动态属性）：
    # q_recon/certainty = 她有多确定自己记得；S/D = 意义/细节残量
    facts_out = []
    for f in result.facts:
        item = {
            "fact": f.fact_text,
            "relationship": f.relationship,
            "current": f.is_current,
        }
        q = getattr(f, "q_recon", None)
        if q is not None:
            item["certainty"] = getattr(f, "certainty_prefix", "")
            item["certainty_note"] = getattr(f, "certainty_note", "")
            item["q_recon"] = q
        if getattr(f, "S", None) is not None:
            item["S"] = round(float(f.S), 2)
            item["D"] = round(float(f.D), 2)
        facts_out.append(item)
    return {"facts": facts_out, "total": result.total_count}


async def _get_emotion_trend(args: dict, be: ToolBackend) -> dict:
    """情绪趋势：从 fact_state 检出含情绪关键词的事实，算简单斜率。"""
    uid = args.get("user_id")
    days = int(args.get("days", 7))
    since = _now() - timedelta(days=days)
    rows = await be.policy_repo.pool.fetch(
        """
        SELECT fact_text, created_at
        FROM fact_state
        WHERE ($1::text IS NULL OR user_id=$1)
          AND archived=FALSE AND superseded=FALSE
          AND created_at >= $2
        ORDER BY created_at ASC
        """,
        uid,
        since,
    )
    emotions = []
    for r in rows:
        txt = r["fact_text"] or ""
        for kw in EMOTION_KEYWORDS:
            if kw in txt:
                emotions.append({"emotion": kw, "time": r["created_at"].isoformat()})
                break
    # 简单趋势：正/负情绪计数差随时间的斜率（MVP：正负比）
    pos = sum(1 for e in emotions if e["emotion"] in ("开心","快乐","高兴","兴奋","满意"))
    neg = len(emotions) - pos
    slope = (pos - neg) / max(len(emotions), 1)
    return {"emotions": emotions, "positive": pos, "negative": neg, "slope": round(slope, 3)}


async def _get_relationship_graph(args: dict, be: ToolBackend) -> dict:
    """用户关系图谱：从 fact_state 构建节点/边。"""
    uid = args.get("user_id")
    rows = await be.policy_repo.pool.fetch(
        """
        SELECT source_entity, target_entity, relationship, fact_text
        FROM fact_state
        WHERE ($1::text IS NULL OR user_id=$1)
          AND archived=FALSE AND superseded=FALSE
          AND source_entity IS NOT NULL AND target_entity IS NOT NULL
        """,
        uid,
    )
    nodes: set[str] = set()
    edges = []
    for r in rows:
        s, t, rel = r["source_entity"], r["target_entity"], r["relationship"]
        nodes.add(s)
        nodes.add(t)
        edges.append({"source": s, "target": t, "relationship": rel})
    return {"nodes": sorted(nodes), "edges": edges, "edge_count": len(edges)}


async def _get_skill(args: dict, be: ToolBackend) -> dict:
    """查找已固化的适用技能（返回步骤计划，执行留给 agent）。

    task_type 为空时返回成功率最高的活跃技能（ILIKE '%%' 匹配全部）。
    """
    task_type = (args.get("task_type") or "").strip()
    skill = await be.procedural.find_applicable(task_type)
    if skill is None:
        return {"found": False, "task_type": task_type}
    return {
        "found": True,
        "skill_id": skill.id,
        "name": skill.name,
        "success_rate": skill.success_rate,
        "steps": [
            {"action": s.action, "input_template": s.input_template, "fallback": s.fallback}
            for s in skill.steps
        ],
    }


async def _record_fact(args: dict, be: ToolBackend) -> dict:
    """记录新事实（带双时态 valid_from）。"""
    episode = await be.graphiti.add_episode(
        content=args["fact"],
        source="user_input",
        user_id=args.get("user_id"),
        valid_from=args.get("valid_from"),
    )
    return {"recorded": True, "episode_id": episode.id}


async def _timeline_query(args: dict, be: ToolBackend) -> dict:
    """查询某实体的完整时间线。"""
    facts = await be.graphiti.get_entity_timeline(
        entity_name=args["entity"], user_id=args.get("user_id")
    )
    return {
        "entity": args["entity"],
        "timeline": [
            {
                "fact": f.fact_text,
                "valid_from": f.valid_from.isoformat() if f.valid_from else None,
                "current": f.is_current,
            }
            for f in facts
        ],
    }


# ── 注册表 ─────────────────────────────────────────────

TOOL_REGISTRY: dict[str, Tool] = {
    "get_user_profile": Tool(
        name="get_user_profile",
        description="获取用户档案卡（身份、偏好、关系）",
        parameters={"user_id": "string"},
        required=["user_id"],
        run=_get_user_profile,
    ),
    "get_recent_context": Tool(
        name="get_recent_context",
        description="获取最近 N 轮对话的摘要和话题",
        parameters={"user_id": "string", "n": "int"},
        required=["user_id"],
        run=_get_recent_context,
    ),
    "search_facts": Tool(
        name="search_facts",
        description="按时间切片搜索事实（双时态）",
        parameters={"query": "string", "time_start": "string", "time_end": "string", "user_id": "string", "top_k": "int"},
        required=["query"],
        run=_search_facts,
    ),
    "get_emotion_trend": Tool(
        name="get_emotion_trend",
        description="获取情绪趋势（最近 N 天）",
        parameters={"user_id": "string", "days": "int"},
        required=["user_id"],
        run=_get_emotion_trend,
    ),
    "get_relationship_graph": Tool(
        name="get_relationship_graph",
        description="获取用户关系图谱",
        parameters={"user_id": "string", "depth": "int"},
        required=["user_id"],
        run=_get_relationship_graph,
    ),
    "get_skill": Tool(
        name="get_skill",
        description="查找已固化的适用技能",
        parameters={"task_type": "string", "context": "object"},
        required=["task_type"],
        run=_get_skill,
    ),
    "record_fact": Tool(
        name="record_fact",
        description="记录新事实（带双时态）",
        parameters={"fact": "string", "valid_from": "string", "entity": "string", "user_id": "string"},
        required=["fact"],
        run=_record_fact,
    ),
    "timeline_query": Tool(
        name="timeline_query",
        description="查询某实体的完整时间线",
        parameters={"entity": "string", "user_id": "string"},
        required=["entity"],
        run=_timeline_query,
    ),
}


def tool_schemas() -> list[dict]:
    """生成 OpenAI function-calling 格式的 schema。"""
    schemas = []
    for t in TOOL_REGISTRY.values():
        properties = {}
        for pname, ptype in t.parameters.items():
            properties[pname] = {"type": ptype if ptype != "object" else "object"}
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": t.required,
                    },
                },
            }
        )
    return schemas


async def execute_tool(name: str, args: dict, backend: ToolBackend) -> dict:
    """执行单个工具。"""
    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return await tool.run(args, backend)
    except Exception as e:
        logger.warning(f"tool {name} failed: {e}")
        return {"error": str(e)}
