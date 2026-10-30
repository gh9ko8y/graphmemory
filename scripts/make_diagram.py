# -*- coding: utf-8 -*-
"""生成 GraphMemory 架构图（PNG）。"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
from matplotlib import font_manager

# 中文字体
for f in ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"]:
    try:
        font_manager.fontManager.addfont(f)
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
        break
    except Exception:
        pass
plt.rcParams["axes.unicode_minus"] = False

fig, ax = plt.subplots(figsize=(12, 8))
ax.set_xlim(0, 12)
ax.set_ylim(0, 8)
ax.axis("off")

def box(x, y, w, h, title, lines, fc, ec, title_color="white", title_size=12, body_size=9.5):
    p = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.12",
                       linewidth=2, edgecolor=ec, facecolor=fc, zorder=2)
    ax.add_patch(p)
    ax.text(x + w/2, y + h - 0.32, title, ha="center", va="center",
            fontsize=title_size, color=title_color, fontweight="bold", zorder=3)
    if lines:
        ax.text(x + w/2, y + h/2 - 0.12, "\n".join(lines), ha="center", va="center",
                fontsize=body_size, color="#222222", zorder=3, linespacing=1.5)

def arrow(x1, y1, x2, y2, color="#555555"):
    a = FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=16,
                        linewidth=1.8, color=color, zorder=1)
    ax.add_patch(a)

# 第 1 层：对话层
box(1.5, 6.6, 9, 1.1, "对话层 / REST API",
    ["/chat   /add   /search   /system2/orchestrate   /status   /agents"],
    "#4F81BD", "#4F81BD")

# 第 2 层：System 2 工具集
box(1.5, 4.9, 9, 1.1, "System 2 工具集（命题三：慢思考，LLM 主动调工具）",
    ["get_user_profile  get_recent_context  search_facts  get_emotion_trend",
     "get_relationship_graph  get_skill  record_fact  timeline_query"],
    "#9BBB59", "#9BBB59")

# 第 3 层：Policy + 程序性
box(1.0, 2.6, 5.0, 1.6, "Policy 引擎（命题二）",
    ["冲突裁决  遗忘机制  压缩机制",
     "Ledger + Views(fact_state) + Policy"],
    "#E36C0A", "#E36C0A")
box(6.0, 2.6, 5.0, 1.6, "程序性记忆（坑二）",
    ["交互轨迹记录  漏斗压缩",
     "Skill 固化  成功率追踪  降级"],
    "#C0504D", "#C0504D")

# 第 4 层：存储层
box(0.6, 0.5, 3.4, 1.4, "Neo4j", ["图 = 事实网", "双时态（Graphiti）"], "#7030A0", "#7030A0")
box(4.3, 0.5, 4.4, 1.4, "PostgreSQL", ["账本 状态 技能 轨迹", "agent/session 归属"], "#31859C", "#31859C")
box(8.9, 0.5, 2.6, 1.4, "Redis", ["读路径缓存"], "#8064A2", "#8064A2")

# 侧栏：多模型路由 + 多 agent
box(10.4, 6.6, 1.6, 1.1, "ModelRouter", ["多模型", "路由"], "#808080", "#808080")
box(10.4, 4.9, 1.6, 1.1, "多 agent", ["房间+箱子", "模型溯源"], "#808080", "#808080")

# 箭头
arrow(6, 6.6, 6, 6.0)
arrow(6, 4.9, 6, 4.2)
arrow(3.5, 4.9, 3.5, 4.2)   # System2 → Policy
arrow(7.5, 4.9, 8.5, 4.2)   # System2 → 程序性
arrow(3.5, 2.6, 2.3, 1.9)   # Policy → Neo4j
arrow(3.5, 2.6, 6.5, 1.9)   # Policy → PostgreSQL
arrow(8.5, 2.6, 8.5, 1.9)   # 程序性 → PostgreSQL

plt.tight_layout()
plt.savefig("D:/GraphMemory/architecture.png", dpi=160, bbox_inches="tight",
            facecolor="white", pad_inches=0.2)
print("saved D:/GraphMemory/architecture.png")
