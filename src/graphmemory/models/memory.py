"""记忆系统核心数据模型"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


# ── 双时态 ──────────────────────────────────────────────


class TimeSlice(BaseModel):
    """时间切片 — 双时态查询的硬约束"""

    start: datetime = Field(description="查询起始时间 (inclusive)")
    end: Optional[datetime] = Field(
        default=None, description="查询结束时间 (exclusive), null = 当前为真"
    )

    @classmethod
    def now(cls) -> "TimeSlice":
        """当前时间切片：只返回当前为真的事实"""
        return cls(start=_now(), end=None)

    @classmethod
    def at(cls, time: datetime) -> "TimeSlice":
        """某时间点切片：返回该时刻为真的事实"""
        return cls(start=time, end=time)

    @classmethod
    def range(cls, start: datetime, end: datetime) -> "TimeSlice":
        """时间范围切片"""
        return cls(start=start, end=end)


# ── 实体 ──────────────────────────────────────────────


class EntityType(str, Enum):
    PERSON = "Person"
    PLACE = "Place"
    EVENT = "Event"
    CONCEPT = "Concept"
    PREFERENCE = "Preference"
    SKILL = "Skill"
    EMOTION = "Emotion"


class Entity(BaseModel):
    """知识图谱节点"""

    id: str = Field(default_factory=_uuid)
    name: str
    entity_type: EntityType
    summary: str = ""
    properties: dict = Field(default_factory=dict)

    # 双时态
    valid_from: datetime = Field(default_factory=_now)
    valid_until: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_now)

    # 置信度
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    access_count: int = 0
    last_accessed: Optional[datetime] = None

    @property
    def is_current(self) -> bool:
        """当前是否为真"""
        return self.valid_until is None

    def invalidate(self, at: Optional[datetime] = None) -> None:
        """标记为失效"""
        self.valid_until = at or _now()


# ── 事实/关系 ──────────────────────────────────────────


class Fact(BaseModel):
    """知识图谱边 — 带双时态的事实"""

    id: str = Field(default_factory=_uuid)
    source_entity: str  # entity name
    target_entity: str
    relationship: str
    fact_text: str

    # 双时态
    valid_from: datetime = Field(default_factory=_now)
    valid_until: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_now)

    # 置信度与来源
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    confirmed_count: int = 1
    source_episode_id: Optional[str] = None

    # 多 agent 归属 + 模型溯源
    agent_id: Optional[str] = None
    session_id: Optional[str] = None
    model: Optional[str] = None

    # 聊天记忆扩展（检索竞争时由 rerank 富化填充；渲染层据此说「有多确定」）
    fact_type: Optional[str] = None
    source_type: str = "remembered_fact"  # remembered_fact/reconstructed_detail/inferred_detail
    q_recon: Optional[float] = None       # 重建可信度：>=0.8 记得 / >=0.6 好像 / <0.6 不确定
    certainty_prefix: str = ""            # 「我想起」/「好像记得」/「不确定」
    certainty_note: str = ""              # 括注（如「细节可能不准」）
    age_label: str = ""                   # 时间梯度档位（1天/3天/7天/…/1年），渲染层用
    age_note: str = ""                    # 年龄类事实的实时口径（如"现在 22 岁"）
    due_at: Optional[datetime] = None     # 明确日期（到日期后进入待确认）
    confirm_state: str = ""               # '' / pending / confirmed
    S: Optional[float] = None             # 意义强度（六变量，渲染/调试用）
    D: Optional[float] = None             # 细节完整度
    E: Optional[float] = None             # 情绪残留
    faded: bool = False                   # 细节已淡出，只剩片段

    @property
    def is_current(self) -> bool:
        return self.valid_until is None

    def invalidate(self, at: Optional[datetime] = None) -> None:
        self.valid_until = at or _now()


# ── 溯源 ──────────────────────────────────────────────


class Episode(BaseModel):
    """原始数据溯源 — Ledger 层"""

    id: str = Field(default_factory=_uuid)
    content: str
    source: str = "conversation"  # conversation / document / api / user_input
    source_id: Optional[str] = None
    user_id: Optional[str] = None
    timestamp: datetime = Field(default_factory=_now)

    # 提取结果
    facts_extracted: list[str] = Field(default_factory=list)  # fact IDs
    entities_extracted: list[str] = Field(default_factory=list)  # entity IDs


# ── 检索结果 ──────────────────────────────────────────


class MemorySearchResult(BaseModel):
    """检索结果"""

    facts: list[Fact]
    entities: list[Entity]
    episodes: list[Episode]
    total_count: int
    query_time_ms: float


# ── 程序性记忆 ──────────────────────────────────────


class TraceStep(BaseModel):
    """交互轨迹步骤"""

    step_index: int
    action: str
    input_data: dict = Field(default_factory=dict)
    output_data: dict = Field(default_factory=dict)
    success: bool
    duration_ms: int = 0


class InteractionTrace(BaseModel):
    """交互轨迹"""

    id: str = Field(default_factory=_uuid)
    task_type: str
    steps: list[TraceStep]
    outcome: str  # SUCCESS / FAILURE
    duration_ms: int = 0
    token_cost: int = 0
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    session_id: Optional[str] = None
    model: Optional[str] = None
    created_at: datetime = Field(default_factory=_now)


class SkillStep(BaseModel):
    """技能步骤"""

    step_index: int
    action: str
    input_template: str = ""
    expected_output: str = ""
    fallback: str = ""
    validation: str = ""


class Skill(BaseModel):
    """固化技能"""

    id: str = Field(default_factory=_uuid)
    name: str
    description: str = ""
    trigger_pattern: str = ""
    steps: list[SkillStep]
    success_rate: float = 1.0
    usage_count: int = 0
    avg_duration_ms: int = 0
    source_trace_ids: list[str] = Field(default_factory=list)
    version: int = 1
    disabled: bool = False
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    session_id: Optional[str] = None
    model: Optional[str] = None
    created_at: datetime = Field(default_factory=_now)


# ── 多 agent 归属 ──────────────────────────────────────


class Agent(BaseModel):
    """记忆的"房间"——一个 agent 一个独立隔离单元。"""

    agent_id: str
    name: str
    description: str = ""
    created_at: datetime = Field(default_factory=_now)


class Session(BaseModel):
    """记忆的"箱子"——agent 房间内的会话/主题分类。"""

    session_id: str = Field(default_factory=_uuid)
    agent_id: str
    title: str = ""
    topic: str = ""
    current_model: Optional[str] = None  # 当前接入的大模型
    started_at: datetime = Field(default_factory=_now)
    ended_at: Optional[datetime] = None
