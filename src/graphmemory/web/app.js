/* GraphMemory 管理台 — 纯原生 JS，无外部依赖 */
"use strict";

const API = "/api/v1/memory";

/* ── 工具 ── */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function fmtTime(v) {
  if (!v) return "—";
  const s = String(v);
  return s.length > 16 ? s.slice(0, 16).replace("T", " ") : s;
}
function val(id) { return document.getElementById(id).value.trim(); }
function $(id) { return document.getElementById(id); }

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  const key = localStorage.getItem("gm_api_key");
  if (key) headers["X-API-Key"] = key;
  const resp = await fetch(API + path, { ...opts, headers });
  const ct = resp.headers.get("content-type") || "";
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const j = await resp.json();
      detail = j.detail || JSON.stringify(j);
    } catch (e) { /* ignore */ }
    throw new Error(`${resp.status} ${detail}`);
  }
  if (ct.includes("application/json")) return resp.json();
  return resp.text();
}

function setMsg(id, text, isErr = false) {
  const el = $(id);
  el.textContent = text;
  el.style.color = isErr ? "#b91c1c" : "";
}

/* ── Tab 切换 ── */
document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
    const loader = { overview: loadOverview, memory: loadFacts, chat: null, skills: () => { loadSkills(); loadTraces(); }, agents: loadAgents };
    if (loader[btn.dataset.tab]) loader[btn.dataset.tab]();
  });
});

/* ── 总览 ── */
function compCard(name, ok, detail) {
  const label = ok ? "正常" : "异常";
  const cls = ok ? "badge-green" : "badge-red";
  return `<div class="card">
    <div class="k">${esc(name)}</div>
    <div class="v">${ok ? "✓" : "✗"}</div>
    <div class="s"><span class="badge ${cls}">${label}</span> ${esc(detail || "")}</div>
  </div>`;
}

async function loadOverview() {
  try {
    const st = await api("/status");
    const pill = $("live-status");
    if (st.status === "ok") { pill.className = "status-pill ok"; pill.textContent = "运行中"; }
    else { pill.className = "status-pill warn"; pill.textContent = "部分异常"; }

    $("component-cards").innerHTML =
      compCard("Neo4j 图谱", st.components.neo4j) +
      compCard("PostgreSQL", st.components.postgres) +
      compCard("Redis 缓存", st.components.redis);

    const f = st.memory.facts || {};
    $("stat-cards").innerHTML =
      `<div class="card"><div class="k">事实总数</div><div class="v">${f.total ?? 0}</div><div class="s">活跃 ${f.active ?? 0} · 归档 ${f.archived ?? 0}</div></div>` +
      `<div class="card"><div class="k">降级 / 被覆盖</div><div class="v">${f.demoted ?? 0} / ${f.superseded ?? 0}</div><div class="s">Policy 遗忘机制生效</div></div>` +
      `<div class="card"><div class="k">平均置信度</div><div class="v">${f.avg_conf ?? 0}</div><div class="s">当前视图</div></div>` +
      `<div class="card"><div class="k">交互轨迹</div><div class="v">${st.memory.traces ?? 0}</div><div class="s">程序性记忆</div></div>` +
      `<div class="card"><div class="k">固化技能</div><div class="v">${st.memory.skills ?? 0}</div><div class="s">压缩自轨迹</div></div>` +
      `<div class="card"><div class="k">Agents</div><div class="v">${st.memory.agents ?? 0}</div><div class="s">房间数</div></div>`;
  } catch (e) {
    const pill = $("live-status");
    pill.className = "status-pill bad";
    pill.textContent = "API 不可达";
    $("component-cards").innerHTML = `<div class="panel">${esc(e.message)}</div>`;
  }
}

