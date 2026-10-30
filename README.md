# GraphMemory

**双时态 AI 记忆系统** — 给 AI 代理一个会积累、会遗忘、会随时间变化的长期记忆库。

A bitemporal memory service for AI agents: facts with two time axes, a six-variable
importance/decay model, time-graded recall, TTL-based expiry, deduplication, and a
competitive retrieval layer on top of [Graphiti](https://github.com/getzep/graphiti)
(temporal knowledge graph) + PostgreSQL/pgvector + Redis.

> 设计思路与完整开发记录见 [docs/DESIGN_ZH.md](docs/DESIGN_ZH.md)。

---

## 演示视频

[![GraphMemory 讲解视频](docs/demo-cover.png)](docs/demo.mp4)

> 67 秒 · 小鱼配音 · 中文字幕 · [直接下载](docs/demo.mp4)

## 特性

| 能力 | 说明 |
|---|---|
| **双时态** | 每条事实同时有「有效时间」(valid_from/until) 和「记录时间」(tx time)，能回答「当时是什么」和「后来改了什么」 |
| **六变量衰减** | S(意义) / D(细节) / R(检索难度) / E(情绪) / C(巩固) / I(干扰)，遗忘是「降级」不是删除 |
| **时间梯度** | 1天/3天/7天/10天/2周/3周/1月/3月/半年/1年：越久 D 上限越低、R 下限越高、口径越不确定 |
| **时效性 TTL** | 临时状态 1 天、动作 3 天、事件 7 天、承诺 10 天、普通 14 天、偏好永久、身份 3 年、不可更改事实永久 |
| **到期确认** | 有明确日期的约定先存 due_at，到日子进入「待确认」，确认后再定时效 |
| **动态年龄** | 年龄存出生锚点，检索时实时计算（2028 年问就是 2028 年的岁数） |
| **竞争检索** | 激活 × 匹配 × 资源适配 → θ 相对阈值 → Top-K，命中会巩固记忆 |
| **去重/遗忘工具** | `memory_dedupe`（语义重复扫描）、`memory_forget`（墓碑式删除，原文账本保留） |
| **图谱连通性** | 渗流视角的 ⟨k⟩=2E/N、巨分量占比、孤立实体名单 |
| **原文账本** | episodes 只追加不更新，可回溯「当时到底说了什么」 |
| **MCP 工具** | 内置 MCP server，任何支持 MCP 的代理都能直接调用 |
| **管理台** | 自带极简 Web GUI（`/ui`）与 REST API |

## 架构

```
                     ┌─────────────────────────────┐
   Agent / MCP 客户端 │  graphmemory (FastAPI :8000) │
                     │  /add  /search  /chat  /decay│
                     └───────┬──────────┬──────────┘
                             │          │
                  ┌──────────▼──┐  ┌────▼────────────┐
                  │ Neo4j 5.26  │  │ PostgreSQL+pgvec │
                  │ 时序知识图谱 │  │ fact_state/state │
                  └─────────────┘  │ episodes/ledger  │
                                   └────┬────────────┘
                                   ┌────▼───────┐
                                   │  Redis 7   │
                                   └────────────┘
```

- **Neo4j**：实体/关系（`RELATES_TO` 事实边 + `MENTIONS` 提及边）
- **PostgreSQL**：双时态事实台账、六变量状态、原文账本、行为账本、墓碑
- **Redis**：检索缓存、会话状态

## 快速开始

### 1. 依赖

- Python ≥ 3.10
- Docker + Docker Compose（跑 Neo4j / PostgreSQL / Redis）

### 2. 启动数据库

```bash
cp .env.example .env          # 填好数据库密码与 LLM key
docker compose up -d
```

### 3. 安装并初始化 schema

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .
psql "postgresql://graphmemory:${POSTGRES_PASSWORD}@localhost:5432/graphmemory" \
     -f scripts/init_db.sql
psql ... -f scripts/upgrade_chat_memory.sql
```

### 4. 启动服务

```bash
uvicorn graphmemory.main:app --host 127.0.0.1 --port 8000
# 管理台: http://127.0.0.1:8000/ui
```

### 5. 试一试

```bash
# 写入
curl -s -X POST localhost:8000/api/v1/memory/add \
  -H 'Content-Type: application/json' \
  -d '{"content":"用户A 2026-10-29 答应周六一起去看海","user_id":"100000002",
       "source_type":"remembered_fact","raw_quote":"周六一起去看海"}'

