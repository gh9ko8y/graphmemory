# GraphMemory API 参考

> 所有接口前缀：`http://127.0.0.1:8000/api/v1/memory`
> 可选鉴权：若 `.env` 里设置了 `api_key`，需加请求头 `X-API-Key: <你的key>`（默认关闭）。
> 本文所有示例用 `curl`，中文内容请直接复制到支持 UTF-8 的工具（Postman / Python / 代码），Windows PowerShell 直接粘贴中文会乱码。

---

## 一、记忆写入与检索

### 1. POST /add — 写入记忆

自动抽取实体/关系，双时态标注，触发 Policy 冲突裁决。

**请求：**
```bash
curl -X POST http://127.0.0.1:8000/api/v1/memory/add \
  -H "Content-Type: application/json" \
  -d '{
    "content": "我叫张三，今年25岁，在腾讯担任产品总监。我喜欢吃火锅。",
    "source": "conversation",
    "user_id": "user_1",
    "agent_id": "agent_a",
    "session_id": "sess_1",
    "model": "glm-4-plus"
  }'
```

> `agent_id` / `session_id` / `model` 为可选溯源字段：写入后会打在 fact_state 与 ledger 上，
> 实现「每条记忆带哪个 agent 在哪个会话用哪个模型记的」。不传则为 null。

**响应：**
```json
{
  "episode_id": "3a9f2c1e-...-8b7d",
  "message": "Memory added successfully"
}
```

### 2. POST /search — 时间切片检索事实

未传时间默认查「当前为真」的事实。

**请求：**
```bash
curl -X POST http://127.0.0.1:8000/api/v1/memory/search \
  -H "Content-Type: application/json" \
  -d '{"query": "张三", "user_id": "user_1", "top_k": 5}'
```

**响应：**
```json
{
  "facts": [
    {
      "id": "edge-uuid-123",
      "source_entity": "张三",
      "target_entity": "腾讯",
      "relationship": "WORKS_AT",
      "fact_text": "张三在腾讯担任产品总监",
      "valid_from": "2026-09-10T10:00:00Z",
      "valid_until": null,
      "is_current": true,
      "confidence": 0.8
    }
  ],
  "total_count": 1,
  "query_time_ms": 860.5
}
```

### 3. POST /timeline — 查询实体完整时间线

**请求：**
```bash
curl -X POST http://127.0.0.1:8000/api/v1/memory/timeline \
  -H "Content-Type: application/json" \
  -d '{"entity_name": "张三", "user_id": "user_1"}'
```

**响应：**
```json
{
  "entity": "张三",
  "timeline": [
    {"fact_text": "张三在A公司任职", "valid_from": "2024-01-01T00:00:00Z", "valid_until": "2024-06-01T00:00:00Z", "is_current": false, "confidence": 0.8},
    {"fact_text": "张三在腾讯任职", "valid_from": "2024-06-01T00:00:00Z", "valid_until": null, "is_current": true, "confidence": 0.9}
  ]
}
```

---

## 二、对话

### 4. POST /chat — 带记忆的对话（System 2 编排 + LLM 回复）

**请求：**
```bash
curl -X POST http://127.0.0.1:8000/api/v1/memory/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "我叫什么名字？我喜欢吃什么？", "user_id": "user_1"}'
```

**响应：**
```json
{
  "system": 2,
  "model": "glm-4-plus",
  "response": "根据我的记忆，你叫张三，喜欢吃火锅。",
  "tools_called": ["get_user_profile", "get_recent_context"],
  "context": "[get_user_profile]\n{\"user_id\":\"user_1\",\"identity\":[\"名字叫张三\"],\"preferences\":[\"喜欢吃火锅\"]}"
}
```

---

## 三、监控与维护

### 5. GET /health — 健康检查

```bash
curl http://127.0.0.1:8000/api/v1/memory/health
```
```json
{"status": "ok", "service": "graphmemory"}
```

### 6. GET /status — 组件健康 + 记忆统计

```bash
curl http://127.0.0.1:8000/api/v1/memory/status
```
```json
{
  "status": "ok",
  "components": {"neo4j": true, "postgres": true, "redis": true},
  "memory": {
    "facts": {"total": 8, "active": 7, "archived": 1, "demoted": 1, "superseded": 0, "avg_conf": 0.71},
    "traces": 3,
    "skills": 1,
    "agents": 2
  }
}
```

### 7. POST /maintenance — 执行一次 Policy 维护（遗忘归档 + 去重）

```bash
curl -X POST http://127.0.0.1:8000/api/v1/memory/maintenance
```
```json
{
  "short_term_demoted": 1,
  "long_term_archived": 1,
  "dedup_merged": 0,
  "compression": "skipped(no conversation store yet)",
  "stats": {"total": 8, "active": 6, "archived": 2, "demoted": 1, "superseded": 0, "avg_conf": 0.75}
}
```

