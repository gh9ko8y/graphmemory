# -*- coding: utf-8 -*-
"""GraphMemory 自动保存 Hook —— 监听 Hermes on_session_end 事件，智能提取值得记的事实写入 GraphMemory。

工作流：
  1. Hermes 每轮对话结束触发 on_session_end，stdin 传入 JSON（含 session_id）
  2. 脚本读 profiles/jiyiku/state.db，取出该 session 最近一轮 user 消息（含上下文）
  3. 调 LLM（复用 GraphMemory 的智谱 glm-4-plus）判断"有没有值得长期记住的事实"
  4. 有 → 调 GraphMemory /add 写入；没有 → 静默跳过

设计原则：
  - 静默失败：任何错误都不阻断对话，只记日志
  - 只存记忆：临时性任务/寒暄/技术问答会被 LLM 过滤掉
  - 幂等安全：GraphMemory 的 Policy 层会做重复确认合并，不会污染

用法（在 Hermes config.yaml 的 hooks 块）：
  hooks:
    on_session_end:
      - command: "D:\\Program\\python.exe D:\\GraphMemory\\scripts\\graphmemory_autosave.py"
        timeout: 120
  hooks_auto_accept: true
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# ── 路径与配置 ──────────────────────────────────────

# Hermes profile 目录（state.db 所在），脚本运行时由环境变量或默认推断
HERMES_PROFILE_DIR = Path(
    os.environ.get("HERMES_PROFILE_DIR", r"C:\Users\Administrator\.hermes\profiles\jiyiku")
)
STATE_DB = HERMES_PROFILE_DIR / "state.db"

# GraphMemory 地址与认证
GRAPHMEMORY_BASE_URL = os.environ.get(
    "GRAPHMEMORY_BASE_URL", "http://127.0.0.1:8000/api/v1/memory"
)

# 日志写到哪里（默认 stderr，会被 Hermes 记入 hook 日志）
LOG_FILE = HERMES_PROFILE_DIR / "graphmemory_autosave.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
logger = logging.getLogger("graphmemory_autosave")


# ── LLM 配置（从 GraphMemory 的 .env 读取，复用同一套凭据）──

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
LLM_API_KEY = _ENV.get("OPENAI_API_KEY", "")
LLM_BASE_URL = _ENV.get("OPENAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
# 提取用 flash 模型：更快、更省，且避开 plus 的限流。GraphMemory 主体仍用 .env 的 DEFAULT_LLM_MODEL
LLM_MODEL = os.environ.get("GRAPHMEMORY_EXTRACT_MODEL", "glm-4-flash")


# ── 取最近一轮用户消息 ──────────────────────────────

def _latest_user_message(session_id: str) -> str | None:
    """从 state.db 取该 session 最近一条 user 消息。"""
    if not STATE_DB.exists():
        logger.warning(f"state.db 不存在: {STATE_DB}")
        return None
    try:
        conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT content FROM messages
            WHERE session_id = ? AND role = 'user'
              AND content IS NOT NULL AND content != ''
            ORDER BY timestamp DESC LIMIT 1
            """,
            (session_id,),
        )
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        logger.warning(f"读 state.db 失败: {e}")
        return None


# ── LLM 判断是否值得记 ──────────────────────────────

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
        # 剥离可能的 markdown 代码块
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
    """调 GraphMemory /add 写入一条记忆。"""
    try:
        resp = httpx.post(
            f"{GRAPHMEMORY_BASE_URL}/add",
            json={
                "content": content,
                "source": "hermes_autosave",
                "user_id": "hermes",
                "agent_id": "hermes",
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

def main() -> None:
    # 从 stdin 读 hook payload
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        logger.warning(f"读 stdin 失败: {e}")
        return

    session_id = payload.get("session_id", "")
    event = payload.get("hook_event_name", "")
    if not session_id:
        logger.warning("payload 无 session_id，跳过")
        return

    logger.info(f"hook 触发: event={event} session={session_id}")

    # 1. 取最近一轮用户消息
    message = _latest_user_message(session_id)
    if not message:
        logger.info("无用户消息，跳过")
        return

    # 2. LLM 提取值得记的事实
    facts = _extract_facts(message)
    if not facts:
        logger.info("LLM 判断无可记事实，跳过")
        return

    # 3. 逐条写入 GraphMemory
    written = 0
    for fact in facts:
        if _write_to_graphmemory(fact, session_id):
            written += 1
            logger.info(f"已写入: {fact[:80]}")

    logger.info(f"完成: 提取 {len(facts)} 条，写入 {written} 条")


if __name__ == "__main__":
    main()