async function runPolicyMaintenance() {
  try {
    const r = await api("/maintenance", { method: "POST" });
    setMsg("maintenance-result", "完成：" + JSON.stringify(r), false);
    loadOverview();
  } catch (e) { setMsg("maintenance-result", e.message, true); }
}
async function runSkillMaintenance() {
  try {
    const r = await api("/skills/maintenance", { method: "POST" });
    setMsg("maintenance-result", "完成：" + JSON.stringify(r), false);
  } catch (e) { setMsg("maintenance-result", e.message, true); }
}

/* ── 记忆 ── */
async function addMemory() {
  const body = { content: val("add-content"), source: "conversation", user_id: val("add-user") || null };
  if (!body.content) { setMsg("add-result", "内容不能为空", true); return; }
  const ag = val("add-agent"); if (ag) body.agent_id = ag;
  const md = val("add-model"); if (md) body.model = md;
  try {
    const r = await api("/add", { method: "POST", body: JSON.stringify(body) });
    setMsg("add-result", `已写入，episode_id=${r.episode_id}`);
    $("add-content").value = "";
    loadFacts();
  } catch (e) { setMsg("add-result", e.message, true); }
}

async function searchMemory() {
  const body = { query: val("search-q"), user_id: val("search-user") || null, top_k: parseInt(val("search-k") || "10") };
  if (!body.query) return;
  try {
    const r = await api("/search", { method: "POST", body: JSON.stringify(body) });
    const rows = (r.facts || []).map((f) => `<tr>
      <td>${esc(f.fact_text)}</td>
      <td>${esc(f.source_entity)} —${esc(f.relationship)}→ ${esc(f.target_entity)}</td>
      <td>${fmtTime(f.valid_from)}</td>
      <td>${f.valid_until ? fmtTime(f.valid_until) : "∞"}</td>
      <td>${f.confidence}</td>
      <td>${f.is_current ? '<span class="badge badge-green">当前</span>' : '<span class="badge badge-gray">历史</span>'}</td>
    </tr>`).join("");
    $("search-result").innerHTML =
      `<table><thead><tr><th>事实</th><th>关系</th><th>生效自</th><th>失效至</th><th>置信度</th><th>状态</th></tr></thead>
       <tbody>${rows || '<tr><td colspan="6" class="muted">无结果</td></tr>'}</tbody></table>
       <div class="muted">共 ${r.total_count} 条，耗时 ${r.query_time_ms}ms</div>`;
  } catch (e) { $("search-result").innerHTML = `<div class="muted">${esc(e.message)}</div>`; }
}

async function timeline() {
  const body = { entity_name: val("tl-entity"), user_id: val("tl-user") || null };
  if (!body.entity_name) return;
  try {
    const r = await api("/timeline", { method: "POST", body: JSON.stringify(body) });
    const rows = (r.timeline || []).map((f) => `<tr>
      <td>${esc(f.fact_text)}</td>
      <td>${fmtTime(f.valid_from)}</td>
      <td>${f.valid_until ? fmtTime(f.valid_until) : "∞"}</td>
      <td>${f.confidence}</td>
      <td>${f.is_current ? '<span class="badge badge-green">当前</span>' : '<span class="badge badge-gray">历史</span>'}</td>
    </tr>`).join("");
    $("tl-result").innerHTML =
      `<table><thead><tr><th>事实</th><th>生效自</th><th>失效至</th><th>置信度</th><th>状态</th></tr></thead>
       <tbody>${rows || '<tr><td colspan="5" class="muted">无结果</td></tr>'}</tbody></table>`;
  } catch (e) { $("tl-result").innerHTML = `<div class="muted">${esc(e.message)}</div>`; }
}

function factBadges(f) {
  const b = [];
  if (f.archived) b.push('<span class="badge badge-gray">归档</span>');
  if (f.superseded) b.push('<span class="badge badge-yellow">被覆盖</span>');
  if (f.demoted) b.push('<span class="badge badge-yellow">降级</span>');
  if (!b.length) b.push('<span class="badge badge-green">活跃</span>');
  const t = f.fact_type;
  if (t === "core_identity") b.push('<span class="badge badge-blue">核心身份</span>');
  if (t === "user_preference") b.push('<span class="badge badge-blue">偏好</span>');
  return b.join(" ");
}

