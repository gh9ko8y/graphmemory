# -*- coding: utf-8 -*-
"""年龄等"随时间变化"的事实：存锚点（出生日期/年份），检索时实时计算当前值。

例：
  写入 "用户今年20岁"（2026-10）→ 存 birth_year=2006
  2028-10 检索 → 自动算出 22 岁，输出"现在 22 岁（2026 年时 20 岁）"

  写入 "用户生日 2006-02-08" → 存 birth_date=2006-02-08
  2028-03 检索 → 22 岁（已过生日）；2028-01 → 21 岁
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Optional

# 年龄 / 出生 相关线索
AGE_CUES = re.compile(r"\d{1,3}\s*岁|今年\s*\d{1,3}|年龄|多大|几岁|出生|生日|属相|生肖")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_date(dt: Optional[datetime | date]) -> Optional[date]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.date()
    return dt


def extract_birth(text: str, created_at: datetime | None = None):
    """从文本提取出生信息。

    返回 (birth_date, birth_year, how)：
      - 有具体日期 → (date, None, 'birth_date')
      - 只有年份   → (None, year, 'birth_year')
      - 只有年龄   → (None, 当前年-年龄, 'age')
      - 没有       → (None, None, '')
    """
    body = text or ""
    now = created_at or _now()
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)

    # 门槛：没有出生/年龄语境就别猜（防止把事实里的日期当出生日期）
    if not re.search(r"出生|生于|生日|生年|属相|生肖|年龄|几岁|多大|\d{1,3}\s*岁|今年\s*\d{1,3}", body):
        return None, None, ""

    # 1) 完整日期：2006-02-08 / 2006/2/8 / 2006年2月8日
    m = re.search(r"(19\d{2}|20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", body)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), None, "birth_date"
        except Exception:
            pass
    # 2) 无分隔符：20060208
    m = re.search(r"(19\d{2}|20\d{2})(\d{2})(\d{2})", body)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), None, "birth_date"
        except Exception:
            pass
    # 3) 只有年份：2006年 / 2006出生
    m = re.search(r"(19\d{2}|20\d{2})\s*年(?:出生|生)?", body)
    if m:
        return None, int(m.group(1)), "birth_year"
    # 4) 年龄：20岁 / 今年20 / 20周岁
    m = re.search(r"(\d{1,3})\s*(?:周岁|岁)", body) or re.search(r"今年\s*(\d{1,3})", body)
    if m:
        age = int(m.group(1))
        if 0 < age < 150:
            return None, now.year - age, "age"
    return None, None, ""


def age_now(birth_date: date | None, birth_year: int | None,
            now: datetime | None = None) -> Optional[int]:
    """计算当前年龄（有精确日期则按生日是否已过；只有年份按年份差）。"""
    n = now or _now()
    n = n if n.tzinfo else n.replace(tzinfo=timezone.utc)
    if birth_date:
        age = n.year - birth_date.year
        if (n.month, n.day) < (birth_date.month, birth_date.day):
            age -= 1
        return age
    if birth_year:
        return n.year - birth_year
    return None


def describe(birth_date: date | None, birth_year: int | None,
             now: datetime | None = None) -> str:
    """渲染成一句人话，用于检索结果注入。"""
    a = age_now(birth_date, birth_year, now)
    if a is None:
        return ""
    if birth_date:
        return f"现在 {a} 岁（出生 {birth_date.isoformat()}）"
    return f"现在 {a} 岁（{birth_year} 年生）"


if __name__ == "__main__":
    from datetime import datetime as dt
    for text, created in [
        ("用户今年20岁", dt(2026, 10, 29, tzinfo=timezone.utc)),
        ("用户生日 2006-02-08", dt(2026, 10, 29, tzinfo=timezone.utc)),
        ("用户2006年出生", dt(2026, 10, 29, tzinfo=timezone.utc)),
        ("用户的出生时间 20060208", dt(2026, 10, 29, tzinfo=timezone.utc)),
    ]:
        bd, by, how = extract_birth(text, created)
        print(f"{text:<22} -> {how:<10} {bd or by} | 2026: {age_now(bd, by, dt(2026,10,29,tzinfo=timezone.utc))} 岁"
              f" | 2028: {age_now(bd, by, dt(2028,10,29,tzinfo=timezone.utc))} 岁")
