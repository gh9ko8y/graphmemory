# -*- coding: utf-8 -*-
"""GraphMemory 自动保存轮询器 — 定时扫描 Hermes 对话新消息，智能提取值得记的事实写入记忆库。

背景：shell hook（graphmemory_autosave.py）在 Ekko Studio Web UI 的 hermes_bridge
进程里不生效（bridge 不注册 shell hooks）。本脚本改用**定时轮询**方案：
每 N 分钟运行一次，扫描 state.db 里新增的 user 消息 → LLM 提取 → 写入 GraphMemory。
进度记录在 PROGRESS_FILE，幂等（不重复处理旧消息）。

用法（Windows 计划任务，每 5 分钟一次）：
    schtasks /create /tn GraphMemoryAutoSave /tr "D:/Program/python.exe D:/GraphMemory/scripts/graphmemory_poller.py" /sc minute /mo 5

或手动跑一次：
    D:/Program/python.exe D:/GraphMemory/scripts/graphmemory_poller.py
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

import httpx

# ── 路径与配置 ──────────────────────────────────────

HERMES_PROFILE_DIR = Path(
    os.environ.get("HERMES_PROFILE_DIR", r"C:\Users\Administrator\.hermes\profiles\jiyiku")
)
# 数据源：优先用 Ekko Studio 总账（覆盖所有 profile 的会话），fallback 到 profile 级 state.db
WEBUI_DB = Path(r"C:\Users\Administrator\.hermes-web-ui\hermes-web-ui.db")
STATE_DB = HERMES_PROFILE_DIR / "state.db"
PROGRESS_FILE = HERMES_PROFILE_DIR / "graphmemory_poll_progress.json"
LOG_FILE = HERMES_PROFILE_DIR / "graphmemory_poller.log"

# 当前数据源（启动时解析，记录在进度文件里防切换）
DB_SOURCE = str(WEBUI_DB) if WEBUI_DB.exists() else str(STATE_DB)

GRAPHMEMORY_BASE_URL = os.environ.get(
    "GRAPHMEMORY_BASE_URL", "http://127.0.0.1:8000/api/v1/memory"
)
# 记忆空间归属：user_id 与 MCP 工具默认值一致（default），这样 MCP 搜索能查到自动保存的记忆；
# agent_id 标记来源为 hermes 自动保存，用于溯源。
USER_ID = os.environ.get("GRAPHMEMORY_POLL_USER_ID", "default")
AGENT_ID = os.environ.get("GRAPHMEMORY_POLL_AGENT_ID", "hermes")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
logger = logging.getLogger("graphmemory_poller")


def _load_graphmemory_env() -> dict[str, str]:
    """读 D:\\GraphMemory\\.env，返回 LLM 相关配置。"""
    env: dict[str, str] = {}
    env_path = Path(r"D:\GraphMemory\.env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


_ENV = _load_graphmemory_env()
LLM_API_KEY = _ENV.get("DEEPSEEK_API_KEY", "") or _ENV.get("OPENAI_API_KEY", "")
LLM_BASE_URL = _ENV.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com") if _ENV.get("DEEPSEEK_API_KEY") else _ENV.get("OPENAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
# 提取判断用 deepseek-flash（免费/极廉，质量比 glm-4-flash 好）
LLM_MODEL = os.environ.get("GRAPHMEMORY_EXTRACT_MODEL", "deepseek-flash")


# ── 进度管理 ────────────────────────────────────────

def _load_progress() -> tuple[str, int]:
    """返回 (数据源, 最后已处理的消息 id)。数据源变了游标重置。"""
    try:
        data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        src = data.get("db_source", "")
        last = int(data.get("last_msg_id", -1))
        if src != DB_SOURCE:
            # 数据源变了（如从 state.db 切到 webui.db），游标作废
            return DB_SOURCE, -1
        return src, last
    except Exception:
        return DB_SOURCE, -1


def _save_progress(last_msg_id: int) -> None:
    PROGRESS_FILE.write_text(
        json.dumps(
            {"db_source": DB_SOURCE, "last_msg_id": last_msg_id}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )


# ── 取新消息 ────────────────────────────────────────

def _fetch_new_messages(last_id: int) -> list[tuple[int, str, str]]:
    """返回 (msg_id, session_id, content) 列表，只取 user 角色、有内容、非 JSON 附件的消息。"""
    if not Path(DB_SOURCE).exists():
        logger.warning(f"数据库不存在: {DB_SOURCE}")
        return []
    conn = sqlite3.connect(f"file:{DB_SOURCE}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, session_id, content FROM messages
            WHERE role = 'user' AND id > ?
              AND content IS NOT NULL AND content != ''
            ORDER BY id ASC
            """,
            (last_id,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    result: list[tuple[int, str, str]] = []
    for msg_id, session_id, content in rows:
        c = content.strip()
        # 跳过文件上传等 JSON 载荷（以 [{ 开头的是结构化消息）
        if c.startswith("[{") or c.startswith("{"):
            continue
        result.append((msg_id, session_id, c))
    return result


# ── LLM 提取值得记的事实 ────────────────────────────

_EXTRACT_PROMPT = """你是记忆提取器。判断下面的用户消息里，有没有值得长期记住的个人信息或事件。

值得记（返回 facts 列表）：
- 用户个人信息：名字、年龄、职业、公司、居住地、身份
- 用户偏好：喜欢/不喜欢什么、习惯、口味
- 用户事件：行程计划（出差/旅行）、做过的事、经历、目标、重要日期
- 用户关系：和谁有什么关系

注意：即使用户在请求帮忙（"帮我查机票"），只要消息里透露出个人事实或行程（如"我下个月去北京出差"），也要提取出来。

不值得记（返回空列表）：
- 纯技术问答、寒暄、闲聊
- 对 AI 的指令、代码、配置
- 完全不含个人信息或事件的临时任务

用户消息：
---
{message}
---

只返回 JSON，格式：{{"facts": ["事实1", "事实2"]}}，没有值得记的就返回 {{"facts": []}}。"""


def _extract_facts(message: str) -> list[str]:
    """调 LLM 提取值得记的事实。失败返回空列表（静默跳过）。"""
    if not LLM_API_KEY:
        logger.warning("无 LLM API key，跳过提取")
        return []
    try:
        resp = httpx.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": "你只输出 JSON，不要输出任何其他内容。"},
                    {"role": "user", "content": _EXTRACT_PROMPT.format(message=message)},
                ],
                "temperature": 0.1,
                "max_tokens": 500,
            },
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(content)
        facts = data.get("facts", [])
        return [f for f in facts if isinstance(f, str) and f.strip()]
    except Exception as e:
        logger.warning(f"LLM 提取失败: {e}")
        return []


