"""API 请求/响应模型"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── 请求模型 ──────────────────────────────────────────


class AddMemoryRequest(BaseModel):
    """添加记忆请求"""

    content: str = Field(description="原始文本内容")
    source: str = Field(default="conversation", description="来源类型")
    source_id: Optional[str] = None
    user_id: Optional[str] = None
    valid_from: Optional[datetime] = Field(
        default=None, description="事实生效时间 (Valid Time)，默认 now"
    )
    agent_id: Optional[str] = Field(
        default=None, description="归属的 agent（房间），用于多 agent 隔离"
    )
    session_id: Optional[str] = Field(
        default=None, description="归属的会话（箱子）"
    )
    model: Optional[str] = Field(
        default=None, description="产生该记忆的大模型名（溯源）"
    )
    # ── 聊天记忆扩展（双时态升级）──────────────────────
    chat_id: Optional[str] = Field(
        default=None, description="聊天标识：group:100000001 / private:100000002"
    )
    actors: Optional[list[str]] = Field(
        default=None, description="涉及的人（QQ号/名字），进 episodes 原文账本"
    )
    importance: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description="意义强度 S 初值；不传按事实类型估计（身份0.95/偏好0.8/承诺0.85/普通0.5）",
    )
    source_type: str = Field(
        default="remembered_fact",
        description="来源标记：remembered_fact=原文直录 / reconstructed_detail=重述 / inferred_detail=推断（服务端会按 origin 强制校正）",
    )
    origin: Optional[str] = Field(
        default=None,
        description="来源闸门：user_said=用户原话（需配 raw_quote）/ my_summary=她的转述（默认）/ inference=她的推断 / self_action=她的行为",
    )
    raw_quote: Optional[str] = Field(
        default=None,
        description="用户原话片段（逐字）。有它才算原文直录；没有就只能算转述",
    )
    meta: Optional[dict] = Field(
        default=None, description="附加信息（图片/语音/引用），进 episodes.meta"
    )


class ActionRecordRequest(BaseModel):
    """行为记录请求（她做过什么 / 失败过什么）"""

    tool: str = Field(description="工具名，如 douyin_publish / sing_song / game_record")
    outcome: str = Field(default="unknown", description="ok / failed / queued / unknown")
    args_digest: Optional[str] = Field(default=None, description="参数摘要（不含长文本）")
    detail: Optional[str] = Field(default=None, description="失败原因/结果摘要")
    product: Optional[str] = Field(default=None, description="产物路径")
    chat_id: Optional[str] = None


class SearchMemoryRequest(BaseModel):
    """检索记忆请求"""

    query: str = Field(description="检索查询")
    user_id: Optional[str] = None
    time_start: Optional[datetime] = Field(
        default=None, description="时间切片起始 (inclusive)"
    )
    time_end: Optional[datetime] = Field(
        default=None, description="时间切片结束 (exclusive), null=当前为真"
    )
    top_k: int = Field(default=10, ge=1, le=100)


class TimelineRequest(BaseModel):
    """时间线查询请求"""

    entity_name: str = Field(description="实体名称")
    user_id: Optional[str] = None


# ── 响应模型 ──────────────────────────────────────────


class FactResponse(BaseModel):
    """事实响应"""

    id: str
    source_entity: str
    target_entity: str
    relationship: str
    fact_text: str
    valid_from: datetime
    valid_until: Optional[datetime] = None
    is_current: bool
    confidence: float

    # 聊天记忆富化（检索竞争后填充；未经 rerank 时为默认值）
    fact_type: Optional[str] = None
    source_type: Optional[str] = None      # remembered_fact / reconstructed_detail / inferred_detail
    certainty: Optional[str] = None        # 我想起 / 好像记得 / 不确定
    certainty_note: Optional[str] = None
    q_recon: Optional[float] = None
    S: Optional[float] = None              # 意义强度
    D: Optional[float] = None              # 细节完整度
    E: Optional[float] = None              # 情绪残留
    faded: bool = False


class MemorySearchResponse(BaseModel):
    """检索响应"""

    facts: list[FactResponse]
    total_count: int
    query_time_ms: float


class AddMemoryResponse(BaseModel):
    """添加记忆响应"""

    episode_id: str
    message: str = "Memory added successfully"
    source_type: Optional[str] = None      # 服务端校正后的来源标记
    has_raw_quote: bool = False             # 是否带逐字原话（决定能不能"当事实说"）


# ── 程序性记忆 ──────────────────────────────────────────


class TraceStepInput(BaseModel):
    action: str
    input_data: dict = Field(default_factory=dict)
    output_data: dict = Field(default_factory=dict)
    success: bool = True
    duration_ms: int = 0


class RecordTraceRequest(BaseModel):
    task_type: str = Field(description="任务类型")
    steps: list[TraceStepInput] = Field(default_factory=list)
    outcome: str = Field(default="SUCCESS", description="SUCCESS | FAILURE")
    duration_ms: int = 0
    token_cost: int = 0
    user_id: Optional[str] = None


class RecordTraceResponse(BaseModel):
    trace_id: str


class CompressSkillRequest(BaseModel):
    trace_id: str
    use_llm: bool = Field(default=False, description="是否用 LLM 生成模板（失败回退启发式）")


class SkillStepResponse(BaseModel):
    step_index: int
    action: str
    input_template: str
    expected_output: str
    fallback: str
    validation: str


class SkillResponse(BaseModel):
    id: str
    name: str
    description: str
    trigger_pattern: str
    steps: list[SkillStepResponse]
    success_rate: float
    usage_count: int
    avg_duration_ms: int
    source_trace_ids: list[str] = Field(default_factory=list)
    version: int
    disabled: bool


class RecordExecutionRequest(BaseModel):
    success: bool
    duration_ms: int = 0


# ── 多 agent 归属 ──────────────────────────────────────


class RegisterAgentRequest(BaseModel):
    agent_id: str = Field(description="agent 唯一 ID")
    name: str
    description: str = ""


class AgentResponse(BaseModel):
    agent_id: str
    name: str
    description: str = ""
    created_at: Optional[datetime] = None


class OpenSessionRequest(BaseModel):
    title: str = ""
    topic: str = ""
    model: Optional[str] = None
    session_id: Optional[str] = None


class SessionResponse(BaseModel):
    session_id: str
    agent_id: str
    title: str = ""
    topic: str = ""
    current_model: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


class SwitchModelRequest(BaseModel):
    model: str = Field(description="新接入的大模型名")


