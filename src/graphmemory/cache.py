"""Redis 缓存 — 给读路径加速（get_user_profile 等），解决 Redis 死依赖。"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class CacheStore:
    def __init__(self, url: str):
        self._url = url
        self._client = None

    async def connect(self) -> None:
        try:
            from redis import asyncio as aioredis

            self._client = aioredis.from_url(self._url, decode_responses=True)
            await self._client.ping()
            logger.info("CacheStore connected")
        except Exception as e:
            logger.warning(f"Redis cache unavailable (continuing without cache): {e}")
            self._client = None

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get_json(self, key: str) -> Optional[Any]:
        if not self._client:
            return None
        try:
            v = await self._client.get(key)
            return json.loads(v) if v else None
        except Exception as e:
            logger.warning(f"cache get failed: {e}")
            return None

    async def set_json(self, key: str, value: Any, ttl: int = 300) -> None:
        if not self._client:
            return
        try:
            await self._client.set(key, json.dumps(value, ensure_ascii=False), ex=ttl)
        except Exception as e:
            logger.warning(f"cache set failed: {e}")

    async def ping(self) -> bool:
        if not self._client:
            return False
        try:
            return bool(await self._client.ping())
        except Exception:
            return False
