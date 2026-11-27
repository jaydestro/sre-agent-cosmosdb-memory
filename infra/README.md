# Isolated IncidentMemory provisioning

These resource-group-scoped Bicep templates create infrastructure only. The application
connects to existing resources; it must not call `create_memory_store`.
Keep deployment parameter files, receipts, identifiers and local `.env` files outside
the repository. No template contains an actual subscription, principal or Owner value.

## Administrator deployment

Use an authenticated Azure CLI session. Never change shared CLI defaults.
Confirm the subscription, region, permissions, model catalog and available quota first.
ME1 requires the registered `Microsoft.InferenceService` provider and a supported region.

Create a dedicated group, explicitly tagging it:

```powershell
az group create --subscription $SubscriptionId --name $ResourceGroupName `
  --location $Location --tags "Owner=$OwnerTagValue"
```

For each template, use an external ARM parameter file of the form
`{"parameters":{"parameterName":{"value":"..."}}}`. Validate and deploy in this order:

```powershell
$steps = @(
  @{ Template = 'infra/main.bicep'; Parameters = $BaseParametersPath }
  @{ Template = 'infra/cosmos-containers.bicep'; Parameters = $ContainerParametersPath }
  @{ Template = 'infra/foundry-models.bicep'; Parameters = $ModelParametersPath }
  @{ Template = 'infra/runtime-roles.bicep'; Parameters = $RoleParametersPath }
)
foreach ($step in $steps) {
  az deployment group validate --subscription $SubscriptionId `
    --resource-group $ResourceGroupName --template-file $step.Template `
    --parameters "@$($step.Parameters)"
  if ($LASTEXITCODE -ne 0) { throw 'Validation failed; deployment stopped.' }
  az deployment group create --subscription $SubscriptionId `
    --resource-group $ResourceGroupName --template-file $step.Template `
    --parameters "@$($step.Parameters)"
  if ($LASTEXITCODE -ne 0) { throw 'Deployment failed; inspect scoped deployment operations.' }
}
```

Required parameter names:

| Template | Required parameters |
|---|---|
| `main.bicep` | `location`, `ownerTagValue`, `cosmosAccountName`, `storageAccountName`, `inferenceAccountName`, `foundryAccountName`, `foundryProjectName` |
| `cosmos-containers.bicep` | `location`, `ownerTagValue`, `cosmosAccountName` |
| `foundry-models.bicep` | `ownerTagValue`, `foundryAccountName`, `reasoningDeploymentName`, `reasoningModelName`, `reasoningModelVersion`, `embeddingDeploymentName`, `embeddingModelName`, `embeddingModelVersion` |
| `runtime-roles.bicep` | `cosmosAccountName`, `storageAccountName`, `inferenceAccountName`, `foundryAccountName`, `foundryProjectName`, `runtimePrincipalId`, `runtimePrincipalType`, `embeddingUserRoleId`, `blobDataContributorRoleId`, `blobDelegatorRoleId`, `openAiUserRoleId`, `foundryUserRoleId` |

Optional names/defaults:

- Base: `blobContainerName=incident-evidence`, `verifiedRuntimeSourceIps=[]`,
  `createInferenceAccount=true`.
- Containers: `databaseName=incident-memory`, `operationsContainerName=operations`,
  `evidenceContainerName=evidence`, `memoryContainerName=agent-memory`,
  `counterContainerName=counter`, `autoscaleMaxRu=1000`.
- Models: `deploymentSku=Standard`, `reasoningCapacity=20`, `embeddingCapacity=10`.
  Capacity units are model-specific; check the actual regional catalog and quota.
- Roles: `databaseName=incident-memory`, `blobContainerName=incident-evidence`.
  Principal type is `User` or `ServicePrincipal`.

Resolve role GUIDs with `az role definition list --subscription $SubscriptionId
--name $RoleName --query "[0].name" --output tsv`: **Embedding User**,
**Storage Blob Data Contributor**, **Storage Blob Delegator**,
**Cognitive Services OpenAI User**, and **Foundry User** respectively.
Cosmos uses its documented native built-in Data Contributor role, not management RBAC.

The first base deployment deliberately disables Cosmos public networking. After roles
propagate, make an Entra SDK request from the intended runtime and record the
new account's firewall-denied source IP. Redeploy base with only these verified IPv4
addresses in `verifiedRuntimeSourceIps`, and **`createInferenceAccount=false`**.
The preview inference provider can reject repeated create PUTs with `Conflict`.
Keep that flag false on every subsequent base deployment. Do not delete/recreate it.
Firewall updates can take 15 minutes to propagate; retry connectivity, never use
an allow-all rule, portal bypass or the `0.0.0.0` Azure-datacenter shortcut.

## Schema and roles

All six containers have **dedicated autoscale**, initially **100–1,000 RU/s each**;
the database has no shared throughput.

| Container | Partition | Index / TTL |
|---|---|---|
| `operations` | `/scope_id` | Scalar consistent index |
| `evidence` | `/scope_id` | ME1 dense 1,536 float32 cosine, `quantizedFlat` |
| `agent-memory` | `[/user_id,/thread_id]` MultiHash | Separate OpenAI embedding space; vector + `/content` full text; TTL -1 |
| `agent-memory-turns` | Same hierarchical key | Same native policy; TTL 2,592,000 seconds |
| `agent-memory-summaries` | Same hierarchical key | Native policy plus user/thread/version composite index; TTL -1 |
| `counter` | Same hierarchical key | Scalar index; required by SDK `add_memory` |

Native policy and TTL are pinned to the public **AgentMemoryToolkit v0.3.0b2**
source. No lease/change-feed container or host is needed for the local in-process demo.
Current full-text setup uses the container policy/index; do not invent an account
feature capability. The account enables documented `EnableNoSQLVectorSearch`.
`flat` cannot index 1,536 dimensions. Below 1,000 vectors, quantized indexing
can fall back to a scan.

Runtime grants:

- Cosmos Data Contributor: **new database scope only**.
- Embedding User: **companion inference account only**.
- Blob Data Contributor: **private image container only**.
- Blob Delegator: **storage account** (user-delegation-key action needs this scope).
- OpenAI User: **Foundry account**, for native Toolkit OpenAI calls.
- Foundry User: **Foundry project**, for project inference.

No runtime Contributor/Owner management role is created. An existing local CLI user
can still have inherited provisioning privileges; these templates do not revoke them
or claim to turn that user into a separate restricted identity. Production should
use separate provisioner and workload identities.

## Endpoint handoff and verification

There are **no template output fields**. Read the actual management-plane properties
after provisioning, without listing account keys:

- Cosmos: `az cosmosdb show ... --query documentEndpoint`.
- Storage: `az storage account show ... --query primaryEndpoints.blob`.
- ME1: `az resource show --api-version 2026-08-01-preview ... --query properties.endpoint`.
- Foundry project: `az resource show --ids $ProjectResourceId --api-version 2025-06-01 ...`;
  use `properties.endpoints["AI Foundry API"]`.

Every command must include `--subscription $SubscriptionId` and the explicit new group
or resource ID. Set `COSMOS_AUTH_MODE=entra`, `ALLOW_AZURE_USE=true`;
map the reasoning deployment to **both** `REASONING_DEPLOYMENT` and `MEMORY_DEPLOYMENT`.
Use the separate text deployment for `MEMORY_EMBEDDING_DEPLOYMENT`.
ME1 goes in `EMBEDDING_ENDPOINT`, never in a Foundry deployment field.
The private Blob account URL/container go in `BLOB_ACCOUNT_URL` / `BLOB_CONTAINER`.

Verify real Entra Cosmos CRUD and native connect-only topology, model inference,
native text embedding, Blob upload/read, and ME1 text/query/private-image embeddings.
ME1 uses `azure-data-ai` 1.1.0b1 with `microsoft-embedding-1-large`, explicit dense
1,536 float32 output. Image inputs use the caller's short-lived, read-only,
HTTPS-only user-delegation SAS. Never log SAS, tokens or credentials.
Anonymous Blob access must fail (including documented `PublicAccessNotPermitted`).

Tag inventory: group, accounts, Foundry project and model deployments are explicitly
tagged and require readback verification. Cosmos SQL database/container schemas expose
generic tag fields, but current service readback does not retain independent child
tags: the documented ARM tag extension resolves these virtual children to their
parent account. Inventory them as independent-tag exceptions, not as tagged children.
Blob service/container, native Cosmos role assignments and Azure RBAC role assignments
have no tags field in their schemas. ARM deployment records are also untagged.
**Tags are not inherited.** See the [official tag-support inventory](https://learn.microsoft.com/azure/azure-resource-manager/management/tag-support).

## Well-Architected trade-offs

| Pillar | Demo decision and trade-off |
|---|---|
| Security | Entra-only Cosmos/Foundry, disabled Blob shared keys, verified Cosmos IPs and private Blob. Public HTTPS Blob networking permits documented ME1 SAS fetch; this is not private-endpoint isolation. |
| Reliability | Single region and LRS match an isolated demo, not a production DR/SLA commitment. |
| Performance efficiency | Session consistency, hierarchical native partitioning, dedicated autoscale and quantized vectors; tiny datasets scan. |
| Cost optimization | Six containers have a nominal combined 600 RU/s idle floor. No VNet, compute host, microservices or duplicated reasoning deployment. |
| Operational excellence | Explicit subscription, validation before deployment, serial model updates, separate provisioning and connect-only runtime, private evidence receipts. |

## Official references

- [ME1 setup, SDK and image SAS](https://learn.microsoft.com/azure/cosmos-db/gen-ai/generate-embedding)
- [ME1 model specifications](https://learn.microsoft.com/azure/cosmos-db/gen-ai/microsoft-embedding-1)
- [Vector search limits](https://learn.microsoft.com/azure/cosmos-db/vector-search)
- [Full-text setup](https://learn.microsoft.com/azure/cosmos-db/gen-ai/full-text-search-quickstart)
- [Cosmos native RBAC](https://learn.microsoft.com/azure/cosmos-db/how-to-connect-role-based-access-control)
- [Cosmos firewall](https://learn.microsoft.com/azure/cosmos-db/how-to-configure-firewall)
- [Foundry region/model availability](https://learn.microsoft.com/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure-region-availability)
- [User-delegation SAS](https://learn.microsoft.com/azure/storage/blobs/storage-blob-user-delegation-sas-create-python)
- [Pinned native public source](https://github.com/AzureCosmosDB/AgentMemoryToolkit/tree/v0.3.0b2)
