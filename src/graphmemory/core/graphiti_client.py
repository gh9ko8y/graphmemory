"""Graphiti 客户端封装 — 双时态知识图谱"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from graphiti_core import Graphiti
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient

from ..config import settings
from ..models import Fact, Entity, Episode, TimeSlice, MemorySearchResult
from ..policy import PolicyEngine

logger = logging.getLogger(__name__)


class GraphitiClient:
    """双时态知识图谱客户端

    封装 Graphiti，提供强制时间切片约束的读写接口。
    """

    def __init__(self):
        self._graphiti: Optional[Graphiti] = None
        self._initialized = False
        self._policy: Optional[PolicyEngine] = None
        # 聊天记忆扩展（episodes 原文账本 + 检索竞争）；main.py 注入
        self._recall_repo: Optional[Any] = None
        self._competitive: Optional[Any] = None

    def set_policy(self, policy: PolicyEngine) -> None:
        self._policy = policy

    def set_recall(self, recall_repo: Any, competitive: Any) -> None:
        self._recall_repo = recall_repo
        self._competitive = competitive

    async def initialize(self) -> None:
        """初始化 Graphiti 连接和索引"""
        if self._initialized:
            return

        llm_config = LLMConfig(
            api_key=settings.deepseek_api_key,
            model=settings.default_llm_model,
            small_model=settings.default_llm_model,
            base_url=settings.deepseek_base_url,
        )

        self._graphiti = Graphiti(
            settings.neo4j_uri,
            settings.neo4j_user,
            settings.neo4j_password,
            llm_client=OpenAIGenericClient(
                config=llm_config,
                structured_output_mode="json_object",
            ),
            embedder=OpenAIEmbedder(
                config=OpenAIEmbedderConfig(
                    api_key=settings.openai_api_key,
                    base_url=settings.openai_base_url,
                    embedding_model=settings.default_embedding_model,
                )
            ),
            cross_encoder=OpenAIRerankerClient(config=llm_config),
        )

        # 初始化索引和约束
        await self._graphiti.build_indices_and_constraints()
        self._initialized = True
        logger.info("Graphiti initialized successfully")

    async def close(self) -> None:
        """关闭连接"""
        if self._graphiti:
            await self._graphiti.close()
            self._initialized = False

    async def graph_stats(self) -> dict:
        """图谱连通性指标（渗流视角）。

        N = 实体数，E = 事实边（RELATES_TO）数，⟨k⟩ = 2E/N。
        连续渗流里 ER 随机图的巨分量判据是 ⟨k⟩ > 1：
        - ⟨k⟩ ≤ 1：记忆是孤岛，只能靠语义精确命中，联想（提 A 想起 B）走不通
        - ⟨k⟩ > 1：存在巨分量，关联扩散才有路
        mentions 边（MENTIONS）单独给出，作为"实体被引用"的参考。
        """
        try:
            from neo4j import AsyncGraphDatabase
        except Exception:
            return {}
        driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
        )
        try:
            async with driver.session() as s:
                r = await s.run("MATCH (n:Entity) RETURN count(n) AS c")
                n = int((await r.single())["c"] or 0)
                r = await s.run("MATCH ()-[r:RELATES_TO]->() RETURN count(r) AS c")
                e = int((await r.single())["c"] or 0)
                r = await s.run("MATCH ()-[r:MENTIONS]->() RETURN count(r) AS c")
                mentions = int((await r.single())["c"] or 0)
                r = await s.run(
                    "MATCH (n:Entity) WHERE NOT (n)-[:RELATES_TO]-() RETURN count(n) AS c"
                )
                iso = int((await r.single())["c"] or 0)
                # 孤立实体名单（之前只有统计没有名单，她没法判断要不要连）
                try:
                    r = await s.run(
                        "MATCH (n:Entity) WHERE NOT (n)-[:RELATES_TO]-() "
                        "RETURN n.name AS name LIMIT 12"
                    )
                    iso_names = [row["name"] for row in await r.data() if row.get("name")]
                except Exception:
                    iso_names = []
            from ..recall.percolation import percolation_report
            k_all = round((2.0 * (e + mentions) / n), 3) if n else 0.0
            rep = percolation_report(n, e, iso)
            rep["mention_edges"] = mentions
            rep["avg_degree_with_mentions"] = k_all
            rep["giant_component"] = rep.get("supercritical", False)
            rep["isolated_ratio"] = round(iso / n, 3) if n else 0.0
            rep["isolated_entity_names"] = iso_names
            return rep
        except Exception as ex:  # 指标拿不到不能影响主接口
            logger.warning(f"graph_stats failed: {ex}")
            return {}
        finally:
            await driver.close()

    async def ping(self) -> bool:
        """Neo4j 连通性检查（/status 监控用）。"""
        try:
            from neo4j import AsyncGraphDatabase
        except Exception:
            return False
        driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
        )
        try:
            async with driver.session() as session:
                await session.run("RETURN 1")
            return True
        except Exception as e:
            logger.warning(f"neo4j ping failed: {e}")
            return False
        finally:
            await driver.close()

    # ── 写入 ──────────────────────────────────────────

    async def add_episode(
        self,
        content: str,
        source: str = "conversation",
        source_id: Optional[str] = None,
        user_id: Optional[str] = None,
        valid_from: Optional[datetime] = None,
        group_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        chat_id: Optional[str] = None,
        actors: Optional[list[str]] = None,
        importance: Optional[float] = None,
        source_type: str = "remembered_fact",
        episode_meta: Optional[dict] = None,
        raw_quote: Optional[str] = None,
    ) -> Episode:
        """添加一个 episode（原始数据），Graphiti 自动提取实体和关系

        聊天记忆扩展：
          chat_id/actors/episode_meta → PG episodes 原文账本（raw_text 永不改）
          importance/source_type → 六变量初值 + Q_recon 来源权重
          raw_quote → 用户原话片段；没有它的事实不能算"原文直录"
          抽出的事实 evidence_ids 指回 episode 原文（证据链自动成立）
        """
        if not self._initialized:
            await self.initialize()

        now = datetime.now(timezone.utc)
        vt = valid_from or now

        # 写入 Graphiti（自动提取实体和关系）
        # custom_extraction_instructions 注入 "JSON" 字样：
        # DeepSeek 的 json_object 模式强制要求 prompt 含 "json" 一词，
        # 而 Graphiti 原生 prompt 没有，会导致 400 → 静默降级为 0 条事实。
        result = await self._graphiti.add_episode(
            name=f"{source}_{source_id or 'direct'}",
            episode_body=content,
            source_description=f"User: {user_id}" if user_id else source,
            reference_time=vt,
            group_id=group_id or user_id or "default",
            custom_extraction_instructions=(
                "Return your extraction result as a JSON object."
            ),
        )

        # 原文账本：先建 episode 记录（事实的证据要指回它）
        # 主键用 Graphiti 的 episode uuid（与 Neo4j 的 Episodic 节点同源），
        # 拿不到时回退本地 uuid —— 账本与图谱可互查。
        episode = Episode(
            content=content,
            source=source,
            source_id=source_id,
            user_id=user_id,
            timestamp=vt,
        )
        if result is not None:
            _g_ep = getattr(result, "episode", None)
            _g_uuid = getattr(_g_ep, "uuid", None)
            if _g_uuid:
                episode.id = str(_g_uuid)
        if self._recall_repo is not None:
            try:
                await self._recall_repo.insert_episode(
                    episode.id,
                    raw_text=content,
                    ts=vt,
                    chat_id=chat_id,
                    actors=actors or ([user_id] if user_id else []),
                    user_id=user_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    meta=episode_meta,
                    raw_quote=raw_quote,
                )
            except Exception as e:  # 账本失败不阻断抽取，但证据链会缺原文
                logger.warning(f"episodes ledger write failed: {e}")

        # 情绪初值：命中关键词给 0.6，否则 0.1（聊天场景够用，参数见 recall.config）
        emotion = None
        if self._policy is not None:
            from ..system2.tools import EMOTION_KEYWORDS

            emotion = 0.6 if any(kw in content for kw in EMOTION_KEYWORDS) else 0.1


        # 从结果中提取实体名映射 + 事实，交给 Policy 引擎
        facts_extracted: list[str] = []
        entities_extracted: list[str] = []
        if result:
            name_map = {n.uuid: n.name for n in result.nodes}
            entities_extracted = list(name_map.values())
            for edge in result.edges:
                fact = self._edge_to_fact(edge, name_map)
                facts_extracted.append(fact.id)
                if self._policy is not None:
                    try:
                        await self._policy.on_fact_added(
                            fact,
                            user_id=user_id,
                            agent_id=agent_id,
                            session_id=session_id,
                            model=model,
                            episode_id=episode.id,
                            source_type=source_type,
                            emotion=emotion,
                            significance=importance,
                        )
                    except Exception as e:  # Policy 失败不应阻断写入
                        logger.warning(f"Policy on_fact_added failed: {e}")

        episode.facts_extracted = facts_extracted
        episode.entities_extracted = entities_extracted

        logger.info(
            f"Episode added: {episode.id}, "
            f"graph edges: {len(result.edges) if result else 0}, "
            f"facts tracked: {len(facts_extracted)}"
        )
        return episode

    # ── 检索（强制时间切片） ──────────────────────────

    async def search(
        self,
        query: str,
        time_slice: Optional[TimeSlice] = None,
        user_id: Optional[str] = None,
        top_k: int = 10,
    ) -> MemorySearchResult:
        """检索记忆 — 强制时间切片约束

        这是双时态的核心：每次检索必须带时间参数，
        不允许无时间约束的模糊查询。
        """
        if not self._initialized:
            await self.initialize()

        ts = time_slice or TimeSlice.now()

        # Graphiti 搜索
        import time

        start = time.monotonic()

        results = await self._graphiti.search(
            query=query,
            num_results=top_k,
            group_ids=[user_id] if user_id else None,
        )

        elapsed_ms = (time.monotonic() - start) * 1000

        # 从 Neo4j 解析 edge 端点 uuid → 实体名（search 路径无 name_map，避免 uuid 占位）
        name_map = await self._resolve_entity_names(
            {getattr(e, "source_node_uuid", "") for e in results}
            | {getattr(e, "target_node_uuid", "") for e in results}
        )

        # 防串味第二道闸：graph 层再按 group_id 兜底过滤
        # （group_ids 传入是主闸；这里防止历史数据/异常写入跨组混进别人的记忆）
        if user_id and results:
            kept = [e for e in results
                    if str(getattr(e, "group_id", "") or "") == str(user_id)]
            dropped = len(results) - len(kept)
            if dropped:
                logger.info(f"search: group guard dropped {dropped} edges (want group={user_id})")
            results = kept

        # 过滤时间切片：只返回在 valid_from/end 范围内的结果
        facts = []
        for edge in results:
            fact = self._edge_to_fact(edge, name_map)
            if self._in_time_slice(fact, ts):
                facts.append(fact)

        # B8 修复：Neo4j 边没有 fact_type/source_type/raw_quote，用 PG 补齐
        _repo = getattr(self, "_recall", None) or getattr(self, "_recall_repo", None)
        if facts and _repo is not None and hasattr(_repo, "pool"):
            try:
                _ids = [f.id for f in facts]
                _rows = await _repo.pool.fetch(
                    "SELECT fact_id, fact_type, source_type, raw_quote, expires_at, due_at, confirm_state, birth_date, birth_year FROM fact_state "
                    "WHERE fact_id = ANY($1::text[])", _ids,
                )
                _map = {r["fact_id"]: r for r in _rows}
                # 时效性：过期 / 已删除（墓碑）→ 本次检索直接排除
                import datetime as _dtm
                _now = _dtm.datetime.now(_dtm.timezone.utc)
                _expired = {r["fact_id"] for r in _rows
                            if r.get("expires_at") and r["expires_at"] < _now}
                _dead = set()
                _trows = await _repo.pool.fetch(
                    "SELECT fact_id FROM memory_tombstones WHERE fact_id = ANY($1::text[])", _ids)
                _dead = {r["fact_id"] for r in _trows}
                facts = [f for f in facts if f.id not in _expired and f.id not in _dead]
                for _f in facts:
                    _r = _map.get(_f.id)
                    if not _r:
                        continue
                    if _r["fact_type"]:
                        _f.fact_type = _r["fact_type"]
                    if _r["source_type"]:
                        _f.source_type = _r["source_type"]
                    if _r["raw_quote"] and _r["source_type"] == "remembered_fact":
                        _f.q_recon = 0.95
                    if _r.get("birth_date") or _r.get("birth_year"):
                        try:
                            from graphmemory.recall.temporal_age import describe as _describe_age
                            _note = _describe_age(_r.get("birth_date"), _r.get("birth_year"))
                            if _note:
                                _f.age_note = _note
                        except Exception:
                            pass
                    if _r.get("due_at") and str(_r.get("confirm_state") or "") != "confirmed":
                        _f.age_label = (_f.age_label + "｜待确认" if getattr(_f, "age_label", "") else "待确认")
            except Exception as _e:
                logger.warning(f"fact enrich failed: {_e}")

        # Policy 遗忘过滤 + 记录访问（不删除，只影响本次检索）
        if self._policy is not None and facts:
            try:
                facts = await self._policy.filter_and_record(facts, user_id=user_id)
            except Exception as e:
                logger.warning(f"Policy filter_and_record failed: {e}")

        # 检索竞争：激活×匹配×资源适配 → θ 过滤 → Top-K → 命中巩固
        # （挂在这里 = 所有检索路径自动获得竞争排序，包括 System2 工具）
        if self._competitive is not None and facts:
            try:
                facts = await self._competitive.rerank(facts, query, user_id=user_id)
            except Exception as e:
                logger.warning(f"Competitive rerank failed: {e}")

        return MemorySearchResult(
            facts=facts,
            entities=[],
            episodes=[],
            total_count=len(facts),
            query_time_ms=elapsed_ms,
        )

    async def search_with_context(
        self,
        query: str,
        user_id: str,
        recent_messages: Optional[list[str]] = None,
        time_slice: Optional[TimeSlice] = None,
    ) -> MemorySearchResult:
        """带上下文的检索 — 用于 System 2 决策"""
        # 先搜索事实
        result = await self.search(
            query=query,
            time_slice=time_slice,
            user_id=user_id,
        )

        # TODO: 补充 entity 和 episode 检索
        return result

    # ── 时间线查询 ──────────────────────────────────

    async def get_entity_timeline(
        self, entity_name: str, user_id: Optional[str] = None
    ) -> list[Fact]:
        """查询某实体的完整时间线（只返回 source 或 target 恰为该实体的事实）"""
        if not self._initialized:
            await self.initialize()

        results = await self._graphiti.search(
            query=entity_name,
            num_results=50,
            group_ids=[user_id] if user_id else None,
        )

        # uuid → 实体名解析，然后按实体名精确过滤（不混入邻居实体的边）
        name_map = await self._resolve_entity_names(
            {getattr(e, "source_node_uuid", "") for e in results}
            | {getattr(e, "target_node_uuid", "") for e in results}
        )
        facts = [
            self._edge_to_fact(e, name_map)
            for e in results
            if entity_name in (
                name_map.get(getattr(e, "source_node_uuid", ""), ""),
                name_map.get(getattr(e, "target_node_uuid", ""), ""),
            )
        ]
        # 按 valid_from 排序
        facts.sort(key=lambda f: f.valid_from)
        return facts

    async def _resolve_entity_names(self, uuids: set[str]) -> dict[str, str]:
        """批量解析实体 uuid → name（search 路径无 name_map 时的兜底）。

        失败静默返回空映射，调用方回退 uuid 占位，不阻断检索。
        """
        uuids = {u for u in uuids if u}
        if not uuids:
            return {}
        try:
            from neo4j import AsyncGraphDatabase

            driver = AsyncGraphDatabase.driver(
                settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
            )
            try:
                async with driver.session() as session:
                    res = await session.run(
                        "MATCH (n) WHERE n.uuid IN $uuids RETURN n.uuid AS uuid, n.name AS name",
                        uuids=list(uuids),
                    )
                    mapping = {}
                    async for rec in res:
                        if rec["uuid"] and rec["name"]:
                            mapping[rec["uuid"]] = rec["name"]
                    return mapping
            finally:
                await driver.close()
        except Exception as e:
            logger.warning(f"resolve entity names failed: {e}")
            return {}

    # ── 内部方法 ──────────────────────────────────

    @staticmethod
    def _in_time_slice(fact: Fact, ts: TimeSlice) -> bool:
        """检查事实是否在时间切片内"""
        # 事实必须在查询时间范围内有效
        if fact.valid_from > (ts.end or datetime.now(timezone.utc)):
            return False  # 事实还未生效
        if ts.start and fact.valid_until and fact.valid_until <= ts.start:
            return False  # 事实在查询时间前已失效
        return True

    @staticmethod
    def _edge_to_fact(edge, name_map: Optional[dict[str, str]] = None) -> Fact:
        """将 Graphiti EntityEdge 转为 Fact。

        name_map: {node_uuid: name}，由 add_episode 从 result.nodes 构建。
        search 路径无 name_map 时回退用 uuid 占位。
        """
        nm = name_map or {}
        src_uuid = getattr(edge, "source_node_uuid", "")
        tgt_uuid = getattr(edge, "target_node_uuid", "")
        return Fact(
            id=getattr(edge, "uuid", "") or "",
            source_entity=nm.get(src_uuid, src_uuid or ""),
            target_entity=nm.get(tgt_uuid, tgt_uuid or ""),
            relationship=getattr(edge, "name", ""),
            fact_text=getattr(edge, "fact", getattr(edge, "name", "")),
            valid_from=getattr(edge, "valid_at", None) or getattr(edge, "created_at", datetime.now(timezone.utc)),
            valid_until=getattr(edge, "invalid_at", None),
            confidence=1.0,
        )
