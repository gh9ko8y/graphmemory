"""37% 法则 —— 信息收集的最优停止决策模型。

数学来源：经典秘书问题（最优停止）。候选随机到达、只接受一次、目标是最大化选到最优的概率。
- 前 r−1 个只观察不取（建立基线），之后遇到"比之前都好"的就取。
- 最优 r/N → 1/e ≈ 0.368，即 **探索 37%，之后一旦超过基线就停**。
- 选到最优的概率也趋近 1/e ≈ 0.368。

映射到我们的「工具预算」场景：
- 预算 N = 本轮允许调用的工具数（或检索步数）
- 前 explore_steps = ceil(0.37·N) 次只建基线，**不许提前收手**
- 之后：某次结果增益超过基线×(1+min_gain) → 停，收敛作答
- 或连续 stall_limit 次没有超过基线 → 也停（防死等）

注意：37% 只解决"什么时候停止收集"，不判断"收集到的东西好不好"——
增益口径要调用方给。这里只提供结构，数字可调。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

EXPLORE_RATIO = 0.37  # 1/e 的整数化近似（秘书问题最优截断比）


def explore_steps(budget: int, ratio: float = EXPLORE_RATIO) -> int:
    """前多少次只探索不定结论。budget=6 → ceil(2.22)=3。"""
    b = max(1, int(budget or 0))
    return max(1, min(b, math.ceil(b * ratio)))


@dataclass
class BudgetStop:
    """探索→过基线即停。用法：

        stopper = BudgetStop(budget=6)
        for i, tool_result in enumerate(results):
            gain = my_gain(tool_result)         # 调用方给增益口径（0~1 或任意单调量）
            stopper.observe(gain)
            if stopper.should_stop():
                break
    """

    budget: int = 6
    min_gain: float = 0.10
    stall_limit: int = 2
    ratio: float = EXPLORE_RATIO
    _seen: int = 0
    _baseline: float = 0.0
    _best: float = 0.0
    _stall: int = 0
    _stopped: bool = False
    _reason: str = ""

    @property
    def explore(self) -> int:
        return explore_steps(self.budget, self.ratio)

    def observe(self, gain: float) -> None:
        try:
            g = float(gain)
        except Exception:
            g = 0.0
        self._seen += 1
        if self._seen <= self.explore:
            # 探索期：只更新基线
            self._baseline = max(self._baseline, g)
            self._best = max(self._best, g)
            return
        # 利用期：是否超过基线阈值
        if g >= self._baseline * (1.0 + self.min_gain):
            self._best = max(self._best, g)
            self._stopped = True
            self._reason = "beat_baseline"
            return
        if g > self._best + 1e-9:
            self._best = g
            self._stall = 0
        else:
            self._stall += 1
        if self._stall >= self.stall_limit:
            self._stopped = True
            self._reason = "stalled"
        if self._seen >= self.budget:
            self._stopped = True
            self._reason = self._reason or "budget_exhausted"

    def should_stop(self) -> bool:
        if self._seen >= self.budget:
            self._stopped = True
            self._reason = self._reason or "budget_exhausted"
        return self._stopped

    def report(self) -> dict:
        return {
            "budget": self.budget,
            "explore_steps": self.explore,
            "seen": self._seen,
            "baseline": round(self._baseline, 4),
            "best": round(self._best, 4),
            "stalled": self._stall,
            "stopped": self._stopped,
            "reason": self._reason,
        }
