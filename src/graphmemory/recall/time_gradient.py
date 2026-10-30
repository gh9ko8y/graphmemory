# -*- coding: utf-8 -*-
"""时间梯度：记忆随时间的退化档位（用户 2026-10-29 指定）。

档位：1天 / 3天 / 7天 / 10天 / 2周 / 3周 / 1月 / 3月 / 半年 / 1年 / 更久

每档规定三件事：
  d_cap     细节完整度上限（D 不能高于它 → 旧记忆"变浅"）
  r_floor   检索难度下限（R 不能低于它 → 旧记忆更难想起）
  q_cap     可信度上限（决定口径：我记得 / 好像记得 / 不太确定 / 很久以前）
  label     时间标签（注入 prompt / 搜索结果里显示）
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tier:
    days: int
    label: str
    d_cap: float
    r_floor: float
    q_cap: float
    certainty: str


TIERS: tuple[Tier, ...] = (
    Tier(1, "1天", 1.00, 0.10, 1.00, "我记得"),
    Tier(3, "3天", 0.92, 0.18, 0.95, "我记得"),
    Tier(7, "7天", 0.84, 0.26, 0.88, "我记得"),
    Tier(10, "10天", 0.76, 0.34, 0.82, "好像记得"),
    Tier(14, "2周", 0.68, 0.42, 0.76, "好像记得"),
    Tier(21, "3周", 0.60, 0.50, 0.70, "好像记得"),
    Tier(30, "1个月", 0.52, 0.58, 0.62, "好像记得"),
    Tier(90, "3个月", 0.36, 0.68, 0.50, "不太确定"),
    Tier(180, "半年", 0.24, 0.78, 0.40, "不太确定"),
    Tier(365, "1年", 0.12, 0.88, 0.30, "很久以前"),
    Tier(10**9, "更久", 0.05, 0.95, 0.20, "很久以前"),
)


def tier_for(age_days: float) -> Tier:
    """按记忆年龄取档位（0 天 = 刚发生）。"""
    a = max(0.0, float(age_days or 0.0))
    for t in TIERS:
        if a <= t.days:
            return t
    return TIERS[-1]


def describe(age_days: float) -> str:
    t = tier_for(age_days)
    return f"{t.label}前｜{t.certainty}"


def apply_caps(age_days: float, d: float, r: float, q: float | None = None):
    """按档位封顶/兜底：D 不得高于档位上限，R 不得低于档位下限，q 不得高于档位上限。"""
    t = tier_for(age_days)
    d2 = min(float(d), t.d_cap)
    r2 = max(float(r), t.r_floor)
    q2 = None if q is None else min(float(q), t.q_cap)
    return d2, r2, q2, t


if __name__ == "__main__":
    print(f"{'档位':<8}{'D上限':>7}{'R下限':>8}{'q上限':>7}  口径")
    for t in TIERS:
        print(f"{t.label:<8}{t.d_cap:>7.2f}{t.r_floor:>8.2f}{t.q_cap:>7.2f}  {t.certainty}")
