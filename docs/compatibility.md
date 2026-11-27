# SDK, model, and store compatibility

Preview APIs can change. [pyproject.toml](../pyproject.toml) defines the package
contract; use documentation and source for those versions when changing adapters.
Source references describe supported interfaces, not a guarantee that a user's
region, identity, quota, model deployment, or network is ready.

## Version contract

| Component | Required interface |
| --- | --- |
| Python | 3.11 or later for the application |
| Azure Data AI | `azure-data-ai==1.1.0b1`, `azure.data.ai.InferenceClient` |
| Evidence model | `microsoft-embedding-1-large` preview |
| Agent Framework core / Foundry / OpenAI | `1.26.0` / `1.16.2` / `1.18.2` |
| Native Cosmos provider | `agent-framework-azure-cosmos-memory==1.0.0a261106` |
| Agent Memory Toolkit | `azure-cosmos-agent-memory==0.3.0b2` |
| Cosmos and Blob SDK ranges | Follow the manifest; container properties and credentials must match the adapters |

The native provider and Toolkit are a coupled preview schema. Toolkit 0.3 uses
`turn`, `thread_summary`, `fact`, `user_summary`, `procedural`, and `episodic`
wire types. It persists the `type` field; `memory_type` is the model/application
alias. Do not substitute a later tenant/user partitioning schema.

Preprovision the native base container, `<base>-turns`, `<base>-summaries`, and
the toolkit's `counter` container with hierarchical `[/user_id, /thread_id]` keys.
The memory stores need the pinned vector/full-text indexes; the counter uses
scalar indexing. Native writes touch the counter even when automatic processing
cadence is disabled.
The counter name must be exactly `counter` for the pinned toolkit; changing only
the infrastructure parameter does not change the SDK's counter lookup.
The native adapter validates existing native topology rather than creating or
migrating it. Operations/evidence schemas and indexes must be prepared and checked
during setup; do not assume runtime independently validates every container
property. Avoid SDK initialization paths that automatically create a memory store.

## Microsoft Embedding 1

Use the **inference endpoint** of the companion
`Microsoft.InferenceService/inferenceAccounts` resource in the Cosmos account's
resource group. Its management API is `2026-08-01-preview`. It is not the Cosmos
document endpoint and must not be assumed to be a Foundry model deployment.
The caller needs the inference account's **Embedding User** role.

Supported regions include East US, East US 2, North Central US, West Central US,
West US 2, West US 3, and Poland Central. Confirm service and deployment
availability before choosing a region.

The adapter calls `get_embeddings` with:

- `kind="query"` for retrieval queries and `kind="text"` for stored passages.
- `kind="image"` with a plain Blob `url` and separate caller-signed `sasToken`.
- Dense output, `dimensions=1536`, `encoding_format="float"`, and
  `output_data_kind="float32"`.

Dense model dimensions are 1,024, 1,536, 2,048, or 2,560; **the model default is
2,560**, so 1,536 must be explicit. Text/query and historical image vectors must
use matching model, kind semantics, dimensions, and similarity configuration.
The application does not implement multi-vector/MaxSim retrieval.

Read inline vectors from `result.data[i].embedding`. Linked `result.output`
delivery requires allowlisted-host download and JSON/vector validation. One
text/query input is permitted per request; image batching permits up to eight
images. Maximum text input is 8,192 tokens. Handle truncation and service warnings
instead of assuming the complete passage was embedded.

For private Blob images, use a short-lived, read-only, HTTPS-only user-delegation
SAS signed by the **same Entra identity calling inference**. Do not store the SAS
in evidence or expose it in the browser. Network reachability remains necessary
even when identity permissions are correct.

## Cosmos vector options

The evidence policy targets `/embedding`, with 1,536 dimensions, `float32`, and
`cosine`. A matching vector index is configured separately from the vector policy.

| Index | Dimension limit | Use |
| --- | --- | --- |
| `flat` | 505 | Exact search for lower-dimensional vectors; invalid for this sample |
| `quantizedFlat` | 4,096 | Quantized exhaustive search |
| `diskANN` | 4,096 | Approximate search for larger datasets |

For `quantizedFlat` and DiskANN, fewer than 1,000 indexed vectors causes a scan
fallback. The small synthetic fixture therefore does not demonstrate large-index
latency or RU performance. Vector search is enabled by default; compatible
container policies/indexes are still required. Shared database throughput is not
supported for this vector-search setup.

The sample provisions dedicated-container autoscale, not serverless. A general
service capability does not imply the application's setup implements it.
Hierarchical native-memory vector search also requires checking the account's
supported partition/index behavior against the pinned toolkit.

## Native memory and Foundry endpoints

