"""Safe Gateway errors without source content or credentials."""
class GatewayVectorStoreError(RuntimeError):
    """网关调用失败；异常消息只包含稳定错误码和 HTTP 状态。"""

    def __init__(
        self,
        code: str,
        status_code: int | None = None,
        *,
        upstream_code: str | None = None,
        gateway_trace_id: str | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.upstream_code = upstream_code
        self.gateway_trace_id = gateway_trace_id
        self.confirmed_ids: list[str] = []
        self.uncertain_ids: list[str] = []
        super().__init__(code)


class GatewayContextError(RuntimeError):
    """Safe Gateway context error code and optional upstream status."""

    def __init__(self, code: str, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)

