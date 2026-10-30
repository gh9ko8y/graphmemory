# -*- coding: utf-8 -*-
"""有效期（时效性 TTL）v2 —— 按用户 2026-10-29 规则：

四类：
  1) 永久（不可更改）：性别、出生、籍贯、来自哪里、亲属等既定陈述 → 永不删除
  2) 偏好：无新事实覆盖就一直保留 → 永不自动删除（只能被更新覆盖）
  3) 身份陈述：默认 3 年（1095 天）
  4) 有明确日期（"答应周六带你去海边"）：先不设 TTL，存 due_at；
     到日期后进入"待确认"，确认结果后自动补 TTL（详见 confirm 流程）
  5) 其余按时效性：临时 1 天 / 动作 3 天 / 事件 7 天 / 承诺 10 天 / 普通 14 天

返回结构：{kind, ttl_days, expires_at, due_at, reason}
  kind: permanent / preference / identity / due / ephemeral / action / event / commitment / normal
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

# ── 不可更改的既定陈述（永久）────────────────────────────
IMMUTABLE_CUES = re.compile(
    r"性别|男的|女的|男生|女生|出生|生日|属相|生肖|星座|籍贯|老家|家乡|来自|出身|血统|"
    r"民族|母语|父母|爸爸|妈妈|父亲|母亲|哥哥|姐姐|弟弟|妹妹|亲生的|真名|本名|身份证|"
    r"几岁|年龄|\d{1,3}\s*岁|今年\s*\d{1,3}|血型|是男|是女|名字叫"
)
# ── 偏好（无覆盖就保留）─────────────────────────────────
PREFERENCE_CUES = re.compile(r"喜欢|讨厌|偏好|爱吃|爱喝|不吃|不爱|最爱|口头禅|习惯|受不了|害怕")
# ── 临时状态 ────────────────────────────────────────────
EPHEMERAL_CUES = re.compile(
    r"现在|正在|今天|今晚|等下|马上|待会|这会儿|这两天|最近在|在忙|在弄|心情|好累|"
    r"刚才|刚刚|刚在|此刻|暂时|一会儿"
)
# ── 承诺词（没有日期也按承诺 10 天）───────────────────────
COMMIT_CUES = re.compile(r"答应|说好|约好|记得|提醒|承诺|一定要|务必|带我|给我|等我")

DAY_WORDS = {"明天": 1, "后天": 2, "大后天": 3, "下周": 7, "这周": 3, "周末": 3}
WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
GRACE_DAYS = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: Optional[datetime]) -> datetime:
    if dt is None:
        return _now()
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_due(text: str, now: datetime) -> Optional[datetime]:
    """解析文本里的明确日期/时间点 → 返回 due 时间（没有就 None）。"""
    body = text or ""
    # 1) ISO 日期：2026-10-30 / 2026/10/30
    m = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", body)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except Exception:
            pass
    # 2) 月日：10月30日
    m = re.search(r"(\d{1,2})月(\d{1,2})[日号]", body)
    if m:
        try:
            dt = datetime(now.year, int(m.group(1)), int(m.group(2)), tzinfo=timezone.utc)
            if dt < now - timedelta(days=1):
                dt = datetime(now.year + 1, int(m.group(1)), int(m.group(2)), tzinfo=timezone.utc)
            return dt
        except Exception:
            pass
    # 3) 明天/后天/下周X
    for w, d in DAY_WORDS.items():
        if w in body:
            return now + timedelta(days=d)
    m = re.search(r"(?:周|星期)([一二三四五六日天])", body)
    if m:
        target = WEEKDAY[m.group(1)]
        delta = (target - now.weekday()) % 7
        if delta == 0:
            delta = 7
        return now + timedelta(days=delta)
    # 4) N号
    m = re.search(r"(\d{1,2})号", body)
    if m:
        day = int(m.group(1))
        d = day - now.day
        if d <= 0:
            d += 30
        try:
            return now + timedelta(days=d)
        except Exception:
            return None
    return None


def compute(fact_type: str | None, text: str, created_at: datetime | None = None) -> dict:
    """返回 {kind, ttl_days, expires_at, due_at, reason}。ttl_days/expires_at 为 None 表示永不自动删除。"""
    now = _as_utc(created_at)
    ft = (fact_type or "normal").strip() or "normal"
    body = text or ""

    # 1) 永久的既定陈述
    if IMMUTABLE_CUES.search(body):
        return {"kind": "permanent", "ttl_days": None, "expires_at": None,
                "due_at": None, "reason": "不可更改的既定陈述（永久）"}

    # 2) 偏好：无新覆盖就保留
    if ft == "user_preference" or PREFERENCE_CUES.search(body):
        return {"kind": "preference", "ttl_days": None, "expires_at": None,
                "due_at": None, "reason": "偏好：无新事实覆盖就保留"}

    # 3) 有明确日期 → 先不设 TTL，存 due_at，到日期后确认再定时效
    #    但：过去的日期（已经发生的事）不算"待确认约定"；动作/失败类也永不 pending
    due = parse_due(body, now)
    if due is not None and due <= _now():
        due = None
    if ft in ("action", "failure"):
        due = None
    if due is not None and (ft in ("commitment", "event", "normal") or COMMIT_CUES.search(body)):
        return {"kind": "due", "ttl_days": None, "expires_at": None,
                "due_at": due, "reason": "有明确日期：到期确认后再定时效"}

    # 4) 身份陈述：默认 3 年
    if ft == "core_identity":
        ttl = 365 * 3
        return {"kind": "identity", "ttl_days": ttl, "expires_at": now + timedelta(days=ttl),
                "due_at": None, "reason": "身份陈述（3 年）"}

    # 5) 临时状态
    if EPHEMERAL_CUES.search(body):
        return {"kind": "ephemeral", "ttl_days": 1, "expires_at": now + timedelta(days=1),
                "due_at": None, "reason": "临时状态（1 天）"}

    # 6) 按时效性
    table = {"action": 3, "failure": 3, "event": 7, "commitment": 10, "normal": 14}
    ttl = table.get(ft, 14)
    if ft == "normal" and COMMIT_CUES.search(body):
        ttl = 10
    return {"kind": ft if ft in table else "normal", "ttl_days": ttl,
            "expires_at": now + timedelta(days=ttl), "due_at": None,
            "reason": f"按类型 {ft} 时效性"}


def is_expired(expires_at, now: datetime | None = None) -> bool:
    if not expires_at:
        return False
    n = now or _now()
    if getattr(expires_at, "tzinfo", None) is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at < n


# 兼容旧接口
TTL_BY_TYPE = {"ephemeral": 1, "action": 3, "failure": 3, "event": 7,
               "commitment": 10, "normal": 14, "user_preference": None, "core_identity": 1095}
