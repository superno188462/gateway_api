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
