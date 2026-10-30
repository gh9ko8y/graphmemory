"""遗忘作业 — 按 17 号视频原文公式衰减，不自己发明。

    细节衰减   D(t) = D₀ · e^(−λ_D·t)
               λ_D  = λ₀ · (1 − αE) · (1 − βC) · (1 + γI)
    意义强度   S(t) = S₀ · e^(−λ_S·t)     λ_S = 0.1·λ₀（意义比细节衰减慢）
    情绪残留   E(t) = E₀ · e^(−λ_E·t)     λ_E = 0.05·λ₀（留得最久）

核心是「降级」不是删除：
    D < 0.2 且 S > 0.7 → 压成 fragment（照片式片段），原文与事实永不删。
「忘记经历 ≠ 抹掉经历造成的改变」——细节没了，S 和 E 留着。
"""

from __future__ import annotations

import logging
import math
from typing import Any

from .config import DecayConfig
from .repository import RecallRepository

logger = logging.getLogger(__name__)


class DecayEngine:
    def __init__(self, repo: RecallRepository, config: DecayConfig | None = None):
        self.repo = repo
        self.config = config or DecayConfig()

    async def run_decay(self, user_id: str | None = None) -> dict[str, Any]:
        """批量衰减一轮。幂等可重入；返回报告。"""
        cfg = self.config
        rows = await self.repo.list_active_for_decay(user_id)
        if not rows:
            return {"decayed": 0, "faded": 0, "fragments_created": 0, "skipped": 0}

        # 干扰度：同关系活跃邻居数 → I（归一，cap 封顶）
        peers = await self.repo.interference_counts([r["memory_id"] for r in rows])

        report = {"decayed": 0, "faded": 0, "fragments_created": 0, "skipped": 0}
        for r in rows:
            try:
                await self._decay_one(r, peers.get(r["memory_id"], 0), report)
            except Exception as e:  # 单条失败不断整轮
                logger.warning(f"decay failed for {r['memory_id']}: {e}")
                report["skipped"] += 1

        logger.info(f"DecayEngine done: {report}")
        return report

    async def _decay_one(self, row: Any, peer_count: int, report: dict) -> None:
        from datetime import datetime, timezone

        cfg = self.config
        now = datetime.now(timezone.utc)
        last = row["last_decay_at"] or now
        # t 以小时计（λ 量纲 = /小时）
        dt_hours = max((now - last).total_seconds() / 3600.0, 0.0)
        if dt_hours < 0.01:
            report["skipped"] += 1
            return

        s = float(row["s_significance"])
        d = float(row["d_detail"])
        e = float(row["e_emotion"])
        c = float(row["c_consolidation"])
        r = float(row.get("r_retrieval") or 0.3)
        # 时间梯度：按记忆年龄取档（1天/3天/7天/10天/2周/3周/1月/3月/半年/1年）
        from datetime import datetime as _dt, timezone as _tz
        from .time_gradient import tier_for
        _created = row.get("created_at") or now
        if getattr(_created, "tzinfo", None) is None:
            _created = _created.replace(tzinfo=_tz.utc)
        age_days = max((now - _created).total_seconds() / 86400.0, 0.0)
        _tier = tier_for(age_days)

        # 核心身份/偏好/承诺：S 不衰减（永不遗忘的另一种表达）
        if row["fact_type"] in ("core_identity", "user_preference", "commitment"):
            new_s = s
        else:
            new_s = s * math.exp(-cfg.lambda_base * cfg.lambda_s_ratio * dt_hours)

        # 干扰度 I：邻居数归一
        i = min(1.0, peer_count / cfg.interference_cap) if peer_count else 0.0
        lambda_d = (
            cfg.lambda_base
            * (1 - cfg.alpha_emotion * e)
            * (1 - cfg.beta_consolidation * c)
            * (1 + cfg.gamma_interference * i)
        )
        # 保底：λ_D 必须恒大于 λ_S，否则「意义比细节衰减慢」这条设计约束会被
        # 高情绪(E→1)+高巩固(C→1) 的组合击穿（此时 λ_D → 0.08λ₀ < λ_S=0.1λ₀）。
        lambda_d = max(lambda_d, cfg.lambda_base * cfg.lambda_s_ratio * 1.2)
        new_d = d * math.exp(-lambda_d * dt_hours)
        new_e = e * math.exp(-cfg.lambda_base * cfg.lambda_e_ratio * dt_hours)
        # R（检索难度）随时效性上升：越久越难想起来
        new_r = min(1.0, r + cfg.r_growth_per_day * (dt_hours / 24.0))
        # 档位封顶/兜底：旧记忆的细节不可能比"该档"更完整，检索难度不可能更低
        new_d = min(new_d, _tier.d_cap)
        new_r = max(new_r, _tier.r_floor)
        await self.repo.apply_decay(row["memory_id"], new_s, new_d, new_e, i, new_r)
        report["decayed"] += 1

        # 淡出降级（2026-10-29 修正）：D < 阈值就降级，不再要求 S 高。
        # S 高 → 压成片段（留下"这件事教了什么"）；S 低 → 直接归档（细节和意义都没了）。
        if not row["faded"] and new_d < cfg.fade_detail_below:
            if new_s > cfg.fade_significance_above:
                await self._fade_to_fragment(row, report)
            else:
                await self.repo.mark_archived(row["memory_id"], row.get("fact_id"))
                report["archived"] = report.get("archived", 0) + 1

    async def _fade_to_fragment(self, row: Any, report: dict) -> None:
        cfg = self.config
        await self.repo.mark_faded(row["memory_id"])
        report["faded"] += 1

        # 片段 = 该消息里置信度最高的几条事实文本（无 LLM 的 MVP 生成法：
        # 保结论、弃细节——"这件事教了什么"留下，"原话长什么样"走原文账本可溯）
        candidates = [(row["fact_text"], row["memory_id"])]
        if row["episode_id"]:
            for f in await self.repo.episode_facts(row["episode_id"], cfg.fragment_keep_max):
                if f["fact_text"] != row["fact_text"]:
                    candidates.append((f["fact_text"], f["fact_id"]))
        for text, fact_id in candidates[: cfg.fragment_keep_max]:
            await self.repo.insert_fragment(
                fact_id, text, episode_id=row["episode_id"], user_id=row["user_id"]
            )
            report["fragments_created"] += 1
