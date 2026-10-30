"""System 2 决策编排 — System 1 快速回复 vs System 2 慢思考。

流程（对应 ARCHITECTURE.md 3.4）：
  Step 1  意图识别 → should_trigger_system2（启发式）
  Step 2  若 System 1 → 直接返回（不调工具）
  Step 3  若 System 2 → select_tools（LLM 可选，启发式兜底）→ 逐个执行工具
  Step 4  汇总工具结果（可选 LLM 摘要上下文）

LLM 调用可选：传入 llm_selector 则用其选工具，异常/为空回退启发式。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from ..config import settings
from .tools import TOOL_REGISTRY, ToolBackend, execute_tool, tool_schemas

logger = logging.getLogger(__name__)

# 触发 System 2 的信号词
SYSTEM2_SIGNALS = (
    "记得", "上次", "最近", "之前", "以前", "我们聊过", "你说过",
    "情绪", "心情", "关系", "朋友", "家人",
    "喜欢", "偏好", "习惯", "技能", "怎么",
    "？", "?",
    # 口语问句常不带问号（语音转写尤甚）：疑问词也算信号，
    # 否则「香菜好吃吗」会被当成 System 1 直接答，记忆完全不参与。
    "吗", "什么", "哪", "谁", "多少", "怎么样",
)
# System 1 兜底的简单问候
SYSTEM1_GREETINGS = ("嗨", "你好", "hello", "hi", "hey", "在吗", "在不在")


def should_trigger_system2(user_input: str) -> bool:
    """Step 1+2: 判断是否需要慢思考。

    简单问候 → System 1；含信号词或足够长的问题 → System 2。
    """
    text = (user_input or "").strip().lower()
    if not text:
        return False
    if any(text == g or text.startswith(g) for g in SYSTEM1_GREETINGS) and len(text) <= 6:
        return False
    if any(sig in text for sig in SYSTEM2_SIGNALS):
        return True
    return len(text) > 12  # 长输入默认走慢思考


def heuristic_select_tools(user_input: str) -> list[str]:
    """启发式工具选择（无 LLM 时的兜底）。"""
    text = user_input or ""
    picks: list[str] = []
    if any(k in text for k in ("情绪", "心情")):
        picks.append("get_emotion_trend")
    if any(k in text for k in ("关系", "朋友", "家人", "认识")):
        picks.append("get_relationship_graph")
    if any(k in text for k in ("上次", "最近", "之前", "聊过")):
        picks.append("get_recent_context")
    if any(k in text for k in ("技能", "怎么做", "如何")):
        picks.append("get_skill")

    # search_facts 是记忆召回的主通道（多线索召回 + 竞争排序 + Top-K + 口径分级）。
    # 聊天场景默认必须走：早先只在出现「记得/事实/知道」时调用，导致
    # 「她喜欢什么天气」这类问句只拿到档案和近期列表——语义匹配、竞争排序、
    # 确定性标注全都没用上，记忆等于没参与。
    if "search_facts" not in picks:
        picks.insert(0, "search_facts")

    # 默认至少拉取用户档案（身份背景）
    if "get_user_profile" not in picks:
        picks.insert(0, "get_user_profile")
    return picks


_SELECTOR_CACHE: dict = {}  # 模块级缓存，避免重复创建 AsyncOpenAI 客户端


def _make_llm_selector():
    """构造可选的 LLM 工具选择器（智谱）。失败由调用方兜底。"""
    if "fn" in _SELECTOR_CACHE:
        return _SELECTOR_CACHE["fn"]
    try:
        from openai import AsyncOpenAI
    except Exception:
        return None
    client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)

    async def select(user_input: str) -> list[str]:
        resp = await client.chat.completions.create(
            model=settings.default_llm_model,
            messages=[
                {
                    "role": "system",
                    "content": "你是工具选择器。根据用户输入选择需要调用的记忆工具，只返回 JSON 数组，如 [\"get_user_profile\"]。",
                },
                {
                    "role": "user",
                    "content": f"可用工具: {json.dumps([s['function']['name'] for s in tool_schemas()])}\n用户输入: {user_input}\n返回需要的工具名 JSON 数组。",
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=200,
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
        # 兼容 {tools:[...]} 或 [...]
        if isinstance(data, list):
            return [str(x) for x in data if x in TOOL_REGISTRY]
        if isinstance(data, dict):
            arr = data.get("tools") or data.get("tool_calls") or data.get("tools_to_call")
            if isinstance(arr, list):
                return [str(x) for x in arr if x in TOOL_REGISTRY]
        return []

    _SELECTOR_CACHE["fn"] = select
    return select


def _tool_gain(result: Any) -> float:
    """把一次工具结果折成 0~1 的增益（37% 停止模型用）。

    口径（可调）：
      - 出错 / 空 → 0
      - 命中列表（facts / items / results）→ min(1, 条数/5)
      - 结构化对象（档案/关系图/近期列表）有内容 → 0.5
    这只是"这次调用有没有收获"的粗估，不追求精确排序。
    """
    if not isinstance(result, dict):
        return 0.0
    if result.get("error"):
        return 0.0
    for key in ("facts", "items", "results", "messages", "timeline"):
        v = result.get(key)
        if isinstance(v, list):
            return min(1.0, len(v) / 5.0)
    return 0.5 if result else 0.0


class System2Orchestrator:
    def __init__(self, backend: ToolBackend, llm_selector=None):
        self.backend = backend
        self._llm_selector = llm_selector  # 可调用对象或 None

    async def select_tools(self, user_input: str) -> list[str]:
        """Step 3: 选择工具。LLM 可选，异常回退启发式。"""
        if self._llm_selector is not None:
            try:
                picks = await self._llm_selector(user_input)
                if picks:
                    return picks
            except Exception as e:
                logger.warning(f"LLM tool selection failed, fallback heuristic: {e}")
        return heuristic_select_tools(user_input)

    async def run(self, user_input: str, user_id: Optional[str] = None) -> dict[str, Any]:
        """完整 System 2 编排。返回决策上下文。"""
        triggered = should_trigger_system2(user_input)
        if not triggered:
            return {"system": 1, "triggered": False, "tools_called": [], "results": {}}

        names = await self.select_tools(user_input)

        # 37% 法则（最优停止）：前 ceil(0.37·N) 次只建基线、不许提前收手；
        # 之后某次增益超过基线×(1+min_gain) 即停，或连续 stall_limit 次无进步也停。
        # N = 本轮工具预算。数学上解决了"什么时候停止收集信息"，增益口径由 _tool_gain 给。
        from ..recall.stopping import BudgetStop

        stopper = BudgetStop(budget=max(1, len(names)), min_gain=0.10, stall_limit=2)
        results: dict[str, Any] = {}
        called: list[str] = []
        for name in names:
            tool = TOOL_REGISTRY.get(name)
            if not tool:
                continue
            args = {"user_id": user_id}
            # 额外参数：search_facts 需要 query
            if name == "search_facts":
                args["query"] = user_input
            if name == "get_skill":
                # 空 task_type = 返回成功率最高的技能（find_applicable 的 ILIKE '%%' 语义）。
                # TODO: 更精确的做法是让 LLM 从用户输入提取任务类型词。
                args["task_type"] = ""
            # 注意：timeline_query 需要显式 entity 参数，启发式不自动调用；
            # 若由 LLM 选择且未提供 entity，execute_tool 会返回 error 而非崩溃。
            results[name] = await execute_tool(name, args, self.backend)
            called.append(name)
            stopper.observe(_tool_gain(results[name]))
            if stopper.should_stop():
                break

        return {
            "system": 2,
            "triggered": True,
            "tools_called": called,
            "tools_planned": names,
            "results": results,
            "stop_policy": stopper.report(),
        }
