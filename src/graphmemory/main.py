"""GraphMemory 主入口"""

import logging
from pathlib import Path

import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .core.graphiti_client import GraphitiClient
from .api.routes import (
    router,
    set_client,
    set_policy_engine,
    set_procedural,
    set_system2,
    set_agents,
    set_cache,
    set_chat,
    set_recall,
)
from .policy import PolicyEngine, PolicyRepository
from .procedural import ProceduralMemory, ProceduralRepository
from .system2 import System2Orchestrator, ToolBackend
from .agents import AgentManager, AgentRepository
from .cache import CacheStore
from .chat import ChatService
from .recall import (
    DecayConfig,
    DecayEngine,
    CompetitiveRecall,
    RecallConfig,
    RecallRepository,
)

# 日志轮转：避免日志无限增长
import logging
from logging.handlers import RotatingFileHandler

logging.basicConfig(level=settings.log_level.upper())
logger = logging.getLogger(__name__)
_fh = RotatingFileHandler(
    "graphmemory.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(_fh)

# 全局 Graphiti 客户端
graphiti_client = GraphitiClient()
policy_repo = PolicyRepository(settings.postgres_dsn)
policy_engine = PolicyEngine(policy_repo)
procedural_repo = ProceduralRepository(settings.postgres_dsn)
procedural_engine = ProceduralMemory(procedural_repo)
agent_repo = AgentRepository(settings.postgres_dsn)
agent_manager = AgentManager(agent_repo)
cache_store = CacheStore(settings.redis_url)

# 聊天记忆扩展（双时态升级）：六变量衰减 + 检索竞争
recall_repo = RecallRepository(settings.postgres_dsn)
decay_engine = DecayEngine(recall_repo, DecayConfig())
competitive = CompetitiveRecall(recall_repo, RecallConfig())


async def _decay_loop():
    """遗忘作业：每 6 小时一轮（细节淡出、意义长留、淡出降级成片段）。"""
    import asyncio

    cfg = DecayConfig()
    first = True
    while True:
        # 启动后 60 秒先跑一轮，之后每 interval_hours 一轮
        await asyncio.sleep(60 if first else cfg.interval_hours * 3600)
        first = False
        try:
            report = await decay_engine.run_decay()
            logger.info(f"scheduled decay: {report}")
        except Exception as e:
            logger.warning(f"scheduled decay failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info("Starting GraphMemory...")
    await graphiti_client.initialize()
    # 共享连接池：三个 Repository 共用一个 asyncpg 池（4 池 → 1 池）
    import asyncpg

    shared_pool = await asyncpg.create_pool(settings.postgres_dsn, min_size=2, max_size=10)
    policy_repo.attach_pool(shared_pool)
    procedural_repo.attach_pool(shared_pool)
    agent_repo.attach_pool(shared_pool)
    recall_repo.attach_pool(shared_pool)
    await cache_store.connect()
    graphiti_client.set_policy(policy_engine)
    graphiti_client.set_recall(recall_repo, competitive)
    policy_engine.set_recall_repo(recall_repo)
    set_client(graphiti_client)
    set_policy_engine(policy_engine)
    set_procedural(procedural_engine)
    set_recall(recall_repo, decay_engine)
    system2 = System2Orchestrator(
        ToolBackend(
            graphiti=graphiti_client,
            policy_repo=policy_repo,
            procedural=procedural_engine,
            cache=cache_store,
        )
    )
    set_system2(system2)
    set_agents(agent_manager)
    set_cache(cache_store)
    set_chat(ChatService(system2))

    # 遗忘作业后台任务（每 6 小时；sleep-first，启动时不跑）
    import asyncio as _asyncio

    decay_task = _asyncio.create_task(_decay_loop())

    async def _validity_loop():
        """时效性清理：每 6 小时一次（到期标记 + 宽限期后删除）。"""
        import asyncio as _a
        first = True
        while True:
            await _a.sleep(90 if first else 6 * 3600)
            first = False
            try:
                rep = await recall_repo.expire_and_purge(grace_days=1)
                logger.info(f"validity cleanup: {rep}")
            except Exception as e:
                logger.warning(f"validity cleanup failed: {e}")

    validity_task = _asyncio.create_task(_validity_loop())

    logger.info(f"GraphMemory ready on {settings.app_host}:{settings.app_port}")
    yield
    # 先等衰减任务真正退出再关连接池，避免与 pool.close() 竞争
    decay_task.cancel()
    validity_task.cancel()
    with contextlib.suppress(_asyncio.CancelledError):
        await decay_task
    logger.info("Shutting down GraphMemory...")
    await cache_store.close()
    await shared_pool.close()
    await graphiti_client.close()


app = FastAPI(
    title="GraphMemory",
    description="双时态 AI 记忆系统 — 基于 Graphiti + 自建扩展",
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def api_key_auth(request, call_next):
    """可选 API Key 鉴权：仅当 settings.api_key 非空时，/api 路径需带 X-API-Key。"""
    if settings.api_key and request.url.path.startswith("/api"):
        from fastapi import HTTPException

        if request.headers.get("X-API-Key", "") != settings.api_key:
            raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")
    return await call_next(request)


app.include_router(router)

# CORS：Streamlit 对照版（localhost:8501）跨端口调用 API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8501", "http://127.0.0.1:8501"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 管理台（网页版 GUI） ─────────────────────────────
# 清爽浅色单页管理台，随 API 一起启动：http://127.0.0.1:8000/ui/
WEB_DIR = Path(__file__).resolve().parent / "web"
if WEB_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(WEB_DIR), html=True), name="ui")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/ui/")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "graphmemory.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
    )