async function loadFacts() {
  try {
    const u = val("facts-user") || null;
    const q = u ? `?user_id=${encodeURIComponent(u)}` : "";
    const r = await api("/facts" + q);
    const rows = (r.facts || []).map((f) => `<tr>
      <td>${esc(f.fact_text)}</td>
      <td>${factBadges(f)}</td>
      <td>${f.confidence}</td>
      <td>${f.access_count}</td>
      <td>${esc(f.user_id || "")}</td>
      <td>${esc(f.agent_id || "")}</td>
      <td>${fmtTime(f.valid_from)} → ${f.valid_until ? fmtTime(f.valid_until) : "∞"}</td>
      <td class="ops">
        <button class="btn btn-mini" onclick="editFact('${f.fact_id}', ${JSON.stringify(esc(f.fact_text)).replace(/\"/g, '&quot;')})">✏️ 修改</button>
        <button class="btn btn-mini btn-danger" onclick="deleteFact('${f.fact_id}')">🗑 删除</button>
      </td>
    </tr>`).join("");
    $("facts-result").innerHTML =
      `<table><thead><tr><th>事实</th><th>状态</th><th>置信度</th><th>访问</th><th>user</th><th>agent</th><th>双时态区间</th><th>操作</th></tr></thead>
       <tbody>${rows || '<tr><td colspan="8" class="muted">暂无事实</td></tr>'}</tbody></table>`;
    $("facts-count").textContent = `共 ${r.total} 条`;
  } catch (e) { $("facts-result").innerHTML = `<div class="muted">${esc(e.message)}</div>`; }
}

async function editFact(factId, oldText) {
  const newText = prompt("修改事实文本：", oldText);
  if (!newText || newText.trim() === "" || newText.trim() === oldText) return;
  try {
    await api(`/facts/${encodeURIComponent(factId)}`, {
      method: "PATCH",
      body: JSON.stringify({ content: newText.trim() }),
    });
    loadFacts();
  } catch (e) { alert("修改失败：" + e.message); }
}

async function deleteFact(factId) {
  if (!confirm("确定归档这条事实吗？\n（双时态原则：软删除，原始数据保留可追溯）")) return;
  try {
    await api(`/facts/${encodeURIComponent(factId)}`, { method: "DELETE" });
    loadFacts();
  } catch (e) { alert("删除失败：" + e.message); }
}

/* ── 对话 ── */
function appendMsg(role, html) {
  const div = document.createElement("div");
  div.className = "msg " + role;
  div.innerHTML = html;
  $("chat-log").appendChild(div);
  $("chat-log").scrollTop = $("chat-log").scrollHeight;
}

async function sendChat() {
  const msg = val("chat-input");
  if (!msg) return;
  $("chat-input").value = "";
  appendMsg("user", esc(msg));
  try {
    const r = await api("/chat", { method: "POST", body: JSON.stringify({ message: msg, user_id: val("chat-user") || null }) });
    const sys = r.system === 2 ? '<span class="badge badge-blue">System 2 慢思考</span>' : '<span class="badge badge-gray">System 1</span>';
    const chips = (r.tools_called || []).map((t) => `<span class="chip">${esc(t)}</span>`).join("");
    const ctx = r.context
      ? `<details class="ctx"><summary>查看记忆上下文</summary><pre>${esc(r.context)}</pre></details>`
      : "";
    appendMsg("ai", `${esc(r.response || "（LLM 未返回内容）")}<div class="meta">${sys}${chips}</div>${ctx}`);
  } catch (e) {
    appendMsg("err", esc("请求失败：" + e.message));
  }
}

