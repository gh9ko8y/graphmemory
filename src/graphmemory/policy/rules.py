"""Policy 规则与阈值配置（命题二的 Policy 层）。

这里的阈值对应 ARCHITECTURE.md 3.2 节的 YAML 规则定义。
所有数值集中在此，便于调参与测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PolicyConfig:
    """Policy 引擎阈值。"""

    # ── 冲突裁决 ──────────────────────────────
    reconfirm_boost_threshold: int = 3  # confirmed_count >= 3 时提升置信度
    reconfirm_boost: float = 0.2
    max_confidence: float = 1.0
    initial_confidence: float = 0.8

    # ── 遗忘机制 ──────────────────────────────
    short_term_days: int = 30  # 短期遗忘：30 天未访问
    short_term_max_access: int = 3  # 且访问次数 < 3
    long_term_days: int = 180  # 长期遗忘：180 天未访问
    long_term_max_confidence: float = 0.3  # 且置信度 < 0.3
    never_forget_types: tuple[str, ...] = ("core_identity", "user_preference", "commitment")

    # ── 检索过滤 ──────────────────────────────
    min_confidence: float = 0.1  # 检索时丢弃低于此置信度的事实

    # ── 压缩机制 ──────────────────────────────
    similar_facts_threshold: int = 5  # 同关系事实 > 5 触发合并

    # ── 事实类型识别（简单关键词启发式） ──────────
    core_identity_keywords: tuple[str, ...] = field(
        default_factory=lambda: (
            "名字",
            "姓名",
            "叫",
            "出生",
            "生日",
            "籍贯",
            "身份",
            "性别",
            "年龄",
        )
    )
    user_preference_keywords: tuple[str, ...] = field(
        default_factory=lambda: (
            "喜欢",
            "偏好",
            "讨厌",
            "爱",
            "不喜欢",
            "习惯",
            "口味",
        )
    )

    # 承诺/待办：答应过的事不该忘（衰减时 S 不降，检索竞争里 wTask 加成）
    commitment_keywords: tuple[str, ...] = field(
        default_factory=lambda: (
            "答应",
            "说好",
            "承诺",
            "帮我",
            "欠",
            "待办",
            "下次一定",
            "明早",
            "别忘了",
        )
    )


def classify_fact_type(fact_text: str, config: PolicyConfig | None = None) -> str:
    """根据事实文本启发式判断事实类型。

    核心身份 / 用户偏好 / 承诺 各有特殊待遇（永不遗忘/加权召回）；
    其余为 normal。真实系统应交给 LLM 分类，这里用关键词做 MVP。
    """
    cfg = config or PolicyConfig()
    text = fact_text or ""
    for kw in cfg.user_preference_keywords:
        if kw in text:
            return "user_preference"
    for kw in cfg.core_identity_keywords:
        if kw in text:
            return "core_identity"
    for kw in cfg.commitment_keywords:
        if kw in text:
            return "commitment"
    return "normal"