# 检索
curl -s -X POST localhost:8000/api/v1/memory/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"周末约了什么","user_id":"100000002","top_k":3}'

# 手动跑一轮衰减
curl -s -X POST localhost:8000/api/v1/memory/decay
```

### 6. 以 MCP 方式接入代理

`mcp_server.py` 提供 stdio MCP server：

```json
{
  "command": "python",
  "args": ["/path/to/graphmemory/mcp_server.py"],
  "env": {
    "GRAPHMEMORY_BASE_URL": "http://127.0.0.1:8000/api/v1/memory",
    "GRAPHMEMORY_AGENT_ID": "my-agent"
  }
}
```

可用工具：`add_memory`、`search_memory`、`memory_stats`、`list_episodes`、`get_timeline`、`list_facts`、`chat`、`memory_dedupe`、`memory_forget`、`get_status`、`list_episodes`。

## 配置

所有配置走环境变量（见 `.env.example`）：

| 变量 | 说明 |
|---|---|
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | 图数据库 |
| `POSTGRES_HOST/PORT/USER/PASSWORD/DB` | 事实台账 |
| `REDIS_HOST/PORT/PASSWORD` | 缓存 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `DEFAULT_EMBEDDING_MODEL` | Embedding（任何 OpenAI 兼容接口） |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEFAULT_LLM_MODEL` | 事实抽取用的 LLM（任何 OpenAI 兼容接口） |
| `API_KEY` | 非空时 `/api` 请求需带 `X-API-Key` |
| `ACTION_LEDGER_DIR` | 行为账本缓存目录（默认 `/tmp/graphmemory-action-ledger`） |

## 时间梯度与 TTL

写库时自动按内容分类并设置有效期（见 `src/graphmemory/recall/validity.py`、`time_gradient.py`）：

| 类型 | 有效期 |
|---|---|
| 临时状态（现在/今天/在忙/心情） | 1 天 |
| 动作 / 失败 | 3 天 |
| 事件 | 7 天 |
| 承诺（无日期） | 10 天 |
| 普通事实 | 14 天 |
| 偏好 | 永久（被新事实覆盖才失效） |
| 身份陈述 | 3 年 |
| 不可更改（性别/出生/籍贯/亲属…） | 永久 |
| 有明确日期的约定 | 无 TTL，到日期进入「待确认」 |

到期后先隐藏、宽限 1 天物理删除（原文账本 episodes 永久保留）。

## 隐私与安全

本仓库**不包含任何真实用户数据、API key、Cookie 或服务器凭据**：

- 所有密钥仅从环境变量读取，`.env` 已被 `.gitignore`
- 示例中的 QQ 号/用户名均为占位符（`100000001`、`用户A`）
- 不含聊天记录、用户档案、数据库转储
- 详见 [docs/PRIVACY.md](docs/PRIVACY.md)

**部署前必改**：数据库密码、`API_KEY`、SSH/端口暴露面；不要把 `.env` 提交进仓库。

## 测试

```bash
pip install -e ".[dev]"
pytest
```

`scripts/verify_chat_memory.py` 提供一个端到端冒烟脚本（需服务已启动）。

## 目录

```
src/graphmemory/
├── main.py              # FastAPI 入口 + 后台衰减/时效清理循环
├── config.py            # 环境变量配置
├── core/graphiti_client.py
├── api/{routes,schemas}.py
├── policy/              # 冲突裁决 / 置信度 / 去重
├── procedural/          # 程序性记忆
├── system2/             # 多工具编排
├── recall/              # ★ 六变量衰减 / 竞争检索 / 时间梯度 / TTL / 去重
└── web/                 # 管理台
mcp_server.py            # MCP stdio server
scripts/                 # schema 迁移、冒烟、工具脚本
docs/                    # 设计文档
```

## 未包含

以下属于特定部署环境的集成，**不在本仓库**，可按需自行实现：

- 桌面控制台小组件（原作者本地组件，不属于本项目）
- QQ / NapCat / 其他 IM 的适配插件（含「每轮自动记忆注入」钩子）
- 私有的提示词/人设与业务数据

## License

MIT — 见 [LICENSE](LICENSE)。

---

> **最后，我们誓死践行开源精神。**