# ── 写入 GraphMemory ────────────────────────────────

def _write_to_graphmemory(content: str, session_id: str) -> bool:
    try:
        resp = httpx.post(
            f"{GRAPHMEMORY_BASE_URL}/add",
            json={
                "content": content,
                "source": "hermes_poller",
                "user_id": USER_ID,
                "agent_id": AGENT_ID,
                "session_id": session_id,
                "model": LLM_MODEL,
            },
            timeout=120,
        )
        if resp.status_code == 200:
            return True
        logger.warning(f"/add 返回 {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        logger.warning(f"写入 GraphMemory 失败: {e}")
        return False


# ── 主流程 ──────────────────────────────────────────

def main() -> int:
    _src, last_id = _load_progress()
    new_msgs = _fetch_new_messages(last_id)
    if not new_msgs:
        logger.info(f"无新消息（游标 {last_id}）")
        return 0

    logger.info(f"发现 {len(new_msgs)} 条新消息，开始处理")
    written_total = 0
    for msg_id, session_id, content in new_msgs:
        facts = _extract_facts(content)
        if facts:
            for fact in facts:
                if _write_to_graphmemory(fact, session_id):
                    written_total += 1
                    logger.info(f"[msg {msg_id}] 已写入: {fact[:80]}")
        else:
            logger.info(f"[msg {msg_id}] 无可记事实")
        # 逐条推进游标：即使某条处理失败，也不会死循环重试
        _save_progress(msg_id)

    logger.info(f"完成: {len(new_msgs)} 条消息，写入 {written_total} 条事实")
    return 0


if __name__ == "__main__":
    sys.exit(main())
