"""Policy 执行引擎（命题二的 Policy 层）。

三类触发：
  - on_fact_added：写入后 → 冲突裁决 + 置信度提升 + 压缩检查
  - filter_and_record：检索期 → 遗忘过滤（排除归档/被覆盖/低置信）+ 记录访问
  - scheduled_maintenance：定时 → 短期/长期遗忘归档 + 去重合并 + 健康报告

设计要点：fact_state 与 graphiti edge 1:1（fact_id = edge.uuid）。
"被覆盖(superseded)"和"归档(archived)"都不删除原始数据 —— 双时态保命。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..models.memory import Fact
from .repository import PolicyRepository
from .rules import PolicyConfig, classify_fact_type

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize(text: str) -> str:
    """事实文本归一化：去空白/标点/小写，用于判断"同一事实重复确认"。"""
    return re.sub(r"[\s\u3000，。.,!！?？:：;；'\"`()（）()\[\]【】]+", "", (text or "")).lower()


class PolicyEngine:
    def __init__(self, repo: PolicyRepository, config: PolicyConfig | None = None):
        self.repo = repo
        self.config = config or PolicyConfig()
        # 聊天记忆扩展（六变量登记）；由 main.py 注入，None 时优雅降级
        self.recall_repo: Any = None

    def set_recall_repo(self, recall_repo: Any) -> None:
        self.recall_repo = recall_repo

    # ── 写入后触发 ───────────────────────────────────

    async def on_fact_added(
        self,
        fact: Fact,
        user_id: str | None = None,
        fact_type: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
        episode_id: str | None = None,
        source_type: str = "remembered_fact",
        emotion: float | None = None,
        significance: float | None = None,
    ) -> list[dict[str, Any]]:
        """新事实写入后：登记 fact_state → 冲突裁决 → 置信度提升 → 初始化六变量。

        episode_id/source_type/emotion/significance 是聊天记忆扩展：
        证据链指向 episodes 原文账本；六变量初值按情绪/重要度定。
        返回触发的 policy 事件列表（已落审计日志）。
        """
        events: list[dict[str, Any]] = []
        now = _now()
        ftype = fact_type or classify_fact_type(fact.fact_text, self.config)

        # 情绪/意义初值：未显式给时按类型保守估计
        if emotion is None:
            emotion = 0.1 if ftype == "core_identity" else 0.0
        if significance is None:
            significance = {
                "core_identity": 0.95,
                "user_preference": 0.8,
                "commitment": 0.85,
            }.get(ftype, 0.5)

        # 1. 登记 fact_state（初始置信度、confirmed_count=1、证据链指回原文）
        evidence_ids = [episode_id] if episode_id else []
        await self.repo.upsert_fact_state(
            {
                "fact_id": fact.id,
                "user_id": user_id,
                "agent_id": agent_id or fact.agent_id,
                "session_id": session_id or fact.session_id,
                "model": model or fact.model,
                "source_entity": fact.source_entity,
                "target_entity": fact.target_entity,
                "relationship": fact.relationship,
                "fact_text": fact.fact_text,
                "fact_type": ftype,
                "confidence": self.config.initial_confidence,
                "confirmed_count": 1,
                "valid_from": fact.valid_from,
                "valid_until": fact.valid_until,
                "source_type": source_type,
                "evidence_ids": evidence_ids,
                "episode_id": episode_id,
            }
        )
        # 1a2. 时效性 TTL：按类型/文本线索给有效期（到期即从检索消失，宽限后删除）
        try:
            from ..recall.validity import compute as _compute_ttl
            _v = _compute_ttl(ftype, fact.fact_text, fact.valid_from or now)
            await self.repo.set_validity(fact.id, _v["ttl_days"], _v["expires_at"],
                                         _v["due_at"], "")
            logger.info(f"validity: {fact.id[:8]} kind={_v['kind']} ttl={_v['ttl_days']} ({_v['reason']})")
        except Exception as e:
            logger.warning(f"set_validity failed: {e}")

        # 1a3. 年龄锚点：年龄随时间变，存出生日期/年份，检索时实时算
        try:
            from ..recall.temporal_age import extract_birth
            _bd, _by, _how = extract_birth(fact.fact_text, fact.valid_from or now)
            if _bd or _by:
                await self.repo.set_birth(fact.id, _bd, _by)
                logger.info(f"birth anchor: {fact.id[:8]} {_how} {_bd or _by}")
        except Exception as e:
            logger.warning(f"set_birth failed: {e}")

        # 1b. 六变量登记（D=0.9 刚发生 / R=0.3 靠线索 / C=0.1 起）
        if self.recall_repo is not None:
            try:
                await self.recall_repo.init_memory_state(
                    fact.id, significance=significance, emotion=emotion
                )
            except Exception as e:
                logger.warning(f"init_memory_state failed: {e}")
        # 1b. 只追加 Ledger：记一条 add_fact 原始事件（命题二）
        await self.repo.append_ledger(
            "add_fact",
            fact_id=fact.id,
            details={"relationship": fact.relationship, "fact_text": fact.fact_text},
            agent_id=agent_id,
            session_id=session_id,
            model=model,
        )

        # 2. 查找同 (source,target,relationship) 的现存活跃事实
        prior = await self.repo.get_active_by_relation(
            user_id, fact.source_entity, fact.target_entity, fact.relationship, fact.id
        )

        if prior:
            same_text = [p for p in prior if _normalize(p["fact_text"]) == _normalize(fact.fact_text)]
            diff_text = [p for p in prior if p not in same_text]

            # 2a. 重复确认 → 合并到新事实：累加 confirmed_count、提升置信度，旧事实标记被覆盖
            if same_text:
                total_confirmed = 1 + sum(int(p["confirmed_count"]) for p in same_text)
                new_conf = min(
                    self.config.max_confidence,
                    self.config.initial_confidence
                    + (total_confirmed - 1) * self.config.reconfirm_boost,
                )
                await self.repo.set_confidence(fact.id, new_conf, total_confirmed)
                for p in same_text:
                    await self.repo.mark_superseded(p["fact_id"], now, superseded_by=fact.id)
                    try:
                        await self.repo.set_validity(p["fact_id"], 3, now + timedelta(days=3),
                                                     None, "confirmed")
                    except Exception:
                        pass
                    await self.repo.append_ledger(
                        "supersede", fact_id=p["fact_id"],
                        details={"reason": "reconfirm_merge", "merged_into": fact.id},
                        agent_id=agent_id, session_id=session_id, model=model,
                    )
                ev = {
                    "rule": "多次确认提升置信度",
                    "type": "conflict",
                    "action": f"confirmed_count={total_confirmed}, confidence={new_conf:.2f}",
                    "fact_id": fact.id,
                }
                await self.repo.log_event(
                    ev["rule"], ev["type"], ev["action"], fact.id, user_id,
                    {"confirmed_count": total_confirmed, "confidence": new_conf},
                    agent_id=agent_id, session_id=session_id, model=model,
                )
                events.append(ev)

            # 2b. 矛盾事实 → 新覆盖旧；若双方高置信则额外标记需人工复核
            for p in diff_text:
                await self.repo.mark_superseded(p["fact_id"], now, superseded_by=fact.id)
                await self.repo.append_ledger(
                    "supersede", fact_id=p["fact_id"],
                    details={"reason": "conflict_override", "by": fact.id},
                    agent_id=agent_id, session_id=session_id, model=model,
                )
                both_high = (
                    float(p["confidence"]) >= 0.8 and self.config.initial_confidence >= 0.8
                )
                # 默认动作：新事实覆盖旧事实
                ev = {
                    "rule": "新事实覆盖旧事实",
                    "type": "conflict",
                    "action": f"old {p['fact_id']} superseded by {fact.id}",
                    "fact_id": fact.id,
                }
                await self.repo.log_event(
                    "新事实覆盖旧事实", "conflict", ev["action"], fact.id, user_id,
                    {"old_fact_id": p["fact_id"], "old_text": p["fact_text"]},
                    agent_id=agent_id, session_id=session_id, model=model,
                )
                events.append(ev)
                # 双方高置信 → 额外标记需人工复核（不替代覆盖动作）
                if both_high:
                    rev = {
                        "rule": "矛盾裁决-需人工复核",
                        "type": "conflict",
                        "action": "flag_for_user_review",
                        "fact_id": fact.id,
                    }
                    await self.repo.log_event(
                        "矛盾裁决-需人工复核", "conflict", "flag_for_user_review",
                        fact.id, user_id,
                        {"old_fact_id": p["fact_id"], "new_fact_id": fact.id},
                        agent_id=agent_id, session_id=session_id, model=model,
                    )
                    events.append(rev)

        return events

    # ── 检索期触发 ───────────────────────────────────

    async def filter_and_record(
        self, facts: list[Fact], user_id: str | None = None
    ) -> list[Fact]:
        """检索过滤：丢弃 archived/superseded/低置信，并对保留项记录访问。

        实现遗忘机制（命题：记忆=影响决策的状态）—— 不删除，只过滤。
        """
        if not facts:
            return facts
        now = _now()
        ids = [f.id for f in facts]
        states = await self.repo.get_states(ids)

        kept: list[Fact] = []
        kept_ids: list[str] = []
        for f in facts:
            st = states.get(f.id)
            if st is None:
                # 未登记状态（如图谱里的事实尚未被 Policy 跟踪）→ 默认保留
                kept.append(f)
                kept_ids.append(f.id)
                continue
            if st["archived"] or st["superseded"]:
                continue
            if float(st["confidence"]) < self.config.min_confidence:
                continue
            # 用持久化的置信度覆盖 DTO 上的默认值
            f.confidence = float(st["confidence"])
            # 用写入时存的真实实体名覆盖检索路径里的 uuid 占位（B8）
            if st["source_entity"]:
                f.source_entity = st["source_entity"]
            if st["target_entity"]:
                f.target_entity = st["target_entity"]
            kept.append(f)
            kept_ids.append(f.id)

        await self.repo.increment_access(kept_ids, now)
        return kept

    # ── 定时维护 ─────────────────────────────────────

    async def scheduled_maintenance(self, user_id: str | None = None) -> dict[str, Any]:
        """每天凌晨执行的维护任务。返回健康报告。"""
        report: dict[str, Any] = {
            "short_term_demoted": 0,
            "long_term_archived": 0,
            "dedup_merged": 0,
            "compression": "skipped(no conversation store yet)",
        }
        now = _now()

        # 1. 遗忘：扫描活跃事实
        facts = await self.repo.list_active_for_user(user_id)
        for f in facts:
            if f["fact_type"] in self.config.never_forget_types:
                continue  # 核心身份/用户偏好永不遗忘
            last = f["last_accessed"] or f["created_at"]
            age = now - last
            # 长期遗忘：180天+低置信 → 归档
            if age > timedelta(days=self.config.long_term_days) and float(
                f["confidence"]
            ) < self.config.long_term_max_confidence:
                await self.repo.mark_archived(f["fact_id"])
                await self.repo.log_event(
                    "长期遗忘", "forgetting", "archive", f["fact_id"], user_id,
                    {"age_days": age.days, "confidence": float(f["confidence"])},
                )
                report["long_term_archived"] += 1
                continue
            # 短期遗忘：30天未访问+访问少 → 降级
            if (
                age > timedelta(days=self.config.short_term_days)
                and int(f["access_count"]) < self.config.short_term_max_access
            ):
                await self.repo.mark_demoted(f["fact_id"])
                await self.repo.log_event(
                    "短期遗忘", "forgetting", "demote", f["fact_id"], user_id,
                    {"age_days": age.days, "access_count": int(f["access_count"])},
                )
                report["short_term_demoted"] += 1

        # 2. 去重合并：同关系事实 > 阈值 → 保留置信度最高，其余归档
        groups = await self.repo.find_similar_groups(user_id, self.config.similar_facts_threshold)
        for g in groups:
            members = await self.repo.fetch_group_members(
                user_id, g["source_entity"], g["target_entity"], g["relationship"]
            )
            # members 已按 confidence DESC 排序，保留首个
            for m in members[1:]:
                await self.repo.mark_archived(m["fact_id"])
                report["dedup_merged"] += 1
            await self.repo.log_event(
                "重复事实合并",
                "compression",
                f"merged {len(members)} into 1",
                members[0]["fact_id"] if members else None,
                user_id,
                {"group_size": len(members)},
            )

        # 3. 对话摘要压缩：对话存储尚未建立，暂跳过（已在 report 标注）
        # 4. 健康报告
        report["stats"] = await self.repo.count_stats()
        logger.info(f"Policy maintenance done: {report}")
        return report