/* ── 技能 ── */
async function loadSkills() {
  try {
    const r = await api("/skills");
    const rows = (r || []).map((s) => `<tr>
      <td>${esc(s.name)}</td>
      <td>${esc(s.trigger_pattern)}</td>
      <td>${s.success_rate}</td>
      <td>${s.usage_count}</td>
      <td>${s.steps.length} 步</td>
      <td>${s.disabled ? '<span class="badge badge-red">已降级</span>' : '<span class="badge badge-green">启用</span>'}</td>
      <td>
        <button class="btn btn-sm" onclick="execSkill('${s.id}', true)">成功</button>
        <button class="btn btn-sm btn-danger" onclick="execSkill('${s.id}', false)">失败</button>
      </td>
    </tr>`).join("");
    $("skills-result").innerHTML =
      `<table><thead><tr><th>名称</th><th>触发词</th><th>成功率</th><th>次数</th><th>步骤</th><th>状态</th><th>执行反馈</th></tr></thead>
       <tbody>${rows || '<tr><td colspan="7" class="muted">暂无技能</td></tr>'}</tbody></table>`;
  } catch (e) { $("skills-result").innerHTML = `<div class="muted">${esc(e.message)}</div>`; }
}

async function execSkill(id, success) {
  try {
    const r = await api(`/skills/${id}/execution`, { method: "POST", body: JSON.stringify({ success }) });
    setMsg("trace-result", success ? `已回报成功：成功率 ${r.success_rate}` : `已回报失败：成功率 ${r.success_rate}${r.demoted ? "（已自动降级）" : ""}`);
    loadSkills();
  } catch (e) { setMsg("trace-result", e.message, true); }
}

function traceSteps(t) {
  let steps = t.steps;
  if (typeof steps === "string") { try { steps = JSON.parse(steps); } catch (e) { steps = []; } }
  return Array.isArray(steps) ? steps : [];
}

async function loadTraces() {
  try {
    const r = await api("/traces");
    const rows = (r.traces || []).map((t) => `<tr>
      <td>${esc(t.task_type)}</td>
      <td>${traceSteps(t).length} 步</td>
      <td>${t.outcome === "SUCCESS" ? '<span class="badge badge-green">成功</span>' : '<span class="badge badge-red">失败</span>'}</td>
      <td>${esc(t.user_id || "")}</td>
      <td>${fmtTime(t.created_at)}</td>
      <td><button class="btn btn-sm" onclick="compressTrace('${t.id}')">压缩为技能</button></td>
    </tr>`).join("");
    $("traces-result").innerHTML =
      `<table><thead><tr><th>任务</th><th>步骤</th><th>结果</th><th>user</th><th>时间</th><th></th></tr></thead>
       <tbody>${rows || '<tr><td colspan="6" class="muted">暂无轨迹</td></tr>'}</tbody></table>`;
  } catch (e) { $("traces-result").innerHTML = `<div class="muted">${esc(e.message)}</div>`; }
}

async function compressTrace(traceId) {
  try {
    const r = await api("/skills/compress", { method: "POST", body: JSON.stringify({ trace_id: traceId, use_llm: false }) });
    setMsg("trace-result", `已压缩为技能：${r.name}（${r.steps.length} 步）`);
    loadSkills();
  } catch (e) { setMsg("trace-result", e.message, true); }
}

async function recordTrace() {
  const task = val("trace-task");
  const stepsText = val("trace-steps");
  if (!task || !stepsText) { setMsg("trace-result", "task_type 和步骤 JSON 必填", true); return; }
  let steps;
  try { steps = JSON.parse(stepsText); } catch (e) { setMsg("trace-result", "步骤 JSON 解析失败：" + e.message, true); return; }
  try {
    const r = await api("/traces", { method: "POST", body: JSON.stringify({ task_type: task, steps, outcome: "SUCCESS" }) });
    setMsg("trace-result", `轨迹已录入：${r.trace_id}`);
    loadTraces();
  } catch (e) { setMsg("trace-result", e.message, true); }
}

/* ── Agents ── */
async function registerAgent() {
  const id = val("ag-id"), name = val("ag-name");
  if (!id || !name) { setMsg("ag-result", "agent_id 和名称必填", true); return; }
  try {
    await api("/agents", { method: "POST", body: JSON.stringify({ agent_id: id, name, description: val("ag-desc") }) });
    setMsg("ag-result", `已注册 agent：${id}`);
    loadAgents();
  } catch (e) { setMsg("ag-result", e.message, true); }
}

