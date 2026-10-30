"""渗流视角的图谱指标。

两套阈值不能混用（这是最容易搞错的地方）：

1. **稀疏随机图（我们的实体图）** —— Erdős–Rényi：
   巨分量出现的判据是平均度 ⟨k⟩ = 2E/N > 1。
   过了临界点后，巨分量占比 g 解自  g = 1 − e^(−⟨k⟩·g)，
   即"能互相走到的记忆占全部记忆的比例"。

2. **二维方形格子** —— 位点渗流：
   占位概率 p > 0.592746 才出现贯穿簇（0.5927 这个数就出自这里）。
   我们的图不是格子（没有固定配位数、没有几何邻接），所以 **0.5927 不是我们的判据**，
   只把它当"占位率刻度"参照：巨分量占比离这个刻度有多远，直观感受一下浓度。

结论口径：判据看 ⟨k⟩>1；0.5927 只作参照刻度，不当阈值用。
"""

from __future__ import annotations

import math

# 二维方形格子位点渗流的经验临界值（参照刻度，不是本系统判据）
LATTICE_SITE_THRESHOLD = 0.592746


def er_giant_fraction(avg_degree: float, iters: int = 60) -> float:
    """解 g = 1 − e^(−⟨k⟩·g)，返回 ER 巨分量占比。

    ⟨k⟩ ≤ 1 时返回 0（亚临界，只有小簇）。
    用不动点迭代，收敛很快；对 g 做阻尼保证稳定。
    """
    k = float(avg_degree or 0.0)
    if k <= 1.0:
        return 0.0
    g = 0.5
    for _ in range(iters):
        nxt = 1.0 - math.exp(-k * g)
        if abs(nxt - g) < 1e-9:
            g = nxt
            break
        g = 0.6 * nxt + 0.4 * g
    return max(0.0, min(1.0, g))


def percolation_report(entities: int, fact_edges: int, isolated: int) -> dict:
    """把原始计数翻成渗流指标。"""
    n = int(entities or 0)
    e = int(fact_edges or 0)
    iso = int(isolated or 0)
    k = (2.0 * e / n) if n else 0.0
    giant = er_giant_fraction(k)
    return {
        "entities": n,
        "fact_edges": e,
        "isolated_entities": iso,
        "avg_degree": round(k, 3),
        "supercritical": k > 1.0,
        "giant_fraction": round(giant, 3),
        "giant_nodes_est": int(round(giant * n)),
        "lattice_site_threshold": LATTICE_SITE_THRESHOLD,
        "lattice_scale_ratio": round(giant / LATTICE_SITE_THRESHOLD, 3) if LATTICE_SITE_THRESHOLD else 0.0,
        "criterion": "ER: avg_degree > 1",
        "note": "0.592746 是二维方形格子位点渗流阈值，仅作参照刻度，不是本图判据",
    }
