"""多模型路由器 — 按任务类型选择最合适的模型（ARCHITECTURE 3.5）。

route(task_type, preference) -> 模型名；preference: quality/speed/cost/balanced。
route_with_fallback(task_type, failed) -> 跳过已失败模型的可用模型。
路由表基于实际部署（智谱 glm-4-plus 为主），可注入自定义表。
"""

from __future__ import annotations

from typing import Optional


class ModelRouter:
    # 默认路由表：任务类型 → {primary, fallback, local}
    DEFAULT_ROUTE_TABLE: dict[str, dict[str, str]] = {
        "extraction": {"primary": "glm-4-plus", "fallback": "deepseek-chat", "local": "glm-4-flash"},
        "conversation": {"primary": "glm-4-plus", "fallback": "deepseek-chat", "local": "glm-4-flash"},
        "emotion": {"primary": "glm-4-plus", "fallback": "deepseek-chat", "local": "glm-4-flash"},
        "embedding": {"primary": "embedding-3", "fallback": "bge-m3", "local": "nomic-embed-text"},
        "compression": {"primary": "glm-4-plus", "fallback": "deepseek-reasoner", "local": "glm-4-flash"},
    }

    def __init__(self, route_table: Optional[dict[str, dict[str, str]]] = None):
        self.table = route_table or self.DEFAULT_ROUTE_TABLE

    def route(self, task_type: str, preference: str = "balanced") -> str:
        """按任务类型 + 偏好选模型。balanced 默认主模型。"""
        route = self.table.get(task_type)
        if route is None:
            # 未登记任务类型：回退到默认主模型（extraction 主模型）
            route = self.table.get("extraction", {"primary": "glm-4-plus", "fallback": "", "local": ""})
        if preference == "quality":
            return route["primary"]
        if preference == "speed":
            return route.get("local") or route["primary"]
        if preference == "cost":
            return route.get("fallback") or route["primary"]
        return route["primary"]  # balanced

    def route_with_fallback(self, task_type: str, failed: list[str] | set[str] | None = None) -> str:
        """按优先级顺序返回第一个未被标记失败的模型。全失败则返回主模型。"""
        route = self.table.get(task_type, {"primary": "glm-4-plus", "fallback": "", "local": ""})
        failed = set(failed or [])
        for key in ("primary", "fallback", "local"):
            model = route.get(key)
            if model and model not in failed:
                return model
        return route["primary"]  # 兜底
