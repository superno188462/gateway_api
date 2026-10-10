# Gateway API Python 客户端

仓库：`git@github.com:superno188462/gateway_api.git`。
本项目是客户端 SDK，不是 Gateway 服务端，不需要启动 Web 服务。
包名为 `agent-gateway-client`，Python 导入名为 `gateway_client`。

完整中文说明见 [模块使用指南](docs/模块使用指南.md)：包含配置、向量检索、父子记录与发布、画像、长期记忆、会话消息和异常处理。

## 开发

```powershell
uv sync --group dev
```

业务项目通过 `uv add --editable ../../gateway_api` 引用本地源码（路径相对于业务后端）。
此 SDK 不读取环境变量；地址、Key、collection 和模型由业务后端注入。

## 使用

```python
from gateway_client import GatewayClient

client = GatewayClient(
    base_url="https://gateway.example.com/gateway/v1",
    api_key=server_side_key,
    collection="knowledge",
    embedding_model="volc/doubao-embedding-vision",
)
await client.create_collection(chunk_size=1000)
await client.add_vectors([
    {"id": "doc-1-part-0001", "content": "已切好的文本", "metadata": {"source": "手册/安装.md"}}
])
hits = await client.similarity_search("如何安装？", k=5)
profile = await client.context.get_profile(verified_user_id)
```

## 配置参数

| 参数 | 用途 | 默认值 |
|---|---|---|
| base_url | Gateway API 地址，包含实际代理前缀 | 必填 |
| api_key | 后端持有的项目 Key | 必填 |
| collection | 向量集合；只调用 context 可省略 | 空字符串 |
| embedding_model | 创建集合时使用的完整模型名 | 空字符串 |
| vector_base_url | 独立向量服务地址 | base_url |
| vector_timeout | 向量请求超时秒数 | 120 |
| context_timeout | 上下文请求超时秒数 | 15 |
| transport | 测试或自定义 httpx 传输 | None |

所有业务方法是异步方法，须 await。向量按 64 条写入、256 个 ID 删除。
业务项目负责切片与 collection 文本长度约束、稳定 ID、metadata、权限和任务队列。

## 构建与部署

```powershell
uv build
```

部署可在业务项目中 `uv add <wheel路径>`；或者在发布标签后使用固定 Git 标签。
不要让生产环境依赖开发机器的本地目录。固定标签/提交并提交业务项目 uv.lock。

## 测试

```powershell
uv run pytest -q
```

目前包含独立导入和统一客户端路径/认证的 Mock 测试，不代表全部 Gateway 接口已经覆盖。
未封装会话列表、会话消息追加等全部 Gateway 功能。画像和记忆更新使用版本参数。
向量错误通过 GatewayVectorStoreError 提供 code、status_code、gateway_trace_id、
confirmed_ids 和 uncertain_ids；业务项目决定部分失败后的重试与清理策略。
上下文错误通过 GatewayContextError 提供 code 和 status_code。

## 源码边界

`src/gateway_client/` 是唯一 SDK 实现。Knowledge 中旧导入路径仅作为兼容转发层。
SDK 不保存 API Key、不验证终端用户身份，也不直接读取业务数据库。

## 0.2 父子记录与发布

```python
scope = dict(namespace="knowledge-platform:user-1", document_id="doc-1", version_id="version-1", staged=True)
await client.add_records([
    {"id": "parent-1", "record_kind": "document", "content": "父块正文",
     "metadata": {"source": "手册.md"}, **scope}
])
await client.add_vectors([
    {"id": "child-1", "parent_id": "parent-1", "content": "子片正文",
     "metadata": {"source": "手册.md"}, **scope}
])
await client.publish_version("knowledge-platform:user-1", "doc-1", "version-1")
hits = await client.similarity_search_with_context("问题", metadata={"source": "手册.md"})
```

add_records 支持 document（最多 20,000 字符）及 vector（最多 6,000 字符，业务仍需满足集合上限）；按 64 条和 4 MiB 双重限制分批。
fetch_records 使用外部 ID，最多 256 个一批，返回 records/missing_ids。
父块与子片先后提交，父子不能同批引用。父正文保存在 content，metadata 保持轻量。
版本 ID 必须在业务侧持久化，同一版本重试保持不变；内容回退也需新的版本 ID。
发布超时可能已生效，应重试同一 publish，不能清理目标版本。
发布成功后先删除旧子片，再删除旧父块。SDK 不编排生命周期，也不提供数据库/队列。
upsert 响应 id 是内部 UUID；external_id 才是 fetch/delete/parent_id 使用的调用方 ID。
旧版 add_vectors 仍然可用。similarity_search_with_context 会保留每条子片命中，调用方自行按 parent_id 去重；metadata 过滤不能替代业务鉴权。
