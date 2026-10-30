-- GraphMemory PostgreSQL 初始化脚本
-- Ledger 层 + 程序性记忆存储

-- 启用 pgvector 扩展
CREATE EXTENSION IF NOT EXISTS vector;

-- ── Ledger 表（只追加不修改） ──────────────────────────

CREATE TABLE IF NOT EXISTS ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action VARCHAR(50) NOT NULL,           -- add_fact / invalidate / update_confidence / ...
    entity_id TEXT,
    fact_id TEXT,
    details JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ledger_created_at ON ledger(created_at);
CREATE INDEX IF NOT EXISTS idx_ledger_entity_id ON ledger(entity_id);
CREATE INDEX IF NOT EXISTS idx_ledger_fact_id ON ledger(fact_id);

-- ── 程序性记忆：交互轨迹 ──────────────────────────────

CREATE TABLE IF NOT EXISTS interaction_traces (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_type VARCHAR(100) NOT NULL,
    steps JSONB NOT NULL DEFAULT '[]',
    outcome VARCHAR(20) NOT NULL,          -- SUCCESS / FAILURE
    duration_ms INTEGER DEFAULT 0,
    token_cost INTEGER DEFAULT 0,
    user_id VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_traces_task_type ON interaction_traces(task_type);
CREATE INDEX IF NOT EXISTS idx_traces_user_id ON interaction_traces(user_id);
CREATE INDEX IF NOT EXISTS idx_traces_outcome ON interaction_traces(outcome);

-- ── 程序性记忆：固化技能 ──────────────────────────────

CREATE TABLE IF NOT EXISTS skills (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    description TEXT DEFAULT '',
    trigger_pattern TEXT DEFAULT '',
    steps JSONB NOT NULL DEFAULT '[]',
    success_rate FLOAT NOT NULL DEFAULT 1.0,
    usage_count INTEGER NOT NULL DEFAULT 0,
    avg_duration_ms INTEGER DEFAULT 0,
    source_trace_ids UUID[] DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    disabled BOOLEAN NOT NULL DEFAULT FALSE,   -- 成功率过低自动降级
    user_id VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_skills_task_type ON skills USING btree (name);
CREATE INDEX IF NOT EXISTS idx_skills_user_id ON skills(user_id);
CREATE INDEX IF NOT EXISTS idx_skills_success_rate ON skills(success_rate DESC);

-- ── 策略引擎：Policy 事件日志 ──────────────────────────

CREATE TABLE IF NOT EXISTS policy_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_name VARCHAR(100) NOT NULL,
    trigger_type VARCHAR(50) NOT NULL,     -- conflict / forgetting / compression / proactive
    entity_id TEXT,
    fact_id TEXT,
    action_taken TEXT NOT NULL,
    details JSONB DEFAULT '{}',
    user_id VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_policy_events_rule ON policy_events(rule_name);
CREATE INDEX IF NOT EXISTS idx_policy_events_type ON policy_events(trigger_type);
CREATE INDEX IF NOT EXISTS idx_policy_events_user ON policy_events(user_id);

-- ── Views 层：fact_state（Policy 维护的派生状态） ──────
-- 命题二 Memory 闭包 = Ledger + Views + Policy。
-- ledger 是只追加原始事件；fact_state 是 Policy 维护的"当前视图"：
-- 置信度 / 访问次数 / 降级 / 归档 / 被覆盖状态，用 graphiti edge uuid 做主键。

CREATE TABLE IF NOT EXISTS fact_state (
    fact_id          TEXT PRIMARY KEY,            -- graphiti EntityEdge.uuid
    user_id          VARCHAR(100),
    source_entity    TEXT,
    target_entity    TEXT,
    relationship     TEXT,
    fact_text        TEXT,
    fact_type        VARCHAR(40) NOT NULL DEFAULT 'normal',  -- normal | core_identity | user_preference
    confidence       REAL      NOT NULL DEFAULT 1.0,
    confirmed_count  INTEGER   NOT NULL DEFAULT 1,
    access_count     INTEGER   NOT NULL DEFAULT 0,
    last_accessed    TIMESTAMPTZ,
    demoted          BOOLEAN   NOT NULL DEFAULT FALSE,   -- 短期遗忘降级
    archived         BOOLEAN   NOT NULL DEFAULT FALSE,   -- 长期遗忘归档（不删除）
    superseded       BOOLEAN   NOT NULL DEFAULT FALSE,   -- 被新事实覆盖
    valid_from       TIMESTAMPTZ,
    valid_until      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fact_state_user ON fact_state(user_id);
CREATE INDEX IF NOT EXISTS idx_fact_state_rel ON fact_state(source_entity, target_entity, relationship);
CREATE INDEX IF NOT EXISTS idx_fact_state_active ON fact_state(archived, superseded) WHERE archived = FALSE AND superseded = FALSE;
CREATE INDEX IF NOT EXISTS idx_fact_state_last_accessed ON fact_state(last_accessed);
CREATE INDEX IF NOT EXISTS idx_fact_state_confidence ON fact_state(confidence);

-- ── 多 agent 归属（房间 + 箱子 + 模型溯源） ─────────────
-- agent = 房间（隔离单元）；session = 箱子（会话/主题分类）；
-- model = 每条记忆由哪个大模型产生（换模型时继承记忆但标记来源）。

-- 给所有记忆表补归属字段（对已存在的表用 ADD COLUMN IF NOT EXISTS 幂等迁移）
ALTER TABLE ledger             ADD COLUMN IF NOT EXISTS agent_id   TEXT;
ALTER TABLE ledger             ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE ledger             ADD COLUMN IF NOT EXISTS model      VARCHAR(100);
ALTER TABLE interaction_traces ADD COLUMN IF NOT EXISTS agent_id   TEXT;
ALTER TABLE interaction_traces ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE interaction_traces ADD COLUMN IF NOT EXISTS model      VARCHAR(100);
ALTER TABLE skills             ADD COLUMN IF NOT EXISTS agent_id   TEXT;
ALTER TABLE skills             ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE skills             ADD COLUMN IF NOT EXISTS model      VARCHAR(100);
ALTER TABLE policy_events      ADD COLUMN IF NOT EXISTS user_id    VARCHAR(100);
ALTER TABLE policy_events      ADD COLUMN IF NOT EXISTS agent_id   TEXT;
ALTER TABLE policy_events      ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE policy_events      ADD COLUMN IF NOT EXISTS model      VARCHAR(100);
ALTER TABLE fact_state         ADD COLUMN IF NOT EXISTS agent_id   TEXT;
ALTER TABLE fact_state         ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE fact_state         ADD COLUMN IF NOT EXISTS model      VARCHAR(100);

-- agent 花名册（房间）
CREATE TABLE IF NOT EXISTS agents (
    agent_id    TEXT PRIMARY KEY,
    name        VARCHAR(200) NOT NULL,
    description TEXT DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 会话箱子（每个 agent 房间内的分类）
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL REFERENCES agents(agent_id),
    title         VARCHAR(300) DEFAULT '',
    topic         VARCHAR(200) DEFAULT '',
    current_model VARCHAR(100),              -- 当前接入的大模型（换模型即更新）
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at      TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent_id);
CREATE INDEX IF NOT EXISTS idx_sessions_topic ON sessions(topic);
CREATE INDEX IF NOT EXISTS idx_fact_state_agent ON fact_state(agent_id);
CREATE INDEX IF NOT EXISTS idx_fact_state_session ON fact_state(session_id);
CREATE INDEX IF NOT EXISTS idx_traces_agent ON interaction_traces(agent_id);
CREATE INDEX IF NOT EXISTS idx_skills_agent ON skills(agent_id);
