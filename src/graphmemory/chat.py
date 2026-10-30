"""对话服务 — 把 System 2 编排 + 记忆上下文 + LLM 回复串成完整对话循环。

这是"与对话系统集成"的 REST 版入口：
  用户输入 → System2Orchestrator.run（System 1/2 路由 + 工具取数）
           → 拼接记忆上下文 → ModelRouter 选对话模型 → LLM 生成回复
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional
from .config import settings
from .router import ModelRouter
from .system2 import System2Orchestrator

logger = logging.getLogger(__name__)


class ChatService:
    def __init__(
        self,
        orchestrator: System2Orchestrator,
        router: Optional[ModelRouter] = None,
    ):
        self.orchestrator = orchestrator
        self.router = router or ModelRouter()
        self._client = None  # 缓存 AsyncOpenAI

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=settings.openai_api_key, base_url=settings.openai_base_url
            )
        return self._client

    @staticmethod
    def _build_context(tool_results: dict[str, Any]) -> str:
        """把工具结果压平成一段文本上下文（供 LLM 参考）。"""
        if not tool_results:
            return ""
        lines: list[str] = []
        for name, result in tool_results.items():
            if isinstance(result, dict) and result.get("error"):
                continue
            lines.append(f"[{name}]")
            lines.append(json.dumps(result, ensure_ascii=False, default=str))
        return "\n".join(lines)

    def _build_structured_context(self, tool_results: dict[str, Any]) -> str:
        """聊天侧结构化注入：记忆走[我想起/好像记得/不确定]口径，其余工具走原格式。

        search_facts 的结果经过检索竞争（Top-4 + θ + 富化），直接渲染成
        「她想起什么 + 有多确定」，而不是 JSON dump —— 聊天模型据此
        把「记得」和「好像记得」说成两种话。
        """
        search_result = tool_results.get("search_facts")
        memory_block = ""
        if (
            isinstance(search_result, dict)
            and isinstance(search_result.get("facts"), list)
            and search_result["facts"]
        ):
            memory_block = self._render_facts(search_result["facts"])

        rest = {
            k: v
            for k, v in tool_results.items()
            if k != "search_facts" and not (isinstance(v, dict) and v.get("error"))
        }
        rest_block = self._build_context(rest)
        return "\n".join(b for b in (memory_block, rest_block) if b)

    def _render_facts(self, facts: list[dict]) -> str:
        """渲染富化后的事实列表（tools 层已带 certainty/certainty_note 字段）。"""
        note_map = {
            "我想起": "记得比较清楚",
            "好像记得": "细节可能不准",
            "不确定": "大概是这样，具体细节我不确定",
        }
        lines: list[str] = ["[记忆]（刚想起来的事，按浮现顺序排列）"]
        for f in facts:
            fact_text = f.get("fact", "")
            certainty = f.get("certainty", "")
            note = f.get("certainty_note") or note_map.get(certainty, "")
            tail = f"（{note}）" if note else ""
            lines.append(f"[{certainty or '我想起'}] {fact_text}{tail}")
        lines.append("")
        lines.append(
            "说话口径：带「好像记得/不确定」的，用「我记得好像是…」的语气说，别把不确定说成确定。"
        )
        return "\n".join(lines)

    async def reply(self, message: str, user_id: Optional[str] = None) -> dict[str, Any]:
        """生成一次回复。System 1 直接答，System 2 带记忆上下文答。"""
        orchestrated = await self.orchestrator.run(message, user_id)
        context = self._build_structured_context(orchestrated["results"])
        model = self.router.route("conversation")

        response: Optional[str] = None
        try:
            client = self._get_client()
            system_prompt = (
                "你是一个带记忆的 AI 伙伴。下面的[记忆]区是你刚刚想起来的事，"
                "每条都标了你有多确定：说出口的语气要跟着确定性走——"
                "「我想起」可以当事实说，「好像记得」要说「我记得好像是…」，"
                "「不确定」要说「大概是这样，具体我记不清了」，别把不确定说成确定。"
                "其余上下文仅供参考。"
            )
            messages = [{"role": "system", "content": system_prompt}]
            if context:
                messages.append({"role": "system", "content": f"记忆上下文:\n{context}"})
            messages.append({"role": "user", "content": message})
            resp = await client.chat.completions.create(
                model=model, messages=messages, temperature=0.7, max_tokens=800
            )
            response = resp.choices[0].message.content
        except Exception as e:
            logger.warning(f"LLM reply failed: {e}")
            response = None

        return {
            "system": orchestrated["system"],
            "model": model,
            "response": response,
            "tools_called": orchestrated["tools_called"],
            "context": context,
        }
