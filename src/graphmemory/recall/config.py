"""聊天记忆参数 — 衰减/召回/Q_recon 全部数值集中在此，便于真跑后调参。

出处（双时态记忆设计）：
  - 衰减公式按 17 号视频原文：λ_D = λ₀·(1−αE)·(1−βC)·(1+γI)，λ_S < λ_D
  - λ 起始值按他源码量级（λ₀≈0.0008/小时），上线后看两三天再调
  - Top-K=4 / θ=0.58 按手稿 04 修正版 + 工程实现
  - Q_recon 口径阈值：>=0.8「我记得」/ >=0.6「好像是」/ <0.6「不确定」
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DecayConfig:
    """遗忘作业参数（每 6 小时批量衰减）。"""

    # ── 基础衰减率（每小时）──────────────────────────
    lambda_base: float = 0.004       # λ₀：细节约 1 个月淡出（配合 λ_S 0.02 → 意义留约半年）
    lambda_s_ratio: float = 0.02     # λ_S = 0.02·λ₀（意义衰减慢 50 倍：细节走了，意义还在）
    lambda_e_ratio: float = 0.05     # λ_E = 0.05·λ₀（情绪残留留得最久）

    # ── λ_D 调制系数 ─────────────────────────────────
    alpha_emotion: float = 0.8       # α：情绪越强忘得越慢
    beta_consolidation: float = 0.6  # β：巩固越深忘得越慢
    gamma_interference: float = 0.5  # γ：干扰越多忘得越快

    # ── 淡出降级（遗忘的核心：不删数据，降成片段）──────
    fade_detail_below: float = 0.2   # D < 0.2
    fade_significance_above: float = 0.35  # 且 S > 0.35 → 生成 fragment（0.7 太严，等于永不触发）
    fragment_keep_max: int = 3       # 每条淡出事实最多保留片段数

    # ── 干扰度 I 的归一 ───────────────────────────────
    interference_cap: int = 5        # 同关系活跃事实 ≥5 条 → I=1.0

    # ── R（检索难度）随时效性增长 ──────────────────────
    r_growth_per_day: float = 0.02

    # ── 作业节奏 ─────────────────────────────────────
    interval_hours: int = 6


@dataclass(frozen=True)
class RecallConfig:
    """检索竞争参数（多线索召回 + 竞争分 + Top-K）。"""

    top_k: int = 4                    # 设计稿写 5，工程实现取 4（工作槽位）
    threshold: float = 0.58           # θ：相对竞争度门槛（≥最强候选的 58% 才进念头）
    min_score_floor: float = 1e-3     # 绝对噪声下限：分低于此的直接丢弃（防除零/纯噪声）

    # ── 激活值权重（激活 = wS·S + wR·新近 + wE·E + wRel·置信 + wTask·未完成）──
    w_significance: float = 0.30
    w_recency: float = 0.20
    w_emotion: float = 0.20
    w_relationship: float = 0.15      # 复用 fact_state.confidence 作关系权重
    w_task: float = 0.15              # commitment 类事实加成（答应过的事不该忘）

    # ── 匹配度权重（匹配 = 语义重叠 + 人物 + 时间 + 承诺）──────────
    m_semantic: float = 0.55          # 字符 bigram 重叠（免 embedding 调用，聊天低延迟）
    m_person: float = 0.25            # 实体名出现在 query 里
    m_recency: float = 0.10           # 7 天内 +1
    m_commitment: float = 0.10        # query 含承诺语境词且事实是 commitment

    # ── 命中副作用（回忆本身会巩固记忆）────────────────
    consolidation_per_recall: float = 0.05  # C += 0.05
    max_consolidation: float = 1.0

    # ── Q_recon 口径 ─────────────────────────────────
    q_high: float = 0.8               # >= 0.8 → 「我记得…」
    q_mid: float = 0.6                # >= 0.6 → 「我记得好像是…」
    # 来源权重：原文直录 1.0 / LLM 重述 0.6 / 推断 0.3
    q_weight_remembered: float = 1.0
    q_weight_reconstructed: float = 0.6
    q_weight_inferred: float = 0.3

    # ── 情绪初值（写入路径用）────────────────────────
    emotion_hit_value: float = 0.6    # 命中情绪关键词的 E 初值
    emotion_default: float = 0.1
