"""API 路由"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from ..core.graphiti_client import GraphitiClient
from ..models import TimeSlice
from ..models.memory import TraceStep, InteractionTrace, Agent, Session
from ..policy import PolicyEngine
from ..procedural import ProceduralMemory
from ..system2 import System2Orchestrator, ToolBackend, execute_tool, tool_schemas
from ..agents import AgentManager
from ..chat import ChatService
from .schemas import (
    AddMemoryRequest,
    AddMemoryResponse,
    ActionRecordRequest,
    SearchMemoryRequest,
    MemorySearchResponse,
    FactResponse,
    TimelineRequest,
    RecordTraceRequest,
    RecordTraceResponse,
    CompressSkillRequest,
    SkillResponse,
    SkillStepResponse,
    RecordExecutionRequest,
    RegisterAgentRequest,
    AgentResponse,
    OpenSessionRequest,
    SessionResponse,
    SwitchModelRequest,
)

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])

# 全局客户端实例（由 app 启动时注入）
_client: GraphitiClient | None = None


def get_client() -> GraphitiClient:
    assert _client is not None, "GraphitiClient not initialized"
    return _client


def set_client(client: GraphitiClient) -> None:
    global _client
    _client = client


# Policy 引擎实例（由 app 启动时注入）
_policy: PolicyEngine | None = None


def set_policy_engine(policy: PolicyEngine) -> None:
    global _policy
    _policy = policy


def get_policy() -> PolicyEngine:
    assert _policy is not None, "PolicyEngine not initialized"
    return _policy


# 程序性记忆引擎（由 app 启动时注入）
_procedural: ProceduralMemory | None = None


def set_procedural(proc: ProceduralMemory) -> None:
    global _procedural
    _procedural = proc


def get_procedural() -> ProceduralMemory:
    assert _procedural is not None, "ProceduralMemory not initialized"
    return _procedural


# System 2 编排器（由 app 启动时注入）
_system2: System2Orchestrator | None = None


def set_system2(orch: System2Orchestrator) -> None:
    global _system2
    _system2 = orch


def get_system2() -> System2Orchestrator:
    assert _system2 is not None, "System2Orchestrator not initialized"
    return _system2


# 多 agent 归属管理（由 app 启动时注入）
_agents: AgentManager | None = None


def set_agents(mgr: AgentManager) -> None:
    global _agents
    _agents = mgr


def get_agents() -> AgentManager:
    assert _agents is not None, "AgentManager not initialized"
    return _agents


# Redis 缓存（由 app 启动时注入，可选）
_cache = None


def set_cache(cache) -> None:
    global _cache
    _cache = cache


def get_cache():
    return _cache


# 对话服务（由 app 启动时注入）
_chat: ChatService | None = None


def set_chat(chat: ChatService) -> None:
    global _chat
    _chat = chat


def get_chat() -> ChatService:
    assert _chat is not None, "ChatService not initialized"
    return _chat


# 聊天记忆扩展（由 app 启动时注入；双时态升级）
_recall_repo = None      # RecallRepository
_decay = None            # DecayEngine


def set_recall(recall_repo, decay_engine) -> None:
    global _recall_repo, _decay
    _recall_repo = recall_repo
    _decay = decay_engine


def get_recall_repo():
    assert _recall_repo is not None, "RecallRepository not initialized"
    return _recall_repo


def get_decay():
    assert _decay is not None, "DecayEngine not initialized"
    return _decay


# ── 写入 ──────────────────────────────────────────


@router.post("/add", response_model=AddMemoryResponse)
async def add_memory(req: AddMemoryRequest, client: GraphitiClient = Depends(get_client)):
    """添加记忆 — 对话/文档/事件等原始数据写入图谱

    聊天扩展：原文进 episodes 账本（不可变），抽出的事实带证据链与六变量初值。

    写入闸（防记忆自证）：可信度由"来源"决定，不由调用方自己声明。
      原始消息逐字（origin=user_said + raw_quote） → remembered_fact
      她的转述/总结（origin=my_summary，默认）   → reconstructed_detail
      她自己的推断（origin=inference）           → inferred_detail
      她自己的行为（origin=self_action）         → remembered_fact（系统观测）
    """
    origin = (req.origin or "my_summary").strip().lower()
    raw_quote = (req.raw_quote or "").strip() or None
    if origin == "user_said":
        source_type = "remembered_fact" if raw_quote else "reconstructed_detail"
    elif origin == "self_action":
        source_type = "remembered_fact"
    elif origin == "inference":
        source_type = "inferred_detail"
    else:
        source_type = "reconstructed_detail"
    # 显式传入的 source_type 只允许"更保守"的方向，防止绕过闸门
    if req.source_type in ("reconstructed_detail", "inferred_detail"):
        source_type = req.source_type

    episode = await client.add_episode(
        content=req.content,
        source=req.source,
        source_id=req.source_id,
        user_id=req.user_id,
        valid_from=req.valid_from,
        agent_id=req.agent_id,
        session_id=req.session_id,
        model=req.model,
        chat_id=req.chat_id,
        actors=req.actors,
        importance=req.importance,
        source_type=source_type,
        episode_meta=req.meta,
        raw_quote=raw_quote,
    )
    return AddMemoryResponse(episode_id=episode.id, source_type=source_type, has_raw_quote=bool(raw_quote))


# ── 行为账本（她做过什么 / 失败过什么） ──────────────


@router.post("/actions")
async def record_action(req: ActionRecordRequest):
    """记录一次行为。会同时写一条"行为事实"进记忆，供"我做过什么"检索。

    outcome: ok / failed / queued / unknown
    失败必须记：否则她下次仍然不知道自己翻过车，只能编原因。
    """
    repo = get_recall_repo()
    tool_name = (req.tool or "").strip()[:80] or "unknown"
    outcome = (req.outcome or "unknown").strip().lower()
    if outcome not in ("ok", "failed", "queued", "unknown"):
        outcome = "unknown"
    args_digest = (req.args_digest or "").strip()[:300] or None
    detail = (req.detail or "").strip()[:600] or None
    product = (req.product or "").strip()[:300] or None

    aid = await repo.insert_action(
        tool_name=tool_name, outcome=outcome, args_digest=args_digest,
        detail=detail, product=product, chat_id=req.chat_id,
    )

    # 行为事实文本：让她检索时能直接看到"做了什么、成没成"
    # 结构化文本：把 JSON 参数串压成人话（修复"我排了：sing_song({...})"这种机器串）
    def _humanize_args(raw: str | None) -> str:
        if not raw:
            return ""
        s = str(raw).strip()
        if not s.startswith("{"):
            return s[:60]
        try:
            import json as _j
            d = _j.loads(s)
            keys = ("song", "title", "text", "video", "audio", "path", "query", "target",
                    "action", "keys", "name", "kind")
            parts = [f"{k}={str(d[k])[:24]}" for k in keys if k in d and d[k] not in (None, "", [])]
            return "，".join(parts[:3])[:60]
        except Exception:
            return s[:40]

    _arg = _humanize_args(args_digest)
    _when = __import__("datetime").datetime.now().strftime("%m-%d %H:%M")
    if outcome == "ok":
        text = f"我 {_when} 用 {tool_name}" + (f"（{_arg}）" if _arg else "") + "，成功"
        if product:
            text += f"，产物 {str(product)[:80]}"
        ftype, sig, emo = "action", 0.5, 0.2
    elif outcome == "failed":
        text = f"我 {_when} 用 {tool_name}" + (f"（{_arg}）" if _arg else "") + "，失败" + (f"：{str(detail)[:80]}" if detail else "")
        ftype, sig, emo = "failure", 0.75, 0.5
    elif outcome == "queued":
        text = f"我 {_when} 用 {tool_name}" + (f"（{_arg}）" if _arg else "") + "，还在排队，结果未定（不能当成功）"
        ftype, sig, emo = "action", 0.6, 0.1
    else:
        text = f"我 {_when} 用 {tool_name}" + (f"（{_arg}）" if _arg else "") + "，结果不明"
        ftype, sig, emo = "action", 0.4, 0.1

    fact_id = await repo.insert_self_fact(
        fact_text=text, fact_type=ftype, significance=sig, emotion=emo,
        evidence_ids=[aid],
    )

    # 给 prompt 注入用的最近动作缓存（插件每轮读这个文件）
    try:
        import os as _os
        import json as _json
        from datetime import datetime as _dt
        _dir = _os.environ.get("ACTION_LEDGER_DIR", "/tmp/graphmemory-action-ledger")
        _os.makedirs(_dir, exist_ok=True)
        _line = {
            "ts": _dt.now().strftime("%m-%d %H:%M"),
            "tool": tool_name,
            "outcome": outcome,
            "detail": (detail or "")[:120],
            "product": product or "",
        }
        with open(_os.path.join(_dir, "recent.jsonl"), "a", encoding="utf-8") as _f:
            _f.write(_json.dumps(_line, ensure_ascii=False) + "\n")
        # 只留最近 50 条
        try:
            _rows = open(_os.path.join(_dir, "recent.jsonl"), encoding="utf-8").read().strip().split("\n")[-50:]
            open(_os.path.join(_dir, "recent.jsonl"), "w", encoding="utf-8").write("\n".join(_rows) + "\n")
        except Exception:
            _rows = []
        _mark = {"ok": "成功", "failed": "失败", "queued": "排队中", "unknown": "不明"}
        _txt = []
        for _ln in _rows[-8:]:
            try:
                _d = _json.loads(_ln)
            except Exception:
                continue
            _s = f"- {_d.get('ts','')} {_d.get('tool','')} → {_mark.get(_d.get('outcome'), _d.get('outcome'))}"
            if _d.get("detail"):
                _s += f"：{_d.get('detail')}"
            if _d.get("product"):
                _s += f"｜{_d.get('product')}"
            _txt.append(_s)
        open(_os.path.join(_dir, "recent.txt"), "w", encoding="utf-8").write("\n".join(_txt) + "\n")
    except Exception:
        pass

    return {"ok": True, "action_id": aid, "fact_id": fact_id, "outcome": outcome}


@router.get("/actions")
async def list_actions(limit: int = 20, chat_id: str | None = None):
    """最近的行为记录（她自己的动作台账）。"""
    rows = await get_recall_repo().list_actions(limit=min(200, max(1, limit)), chat_id=chat_id)
    return {
        "actions": [
            {
                "id": r["id"],
                "ts": r["ts"].isoformat() if r["ts"] else None,
                "tool": r["tool"],
                "outcome": r["outcome"],
                "args": r["args_digest"],
                "detail": r["detail"],
                "product": r["product"],
                "chat_id": r["chat_id"],
            }
            for r in rows
        ]
    }


# ── 检索 ──────────────────────────────────────────


@router.post("/search", response_model=MemorySearchResponse)
async def search_memory(req: SearchMemoryRequest, client: GraphitiClient = Depends(get_client)):
    """检索记忆 — 强制时间切片约束

    未传 time_start/time_end 时默认查"当前为真"的事实（TimeSlice.now()）。
    """
    if req.time_start is None and req.time_end is None:
        time_slice = TimeSlice.now()
    else:
        time_slice = TimeSlice(
            start=req.time_start or datetime.now(timezone.utc),
            end=req.time_end,
        )

    result = await client.search(
        query=req.query,
        time_slice=time_slice,
        user_id=req.user_id,
        top_k=req.top_k,
    )

    facts_payload = [
        FactResponse(
            id=f.id,
            source_entity=f.source_entity,
            target_entity=f.target_entity,
            relationship=f.relationship,
            fact_text=f.fact_text,
            valid_from=f.valid_from,
            valid_until=f.valid_until,
            is_current=f.is_current,
            confidence=f.confidence,
            fact_type=f.fact_type,
            source_type=f.source_type,
            certainty=f.certainty_prefix or None,
            certainty_note=f.certainty_note or None,
            q_recon=f.q_recon,
            S=f.S,
            D=f.D,
            E=f.E,
            faded=f.faded,
        )
        for f in result.facts
    ]

    # PG 侧兜底召回：行为/失败事实只存在 PG（没进 Neo4j），必须补进来，
    # 否则"我做过什么/我发了吗"永远搜不到答案。
    try:
        import re as _re
        _q = req.query or ""
        _prefer = bool(_re.search(r"发|做|上传|画|唱|录|成功|失败|了吗|有没有|完成|搞定|办", _q))
        pg_rows = await get_recall_repo().search_facts_pg(
            req.query, req.user_id, limit=max(4, min(10, req.top_k)),
            prefer_actions=_prefer,
        )
    except Exception:
        pg_rows = []
    seen_ids = {str(f.id) for f in facts_payload}
    seen_text = {(f.fact_text or "")[:60] for f in facts_payload}
    _cert = {
        "remembered_fact": ("我想起", "记得比较清楚"),
        "reconstructed_detail": ("好像记得", "细节可能不准"),
        "inferred_detail": ("不确定", "大概是这样，具体细节我不确定"),
    }
    for r in pg_rows:
        fid = str(r.get("fact_id") or "")
        text = r.get("fact_text") or ""
        if fid in seen_ids or text[:60] in seen_text:
            continue
        st = r.get("source_type") or "reconstructed_detail"
        prefix, note = _cert.get(st, ("好像记得", "细节可能不准"))
        # 有逐字原话支撑才算"我想起"
        if st == "remembered_fact" and not (r.get("raw_quote") or r.get("fact_type") in ("action", "failure")):
            prefix, note = ("好像记得", "细节可能不准")
        facts_payload.append(FactResponse(
            id=fid,
            source_entity=r.get("source_entity") or "我",
            target_entity=r.get("target_entity") or "",
            relationship=r.get("relationship") or "",
            fact_text=text,
            valid_from=r.get("valid_from"),
            valid_until=r.get("valid_until"),
            is_current=r.get("valid_until") is None,
            confidence=float(r.get("confidence") or 0.5),
            fact_type=r.get("fact_type"),
            source_type=st,
            certainty=prefix,
            certainty_note=note,
            q_recon=(0.95 if prefix == "我想起" else (0.65 if prefix == "好像记得" else 0.35)),
            S=None, D=None, E=None, faded=False,
        ))
        seen_ids.add(fid)
        seen_text.add(text[:60])

    return MemorySearchResponse(
        facts=facts_payload,
        total_count=len(facts_payload),
        query_time_ms=result.query_time_ms,
    )


# ── 时间线 ──────────────────────────────────────────


@router.post("/timeline")
async def get_timeline(req: TimelineRequest, client: GraphitiClient = Depends(get_client)):
    """查询实体完整时间线"""
    facts = await client.get_entity_timeline(
        entity_name=req.entity_name,
        user_id=req.user_id,
    )
    return {
        "entity": req.entity_name,
        "timeline": [
            {
                "fact_text": f.fact_text,
                "valid_from": f.valid_from.isoformat(),
                "valid_until": f.valid_until.isoformat() if f.valid_until else None,
                "is_current": f.is_current,
                "confidence": f.confidence,
            }
            for f in facts
        ],
    }


# ── 健康检查 ──────────────────────────────────────────


@router.get("/health")
async def health_check():
    return {"status": "ok", "service": "graphmemory"}


# ── Policy 维护 ──────────────────────────────────────


@router.post("/maintenance")
async def run_maintenance(policy: PolicyEngine = Depends(get_policy)):
    """执行一次 Policy 定时维护：遗忘归档 + 去重合并 + 健康报告。

    生产环境应由定时任务每天凌晨调用；此处手动触发便于验证。
    """
    report = await policy.scheduled_maintenance()
    return report


@router.get("/health-report")
async def memory_health_report(policy: PolicyEngine = Depends(get_policy)):
    """返回记忆健康报告（事实总数/活跃/归档/降级/被覆盖/平均置信度）。"""
    return await policy.repo.count_stats()


# ── 程序性记忆 ──────────────────────────────────────


def _skill_to_response(skill) -> SkillResponse:
    return SkillResponse(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        trigger_pattern=skill.trigger_pattern,
        steps=[
            SkillStepResponse(
                step_index=s.step_index,
                action=s.action,
                input_template=s.input_template,
                expected_output=s.expected_output,
                fallback=s.fallback,
                validation=s.validation,
            )
            for s in skill.steps
        ],
        success_rate=skill.success_rate,
        usage_count=skill.usage_count,
        avg_duration_ms=skill.avg_duration_ms,
        source_trace_ids=list(skill.source_trace_ids),
        version=skill.version,
        disabled=skill.disabled,
    )


@router.post("/traces", response_model=RecordTraceResponse)
async def record_trace(req: RecordTraceRequest):
    """记录一次交互轨迹（任务完整步骤序列）。"""
    steps = [
        TraceStep(
            step_index=i,
            action=s.action,
            input_data=s.input_data,
            output_data=s.output_data,
            success=s.success,
            duration_ms=s.duration_ms,
        )
        for i, s in enumerate(req.steps)
    ]
    trace = InteractionTrace(
        task_type=req.task_type,
        steps=steps,
        outcome=req.outcome,
        duration_ms=req.duration_ms,
        token_cost=req.token_cost,
    )
    trace.user_id = req.user_id  # type: ignore[attr-defined]
    await get_procedural().repo.insert_trace(trace)
    return RecordTraceResponse(trace_id=trace.id)


@router.post("/skills/compress", response_model=SkillResponse)
async def compress_skill(req: CompressSkillRequest, proc: ProceduralMemory = Depends(get_procedural)):
    """把一条轨迹漏斗压缩成固化技能。"""
    row = await proc.repo.get_trace(req.trace_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"trace not found: {req.trace_id}")
    import json as _json

    raw_steps = row["steps"] if isinstance(row["steps"], list) else _json.loads(row["steps"] or "[]")
    steps = [
        TraceStep(
            step_index=i,
            action=s.get("action", ""),
            input_data=s.get("input_data", {}),
            output_data=s.get("output_data", {}),
            success=bool(s.get("success", True)),
            duration_ms=int(s.get("duration_ms", 0)),
        )
        for i, s in enumerate(raw_steps)
    ]
    trace = InteractionTrace(
        id=str(row["id"]),
        task_type=row["task_type"],
        steps=steps,
        outcome=row["outcome"],
        duration_ms=int(row["duration_ms"]),
        token_cost=int(row["token_cost"]),
    )
    trace.user_id = row.get("user_id")  # type: ignore[attr-defined]
    skill = await proc.compress_and_save(trace, use_llm=req.use_llm)
    return _skill_to_response(skill)


@router.get("/skills", response_model=list[SkillResponse])
async def list_skills(
    task_type: str | None = None,
    min_rate: float = 0.0,
    limit: int = 100,
    offset: int = 0,
    proc: ProceduralMemory = Depends(get_procedural),
):
    """列出技能（可按 task_type/最低成功率过滤，分页）。"""
    rows = await proc.repo.list_skills(task_type=task_type, min_rate=min_rate, limit=limit, offset=offset)
    return [proc.row_to_skill(r) for r in rows]


@router.get("/skills/applicable", response_model=SkillResponse | None)
async def find_applicable_skill(
    task_type: str, proc: ProceduralMemory = Depends(get_procedural)
):
    """查找该任务类型下成功率达标的最优技能（成功率 >= 0.7）。"""
    skill = await proc.find_applicable(task_type)
    return _skill_to_response(skill) if skill else None


@router.post("/skills/{skill_id}/execution")
async def record_execution(
    skill_id: str,
    req: RecordExecutionRequest,
    proc: ProceduralMemory = Depends(get_procedural),
):
    """回报一次技能执行结果，更新成功率（增量平均）；低于阈值自动降级。"""
    return await proc.record_execution(skill_id, req.success, req.duration_ms)


@router.post("/skills/maintenance")
async def procedural_maintenance(proc: ProceduralMemory = Depends(get_procedural)):
    """程序性记忆维护：低成功率技能自动降级。"""
    return await proc.scheduled_maintenance()


# ── System 2 工具集 ─────────────────────────────────


@router.get("/tools")
async def list_tools():
    """返回 8 个记忆工具的 function-calling schema。"""
    return {"tools": tool_schemas(), "count": len(tool_schemas())}


@router.post("/tools/{name}")
async def run_tool(name: str, args: dict, orch: System2Orchestrator = Depends(get_system2)):
    """执行单个记忆工具。请求体为该工具的参数 dict。"""
    return await execute_tool(name, args, orch.backend)


@router.post("/system2/orchestrate")
async def orchestrate(req: dict, orch: System2Orchestrator = Depends(get_system2)):
    """System 2 决策编排：意图识别 → 工具选择 → 主动查询。

    请求体: {"user_input": "...", "user_id": "..."}
    """
    return await orch.run(req.get("user_input", ""), req.get("user_id"))


# ── 多 agent 归属（房间 + 箱子 + 模型溯源） ────────────


@router.post("/agents", response_model=AgentResponse)
async def register_agent(req: RegisterAgentRequest, mgr: AgentManager = Depends(get_agents)):
    """登记一个新 agent（房间）。各 agent 记忆互相隔离。"""
    return await mgr.register_agent(req.agent_id, req.name, req.description)


@router.get("/agents", response_model=list[AgentResponse])
async def list_agents(
    limit: int = 100, offset: int = 0, mgr: AgentManager = Depends(get_agents)
):
    """列出所有已登记的 agent（分页）。"""
    return await mgr.list_agents(limit, offset)


@router.get("/agents/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: str, mgr: AgentManager = Depends(get_agents)):
    agent = await mgr.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent not found: {agent_id}")
    return agent


@router.post("/agents/{agent_id}/sessions", response_model=SessionResponse)
async def open_session(
    agent_id: str, req: OpenSessionRequest, mgr: AgentManager = Depends(get_agents)
):
    """在 agent 房间内开一个会话箱子（可选标题/主题/当前模型）。"""
    try:
        return await mgr.open_session(agent_id, req.title, req.topic, req.model, req.session_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/agents/{agent_id}/sessions", response_model=list[SessionResponse])
async def list_sessions(
    agent_id: str, limit: int = 100, offset: int = 0, mgr: AgentManager = Depends(get_agents)
):
    """列出某 agent 下的所有会话箱子（分页）。"""
    return await mgr.list_sessions(agent_id, limit, offset)


@router.post("/sessions/{session_id}/model")
async def switch_model(
    session_id: str, req: SwitchModelRequest, mgr: AgentManager = Depends(get_agents)
):
    """换大模型：记忆继承（箱子不变），仅标记当前模型。"""
    try:
        return await mgr.switch_model(session_id, req.model)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/sessions/{session_id}/close")
async def close_session(session_id: str, mgr: AgentManager = Depends(get_agents)):
    """关闭会话箱子（设置 ended_at）。"""
    await mgr.close_session(session_id)
    return {"session_id": session_id, "closed": True}


# ── 监控 ──────────────────────────────────────────────


@router.get("/status")
async def status():
    """组件健康 + 记忆统计（监控）。"""
    client = get_client()
    policy = get_policy()
    proc = get_procedural()
    mgr = get_agents()
    cache = get_cache()

    # 组件连通性
    neo4j_ok = await client.ping()
    pg_ok = False
    try:
        await policy.repo.pool.fetchval("SELECT 1")
        pg_ok = True
    except Exception:
        pg_ok = False
    redis_ok = bool(await cache.ping()) if cache else False

    # 记忆统计
    fact_stats = await policy.repo.count_stats()
    proc_stats = await proc.repo.count_stats()
    agent_count = len(await mgr.list_agents(limit=1000))

    components = {"neo4j": neo4j_ok, "postgres": pg_ok, "redis": redis_ok}
    return {
        "status": "ok" if (neo4j_ok and pg_ok) else "degraded",
        "components": components,
        "memory": {
            "facts": fact_stats,
            "traces": proc_stats.get("traces", 0),
            "skills": proc_stats.get("skills", 0),
            "agents": agent_count,
        },
    }


# ── 对话集成 ──────────────────────────────────────────


@router.post("/chat")
async def chat(req: dict, chat_svc: ChatService = Depends(get_chat)):
    """完整对话循环：System 2 编排 → 记忆上下文 → LLM 回复。

    请求体: {"message": "...", "user_id": "..."}
    返回: {system, model, response, tools_called, context}
    """
    return await chat_svc.reply(req.get("message", ""), req.get("user_id"))


# ── 聊天记忆扩展（双时态升级）──────────────────────


@router.post("/decay")
async def run_decay(user_id: str | None = None):
    """手动执行一轮遗忘衰减（平时由后台任务每 6 小时跑）。

    D/S/E 按 λ 公式衰减；D<0.2 且 S>0.7 的事实降级成照片式片段。
    绝不删除任何数据。
    """
    return await get_decay().run_decay(user_id)


@router.get("/episodes")
async def list_episodes(
    chat_id: str | None = None,
    user_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """原文账本浏览（raw_text 不可变，唯一真相源）。"""
    rows = await get_recall_repo().list_episodes(chat_id, user_id, limit, offset)
    return {"episodes": [dict(r) for r in rows], "total": len(rows)}


@router.post("/dedupe")
async def dedupe(dry_run: bool = True, threshold: float = 0.86, user_id: str | None = None):
    """重复事实扫描/清理：默认 dry_run，只返回分组；apply 时按组删除多余项。"""
    from ..recall import memory_admin
    from ..recall.repository import RecallRepository

    repo = get_recall_repo()
    pool = repo.pool
    groups = await memory_admin.scan(pool, threshold=threshold, user_id=user_id)
    try:
        neo_groups = await memory_admin.scan_neo4j(threshold=threshold, embed_fn=memory_admin._embed)
        groups = (groups or []) + neo_groups
    except Exception:
        pass
    if dry_run:
        return {"dry_run": True, "groups": groups, "drop_count": sum(len(g["drop"]) for g in groups)}
    dropped = []
    for g in groups:
        dropped.extend(g["drop"])
    res = await memory_admin.forget(pool, dropped, reason="dedupe")
    return {"dry_run": False, "groups": len(groups), **res}


@router.post("/forget")
async def forget(req: dict):
    """按 fact_id 删除（墓碑 + PG + Neo4j 边；原文账本保留）。"""
    from ..recall import memory_admin

    ids = [str(x) for x in (req.get("fact_ids") or [])]
    reason = str(req.get("reason") or "manual")
    repo = get_recall_repo()
    return await memory_admin.forget(repo.pool, ids, reason=reason)


@router.get("/isolated-entities")
async def isolated_entities(limit: int = 12):
    from ..recall import memory_admin

    return {"entities": await memory_admin.isolated_entities(limit)}


@router.get("/memory-state")
async def memory_state_stats(client: GraphitiClient = Depends(get_client)):
    """六变量统计 + 图谱连通性（渗流判据 ⟨k⟩>1）。"""
    stats = dict(await get_recall_repo().count_stats())
    graph = await client.graph_stats()
    if graph:
        stats["graph"] = graph
    return stats


# ── 浏览与导出（GUI 管理台使用） ───────────────────────


@router.get("/facts")
async def list_facts(
    user_id: str | None = None,
    limit: int = 200,
    offset: int = 0,
    policy: PolicyEngine = Depends(get_policy),
):
    """列出事实视图 fact_state（按创建时间倒序，分页）。

    GUI 管理台的「记忆库」浏览页数据源。双时态与遗忘状态全量暴露。
    """
    rows = await policy.repo.pool.fetch(
        """
        SELECT * FROM fact_state
        WHERE ($1::text IS NULL OR user_id=$1)
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
        """,
        user_id,
        limit,
        offset,
    )
    return {"facts": [dict(r) for r in rows], "total": len(rows)}


@router.patch("/facts/{fact_id}")
async def update_fact(
    fact_id: str,
    req: AddMemoryRequest,
    policy: PolicyEngine = Depends(get_policy),
):
    """修改事实文本（GUI 管理台编辑）。

    同步更新 PG fact_state 和 Neo4j 边的 fact 属性。
    走简单编辑路径：仅改文本，不重抽实体（重抽留给 /add 新写入）。
    """
    new_text = (req.content or "").strip()
    if not new_text:
        raise HTTPException(400, "content 不能为空")

    row = await policy.repo.pool.fetchrow(
        "SELECT fact_id FROM fact_state WHERE fact_id = $1", fact_id
    )
    if not row:
        raise HTTPException(404, f"fact not found: {fact_id}")

    # 1. 更新 PG 视图
    await policy.repo.pool.execute(
        "UPDATE fact_state SET fact_text = $2, updated_at = NOW() WHERE fact_id = $1",
        fact_id,
        new_text,
    )
    # 2. 更新 Neo4j 边（Graphiti edge 的 fact 属性，双时态原始数据同步）
    from neo4j import AsyncGraphDatabase
    from ..config import settings as _s

    driver = AsyncGraphDatabase.driver(
        _s.neo4j_uri, auth=(_s.neo4j_user, _s.neo4j_password)
    )
    try:
        async with driver.session() as session:
            await session.run(
                "MATCH ()-[e:RELATES_TO {uuid: $id}]->() SET e.fact = $text",
                id=fact_id,
                text=new_text,
            )
    finally:
        await driver.close()

    # 3. 记审计（ledger 只追加）
    await policy.repo.append_ledger(
        "edit_fact", fact_id=fact_id, details={"new_text": new_text}
    )
    return {"fact_id": fact_id, "fact_text": new_text, "updated": True}


@router.delete("/facts/{fact_id}")
async def archive_fact(
    fact_id: str,
    policy: PolicyEngine = Depends(get_policy),
):
    """归档事实（GUI 管理台删除，软删）。

    双时态原则：不物理删除，标记 archived。检索时被 Policy 过滤，
    原始数据永久保留可追溯。
    """
    row = await policy.repo.pool.fetchrow(
        "SELECT fact_id FROM fact_state WHERE fact_id = $1", fact_id
    )
    if not row:
        raise HTTPException(404, f"fact not found: {fact_id}")

    await policy.repo.pool.execute(
        "UPDATE fact_state SET archived = TRUE, updated_at = NOW() WHERE fact_id = $1",
        fact_id,
    )
    # 同步 Neo4j 边失效（双时态 transaction time）
    from neo4j import AsyncGraphDatabase
    from ..config import settings as _s
    from datetime import datetime as _dt

    driver = AsyncGraphDatabase.driver(
        _s.neo4j_uri, auth=(_s.neo4j_user, _s.neo4j_password)
    )
    try:
        async with driver.session() as session:
            await session.run(
                "MATCH ()-[e:RELATES_TO {uuid: $id}]->() SET e.invalid_at = $now",
                id=fact_id,
                now=_dt.utcnow(),
            )
    finally:
        await driver.close()

    await policy.repo.append_ledger(
        "archive_fact", fact_id=fact_id, details={"reason": "manual_delete_via_gui"}
    )
    return {"fact_id": fact_id, "archived": True}


@router.get("/traces")
async def list_traces(
    task_type: str | None = None,
    user_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    proc: ProceduralMemory = Depends(get_procedural),
):
    """列出交互轨迹（按时间倒序，可按 task_type/user 过滤）。

    GUI 技能页的「轨迹 → 压缩」数据源。
    """
    rows = await proc.repo.list_traces_by_task(task_type or "", user_id, limit)
    return {"traces": [dict(r) for r in rows], "total": len(rows)}


def _parse_jsonb(value):
    """asyncpg 返回 JSONB 为 str，导出时还原成对象。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _export_markdown(data: dict) -> str:
    """把导出数据渲染成人类可读的 Markdown。"""
    lines = [f"# GraphMemory 记忆导出", "", f"> 导出时间：{data['exported_at']}", ""]

    lines += ["## 事实（fact_state）", "", "| 事实 | 类型 | 置信度 | 状态 | 用户 |", "|---|---|---|---|---|"]
    for f in data["facts"]:
        flags = []
        if f.get("archived"):
            flags.append("归档")
        if f.get("superseded"):
            flags.append("被覆盖")
        if f.get("demoted"):
            flags.append("降级")
        lines.append(
            f"| {f.get('fact_text', '')} | {f.get('fact_type', '')} | "
            f"{f.get('confidence', '')} | {'/'.join(flags) or '活跃'} | {f.get('user_id') or ''} |"
        )
    lines.append("")

    lines += ["## 技能（skills）", "", "| 名称 | 触发词 | 成功率 | 使用次数 | 状态 |", "|---|---|---|---|---|"]
    for s in data["skills"]:
        lines.append(
            f"| {s.get('name', '')} | {s.get('trigger_pattern', '')} | "
            f"{s.get('success_rate', '')} | {s.get('usage_count', '')} | "
            f"{'停用' if s.get('disabled') else '启用'} |"
        )
    lines.append("")

    lines += ["## 交互轨迹（interaction_traces）", "", "| 任务 | 结果 | 步骤数 | 耗时ms |", "|---|---|---|---|"]
    for t in data["traces"]:
        steps = t.get("steps") or []
        lines.append(
            f"| {t.get('task_type', '')} | {t.get('outcome', '')} | "
            f"{len(steps) if isinstance(steps, list) else '?'} | {t.get('duration_ms', '')} |"
        )
    lines.append("")

    lines += ["## Agents（房间）", "", "| ID | 名称 |", "|---|---|"]
    for a in data["agents"]:
        lines.append(f"| {a.get('agent_id', '')} | {a.get('name', '')} |")
    lines.append("")

    lines += ["## Sessions（会话箱子）", "", "| ID | 房间 | 标题 | 模型 | 状态 |", "|---|---|---|---|---|"]
    for s in data["sessions"]:
        lines.append(
            f"| {s.get('session_id', '')[:8]} | {s.get('agent_id', '')} | {s.get('title', '')} | "
            f"{s.get('current_model') or ''} | {'已关闭' if s.get('ended_at') else '进行中'} |"
        )
    lines.append("")

    lines += ["## 审计与账本", "", f"- policy_events：{len(data['policy_events'])} 条", f"- ledger：{len(data['ledger'])} 条"]
    return "\n".join(lines)


