"""检索竞争 + 确信口径 — 聊天侧的心脏。

手稿 04 修正版：
    竞争分 = 激活值 × 匹配度 × 资源适配度
    激活值 = wS·S + wR·新近度 + wE·E + wRel·关系权重 + wTask·未完成权重
    匹配度 = 语义重叠 + 人物匹配 + 时间匹配 + 承诺语境
    Top-K=4、θ=0.58：低于阈值留在库里，不进入当前念头。

Q_recon（防"编得很自信"）：
    Q = 来源权重 × 证据数 / (1 + 推断跳数)
    >=0.8 「我记得」 / >=0.6 「好像是」 / <0.6 「不确定」
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .config import RecallConfig
from .repository import RecallRepository

logger = logging.getLogger(__name__)

COMMITMENT_CUES = ("答应", "说好", "承诺", "帮我", "欠", "待办", "下次", "明早", "明天", "别忘了")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bigrams(text: str) -> set[str]:
    """字符 bigram（中文友好），用于免 embedding 的语义重叠粗估。"""
    t = re.sub(r"[\s，。.,!！?？的了吗呢吧]", "", text or "")
    return {t[i : i + 2] for i in range(len(t) - 1)} if len(t) > 1 else {t} if t else set()


def q_recon(
    source_type: str, evidence_count: int, inference_count: int, cfg: RecallConfig,
    has_raw_quote: bool = False,
) -> float:
    """重建可信度：来源质量 × 证据 × 推断折扣。

    关键修正（2026-10-27）：旧公式 weight(1.0) × evidence(≥1) / 1 = 恒等于 1.0，
    三档口径形同不存在，她的转述和推断都被当成"记得比较清楚"。

    新口径：
      remembered_fact + 逐字原话  → 0.95（可以说"我记得"）
      remembered_fact 无原话      → 0.75（只能说"好像记得"）
      reconstructed_detail        → 0.60（"好像记得"）
      inferred_detail             → 0.35（"不确定"）
      推断跳数每 +1，再乘 0.7 折扣。
    """
    st = source_type or "reconstructed_detail"
    if st == "remembered_fact":
        base = 0.95 if has_raw_quote else 0.75
    elif st == "reconstructed_detail":
        base = 0.60
    elif st == "inferred_detail":
        base = 0.35
    else:
        base = 0.60
    # 证据数量只做小幅加权（0.9~1.0），避免"证据多=绝对确定"的旧病
    ev = 1.0 if evidence_count <= 0 else min(1.0, 0.9 + 0.1 * evidence_count)
    return min(1.0, base * ev * (0.7 ** max(0, inference_count)))


def certainty_label(q: float, cfg: RecallConfig) -> tuple[str, str]:
    """回答口径：让她知道自己是「记得」还是「拼的」。返回 (前缀, 括注)。"""
    if q >= cfg.q_high:
        return ("我想起", "记得比较清楚")
    if q >= cfg.q_mid:
        return ("好像记得", "细节可能不准")
    return ("不确定", "大概是这样，具体细节我不确定")


@dataclass
class ScoredFact:
    fact: Any                 # Fact DTO（带动态属性 S/D/E/q/label）
    score: float              # 竞争分
    activation: float
    match: float


class CompetitiveRecall:
    """挂在检索路径末端的竞争排序器（graphiti_client.search 调用）。"""

    def __init__(self, repo: RecallRepository, config: RecallConfig | None = None):
        self.repo = repo
        self.config = config or RecallConfig()

    # ── 竞争分 ───────────────────────────────────────────

    def _activation(self, fs: dict, ms: Optional[dict], now: datetime) -> float:
        cfg = self.config
        # memory_state 列名在 PG 里是小写（S_significance 建表时未加引号）
        s = float(ms.get("s_significance", 0.5)) if ms else 0.5
        e = float(ms.get("e_emotion", 0.0)) if ms else 0.0
        d = float(ms.get("d_detail", 0.9)) if ms else 0.9

        created = fs.get("created_at") or now
        age_days = max((now - created).total_seconds() / 86400.0, 0.0)
        recency = 1.0 / (1.0 + age_days)

        conf = float(fs.get("confidence", 0.5) or 0.5)
        is_task = 1.0 if fs.get("fact_type") == "commitment" else 0.0

        a = (
            cfg.w_significance * s
            + cfg.w_recency * recency
            + cfg.w_emotion * e
            + cfg.w_relationship * conf
            + cfg.w_task * is_task
        )
        # 细节淡出的记忆：激活打折（只剩一个影子的印象，不跟鲜活的抢槽位）
        if ms and ms.get("faded"):
            a *= 0.6
        # 细节残量本身参与（D 低 → 这条记忆"变浅了"）
        a *= 0.5 + 0.5 * d
        # 检索难度 R：R 高（久未回忆）→ 更难点亮，但不会归零
        r = float(ms.get("r_retrieval", 0.3)) if ms else 0.3
        a *= 1.0 - 0.3 * min(1.0, max(0.0, r))
        return a

    def _match(self, fs: dict, query: str, now: datetime) -> float:
        cfg = self.config
        text = fs.get("fact_text") or ""
        qg, tg = _bigrams(query), _bigrams(text)
        semantic = (len(qg & tg) / len(qg | tg)) if (qg and tg) else 0.0

        person = 0.0
        for ent in (fs.get("source_entity"), fs.get("target_entity")):
            # 通用实体（我/行为等）不算人物命中，否则几乎恒真
            if ent and ent not in ("我", "行为", "你", "他", "她", "它") and ent in query:
                person = 1.0
                break

        created = fs.get("created_at") or now
        rec = 1.0 if (now - created).days <= 7 else 0.0

        commit = (
            1.0
            if fs.get("fact_type") == "commitment" and any(c in query for c in COMMITMENT_CUES)
            else 0.0
        )

        return (
            cfg.m_semantic * semantic
            + cfg.m_person * person
            + cfg.m_recency * rec
            + cfg.m_commitment * commit
        )

    @staticmethod
    def _resource_fit() -> float:
        """资源适配度 = min(1, 可用预算/所需预算)。

        单机聊天场景各候选所需预算无实测差异，取常数 1.0；
        若未来接外部预算系统，在此替换为真实值（只要每条候选
        所需预算不同即可影响排名）。
        """
        return 1.0

    # ── 主入口：富化 + 竞争排序 + 命中副作用 ────────────────

    async def rerank(self, facts: list[Any], query: str, user_id: str | None = None):
        """对 Policy 过滤后的事实做竞争排序。

        - 富化：六变量 + Q_recon + 口径标签挂到 Fact 动态属性（渲染层直接用）
        - 过滤：竞争分 < θ 不进入当前念头（留在库里）
        - 截断：Top-K
        - 副作用：命中项 recall_count+1、C+0.05（回忆巩固记忆）
        """
        if not facts:
            return facts
        cfg = self.config
        now = _now()

        states = await self.repo.get_states([f.id for f in facts])
        fact_state_rows = await self._fact_states([f.id for f in facts])

        scored: list[ScoredFact] = []
        for f in facts:
            fs = fact_state_rows.get(f.id, {})
            ms = dict(states[f.id]) if f.id in states else None

            # 富化（渲染层用）
            q = q_recon(
                fs.get("source_type", "reconstructed_detail"),
                len(fs.get("evidence_ids") or []) or int(fs.get("confirmed_count", 1) or 1),
                int(fs.get("inference_count", 0) or 0),
                cfg,
                bool(fs.get("raw_quote")),
            )
            # 时间梯度：旧记忆的"可信度"和口径按档下调（记忆本身不删，只是说得更不确定）
            from .time_gradient import tier_for
            _created = fs.get("created_at") or now
            if getattr(_created, "tzinfo", None) is None:
                from datetime import timezone as _tz
                _created = _created.replace(tzinfo=_tz.utc)
            _age_days = max((now - _created).total_seconds() / 86400.0, 0.0)
            _tier = tier_for(_age_days)
            q = min(q, _tier.q_cap)
            f.q_recon = round(q, 2)
            f.certainty_prefix = _tier.certainty
            f.certainty_note = f"{_tier.label}前"
            f.age_label = _tier.label
            f.S = float(ms["s_significance"]) if ms else None
            f.D = float(ms["d_detail"]) if ms else None
            f.E = float(ms["e_emotion"]) if ms else None
            f.faded = bool(ms["faded"]) if ms else False
            f.source_type = fs.get("source_type", "reconstructed_detail")

            activation = self._activation(fs, ms, now)
            match = self._match(fs, query, now)
            score = activation * match * self._resource_fit()
            scored.append(ScoredFact(fact=f, score=score, activation=activation, match=match))

        scored.sort(key=lambda x: x.score, reverse=True)
        if not scored or scored[0].score <= 0:
            return []

        # θ 作用在「相对竞争度」上：score / 本批最高分。
        # 手稿的 θ=0.58 是对意识访问值设的绝对阈值，但那套里候选池已被
        # 注意力焦点预筛（匹配度≈1）；聊天场景的语义相似度天然在 0.1~0.4，
        # 直接拿绝对分比 θ 会把所有记忆都挡在念头外。θ 的语义保留为
        # 「至少有最强候选 58% 的分量才进当前念头」。
        best = scored[0].score
        kept = [
            s
            for s in scored
            if s.score >= cfg.min_score_floor and (s.score / best) >= cfg.threshold
        ][: cfg.top_k]

        # 命中副作用（回忆巩固）：只有真正进入念头的才算一次回忆
        await self.repo.touch_recall(
            [s.fact.id for s in kept],
            cfg.consolidation_per_recall,
            cfg.max_consolidation,
        )
        return [s.fact for s in kept]

    async def _fact_states(self, fact_ids: list[str]) -> dict[str, dict]:
        rows = await self.repo.pool.fetch(
            """
            SELECT fact_id, fact_type, confidence, confirmed_count, source_type,
                   evidence_ids, inference_count, source_entity, target_entity,
                   fact_text, created_at, raw_quote
            FROM fact_state WHERE fact_id = ANY($1::text[])
            """,
            fact_ids,
        )
        return {r["fact_id"]: dict(r) for r in rows}

    # ── 结构化注入（chat.py 用）────────────────────────────

    def render_context(self, facts: list[Any]) -> str:
        """把竞争胜出的事实渲染成结构化注入文本。

        注意：/chat 主链路不走这里（那边拿到的是 tools 层的 dict，
        由 chat.py._render_facts 渲染）；此方法服务于调试、单测
        和未来的管理台预览。两处格式保持一致，改口径时同步改。
        """
        if not facts:
            return ""
        lines: list[str] = ["[记忆]（下面是刚想起来的事，按浮现顺序排列）"]
        for f in facts:
            prefix = getattr(f, "certainty_prefix", "我想起")
            note = getattr(f, "certainty_note", "")
            faded = "，细节已模糊" if getattr(f, "faded", False) else ""
            is_commit = getattr(f, "fact_type", None) == "commitment"
            head = "[重要承诺] " if is_commit else f"[{prefix}] "
            tail = f"（{note}{faded}）" if (note or faded) else ""
            lines.append(f"{head}{f.fact_text}{tail}")
        lines.append("")
        lines.append(
            "说话口径：带「好像记得/不确定」的，用「我记得好像是…」的语气说，"
            "别把不确定说成确定；带[重要承诺]的是答应过对方的事，主动跟进。"
        )
        return "\n".join(lines)