### 8. GET /health-report — 记忆健康报告

```bash
curl http://127.0.0.1:8000/api/v1/memory/health-report
```
```json
{"total": 8, "active": 6, "archived": 2, "demoted": 1, "superseded": 0, "avg_conf": 0.75}
```

### 8b. GET /facts — 列出事实视图（管理台使用）

按创建时间倒序分页列出 fact_state（双时态区间 + 遗忘状态全量暴露）。

```bash
curl "http://127.0.0.1:8000/api/v1/memory/facts?user_id=user_1&limit=200"
```
```json
{"facts": [{"fact_id": "...", "fact_text": "张三喜欢吃火锅", "fact_type": "user_preference",
  "confidence": 0.8, "demoted": false, "archived": false, "superseded": false,
  "valid_from": "...", "valid_until": null, "agent_id": "agent_a"}], "total": 1}
```

### 8c. GET /traces — 列出交互轨迹（管理台使用）

与 `POST /traces`（记录轨迹，见第 9 号）同路径不同方法。按时间倒序，可按 task_type/user_id 过滤。

```bash
curl "http://127.0.0.1:8000/api/v1/memory/traces?limit=50"
```

### 8d. GET /export — 全量导出记忆数据（备份）

`format=json`（默认）导出完整结构化快照：facts / ledger / traces / skills / policy_events / agents / sessions。
`format=markdown` 导出人类可读摘要。图谱（Neo4j）本体备份请用 Docker 卷。

```bash
curl "http://127.0.0.1:8000/api/v1/memory/export?format=markdown" -o graphmemory-export.md
```

---

## 四、程序性记忆（轨迹 → 技能）

### 9. POST /traces — 记录交互轨迹

**请求：**
```bash
curl -X POST http://127.0.0.1:8000/api/v1/memory/traces \
  -H "Content-Type: application/json" \
  -d '{
    "task_type": "deploy",
    "steps": [
      {"action": "docker build", "input_data": {"tag": "v1"}, "success": true, "duration_ms": 3000},
      {"action": "docker push", "input_data": {"tag": "v1"}, "success": false, "duration_ms": 2000},
      {"action": "kubectl apply", "input_data": {"file": "app.yaml"}, "success": true, "duration_ms": 1500}
    ],
    "outcome": "SUCCESS",
    "duration_ms": 6500,
    "user_id": "user_1"
  }'
```

**响应：**
```json
{"trace_id": "6f2b8c...-9e1a"}
```

### 10. POST /skills/compress — 轨迹漏斗压缩成技能

**请求：**
```bash
curl -X POST http://127.0.0.1:8000/api/v1/memory/skills/compress \
  -H "Content-Type: application/json" \
  -d '{"trace_id": "6f2b8c...-9e1a", "use_llm": false}'
```

**响应：**
```json
{
  "id": "skill-uuid",
  "name": "auto_deploy_12c769a4",
  "description": "自动固化的 deploy 技能",
  "trigger_pattern": "deploy",
  "steps": [
    {"step_index": 0, "action": "docker build", "input_template": "{\"tag\": \"{{tag}}\"}", "expected_output": "success", "fallback": "retry up to 3x", "validation": "output indicates success"},
    {"step_index": 1, "action": "kubectl apply", "input_template": "{\"file\": \"{{file}}\"}", "expected_output": "success", "fallback": "retry", "validation": "output indicates success"}
  ],
  "success_rate": 1.0,
  "usage_count": 1,
  "avg_duration_ms": 6500,
  "source_trace_ids": ["6f2b8c...-9e1a"],
  "version": 1,
  "disabled": false
}
```

### 11. GET /skills — 列出技能

```bash
curl "http://127.0.0.1:8000/api/v1/memory/skills?min_rate=0.7&limit=20"
```
```json
[ { ...SkillResponse 同上面... } ]
```

### 12. GET /skills/applicable — 查适用技能（成功率 ≥ 0.7）

```bash
curl "http://127.0.0.1:8000/api/v1/memory/skills/applicable?task_type=deploy"
```
```json
{ ...SkillResponse 或 null... }
```

### 13. POST /skills/{id}/execution — 回报执行结果（更新成功率）

```bash
curl -X POST http://127.0.0.1:8000/api/v1/memory/skills/skill-uuid/execution \
  -H "Content-Type: application/json" \
  -d '{"success": false, "duration_ms": 4000}'
```
```json
{"skill_id": "skill-uuid", "success": false, "success_rate": 0.5, "usage_count": 2, "avg_duration_ms": 5250, "demoted": false}
```

### 14. POST /skills/maintenance — 低成功率技能降级

