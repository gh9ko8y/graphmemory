"""端到端验收：/add(聊天字段) → episodes 账本 → /search 竞争召回 → /chat 口径注入。

用法（服务需在跑）：python scripts/verify_chat_memory.py

安全约束：只允许访问本机 GraphMemory API（http + 环回地址 + 固定端口），
拒绝任何其他主机，避免这个验收脚本被当作通用请求代理。
"""

import json
import urllib.parse
import urllib.request

ALLOWED_HOST = "127.0.0.1"
ALLOWED_PORT = 8000
ALLOWED_SCHEME = "http"
API_PREFIX = "/api/v1/memory"


def _build_url(path: str) -> str:
    """拼 URL 并强校验：协议/主机/端口三项白名单。"""
    url = f"{ALLOWED_SCHEME}://{ALLOWED_HOST}:{ALLOWED_PORT}{API_PREFIX}{path}"
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme != ALLOWED_SCHEME
        or parts.hostname != ALLOWED_HOST
        or parts.port != ALLOWED_PORT
    ):
        raise ValueError(f"refusing non-local target: {url}")
    return url


def post(path, payload):
    req = urllib.request.Request(
        _build_url(path),
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as r:  # noqa: S310 (白名单已校验)
        return json.loads(r.read())


def get(path):
    with urllib.request.urlopen(_build_url(path), timeout=60) as r:  # noqa: S310
        return json.loads(r.read())


def main():
    print("=== 1. /add 写入聊天记忆（带 chat_id/actors/importance）===")
    r = post(
        "/add",
        {
            "content": (
                "用户B说她最近特别喜欢下雨天，因为小时候外婆家院子里有雨声。"
                "她还答应我下周三一起去看《海上钢琴师》。"
            ),
            "user_id": "100000002",
            "chat_id": "private:100000002",
            "actors": ["100000002", "assistant"],
            "importance": 0.85,
            "source": "conversation",
        },
    )
    print("episode_id:", r["episode_id"])

    print("\n=== 2. episodes 原文账本（原文必须一字不差）===")
    eps = get("/episodes?user_id=100000002")
    print("条数:", eps["total"])
    if eps["episodes"]:
        print("chat_id:", eps["episodes"][0]["chat_id"])
        print("raw_text:", eps["episodes"][0]["raw_text"][:60], "...")

    print("\n=== 3. /search 竞争召回 ===")
    s = post("/search", {"query": "她喜欢什么天气？", "user_id": "100000002", "top_k": 10})
    print("命中:", s["total_count"], "条")
    for f in s["facts"]:
        print(f"  - {f['fact_text'][:50]} | conf={f['confidence']:.2f}")

    print("\n=== 4. /chat 结构化注入口径 ===")
    c = post("/chat", {"message": "你还记得我喜欢什么天气吗？", "user_id": "100000002"})
    print("system:", c["system"], "| model:", c["model"])
    print("--- 注入的记忆上下文 ---")
    print((c.get("context") or "(空)")[:700])
    print("--- 她的回复 ---")
    print((c.get("response") or "(无)")[:400])

    print("\n=== 5. /decay 衰减一轮（刚写入的不会被淡出）===")
    print(post("/decay?user_id=100000002", {}))
    print("memory-state:", get("/memory-state"))

    print("\n=== 6. 口径分级：推断类记忆必须说「不确定」 ===")
    post(
        "/add",
        {
            "content": "我猜用户B可能不太爱吃香菜，因为她上次把香菜挑出来了。",
            "user_id": "100000002",
            "chat_id": "private:100000002",
            "source_type": "inferred_detail",   # ← 推断，不是原文直录
            "importance": 0.4,
        },
    )
    c2 = post("/chat", {"message": "用户B爱吃香菜吗？", "user_id": "100000002"})
    ctx = c2.get("context") or ""
    print("--- 注入上下文 ---")
    print(ctx[:500])
    has_uncertain = "[不确定]" in ctx
    print(f"→ 口径分级 {'OK：出现[不确定]' if has_uncertain else 'FAIL：未出现[不确定]'}")

    print("\n=== 7. 淡出降级（真实 HTTP 路径）：D 没了 S 还在 → 片段 ===")
    print("提示：需先把某条事实的 D 打低（见测试脚本 or SQL），此处仅确认端点可用")
    print("memory-state:", get("/memory-state"))


if __name__ == "__main__":
    main()
