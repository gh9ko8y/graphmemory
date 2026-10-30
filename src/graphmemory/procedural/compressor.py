"""漏斗压缩算法 — 交互轨迹 → 固化技能。

四步（对应 ARCHITECTURE.md 3.3）：
  1. filter_noise     过滤噪音（失败步骤、连续重复）
  2. extract_key_path 提取关键路径（导致成功的核心步骤）
  3. generate_template 生成参数化模板（具体值 → 占位符）；LLM 可选，失败回退启发式
  4. 组装 Skill（指标：success_rate=1.0, usage_count=1）

LLM 调用是可选的：传入 llm 摘要器则用其结果，否则走启发式。测试不依赖 LLM。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable, Optional

from ..models.memory import InteractionTrace, Skill, SkillStep, TraceStep

logger = logging.getLogger(__name__)

# llm 摘要器协议：async (trace_dict, key_path_dict) -> dict(name,description,trigger_pattern,steps)
LlmSummarizer = Callable[[dict, list], "Any"]


class TraceCompressor:
    def filter_noise(self, steps: list[TraceStep]) -> list[TraceStep]:
        """Step 1: 删除失败步骤 + 连续重复步骤。保留成功路径。"""
        effective: list[TraceStep] = []
        prev_sig: str | None = None
        for s in steps:
            if not s.success:
                continue  # 丢掉失败步骤
            sig = f"{s.action}:{json.dumps(s.input_data, sort_keys=True, default=str)}"
            if sig == prev_sig:
                continue  # 连续重复
            effective.append(s)
            prev_sig = sig
        return effective

    def extract_key_path(self, effective_steps: list[TraceStep]) -> list[TraceStep]:
        """Step 2: 关键路径。MVP 直接取过滤后的成功步骤；
        可进一步裁掉纯 setup/teardown，这里保持完整成功路径。"""
        return list(effective_steps)

    def _heuristic_template(self, key_path: list[TraceStep]) -> list[SkillStep]:
        """Step 3 启发式：具体值 → {{key}} 占位符。"""
        out: list[SkillStep] = []
        for i, s in enumerate(key_path):
            input_template = {
                k: f"{{{{{k}}}}}" for k in s.input_data.keys()
            } if isinstance(s.input_data, dict) else {}
            out.append(
                SkillStep(
                    step_index=i,
                    action=s.action,
                    input_template=json.dumps(input_template, ensure_ascii=False),
                    expected_output="success",
                    fallback="retry up to 3x, then escalate to a stronger model",
                    validation="output indicates success",
                )
            )
        return out

    async def generate_template(
        self, key_path: list[TraceStep], llm: Optional[LlmSummarizer] = None
    ) -> tuple[list[SkillStep], dict]:
        """Step 3: 生成模板。返回 (steps, meta{name,description,trigger_pattern})。

        LLM 可选：成功则用其输出，异常/为空则回退启发式。
        """
        meta = {"name": "", "description": "", "trigger_pattern": ""}
        if llm is not None:
            try:
                kp = [s.model_dump(mode="json") for s in key_path]
                res = await llm(
                    {"task_type": "", "steps": kp},
                    kp,
                )
                if isinstance(res, dict):
                    meta = {
                        "name": str(res.get("name", "")),
                        "description": str(res.get("description", "")),
                        "trigger_pattern": str(res.get("trigger_pattern", "")),
                    }
                    raw_steps = res.get("steps", [])
                    steps = [
                        SkillStep(
                            step_index=i,
                            action=str(st.get("action", "")),
                            input_template=str(st.get("input_template", "")),
                            expected_output=str(st.get("expected_output", "")),
                            fallback=str(st.get("fallback", "")),
                            validation=str(st.get("validation", "")),
                        )
                        for i, st in enumerate(raw_steps)
                    ]
                    if steps:
                        return steps, meta
            except Exception as e:
                logger.warning(f"LLM summarize failed, fallback to heuristic: {e}")
        return self._heuristic_template(key_path), meta

    @staticmethod
    def _short_hash(trace: InteractionTrace) -> str:
        h = hashlib.sha1(trace.id.encode()).hexdigest()[:8]
        return h

    async def compress_trace(
        self,
        trace: InteractionTrace,
        llm: Optional[LlmSummarizer] = None,
    ) -> Skill:
        """完整漏斗压缩：trace -> Skill。"""
        effective = self.filter_noise(trace.steps)
        key_path = self.extract_key_path(effective)
        steps, meta = await self.generate_template(key_path, llm)

        # 若轨迹整体失败，无成功步骤 → 用原始步骤做模板但标记低成功率
        success_rate = 1.0 if trace.outcome == "SUCCESS" and key_path else 0.0

        trigger = meta["trigger_pattern"] or trace.task_type
        name = meta["name"] or f"auto_{trace.task_type}_{self._short_hash(trace)}"
        description = meta["description"] or f"自动固化的 {trace.task_type} 技能（来自轨迹 {trace.id[:8]}）"

        return Skill(
            name=name,
            description=description,
            trigger_pattern=trigger,
            steps=steps,
            success_rate=success_rate,
            usage_count=1,
            avg_duration_ms=trace.duration_ms,
            source_trace_ids=[trace.id],
            version=1,
            disabled=False,
        )