async function loadAgents() {
  try {
    const agents = await api("/agents");
    const cards = [];
    for (const a of agents) {
      let sessionsHtml = "";
      try {
        const ss = await api(`/agents/${encodeURIComponent(a.agent_id)}/sessions`);
        sessionsHtml = (ss || []).map((s) => `<tr>
          <td>${esc(s.session_id).slice(0, 8)}…</td>
          <td>${esc(s.title || "—")}</td>
          <td>${esc(s.current_model || "—")}</td>
          <td>${s.ended_at ? '<span class="badge badge-gray">已关闭</span>' : '<span class="badge badge-green">进行中</span>'}</td>
          <td>
            <input class="model-input" data-sid="${esc(s.session_id)}" placeholder="新模型" style="min-width:110px">
            <button class="btn btn-sm" onclick="switchModel(this)">换模型</button>
            ${s.ended_at ? "" : `<button class="btn btn-sm" onclick="closeSession('${esc(s.session_id)}')">关闭</button>`}
          </td>
        </tr>`).join("");
      } catch (e) { sessionsHtml = `<tr><td colspan="5" class="muted">${esc(e.message)}</td></tr>`; }

      cards.push(`<div class="agent-card">
        <div class="head">
          <span class="name">${esc(a.name)} <span class="muted">（${esc(a.agent_id)}）</span></span>
          <button class="btn btn-sm" onclick="openSession('${esc(a.agent_id)}')">开会话</button>
        </div>
        <div class="muted">${esc(a.description || "")}</div>
        <table style="margin-top:8px"><thead><tr><th>会话</th><th>标题</th><th>模型</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>${sessionsHtml || '<tr><td colspan="5" class="muted">暂无会话</td></tr>'}</tbody></table>
      </div>`);
    }
    $("agents-result").innerHTML = cards.join("") || '<div class="muted">暂无 agent，先注册一个</div>';
  } catch (e) { $("agents-result").innerHTML = `<div class="muted">${esc(e.message)}</div>`; }
}

async function openSession(agentId) {
  try {
    await api(`/agents/${encodeURIComponent(agentId)}/sessions`, { method: "POST", body: JSON.stringify({ title: "新会话", topic: "", model: null }) });
    loadAgents();
  } catch (e) { alert(e.message); }
}

async function switchModel(btn) {
  const input = btn.previousElementSibling;
  const sid = input.dataset.sid;
  const model = input.value.trim();
  if (!model) return;
  try {
    await api(`/sessions/${encodeURIComponent(sid)}/model`, { method: "POST", body: JSON.stringify({ model }) });
    loadAgents();
  } catch (e) { alert(e.message); }
}

async function closeSession(sid) {
  try {
    await api(`/sessions/${encodeURIComponent(sid)}/close`, { method: "POST" });
    loadAgents();
  } catch (e) { alert(e.message); }
}

/* ── 导出 ── */
function exportData(format) {
  const key = localStorage.getItem("gm_api_key");
  const url = `${API}/export?format=${format}`;
  if (key) {
    fetch(url, { headers: { "X-API-Key": key } })
      .then((r) => r.blob())
      .then((b) => triggerDownload(b, `graphmemory-export-${new Date().toISOString().slice(0, 10)}.${format === "markdown" ? "md" : "json"}`));
  } else {
    // 无鉴权直接交给浏览器下载
    const a = document.createElement("a");
    a.href = url;
    a.download = `graphmemory-export-${new Date().toISOString().slice(0, 10)}.${format === "markdown" ? "md" : "json"}`;
    a.click();
  }
}

function triggerDownload(blob, filename) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ── 启动 ── */
loadOverview();
setInterval(() => { if ($("tab-overview").classList.contains("active")) loadOverview(); }, 8000);
