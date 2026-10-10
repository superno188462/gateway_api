"""异步调用 Agent Gateway 向量数据库 API。

此适配器遵循 Gateway 向量 API。文档解析、切片与生命周期由本项目管理；Gateway
只负责每条预切片文本的 Embedding、存储、删除和相似度检索。
"""

from __future__ import annotations

import json
import logging
import asyncio
from typing import Any, Awaitable, Callable
from urllib.parse import quote
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)


from .errors import GatewayVectorStoreError
from .transport import GatewayTransport, normalize_base_url


class GatewayVectorStore:
    """项目隔离的 Gateway collection 客户端。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        collection: str | None = None,
        *,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = normalize_base_url(base_url or "")
        self.api_key = api_key or ""
        self.collection = (collection or "").strip()
        self.embedding_model = ""
        self.timeout = timeout
        self.transport = transport

    def ensure_configured(self, *, require_collection: bool = True) -> None:
        """Validate explicit configuration before building a request path."""
        if not self.base_url or not self.api_key or (require_collection and not self.collection):
            raise GatewayVectorStoreError("VECTOR_STORE_NOT_CONFIGURED")

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """Retry transient errors at most twice; stable IDs make upsert replay safe."""
        for attempt in range(3):
            try:
                return await self._request_once(method, path, body)
            except GatewayVectorStoreError as error:
                retryable = error.code in {"VECTOR_GATEWAY_TIMEOUT", "VECTOR_GATEWAY_NETWORK_ERROR"} or error.status_code in {502, 503, 504}
                if not retryable or attempt == 2:
                    raise
                await asyncio.sleep(0.5 * (2 ** attempt))
        raise AssertionError("unreachable")

    async def _request_once(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.ensure_configured(require_collection=path != "/collections" or method != "GET")
        request_trace_id = f"knowledge_{uuid4().hex}"
        try:
            response = await GatewayTransport(
                self.base_url, self.api_key, timeout=self.timeout, transport=self.transport,
            ).request(method, f"/vector-stores{path}", json=body, trace_id=request_trace_id)
        except httpx.TimeoutException as exc:
            logger.warning("vector_gateway operation=%s collection=%s error=timeout", method, self.collection)
            raise GatewayVectorStoreError("VECTOR_GATEWAY_TIMEOUT", gateway_trace_id=request_trace_id) from exc
        except httpx.HTTPError as exc:
            logger.warning("vector_gateway operation=%s collection=%s error=network", method, self.collection)
            raise GatewayVectorStoreError("VECTOR_GATEWAY_NETWORK_ERROR") from exc

        if response.status_code >= 400:
            gateway_trace_id = response.headers.get("X-Trace-ID") or request_trace_id
            upstream_code = None
            try:
                payload = response.json()
                detail = payload.get("detail") if isinstance(payload, dict) else None
                upstream_code = detail.get("code") if isinstance(detail, dict) else None
                error = payload.get("error") if isinstance(payload, dict) else None
                if upstream_code is None and isinstance(error, dict):
                    upstream_code = error.get("code")
                if upstream_code is None and isinstance(detail, list):
                    # FastAPI/Pydantic validation errors contain submitted values in
                    # some cases. Keep only a generic code; never log or return bodies.
                    upstream_code = "request_validation_error"
            except ValueError:
                pass
            known_codes = {
                "parent_record_not_found": "VECTOR_PARENT_NOT_FOUND",
                "parent_record_in_use": "VECTOR_PARENT_IN_USE",
                "parent_record_kind_invalid": "VECTOR_PARENT_KIND_INVALID",
                "parent_record_scope_conflict": "VECTOR_PARENT_SCOPE_CONFLICT",
                "published_version_immutable": "VECTOR_VERSION_IMMUTABLE",
                "published_version_retired": "VECTOR_VERSION_RETIRED",
                "publication_history_unknown": "VECTOR_PUBLICATION_HISTORY_UNKNOWN",
                "staged_version_not_found": "VECTOR_STAGED_VERSION_NOT_FOUND",
                "request_too_large": "VECTOR_REQUEST_TOO_LARGE",
                "embedding_dimensions_conflict": "VECTOR_COLLECTION_DIMENSIONS_MISMATCH",
                "embedding_dimensions_invalid": "VECTOR_EMBEDDING_DIMENSIONS_INVALID",
                "embedding_response_invalid": "VECTOR_EMBEDDING_RESPONSE_INVALID",
                "collection_not_found": "VECTOR_COLLECTION_NOT_FOUND",
                "document_not_found": "VECTOR_GATEWAY_DOCUMENT_STATE_CONFLICT",
                "service_not_enabled": "VECTOR_GATEWAY_SERVICE_NOT_ENABLED",
                "user_quota_exceeded": "VECTOR_USER_QUOTA_EXCEEDED",
                "user_quota_missing": "VECTOR_USER_QUOTA_MISSING",
                "project_quota_exceeded": "VECTOR_PROJECT_QUOTA_EXCEEDED",
                "service_quota_configuration_error": "VECTOR_QUOTA_CONFIGURATION_ERROR",
                "vector_too_large": "VECTOR_RECORD_TOO_LARGE",
                "empty_vector": "VECTOR_RECORD_EMPTY",
                "invalid_api_key": "VECTOR_GATEWAY_AUTH_FAILED",
                "upstream_embedding_failed": "VECTOR_EMBEDDING_UPSTREAM_FAILED",
                "invalid_embedding_input": "VECTOR_EMBEDDING_INPUT_INVALID",
                "unsupported_embedding_dimensions": "VECTOR_EMBEDDING_DIMENSIONS_UNSUPPORTED",
            }
            code = known_codes.get(upstream_code)
            if code is None:
                if response.status_code in (401, 403):
                    code = "VECTOR_GATEWAY_AUTH_FAILED"
                elif response.status_code == 429:
                    code = "VECTOR_EMBEDDING_QUOTA_EXCEEDED"
                else:
                    code = "VECTOR_GATEWAY_ERROR"
            logger.warning(
                "vector_gateway operation=%s collection=%s status=%s upstream_code=%s trace_id=%s",
                method,
                self.collection,
                response.status_code,
                upstream_code or "unknown",
                gateway_trace_id or "unknown",
            )
            raise GatewayVectorStoreError(
                code,
                response.status_code,
                upstream_code=upstream_code,
                gateway_trace_id=gateway_trace_id,
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_RESPONSE", response.status_code) from exc

    async def list_collections(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/collections")
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_RESPONSE")
        return result

    async def ensure_collection(self, *, model: str, description: str, chunk_size: int) -> dict[str, Any]:
        """创建集合或验证其 Embedding 模型与单条记录长度上限。"""

        self.ensure_configured()
        collections = await self.list_collections()
        current = next((item for item in collections if item.get("collection") == self.collection), None)
        if current is None:
            result = await self._request(
                "POST",
                "/collections",
                {
                    "name": self.collection,
                    "description": description,
                    "model": model,
                    "chunk_size": chunk_size,
                    "chunk_overlap": 0,
                },
            )
            if not isinstance(result, dict):
                raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_RESPONSE")
            current = result
        self._validate_collection(current, model=model, chunk_size=chunk_size)
        return current

    async def create_collection(self, model: str | None = None, *, description: str = "", chunk_size: int = 1000) -> dict[str, Any]:
        """Create or validate a collection whose model and record limit are fixed."""
        selected = model or self.embedding_model
        if not selected:
            raise GatewayVectorStoreError("VECTOR_EMBEDDING_MODEL_REQUIRED")
        return await self.ensure_collection(model=selected, description=description, chunk_size=chunk_size)

    def _validate_collection(self, item: dict[str, Any], *, model: str, chunk_size: int) -> None:
        if item.get("model") != model:
            raise GatewayVectorStoreError("VECTOR_COLLECTION_MODEL_MISMATCH")
        if item.get("chunk_size", chunk_size) != chunk_size:
            raise GatewayVectorStoreError("VECTOR_COLLECTION_CHUNK_SIZE_MISMATCH")

    async def add_vectors(self, vectors: list[dict[str, Any]], *, on_batch: Callable[[int, int], Awaitable[None]] | None = None) -> dict[str, Any]:
        """Send pre-split records in batches of 64 and confirm every external ID.

        On failure, confirmed_ids identifies successful earlier batches and
        uncertain_ids identifies the current batch whose result is unconfirmed.
        No document lifecycle or text splitting takes place here.
        """
        if any(item.get("record_kind", "vector") != "vector" for item in vectors):
            raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_REQUEST")
        return await self.add_records(vectors, on_batch=on_batch)

    async def add_records(self, vectors: list[dict[str, Any]], *,
                          on_batch: Callable[[int, int], Awaitable[None]] | None = None) -> dict[str, Any]:
        """写入预备好的普通/向量记录；调用方按父记录、子记录的顺序分别提交。"""
        ids = [item.get("id") for item in vectors]
        if any(item.get("record_kind", "vector") not in {"vector", "document"}
               or not isinstance(item.get("content"), str) or not item["content"].strip()
               or len(item["content"]) > (20000 if item.get("record_kind") == "document" else 6000)
               or not isinstance(item.get("metadata", {}), dict) for item in vectors):
            raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_REQUEST")
        if any(not isinstance(value, str) or not value or len(value) > 200 for value in ids):
            raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_REQUEST")
        if len(ids) != len(set(ids)):
            raise GatewayVectorStoreError("VECTOR_DUPLICATE_RECORD_ID")
        confirmed: list[dict[str, Any]] = []
        batches: list[list[dict[str, Any]]] = []
        batch: list[dict[str, Any]] = []
        for record in vectors:
            candidate = batch + [record]
            if len(candidate) > 64 or len(json.dumps({"vectors": candidate}, ensure_ascii=True).encode("utf-8")) > 4 * 1024 * 1024:
                if not batch:
                    raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_REQUEST")
                batches.append(batch)
                batch = [record]
                if len(json.dumps({"vectors": batch}, ensure_ascii=True).encode("utf-8")) > 4 * 1024 * 1024:
                    raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_REQUEST")
            else:
                batch = candidate
        if batch:
            batches.append(batch)
        for batch in batches:
            try:
                result = await self._add_vector_batch(batch)
            except GatewayVectorStoreError as error:
                error.confirmed_ids = [item["external_id"] for item in confirmed]
                error.uncertain_ids = [item["id"] for item in batch]
                raise
            confirmed.extend(result["upserted"])
            if on_batch:
                await on_batch(len(confirmed), len(vectors))
        return {"collection": self.collection, "upserted": confirmed}

    async def _add_vector_batch(self, vectors: list[dict[str, Any]]) -> dict[str, Any]:
        """写入调用方预先切分的向量记录，每条记录携带自己的 metadata。"""
        self.ensure_configured()
        expected_ids = [item["id"] for item in vectors]
        if any(not isinstance(item, str) or not item for item in expected_ids):
            raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_REQUEST")
        if len(expected_ids) != len(set(expected_ids)):
            raise GatewayVectorStoreError("VECTOR_DUPLICATE_RECORD_ID")
        result = await self._request(
            "POST",
            f"/collections/{quote(self.collection, safe='')}/upsert",
            {"vectors": vectors},
        )
        if not isinstance(result, dict):
            raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_RESPONSE")
        upserted = result.get("upserted")
        if not isinstance(upserted, list) or any(not isinstance(item, dict) for item in upserted):
            raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_RESPONSE")
        returned_ids = [item.get("external_id") for item in upserted]
        if any(not isinstance(item, str) for item in returned_ids):
            raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_RESPONSE")
        if len(returned_ids) != len(expected_ids) or set(returned_ids) != set(expected_ids):
            raise GatewayVectorStoreError("VECTOR_GATEWAY_INCOMPLETE_UPSERT")
        return result

    async def fetch_records(self, ids: list[str]) -> dict[str, Any]:
        """按外部 ID 读取已发布正文，保留 missing_ids，不调用 Embedding。"""
        self.ensure_configured()
        if any(not isinstance(value, str) or not value or len(value) > 200 for value in ids):
            raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_REQUEST")
        requested = list(dict.fromkeys(ids))
        records, missing = [], []
        for offset in range(0, len(requested), 256):
            batch = requested[offset:offset + 256]
            result = await self._request("POST", f"/collections/{quote(self.collection, safe='')}/fetch", {"ids": batch})
            if not isinstance(result, dict) or not isinstance(result.get("records"), list) or not isinstance(result.get("missing_ids"), list):
                raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_RESPONSE")
            returned = result["records"]
            if any(not isinstance(record, dict) or not isinstance(record.get("id"), str) for record in returned):
                raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_RESPONSE")
            record_ids = [record["id"] for record in returned]
            if any(not isinstance(value, str) for value in result["missing_ids"]):
                raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_RESPONSE")
            accounted = record_ids + result["missing_ids"]
            if len(accounted) != len(batch) or set(accounted) != set(batch):
                raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_RESPONSE")
            by_id = {record["id"]: record for record in returned}
            records.extend(by_id[value] for value in batch if value in by_id)
            missing.extend(value for value in batch if value not in by_id)
        return {"collection": self.collection, "records": records, "missing_ids": missing}

    async def publish_version(self, namespace: str, document_id: str, version_id: str) -> dict[str, Any]:
        """原子发布指定版本；超时可能已经生效，调用方须重试，不能删除目标记录。"""
        self.ensure_configured()
        if any(not isinstance(value, str) or not value or len(value) > 200 for value in (namespace, document_id, version_id)):
            raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_REQUEST")
        result = await self._request("POST", f"/collections/{quote(self.collection, safe='')}/publish",
                                     {"namespace": namespace, "document_id": document_id, "version_id": version_id})
        if not isinstance(result, dict) or result.get("version_id") != version_id or type(result.get("published_records")) is not int:
            raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_RESPONSE")
        return result

    async def delete(self, ids: list[str]) -> dict[str, Any]:
        """Compatibility alias for bounded bulk record deletion."""
        return await self.delete_vectors(ids)

    async def delete_vectors(self, ids: list[str]) -> dict[str, Any]:
        if any(not isinstance(value, str) or not value for value in ids):
            raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_REQUEST")
        unique_ids = list(dict.fromkeys(ids))
        deleted = 0
        deleted_ids, missing_ids = [], []
        for offset in range(0, len(unique_ids), 256):
            result = await self._delete_batch(unique_ids[offset:offset + 256])
            deleted += result["deleted"]
            deleted_ids.extend(result.get("deleted_ids", []))
            missing_ids.extend(result.get("missing_ids", []))
        return {"collection": self.collection, "deleted": deleted, "deleted_ids": deleted_ids, "missing_ids": missing_ids}

    async def _delete_batch(self, ids: list[str]) -> dict[str, Any]:
        self.ensure_configured()
        result = await self._request(
            "POST",
            f"/collections/{quote(self.collection, safe='')}/delete",
            {"ids": ids},
        )
        if not isinstance(result, dict) or type(result.get("deleted")) is not int or result["deleted"] < 0:
            raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_RESPONSE")
        return result

    async def similarity_search(
        self, query: str, *, k: int = 5, metadata: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        # Resolve lazy configuration before interpolating collection into the URL.
        self.ensure_configured()
        if not query.strip() or len(query) > 10000 or not 1 <= k <= 20:
            raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_REQUEST")
        result = await self._request(
            "POST",
            f"/collections/{quote(self.collection, safe='')}/query",
            {"query": query, "top_k": k, "metadata": metadata},
        )
        if not isinstance(result, dict) or not isinstance(result.get("results"), list):
            raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_RESPONSE")
        if any(not isinstance(item, dict) for item in result["results"]):
            raise GatewayVectorStoreError("VECTOR_GATEWAY_INVALID_RESPONSE")
        return result["results"]