```bash
curl -X POST http://127.0.0.1:8000/api/v1/memory/skills/maintenance
```
```json
{"demoted": 1, "stats": {"traces": 1, "skills": 1, "active_skills": 0, "avg_rate": 0.0}}
```

---

## 五、System 2 工具集

### 15. GET /tools — 8 个工具 schema

```bash
curl http://127.0.0.1:8000/api/v1/memory/tools
```
```json
{
  "tools": [
    {"type": "function", "function": {"name": "get_user_profile", "description": "获取用户档案卡", "parameters": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]}}},
    "...共 8 个"
  ],
  "count": 8
}
```

### 16. POST /tools/{name} — 执行单个工具

```bash
curl -X POST http://127.0.0.1:8000/api/v1/memory/tools/get_user_profile \
  -H "Content-Type: application/json" \
  -d '{"user_id": "user_1"}'
```
```json
{"user_id": "user_1", "identity": ["名字叫张三"], "preferences": ["喜欢吃火锅"]}
```

### 17. POST /system2/orchestrate — System 2 编排

```bash
curl -X POST http://127.0.0.1:8000/api/v1/memory/system2/orchestrate \
  -H "Content-Type: application/json" \
  -d '{"user_input": "记得上次我们聊过什么吗", "user_id": "user_1"}'
```
```json
{
  "system": 2,
  "triggered": true,
  "tools_called": ["get_user_profile", "get_recent_context"],
  "results": {
    "get_user_profile": {"user_id": "user_1", "identity": [], "preferences": []},
    "get_recent_context": {"recent_traces": [], "recent_facts": []}
  }
}
```

---

## 六、多 agent 归属

### 18. POST /agents — 注册 agent（房间）

```bash
curl -X POST http://127.0.0.1:8000/api/v1/memory/agents \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "agent_a", "name": "助手小红", "description": "负责生活记录"}'
```
```json
{"agent_id": "agent_a", "name": "助手小红", "description": "负责生活记录", "created_at": "2026-09-10T10:00:00Z"}
```

### 19. GET /agents — 列出 agent

```bash
curl "http://127.0.0.1:8000/api/v1/memory/agents?limit=100"
```
```json
[{"agent_id": "agent_a", "name": "助手小红", "description": "..."}]
```

### 20. GET /agents/{id} — 查单个 agent

```bash
curl http://127.0.0.1:8000/api/v1/memory/agents/agent_a
```

### 21. POST /agents/{id}/sessions — 开会话箱子

```bash
curl -X POST http://127.0.0.1:8000/api/v1/memory/agents/agent_a/sessions \
  -H "Content-Type: application/json" \
  -d '{"title": "技术讨论", "topic": "coding", "model": "glm-4-plus"}'
```
```json
{"session_id": "sess-1", "agent_id": "agent_a", "title": "技术讨论", "topic": "coding", "current_model": "glm-4-plus", "started_at": "...", "ended_at": null}
```

### 22. GET /agents/{id}/sessions — 列会话

```bash
curl "http://127.0.0.1:8000/api/v1/memory/agents/agent_a/sessions?limit=100"
```

### 23. POST /sessions/{id}/model — 换大模型（继承+标记）

```bash
curl -X POST http://127.0.0.1:8000/api/v1/memory/sessions/sess-1/model \
  -H "Content-Type: application/json" \
  -d '{"model": "deepseek-chat"}'
```
```json
{"session_id": "sess-1", "agent_id": "agent_a", "current_model": "deepseek-chat"}
```

### 24. POST /sessions/{id}/close — 关闭会话

```bash
curl -X POST http://127.0.0.1:8000/api/v1/memory/sessions/sess-1/close
```
```json
{"session_id": "sess-1", "closed": true}
```

---

## 错误响应约定

| 状态码 | 场景 | 响应体 |
|---|---|---|
| 404 | 资源不存在（agent/session/trace 等） | `{"detail": "agent not found: xxx"}` |
| 401 | 未带正确的 X-API-Key | `{"detail": "invalid or missing X-API-Key"}` |
| 500 | 服务内部错误 | `{"detail": "Internal Server Error"}` |

---

## 完整流程示例（Python）

```python
import httpx

H = "http://127.0.0.1:8000/api/v1/memory"

async def demo():
    async with httpx.AsyncClient(timeout=120) as c:
        # 注册 agent + 开箱
        await c.post(f"{H}/agents", json={"agent_id": "a1", "name": "助手"})
        s = (await c.post(f"{H}/agents/a1/sessions", json={"model": "glm-4-plus"})).json()

        # 存记忆
        await c.post(f"{H}/add", json={"content": "我叫小明，喜欢打游戏", "agent_id": "a1"})

        # 带记忆对话
        r = (await c.post(f"{H}/chat", json={"message": "我叫什么", "user_id": "a1"})).json()
        print(r["response"])
```
