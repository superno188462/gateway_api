"""父子记录客户端契约测试；不调用真实网关。"""
import asyncio
import json

import httpx
import pytest
from gateway_client import GatewayClient, GatewayVectorStore, GatewayVectorStoreError


def test_ordinary_records_split_by_body_limit_without_embedding():
    seen = []
    def handler(request):
        assert request.url.path.endswith("/upsert")
        assert len(request.content) <= 4 * 1024 * 1024
        records = json.loads(request.content)["vectors"]
        seen.append(records)
        return httpx.Response(200, json={"upserted": [{"external_id": r["id"]} for r in records]})
    store = GatewayVectorStore("https://example.com/v1", "mock-key", "kb", transport=httpx.MockTransport(handler))
    records = [{"id": f"p{i}", "record_kind": "document", "content": "汉" * 20000} for i in range(65)]
    result = asyncio.run(store.add_records(records))
    assert len(result["upserted"]) == 65
    assert len(seen) >= 2
    assert all(len(batch) <= 64 for batch in seen)
    with pytest.raises(GatewayVectorStoreError):
        asyncio.run(store.add_vectors(records))


def test_vector_batch_limit_and_partial_failure_tracks_ids():
    calls = []
    def handler(request):
        records = json.loads(request.content)["vectors"]
        calls.append(records)
        if len(calls) == 2:
            return httpx.Response(500, json={"detail": {"code": "test"}})
        return httpx.Response(200, json={"upserted": [{"external_id": r["id"]} for r in records]})
    store = GatewayVectorStore("https://example.com/v1", "mock-key", "kb", transport=httpx.MockTransport(handler))
    with pytest.raises(GatewayVectorStoreError) as caught:
        asyncio.run(store.add_vectors([{"id": str(i), "content": "text"} for i in range(65)]))
    assert [len(batch) for batch in calls] == [64, 1]
    assert caught.value.confirmed_ids == [str(i) for i in range(64)]
    assert caught.value.uncertain_ids == ["64"]


def test_fetch_batches_deduplicates_and_preserves_missing():
    batches = []
    def handler(request):
        ids = json.loads(request.content)["ids"]
        batches.append(ids)
        return httpx.Response(200, json={"records": [{"id": i, "content": "parent"} for i in ids if i != "1"],
                                        "missing_ids": ["1"] if "1" in ids else []})
    store = GatewayVectorStore("https://example.com/v1", "mock-key", "kb", transport=httpx.MockTransport(handler))
    result = asyncio.run(store.fetch_records([str(i) for i in range(257)] + ["0"]))
    assert [len(batch) for batch in batches] == [256, 1]
    assert result["missing_ids"] == ["1"]
    assert [r["id"] for r in result["records"]] == [str(i) for i in range(257) if i != 1]


def test_fetch_rejects_unaccounted_response():
    store = GatewayVectorStore("https://example.com/v1", "mock-key", "kb", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"records": [], "missing_ids": []})))
    with pytest.raises(GatewayVectorStoreError):
        asyncio.run(store.fetch_records(["parent"]))


def test_facade_publish_and_context_fetch():
    seen = []
    def handler(request):
        seen.append(request.url.path.rsplit("/", 1)[-1])
        payload = json.loads(request.content)
        if seen[-1] == "publish":
            assert payload == {"namespace": "app:u", "document_id": "doc", "version_id": "v1"}
            return httpx.Response(200, json={"version_id": "v1", "published_records": 2, "already_published": True})
        if seen[-1] == "query":
            return httpx.Response(200, json={"results": [{"content": "child", "parent_id": "parent", "score": .9}]})
        return httpx.Response(200, json={"records": [{"id": "parent", "record_kind": "document", "content": "whole parent"}], "missing_ids": []})
    async def exercise():
        client = GatewayClient("https://example.com/v1", "mock-key", "kb", transport=httpx.MockTransport(handler))
        result = await client.publish_version("app:u", "doc", "v1")
        assert result["already_published"]
        hits = await client.similarity_search_with_context("question")
        assert hits[0]["context_content"] == "whole parent"
    asyncio.run(exercise())
    assert seen == ["publish", "query", "fetch"]
