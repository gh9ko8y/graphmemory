# -*- coding: utf-8 -*-
"""GraphMemory MCP Server — 让任何 MCP 客户端（Hermes / Claude Desktop 等）读写记忆库。

通过 stdio 传输，内部用 httpx 调 GraphMemory REST API（http://127.0.0.1:8000）。
核心逻辑全在后端 API，这里只做薄转发。

用法（在 D:\\GraphMemory 下）：
    D:\\Program\\python.exe mcp_server.py

环境变量：
    GRAPHMEMORY_BASE_URL  — API 地址，默认 http://127.0.0.1:8000/api/v1/memory
    GRAPHMEMORY_API_KEY    — 可选，若 .env 里设了 api_key 则填这里
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ── 配置 ──────────────────────────────────────────

BASE_URL = os.environ.get(
    "GRAPHMEMORY_BASE_URL", "http://127.0.0.1:8000/api/v1/memory"
)
API_KEY = os.environ.get("GRAPHMEMORY_API_KEY", "")
# 来源工作站标识：由各客户端的 MCP 配置注入（hermes / claude-desktop / bigfish），
# 这样每条记忆自动带上"是哪台工作站写的"溯源
AGENT_ID = os.environ.get("GRAPHMEMORY_AGENT_ID", "mcp")
HTTP_TIMEOUT = 180  # /add 抽取实体可能较慢

app = Server("graphmemory")


def _headers() -> dict[str, str]:
    h: dict[str, str] = {}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    return h


async def _api(method: str, path: str, json_body: dict | None = None) -> dict:
    """调 GraphMemory API，返回解析后的 JSON。"""
    url = f"{BASE_URL}{path}"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
        if method == "GET":
            r = await c.get(url, headers=_headers())
        else:
            r = await c.post(url, json=json_body or {}, headers=_headers())
        r.raise_for_status()
        return r.json()


# ── 工具定义 ──────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="add_memory",
            description=(
                "往记忆库写入一条记忆。系统会自动抽取实体和关系，并做双时态标注。\n"
                "**写作要求（强制）**：content 必须是一句结构化事实 =「谁 + 做了什么/是什么 + 时间或数字」，"
                "例如「用户A 2026-10-29 答应周六一起去海边」「用户B 来自某个兴趣群」。\n"
                "禁止：原文片段、JSON、工具参数串（如 sing_song({...})）、没有主语的短句。\n"
                "user_id 必填（对方的 QQ 号）；自己做过的事用 origin=self_action。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要记住的内容，自然语言文本。例如：'我叫张三，在深圳工作，喜欢喝茶'",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "用户 ID，区分不同人的记忆空间。默认 'default'",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "写入的 agent 标识（溯源用）。默认 'mcp'",
                    },
                    "source": {
                        "type": "string",
                        "description": "记忆来源类型：conversation / document / event。默认 conversation",
                    },
                    "origin": {
                        "type": "string",
                        "enum": ["user_said", "my_summary", "inference", "self_action"],
                        "description": "来源闸门（决定可信度）：user_said=用户原话（必须同时给 raw_quote）/ my_summary=你的转述和总结（默认）/ inference=你自己推出来的 / self_action=你自己的行为",
                    },
                    "raw_quote": {
                        "type": "string",
                        "description": "用户原话片段（逐字复制）。只有给了它，这条才会被当作「原文直录」，检索时才能说「我记得」",
                    },
                    "chat_id": {
                        "type": "string",
                        "description": "会话标识，如 group:100000001 / private:100000002。用于原文账本和按会话回溯",
                    },
                    "actors": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "这句话涉及哪些人（QQ 号或名字）",
                    },
                    "importance": {
                        "type": "number",
                        "description": "重要度 0~1，影响意义强度 S。身份0.95/偏好0.8/承诺0.85/普通0.5",
                    },
                    "source_type": {
                        "type": "string",
                        "description": "来源标记：remembered_fact(原文直录) / reconstructed_detail(转述) / inferred_detail(自己推断，会让她用「不确定」语气)",
                    },
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="search_memory",
            description=(
                "检索记忆库中当前为真的事实。用自然语言查询，返回匹配的事实列表。"
                "用于回答'用户叫什么''用户喜欢什么''用户在哪工作'等问题。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "查询内容，自然语言。例如：'用户的名字'、'工作信息'、'张三'",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "用户 ID。默认 'default'",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回的最大结果数。默认 5",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_timeline",
            description=(
                "查询某个实体的完整时间线，包括过去和现在的事实。"
                "用于了解某个实体（人、公司、地点等）的历史变化。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_name": {
                        "type": "string",
                        "description": "实体名称，如 '张三'、'腾讯'、'深圳'",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "用户 ID。默认 'default'",
                    },
                },
                "required": ["entity_name"],
            },
        ),
        Tool(
            name="chat",
            description=(
                "带记忆的对话。系统会用 System 2 慢思考：先主动调记忆工具查上下文，"
                "再生成回复。适合需要参考记忆历史的对话。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "用户的消息",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "用户 ID。默认 'default'",
                    },
                },
                "required": ["message"],
            },
        ),
        Tool(
            name="get_status",
            description=(
                "查看记忆库系统状态：三个数据库是否在线 + 记忆统计"
                "（事实总数 / 活跃 / 归档 / 降级 / agent 数 / skill 数）。"
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="memory_dedupe",
            description=(
                "扫描记忆库里的重复事实。默认 dry_run=true 只报告分组（keep=建议保留，drop=建议删除）；"
                "确认后传 dry_run=false 才真正删除（墓碑+PG+Neo4j 边，原文账本保留）。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "threshold": {"type": "number", "description": "相似度阈值，默认 0.86"},
                    "dry_run": {"type": "boolean", "description": "默认 true 只报告"},
                    "user_id": {"type": "string", "description": "只清理某个人的记忆空间（可选）"},
                },
            },
        ),
        Tool(
            name="memory_forget",
            description=(
                "删除指定记忆（fact_id 列表）。会写墓碑、删 PG 三表、删 Neo4j 边；原文账本 episodes 保留。"
                "只用于清理重复/错误记忆，别删用户在意的事。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "fact_ids": {"type": "array", "items": {"type": "string"}, "description": "要删除的 fact_id"},
                    "reason": {"type": "string", "description": "删除原因（写进墓碑）"},
                },
                "required": ["fact_ids"],
            },
        ),
        Tool(
            name="memory_stats",
            description=(
                "看记忆库当前规模：跟踪多少条事实、多少条已淡出、原文账本多少条、照片式片段多少条。"
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="list_episodes",
            description=(
                "翻原文账本：按会话或用户查看原始聊天记录（raw_text 一字不改，用来核对记忆有没有记歪）。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "会话标识，如 group:100000001 / private:100000002",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "按用户过滤（可选）",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回条数，默认 20",
                    },
                },
            },
        ),
        Tool(
            name="my_actions",
            description=(
                "查你自己最近做过什么、成没成（行为台账）。"
                "被问『你发了吗/你做了吗/你不是说…』时，先查这个再说，别凭印象回答。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "返回条数，默认 10"},
                },
            },
        ),        Tool(
            name="list_facts",
            description=(
                "列出记忆库中的全部事实（分页）。用于浏览已有记忆。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "每页数量，默认 50",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "偏移量，默认 0",
                    },
                },
            },
        ),
    ]


# ── 工具执行 ──────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        if name == "add_memory":
            _uid = str(arguments.get("user_id") or "").strip()
            if not _uid or _uid == "default" or not _uid.isdigit():
                return [TextContent(type="text", text=(
                    "❌ 拒绝写入：user_id 必填且必须是对方 QQ 号（纯数字）。\n"
                    "例：add_memory({content: \"用户A 2026-10-29 答应周六一起去海边\", "
                    "user_id: \"100000002\", chat_id: \"private_100000002\"})"
                ), isError=True)]
            _content = str(arguments.get("content") or "").strip()
            if len(_content) < 8:
                return [TextContent(type="text", text="❌ 内容太短，至少写成一句完整事实（谁+做了什么+时间/数字）", isError=True)]
            if ("{" in _content and "}" in _content) or _content.startswith(("sing_song(", "douyin_", "game_")):
                return [TextContent(type="text", text=(
                    "❌ 拒绝写入：这是原文/参数串，不是结构化事实。\n"
                    "请改写成：谁 + 做了什么 + 时间或数字。\n"
                    "例：「我 2026-10-29 用 sing_song 给群 100000001 唱了《蝴蝶》，产物 media/audio/x.mp3」"
                ), isError=True)]
            body = {
                "content": _content,
                "source": arguments.get("source", "conversation"),
                "user_id": _uid,
                "agent_id": arguments.get("agent_id", AGENT_ID),
                "origin": arguments.get("origin") or "my_summary",
            }
            if arguments.get("raw_quote"):
                body["raw_quote"] = arguments["raw_quote"]
            # 聊天记忆新增字段（原文账本 / 证据链 / 来源标记）
            for _k in ("chat_id", "actors", "importance", "source_type"):
                _v = arguments.get(_k)
                if _v not in (None, "", []):
                    body[_k] = _v
            result = await _api("POST", "/add", body)
            text = (
                f"✅ 记忆已写入\n"
                f"  episode_id: {result.get('episode_id', '?')}\n"
                f"  user_id: {body['user_id']}\n"
                f"  内容: {body['content'][:100]}"
            )

        elif name == "search_memory":
            _suid = str(arguments.get("user_id") or "").strip()
            if not _suid or _suid == "default":
                return [TextContent(type="text", text=(
                    "❌ 检索必须带 user_id（对方 QQ 号），否则会串到别人的记忆。\n"
                    "例：search_memory({query: \"用户A答应过什么\", user_id: \"100000002\"})"
                ), isError=True)]
            body = {
                "query": arguments["query"],
                "user_id": _suid,
                "top_k": arguments.get("top_k", 5),
            }
            result = await _api("POST", "/search", body)
            facts = result.get("facts", [])
            if not facts:
                text = "未找到匹配的记忆。"
            else:
                lines = [f"找到 {result.get('total_count', len(facts))} 条记忆："]
                for i, f in enumerate(facts, 1):
                    # 口径标签：决定她该用"我记得/好像记得/不确定"哪种语气
                    lab = f.get("certainty") or ("不确定" if (f.get("source_type") == "inferred_detail")
                                                 else ("好像记得" if f.get("source_type") == "reconstructed_detail" else "我想起"))
                    note = f.get("certainty_note") or ""
                    hist = "" if f.get("is_current") else "（已过期）"
                    _agen = f.get("age_note") or ""
                    if not _agen:
                        try:
                            import sys as _sys
                            if "/opt/graphmemory/src" not in _sys.path:
                                _sys.path.insert(0, "/opt/graphmemory/src")
                            from graphmemory.recall.temporal_age import extract_birth as _eb, describe as _desc
                            _bd, _by, _how = _eb(str(f.get("fact_text") or ""))
                            if _bd or _by:
                                _agen = _desc(_bd, _by)
                        except Exception:
                            pass
                    _age = f.get("age_label") or ""
                    if _agen:
                        lines.append(f"  {i}. [{lab}] {f.get('fact_text', '?')}（{_agen}）")
                        continue
                    if "待确认" in _age:
                        _tag = "（已到约定日期，主动问一下结果并记下来）"
                    else:
                        _tag = f"（{_age}前｜{note}）" if _age else (f"（{note}）" if note else "")
                    lines.append(f"  {i}. [{lab}] {f.get('fact_text', '?')}{hist}{_tag}")
                lines.append("")
                lines.append("说话口径：「我想起」可以当事实说；「好像记得」要说「我记得好像是…」；"
                             "「不确定」要说「大概是这样，具体我记不清了」。别把不确定说成确定。")
                text = "\n".join(lines)

        elif name == "get_timeline":
            body = {
                "entity_name": arguments["entity_name"],
                "user_id": arguments.get("user_id", "default"),
            }
            result = await _api("POST", "/timeline", body)
            tl = result.get("timeline", [])
            if not tl:
                text = f"未找到实体「{arguments['entity_name']}」的时间线。"
            else:
                lines = [f"实体「{result.get('entity', '?')}」的时间线（{len(tl)} 条）："]
                for i, f in enumerate(tl, 1):
                    vf = f.get("valid_from", "?")[:10]
                    vu = f.get("valid_until")
                    vu_s = vu[:10] if vu else "至今"
                    cur = "✅当前" if f.get("is_current") else "⏰过去"
                    lines.append(
                        f"  {i}. [{cur}] {f.get('fact_text', '?')}"
                        f"  ({vf} → {vu_s})"
                    )
                text = "\n".join(lines)

        elif name == "chat":
            body = {
                "message": arguments["message"],
                "user_id": arguments.get("user_id", "default"),
            }
            result = await _api("POST", "/chat", body)
            text = (
                f"回复: {result.get('response', '?')}\n"
                f"系统: System {result.get('system', 1)}"
            )
            tools = result.get("tools_called", [])
            if tools:
                text += f"\n调用工具: {', '.join(tools)}"

        elif name == "my_actions":
            limit = int(arguments.get("limit", 10) or 10)
            result = await _api("GET", f"/actions?limit={limit}")
            acts = result.get("actions") or []
            if not acts:
                text = "行为台账是空的（还没有记录）"
            else:
                lines = [f"你最近做过的 {len(acts)} 件事（新→旧）："]
                for a in acts:
                    mark = {"ok": "成功", "failed": "失败", "queued": "排队中", "unknown": "结果不明"}.get(a.get("outcome"), a.get("outcome"))
                    ts = (a.get("ts") or "")[:16].replace("T", " ")
                    line = f"  - {ts} {a.get('tool')} → {mark}"
                    if a.get("args"):
                        line += f"（{a.get('args')}）"
                    if a.get("detail"):
                        line += f"：{a.get('detail')}"
                    if a.get("product"):
                        line += f"｜产物 {a.get('product')}"
                    lines.append(line)
                lines.append("")
                lines.append("记住：这里没有的记录 = 你没做过。别把『打算做』说成『做过了』。")
                text = "\n".join(lines)
        elif name == "memory_stats":
            result = await _api("GET", "/memory-state")
            g = result.get("graph") or {}
            lines = [
                "📊 记忆库规模",
                f"  跟踪的事实: {result.get('memory_tracked', 0)}",
                f"  已淡出: {result.get('faded', 0)}",
                f"  原文账本: {result.get('episodes', 0)}",
                f"  照片式片段: {result.get('fragments', 0)}",
            ]
            if g:
                k = g.get("avg_degree", 0)
                lines += [
                    "",
                    "🕸 图谱连通性（渗流视角）",
                    f"  实体 N: {g.get('entities', 0)}　事实边 E: {g.get('fact_edges', 0)}",
                    f"  平均度 ⟨k⟩: {k}（2E/N，阈值 1.0）",
                    f"  孤立实体: {g.get('isolated_entities', 0)}（占比 {g.get('isolated_ratio', 0)}）",
                    f"  结论: {'已过阈值，联想链路通' if g.get('giant_component') else '⟨k⟩<1，记忆还是孤岛，先抽边再谈聪明检索'}",
                ]
                _names = g.get("isolated_entity_names") or []
                if _names:
                    lines.append("  孤立名单: " + "、".join(str(x) for x in _names[:12]))
                    lines.append("  （垃圾实体用 memory_forget 清；真人/群可以补一条关系事实连上）")
                lines.append("")
            text = "\n".join(lines)

        elif name == "memory_dedupe":
            qs = ["threshold=" + str(arguments.get("threshold", 0.86)),
                  "dry_run=" + ("true" if arguments.get("dry_run", True) else "false")]
            if arguments.get("user_id"):
                qs.append("user_id=" + str(arguments["user_id"]))
            result = await _api("POST", "/dedupe?" + "&".join(qs))
            if result.get("dry_run"):
                groups = result.get("groups") or []
                if not groups:
                    text = "没有发现重复事实。"
                else:
                    lines = [f"发现 {len(groups)} 组重复，共可删 {result.get('drop_count')} 条："]
                    for i, g in enumerate(groups, 1):
                        lines.append(f"  {i}. 保留：{g['keep_text'][:50]}（{g['keep'][:8]}）")
                        for t in g["drop_texts"]:
                            lines.append(f"     删除：{t[:50]}")
                    lines.append("确认后调 memory_dedupe({dry_run: false}) 执行。")
                    text = chr(10).join(lines)
            else:
                text = (f"✅ 已清理：删除 {result.get('deleted', 0)} 条，"
                        f"Neo4j 边 {result.get('neo4j_edges_deleted', 0)} 条")
        elif name == "memory_forget":
            result = await _api("POST", "/forget", {
                "fact_ids": arguments.get("fact_ids") or [],
                "reason": arguments.get("reason") or "manual",
            })
            text = (f"✅ 已删除 {result.get('deleted', 0)} 条记忆，"
                    f"Neo4j 边 {result.get('neo4j_edges_deleted', 0)} 条（原文账本保留）")
        elif name == "list_episodes":
            qs = []
            if arguments.get("chat_id"):
                qs.append("chat_id=" + str(arguments["chat_id"]))
            if arguments.get("user_id"):
                qs.append("user_id=" + str(arguments["user_id"]))
            qs.append("limit=" + str(int(arguments.get("limit", 20))))
            result = await _api("GET", "/episodes?" + "&".join(qs))
            eps = result.get("episodes") or []
            if not eps:
                text = "原文账本里没有符合条件的记录。"
            else:
                lines = [f"共 {result.get('total', len(eps))} 条原文："]

                def _fmt(e):
                    return (
                        "  - [" + str(e.get("created_at", ""))[:19] + "] "
                        + str(e.get("chat_id") or "-") + "｜"
                        + str(e.get("raw_text") or "")[:120]
                    )

                lines.extend(_fmt(e) for e in eps)
                text = "\n".join(lines)

        elif name == "get_status":
            result = await _api("GET", "/status")
            comp = result.get("components", {})
            mem = result.get("memory", {})
            facts = mem.get("facts", {})
            text = (
                f"系统状态: {result.get('status', '?')}\n"
                f"数据库: Neo4j={'✅' if comp.get('neo4j') else '❌'} "
                f"PostgreSQL={'✅' if comp.get('postgres') else '❌'} "
                f"Redis={'✅' if comp.get('redis') else '❌'}\n"
                f"记忆: {facts.get('total', 0)} 条（活跃 {facts.get('active', 0)} / "
                f"归档 {facts.get('archived', 0)} / 降级 {facts.get('demoted', 0)}）\n"
                f"平均置信度: {facts.get('avg_conf', 0):.2f}\n"
                f"Agents: {mem.get('agents', 0)}  Skills: {mem.get('skills', 0)}  "
                f"Traces: {mem.get('traces', 0)}"
            )

        elif name == "list_facts":
            limit = arguments.get("limit", 50)
            offset = arguments.get("offset", 0)
            result = await _api("GET", f"/facts?limit={limit}&offset={offset}")
            facts = result.get("facts", [])
            total = result.get("total", 0)
            if not facts:
                text = "记忆库为空。"
            else:
                lines = [f"记忆库事实列表（{offset + 1}-{offset + len(facts)} / 共 {total} 条）："]
                for i, f in enumerate(facts, 1):
                    lines.append(
                        f"  {i + offset}. {f.get('fact_text', '?')}"
                        f"  [user={f.get('user_id', '?')}]"
                        f"  置信度={f.get('confidence', 0):.2f}"
                    )
                text = "\n".join(lines)

        else:
            text = f"未知工具: {name}"

    except httpx.ConnectError:
        text = (
            "❌ 无法连接 GraphMemory API（http://127.0.0.1:8000）。\n"
            "请确认 GraphMemory 服务已启动（README 的快速开始），"
            "或运行 start.ps1"
        )
    except httpx.HTTPStatusError as e:
        text = f"❌ API 返回错误 {e.response.status_code}: {e.response.text[:200]}"
    except Exception as e:
        text = f"❌ 执行出错: {type(e).__name__}: {e}"

    return [TextContent(type="text", text=text)]


# ── 入口 ──────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
