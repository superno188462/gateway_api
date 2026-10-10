"""Single portable entry point for Gateway vectors and user context."""
import httpx
from typing import Any, Awaitable, Callable
from .vector_store import GatewayVectorStore
from .context import GatewayContextClient


class GatewayClient:
    """Configure Gateway interaction once, then use vectors and context capabilities.

    Copy this entire package to another project. It has no FastAPI, ORM, login
    or environment-file dependencies. User identity and authorization remain
    the responsibility of the caller.
    """
    def __init__(self, base_url: str, api_key: str, collection: str = "", *,
                 embedding_model: str = "", vector_base_url: str | None = None,
                 vector_timeout: float = 120, context_timeout: float = 15,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.vectors = GatewayVectorStore(vector_base_url or base_url, api_key, collection,
                                         timeout=vector_timeout, transport=transport)
        self.vectors.embedding_model = embedding_model
        self.context = GatewayContextClient(api_key, base_url=base_url,
                                            timeout=context_timeout, transport=transport)

    async def create_collection(self, model: str | None = None, *, description: str = "", chunk_size: int = 1000) -> dict[str, Any]:
        """Create or validate the configured collection; preserve the full model name."""
        return await self.vectors.create_collection(model, description=description, chunk_size=chunk_size)

    async def add_vectors(self, vectors: list[dict[str, Any]], *,
                          on_batch: Callable[[int, int], Awaitable[None]] | None = None) -> dict[str, Any]:
        """Write caller-prepared records in confirmed batches of at most 64."""
        return await self.vectors.add_vectors(vectors, on_batch=on_batch)

    async def delete_vectors(self, ids: list[str]) -> dict[str, Any]:
        """Delete external record IDs in batches of at most 256."""
        return await self.vectors.delete_vectors(ids)

    async def similarity_search(self, query: str, *, k: int = 5,
                                metadata: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Retrieve text, score and metadata using Gateway JSON containment filters."""
        return await self.vectors.similarity_search(query, k=k, metadata=metadata)

    async def add_records(self, records: list[dict[str, Any]], *, on_batch=None) -> dict[str, Any]:
        """写普通或向量记录；父子关联与发布由业务项目编排。"""
        return await self.vectors.add_records(records, on_batch=on_batch)

    async def fetch_records(self, ids: list[str]) -> dict[str, Any]:
        """批量读取外部 ID，不调用 Embedding。"""
        return await self.vectors.fetch_records(ids)

    async def publish_version(self, namespace: str, document_id: str, version_id: str) -> dict[str, Any]:
        """发布完整的暂存版本，隐藏同一业务范围的旧版本。"""
        return await self.vectors.publish_version(namespace, document_id, version_id)

    async def similarity_search_with_context(self, query: str, *, k: int = 5,
                                             metadata: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """检索子片后从同一 collection 批量读取父块；权限过滤由调用方提供。

        fetch 失败时向调用方抛异常；缺失父块则保留子片并标记 context_status=missing。
        返回每个命中，不自动去重父块；调用方可按 parent_id 合并。
        """
        hits = await self.similarity_search(query, k=k, metadata=metadata)
        parent_ids = list(dict.fromkeys(str(hit["parent_id"]) for hit in hits if hit.get("parent_id")))
        parents = await self.fetch_records(parent_ids) if parent_ids else {"records": []}
        by_id = {record["id"]: record for record in parents["records"] if record.get("record_kind") == "document"}
        for hit in hits:
            parent_id = hit.get("parent_id")
            parent = by_id.get(parent_id)
            hit["context_content"] = parent["content"] if parent else hit.get("content", "")
            hit["context_status"] = "available" if not parent_id or parent else "missing"
        return hits
