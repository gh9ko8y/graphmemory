"""程序性记忆引擎 — Skill 固化、执行反馈、成功率追踪、查找与降级。

把"实际执行"与"记忆"解耦：
  - agent 侧执行步骤并记录轨迹（TraceRecorder）或回报执行结果
  - 本引擎负责压缩成 Skill、按执行结果更新成功率（增量平均）、低成功率自动降级
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from ..config import settings
from ..models.memory import InteractionTrace, Skill
from .compressor import TraceCompressor
from .repository import ProceduralRepository

logger = logging.getLogger(__name__)


class ProceduralMemory:
    def __init__(
        self,
        repo: ProceduralRepository,
        config: Optional["ProceduralConfig"] = None,
    ):
        self.repo = repo
        self.config = config or ProceduralConfig()
        self.compressor = TraceCompressor()
        self._summarizer = None

    # ── 固化 ───────────────────────────────────────────

    async def compress_and_save(
        self, trace: InteractionTrace, use_llm: bool = False
    ) -> Skill:
        """轨迹 → 漏斗压缩 → 落库为 Skill。"""
        llm = self._make_llm_summarizer() if use_llm else None
        skill = await self.compressor.compress_trace(trace, llm=llm)
        skill.user_id = getattr(trace, "user_id", None)  # type: ignore[attr-defined]
        await self.repo.insert_skill(skill)
        logger.info(
            f"Skill saved: {skill.id} ({skill.name}, rate={skill.success_rate}, "
            f"{len(skill.steps)} steps)"
        )
        return skill

    # ── 执行反馈 + 成功率追踪 ──────────────────────────

    async def record_execution(
        self, skill_id: str, success: bool, duration_ms: int = 0
    ) -> dict[str, Any]:
        """回报一次执行结果，更新成功率（增量平均）与平均耗时。

        成功率 = (旧成功率 * 旧usage + 本次结果) / 新usage
        若更新后低于阈值 → 自动降级（disabled=True）。
        """
        row = await self.repo.get_skill(skill_id)
        if row is None:
            raise ValueError(f"skill not found: {skill_id}")

        old_rate = float(row["success_rate"])
        old_usage = int(row["usage_count"])
        new_usage = old_usage + 1
        new_rate = (old_rate * old_usage + (1.0 if success else 0.0)) / new_usage

        old_dur = int(row["avg_duration_ms"] or 0)
        new_dur = (old_dur * old_usage + duration_ms) // new_usage if duration_ms else old_dur

        await self.repo.update_skill_stats(skill_id, new_rate, new_usage, new_dur)

        demoted = False
        if new_rate < self.config.demotion_threshold:
            await self.repo.set_skill_disabled(skill_id, True)
            demoted = True
            logger.warning(
                f"Skill {skill_id} auto-demoted: rate={new_rate:.2f} < "
                f"{self.config.demotion_threshold}"
            )
        return {
            "skill_id": skill_id,
            "success": success,
            "success_rate": new_rate,
            "usage_count": new_usage,
            "avg_duration_ms": new_dur,
            "demoted": demoted,
        }

    # ── 查找适用技能 ───────────────────────────────────

    async def find_applicable(
        self, task_type: str, user_id: Optional[str] = None
    ) -> Optional[Skill]:
        """查找该任务类型下成功率达标的最优技能。"""
        row = await self.repo.find_applicable(
            task_type, user_id, min_rate=self.config.applicable_threshold
        )
        if row is None:
            return None
        return self.row_to_skill(row)

    # ── 定时维护：低成功率降级 ─────────────────────────

    async def scheduled_maintenance(self) -> dict[str, Any]:
        """扫描所有活跃技能，成功率低于阈值者降级。"""
        low = await self.repo.list_low_success(self.config.demotion_threshold)
        for r in low:
            await self.repo.set_skill_disabled(r["id"], True)
        stats = await self.repo.count_stats()
        report = {"demoted": len(low), "stats": stats}
        logger.info(f"Procedural maintenance done: {report}")
        return report

    # ── 内部 ───────────────────────────────────────────

    @staticmethod
    def row_to_skill(row) -> Skill:
        steps_json = row["steps"] if isinstance(row["steps"], list) else json.loads(row["steps"] or "[]")
        from ..models.memory import SkillStep

        steps = [
            SkillStep(
                step_index=i,
                action=s.get("action", ""),
                input_template=s.get("input_template", ""),
                expected_output=s.get("expected_output", ""),
                fallback=s.get("fallback", ""),
                validation=s.get("validation", ""),
            )
            for i, s in enumerate(steps_json)
        ]
        return Skill(
            id=str(row["id"]),
            name=row["name"],
            description=row.get("description", "") or "",
            trigger_pattern=row.get("trigger_pattern", "") or "",
            steps=steps,
            success_rate=float(row["success_rate"]),
            usage_count=int(row["usage_count"]),
            avg_duration_ms=int(row.get("avg_duration_ms") or 0),
            source_trace_ids=[str(x) for x in (row.get("source_trace_ids") or [])],
            version=int(row.get("version") or 1),
            disabled=bool(row.get("disabled")),
            created_at=row["created_at"],
        )

    def _make_llm_summarizer(self):
        """构造可选的 LLM 摘要器（智谱 json_object）。失败由调用方兜底。

        AsyncOpenAI 客户端缓存为实例属性，避免每次调用重建连接。
        """
        if self._summarizer is not None:
            return self._summarizer
        try:
            from openai import AsyncOpenAI
        except Exception:
            self._summarizer = None
            return None

        client = AsyncOpenAI(
            api_key=settings.openai_api_key, base_url=settings.openai_base_url
        )
        model = settings.default_llm_model

        async def summarize(trace_dict: dict, key_path: list) -> dict:
            schema = {
                "name": "技能名称",
                "description": "一句话描述",
                "trigger_pattern": "触发条件关键词",
                "steps": [
                    {
                        "action": "动作",
                        "input_template": "参数化输入 JSON",
                        "expected_output": "预期输出",
                        "fallback": "失败兜底",
                        "validation": "验证方法",
                    }
                ],
            }
            prompt = (
                "你是程序性记忆压缩器。把下面的成功步骤序列压缩成一个可复用的参数化技能。"
                "用具体值替换为 {{占位符}}。只返回 JSON。\n\n"
                f"步骤序列:\n{json.dumps(key_path, ensure_ascii=False)}\n\n"
                f"按此 JSON 结构返回:\n{json.dumps(schema, ensure_ascii=False)}"
            )
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You return JSON only."},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=1200,
            )
            content = resp.choices[0].message.content or "{}"
            return json.loads(content)

        self._summarizer = summarize
        return summarize


class ProceduralConfig:
    """程序性记忆阈值。"""

    def __init__(
        self,
        demotion_threshold: float = 0.5,  # 成功率低于此值 → 降级
        applicable_threshold: float = 0.7,  # 查找适用技能的最低成功率
    ):
        self.demotion_threshold = demotion_threshold
        self.applicable_threshold = applicable_threshold
