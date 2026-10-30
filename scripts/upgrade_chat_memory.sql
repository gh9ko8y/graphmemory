-- ══════════════════════════════════════════════════════════════
-- 聊天记忆升级（双时态 + 六变量 + 证据链）
-- 依据：双时态记忆设计（P0-1/2/3 + P1-5 + P2-7/8）
-- 幂等：可重复执行。升级后与 init_db.sql 兼容共存。
--
-- 设计裁决（相对原方案的偏离，均为了不打架）：
--   1. 不新建 facts 表 —— fact_state 已承担该职责，只补缺失列
--      （provenance / 证据链 / 事务轴收口 tx_to / superseded_by）。
--   2. tx 轴 = created_at（何时记下）+ tx_to（何时被修正）；
--      valid 轴沿用 valid_from/valid_until。时间旅行查询两轴同过滤。
--   3. episodes 表是 PG 侧原文账本（raw_text 不可变），
--      Neo4j 的 episode 节点只服务检索，不是真相源。
-- ══════════════════════════════════════════════════════════════

-- ── 1. fact_state 补列：来源标记 + 证据链 + 事务轴 ──────────
ALTER TABLE fact_state
    ADD COLUMN IF NOT EXISTS source_type     TEXT NOT NULL DEFAULT 'remembered_fact',
        -- remembered_fact=原文直录 / reconstructed_detail=重述 / inferred_detail=推断
    ADD COLUMN IF NOT EXISTS evidence_ids    TEXT[] NOT NULL DEFAULT '{}',
        -- 证据指向 episodes.id（原文账本），原始证据永不覆盖
    ADD COLUMN IF NOT EXISTS episode_id      TEXT,
        -- 该事实从哪条消息抽出来的
    ADD COLUMN IF NOT EXISTS inference_count INTEGER NOT NULL DEFAULT 0,
        -- 证据链上的推断跳数（Q_recon 分母用）
    ADD COLUMN IF NOT EXISTS tx_to           TIMESTAMPTZ,
        -- 事务轴收口：这条版本何时被修正（NULL=当前版本）；tx_from 即 created_at
    ADD COLUMN IF NOT EXISTS superseded_by   TEXT;
        -- 被哪条新事实取代（显式版本链）

CREATE INDEX IF NOT EXISTS idx_fact_state_episode ON fact_state(episode_id);
CREATE INDEX IF NOT EXISTS idx_fact_state_tx      ON fact_state(created_at, tx_to);
CREATE INDEX IF NOT EXISTS idx_fact_state_evidence ON fact_state USING gin(evidence_ids);

-- ── 2. 记忆六变量表 M = {S, D, R, E, C, I} ─────────────────
-- 挂在 fact 粒度（聊天注入的基本单位是事实，不是整段消息）。
CREATE TABLE IF NOT EXISTS memory_state (
    memory_id       TEXT PRIMARY KEY,          -- = fact_state.fact_id
    memory_kind     TEXT NOT NULL DEFAULT 'fact',
    S_significance  REAL NOT NULL DEFAULT 0.5, -- 意义强度（慢衰减，值得被想起的程度）
    D_detail        REAL NOT NULL DEFAULT 0.9, -- 细节完整度（快衰减）
    R_retrieval     REAL NOT NULL DEFAULT 0.3, -- 可检索性（基线，靠线索激活）
    E_emotion       REAL NOT NULL DEFAULT 0.0, -- 情绪残留（衰减最慢）
    C_consolidation REAL NOT NULL DEFAULT 0.1, -- 巩固程度（被想起会加深）
    I_interference  REAL NOT NULL DEFAULT 0.0, -- 干扰程度（相似事实互相干扰）
    recall_count    INTEGER NOT NULL DEFAULT 0,
    last_recall_at  TIMESTAMPTZ,
    faded           BOOLEAN NOT NULL DEFAULT FALSE, -- 细节已淡出 → 只剩片段
    last_decay_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_memory_state_faded ON memory_state(faded) WHERE faded = TRUE;
CREATE INDEX IF NOT EXISTS idx_memory_state_S ON memory_state(S_significance DESC);

-- ── 3. episodes：原文账本（raw_text 永不可改）────────────────
-- 「即使被理解后，原始信息也要保存 → 不能丢！」（设计手稿 01）
CREATE TABLE IF NOT EXISTS episodes (
    id         TEXT PRIMARY KEY,             -- graphiti episode uuid（写入路径优先取 result.episode.uuid，与 Neo4j 同源）
    chat_id    TEXT,                         -- group:100000001 / private:100000002
    ts         TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- 事情发生时间（valid 侧）
    actors     TEXT[] NOT NULL DEFAULT '{}', -- 涉及的人（QQ 号/名字）
    user_id    TEXT,                         -- 记忆归属
    agent_id   TEXT,
    session_id TEXT,
    raw_text   TEXT NOT NULL,                -- 原话，只可 INSERT 不可 UPDATE
    meta       JSONB NOT NULL DEFAULT '{}',  -- 图片/语音/引用等
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()  -- 她何时记下（tx 侧）
);
CREATE INDEX IF NOT EXISTS idx_episodes_chat_ts ON episodes(chat_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_user   ON episodes(user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_actors ON episodes USING gin(actors);

-- ── 4. memory_fragments：淡出后的"照片式片段" ────────────────
-- D<0.2 且 S>0.7 时由衰减作业生成；episode/事实原文永不删除。
CREATE TABLE IF NOT EXISTS memory_fragments (
    id          TEXT PRIMARY KEY,
    fact_id     TEXT NOT NULL,               -- 来源事实
    episode_id  TEXT,                        -- 来源消息（可回溯原文）
    user_id     TEXT,
    text        TEXT NOT NULL,               -- 保下来的那句话/画面
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_fragments_fact ON memory_fragments(fact_id);
CREATE INDEX IF NOT EXISTS idx_fragments_user ON memory_fragments(user_id, created_at DESC);

-- ── 5. 回填：存量 fact_state 初始化六变量 ────────────────────
-- 新记忆的 S/E 初值由写入路径按情绪/重要度定；存量用保守默认值。
INSERT INTO memory_state (memory_id, memory_kind, S_significance, D_detail, R_retrieval, E_emotion, C_consolidation)
SELECT f.fact_id, 'fact',
       CASE f.fact_type WHEN 'core_identity' THEN 0.95
                        WHEN 'user_preference' THEN 0.8
                        WHEN 'commitment' THEN 0.85
                        ELSE 0.5 END,
       0.9, 0.3,
       CASE f.fact_type WHEN 'core_identity' THEN 0.1 ELSE 0.0 END,
       LEAST(0.1 + 0.05 * LEAST(f.confirmed_count - 1, 4), 0.5)
FROM fact_state f
WHERE NOT EXISTS (SELECT 1 FROM memory_state m WHERE m.memory_id = f.fact_id);

-- 存量证据回填：episode_id 空 = 无原文可溯（升级前的旧数据），evidence_ids 留空。

-- ── 6. 验收：时间旅行查询（双时态两轴同过滤）──────────────────
-- 「10月25日那天，我以为用户A喜欢什么？」
-- SELECT fact_text, valid_from, valid_until, created_at AS tx_from, tx_to
-- FROM fact_state
-- WHERE user_id = '100000002' AND relationship LIKE '%喜欢%'
--   AND valid_from <= '2026-10-25' AND (valid_until IS NULL OR valid_until > '2026-10-25')
--   AND created_at <= '2026-10-25' AND (tx_to IS NULL OR tx_to > '2026-10-25')
--   AND superseded = FALSE AND archived = FALSE;