`FOUNDRY_ENDPOINT` is the Agent Framework project endpoint, typically ending
`/api/projects/<project>`. The pinned toolkit passes `ai_foundry_endpoint` to
`AsyncAzureOpenAI(azure_endpoint=...)`; a project URL cannot be passed there
unchanged. The application adapter normalizes the separate resource/OpenAI
endpoint for native memory while retaining the project endpoint for the agent.

`REASONING_DEPLOYMENT` must support tool calling and image input.
`MEMORY_DEPLOYMENT` is the extraction chat deployment.
`MEMORY_EMBEDDING_DEPLOYMENT` identifies the toolkit's Foundry text-embedding
deployment, producing the native store's required 1,536-dimensional vectors.
Select compatible deployment settings; do not assume a 3,072-dimensional default
fits a 1,536-dimensional container.

The toolkit's native embedding backend is not interchangeable with Microsoft
Embedding 1 merely because both can return 1,536 numbers. No Microsoft Embedding 1
native-memory adapter is implemented here.

Native wire records have optional `embedding` and do not themselves persist all
model/dimension provenance. Application receipts bind that information.
The pinned toolkit can suppress an embedding-generation failure when adding a
record, and updating a record does not automatically re-embed it. The adapter
therefore checks persisted finite, nonzero vectors, exact content/type/scope, and
receipts; repair requires re-embedding and readback, not just a successful add call.

### Typed native records

Toolkit 0.3.0b2 procedures require `name`, `summary`, `retrieval_text`, and
`procedure_kind`, in addition to the shared record fields. Its generic
`AsyncCosmosMemoryClient.upsert_memory(memory_type="procedural", ...)` does not
supply that complete typed schema and is not the application integration path.

The [memory adapter](../app/memory.py) validates `EpisodicRecord` and
`ProceduralRecord` with `model_validate`, converts them with `to_cosmos_dict`,
and persists through the public `AsyncMemoryStore.upsert_memory(record)` interface
using existing container handles. It creates a deterministic verified episode
and an **active, read-only diagnostic procedure**, with source resolution,
episode linkage, user scope, diagnostic steps, and safety constraints.
Candidate, deprecated, rejected, or superseded procedures cannot become trusted
resolution references.

A shared native `AsyncEmbeddingsClient` uses the configured Foundry resource
endpoint, text-embedding deployment, and 1,536 dimensions. Typed writes, native
processing, and recall therefore remain in the same native OpenAI embedding
space, separate from Microsoft Embedding 1 evidence.

Vector repair performs a typed in-place upsert, preserving native ID and
provenance, then exact scoped content/type/vector readback. It does not replace a
procedure with an incomplete generic add. Invalidation tombstones block repair
and retry regeneration.

The native `content_hash` is a 32-hex-character hash of normalized text.
Application receipt hashes are full 64-hex-character SHA256 hashes of exact
persisted content; they serve different purposes and must not be interchanged.

## Official references

- [Microsoft Embedding 1 specifications](https://learn.microsoft.com/azure/cosmos-db/gen-ai/microsoft-embedding-1)
- [Inference setup, authentication, and private images](https://learn.microsoft.com/azure/cosmos-db/gen-ai/generate-embedding)
- [Azure Data AI 1.1.0b1 API](https://github.com/Azure/azure-sdk-for-python/blob/azure-data-ai_1.1.0b1/sdk/dataai/azure-data-ai/api.md)
- [Cosmos vector policies and limits](https://learn.microsoft.com/azure/cosmos-db/vector-search)
- [Agent Framework Cosmos context provider](https://learn.microsoft.com/agent-framework/integrations/by-component/context-providers/azure-cosmos)
- [Agent Memory Toolkit](https://learn.microsoft.com/azure/cosmos-db/gen-ai/agent-memory-toolkit)
- [Toolkit v0.3.0b2 client source](https://github.com/AzureCosmosDB/AgentMemoryToolkit/blob/v0.3.0b2/azure/cosmos/agent_memory/aio/cosmos_memory_client.py)
- [Toolkit v0.3.0b2 chat endpoint source](https://github.com/AzureCosmosDB/AgentMemoryToolkit/blob/v0.3.0b2/azure/cosmos/agent_memory/aio/chat.py)
- [Toolkit v0.3.0b2 typed memory store](https://github.com/AzureCosmosDB/AgentMemoryToolkit/blob/v0.3.0b2/azure/cosmos/agent_memory/aio/store/memory_store.py)
- [Cosmos data-plane RBAC](https://learn.microsoft.com/azure/cosmos-db/how-to-setup-rbac)
- [Foundry documentation](https://learn.microsoft.com/azure/ai-foundry/)

Upstream hybrid search, memory capabilities, or provisioning examples are not
additional application features. Use the [architecture](architecture.md) and
actual adapters to distinguish implemented behavior from service options.