@router.get("/export")
async def export_memory(format: str = "json"):
    """全量导出记忆数据。

    format=json（默认）：结构化的完整快照（facts/ledger/traces/skills/policy_events/agents/sessions）。
    format=markdown：人类可读摘要。
    图谱（Neo4j）本体备份请用 Docker 卷备份，此处导出的是 PG 侧的记忆视图与账本。
    """
    from fastapi.responses import Response

    policy = get_policy()
    pool = policy.repo.pool

    async def rows(sql: str, *args) -> list[dict]:
        rs = await pool.fetch(sql, *args)
        out = []
        for r in rs:
            d = dict(r)
            for k, v in d.items():
                if isinstance(v, datetime):
                    d[k] = v.isoformat()
                else:
                    d[k] = _parse_jsonb(v)
            out.append(d)
        return out

    data = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "facts": await rows("SELECT * FROM fact_state ORDER BY created_at"),
        "ledger": await rows("SELECT * FROM ledger ORDER BY created_at"),
        "traces": await rows("SELECT * FROM interaction_traces ORDER BY created_at"),
        "skills": await rows("SELECT * FROM skills ORDER BY created_at"),
        "policy_events": await rows("SELECT * FROM policy_events ORDER BY created_at"),
        "agents": await rows("SELECT * FROM agents ORDER BY created_at"),
        "sessions": await rows("SELECT * FROM sessions ORDER BY created_at"),
    }

    if format == "markdown":
        return Response(
            content=_export_markdown(data),
            media_type="text/markdown; charset=utf-8",
        )
    return data
