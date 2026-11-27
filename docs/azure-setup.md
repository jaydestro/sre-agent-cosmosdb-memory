# Azure setup

Use your own dedicated development resource group and identities. The application
connects to prepared data resources; it does not create infrastructure at startup.
The payments workload is simulated, but persistence, Blob access, embedding
inference, and native-memory processing make real service calls.

## Resource checklist

| Resource | Required configuration |
| --- | --- |
| Azure Cosmos DB for NoSQL | Dedicated-container autoscale; no shared database throughput |
| Operations container | `/scope_id` partition key; operational indexes |
| Evidence container | `/scope_id`; `/embedding` vector policy: 1,536, float32, cosine; compatible vector index |
| Native memory containers | Base, `<base>-turns`, `<base>-summaries`, and `counter`; hierarchical `[/user_id, /thread_id]`; pinned Toolkit schema/indexes |
| Microsoft InferenceService account | Companion `Microsoft.InferenceService/inferenceAccounts` resource in the Cosmos resource group; API `2026-08-01-preview` |
| Blob Storage | Private evidence container, public Blob access disabled, caller access and user-delegation-key permission |
| Foundry | Project for reasoning; extraction chat deployment; separate native text-embedding deployment |

Provision an initial autoscale maximum of 1,000 RU/s for **each** required
container, including native turns/summaries and the counter. This is not a serverless setup.
See [costs](costs.md) for the idle floor and additional services.

East US supports Microsoft Embedding 1; independently check Foundry model
availability and quota there. Other supported inference regions are listed in
[compatibility](compatibility.md). Do not assume all services or models share
the same availability.

An infrastructure administrator needs resource-provider registration, creation,
and role-assignment permissions. The runtime identity does not need those rights.
Use generic names and a developer-supplied `Owner` tag value in your own setup;
never put personal email addresses or real deployment identifiers into source.

## Infrastructure templates

The repository includes four resource-group-scoped Bicep templates:

| Template | Creates |
| --- | --- |
| [main.bicep](../infra/main.bicep) | Cosmos, Blob storage/private container, companion inference account, Foundry account/project |
| [cosmos-containers.bicep](../infra/cosmos-containers.bicep) | Database and six dedicated-autoscale containers |
| [foundry-models.bicep](../infra/foundry-models.bicep) | Regional reasoning/extraction and native text-embedding deployments |
| [runtime-roles.bicep](../infra/runtime-roles.bicep) | Explicit data-role assignments to an existing user or service principal |

Deploy in that order to a new development resource group. Select supported
model names/versions and regional quota before deployment; the model template
uses regional `Standard` deployments and serial updates. Install the Azure CLI
Bicep support if needed with `az bicep install`. The
[infrastructure guide](../infra/README.md) lists all parameters and the
validation-before-deployment workflow.

Sign in, select your subscription, and register the companion inference provider
before deploying its resource:

```text
az login
az provider register --subscription "<subscription-id>" --namespace Microsoft.InferenceService --wait
```

For Windows PowerShell:

```powershell
az group create --subscription "<subscription-id>" --name "<development-resource-group>" --location eastus --tags "Owner=<developer-owner-value>"
```

For macOS/Linux Bash:

```bash
az group create --subscription "<subscription-id>" --name "<development-resource-group>" --location eastus --tags "Owner=<developer-owner-value>"
```

Save the following as a private parameter file **outside the checkout**, replacing
every placeholder with your own value. Use valid globally unique account names.
The initial deployment intentionally disables Cosmos public networking:

```json
{
  "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
  "contentVersion": "1.0.0.0",
  "parameters": {
    "location": { "value": "eastus" },
    "ownerTagValue": { "value": "<developer-owner-value>" },
    "cosmosAccountName": { "value": "<unique-cosmos-account-name>" },
    "storageAccountName": { "value": "<unique-lowercase-storage-name>" },
    "inferenceAccountName": { "value": "<inference-account-name>" },
    "foundryAccountName": { "value": "<unique-foundry-account-name>" },
    "foundryProjectName": { "value": "<foundry-project-name>" },
    "verifiedRuntimeSourceIps": { "value": [] },
    "createInferenceAccount": { "value": true }
  }
}
```

Create separate external parameter files in the same format for containers,
models, and roles, using the
[template parameter table](../infra/README.md#administrator-deployment).
In each command below, replace the external-file placeholder with your private
file's actual path. Validate the same template/parameters with
`az deployment group validate` before each `create`.

Deploy resources and containers from the checkout root:

PowerShell:

```powershell
az deployment group create --subscription "<subscription-id>" --resource-group "<development-resource-group>" --template-file .\infra\main.bicep --parameters "@<external-base-parameters-file>"
az deployment group create --subscription "<subscription-id>" --resource-group "<development-resource-group>" --template-file .\infra\cosmos-containers.bicep --parameters "@<external-container-parameters-file>"
```

Bash:

```bash
az deployment group create --subscription "<subscription-id>" --resource-group "<development-resource-group>" --template-file infra/main.bicep --parameters "@<external-base-parameters-file>"
az deployment group create --subscription "<subscription-id>" --resource-group "<development-resource-group>" --template-file infra/cosmos-containers.bicep --parameters "@<external-container-parameters-file>"
```

The Cosmos firewall allows only `verifiedRuntimeSourceIps`; an empty array
disables its network access. After assigning roles below, make an Entra SDK
request from the intended runtime and use the new account's firewall-denied
source IPv4 to populate that array. Redeploy the base template with
**`createInferenceAccount=false`**, and keep that flag false for every subsequent
base deployment. The preview inference provider can reject repeated creation
PUTs; do not delete/recreate it to update the Cosmos firewall. Allow firewall
propagation time and retry the scoped request, never substitute an allow-all
rule or Azure-datacenter bypass. The Blob
container is private, but its HTTPS service endpoint is network-reachable so the
embedding service can fetch caller-signed images. Private Blob access does not
mean a private endpoint is configured.

The container template defaults to `incident-memory`, `operations`, `evidence`,
`agent-memory`, and `counter`. Keep the native counter name exactly **`counter`**
to match the pinned toolkit. It uses quantizedFlat for evidence/native vectors,
native full-text indexes, and a 30-day native-turn TTL. No shared database
throughput or change-feed lease container is created.

Deploy the selected models before role assignment. The model parameter file
requires `ownerTagValue`, `foundryAccountName`, `reasoningDeploymentName`,
`reasoningModelName`, `reasoningModelVersion`, `embeddingDeploymentName`,
`embeddingModelName`, and `embeddingModelVersion`. Default reasoning/embedding
capacities are 20/10; units are model-specific and require quota checks.

PowerShell:

```powershell
az deployment group create --subscription "<subscription-id>" --resource-group "<development-resource-group>" --template-file .\infra\foundry-models.bicep --parameters "@<external-model-parameters-file>"
```

Bash:

```bash
az deployment group create --subscription "<subscription-id>" --resource-group "<development-resource-group>" --template-file infra/foundry-models.bicep --parameters "@<external-model-parameters-file>"
```

For role assignments, prepare an external parameter file in the same format
with these required values:

- `cosmosAccountName`, `storageAccountName`, `inferenceAccountName`,
  `foundryAccountName`, `foundryProjectName`.
- `runtimePrincipalId` (Entra object ID) and `runtimePrincipalType`
  (`User` or `ServicePrincipal`).
- `embeddingUserRoleId`, `blobDataContributorRoleId`, `blobDelegatorRoleId`,
  `  openAiUserRoleId`, `foundryUserRoleId`: resolve the appropriate built-in Azure
  role GUIDs for your subscription and Foundry deployment access. These are
  role-definition IDs, not role-assignment or principal IDs.

For example, resolve the inference role without inventing a GUID:

```text
az role definition list --subscription "<subscription-id>" --name "Embedding User" --query "[0].name" --output tsv
```

The other role names are **Storage Blob Data Contributor**, **Storage Blob
Delegator**, **Cognitive Services OpenAI User**, and **Foundry User**, respectively.
Deploy the populated role file:

PowerShell:

```powershell
az deployment group create --subscription "<subscription-id>" --resource-group "<development-resource-group>" --template-file .\infra\runtime-roles.bicep --parameters "@<external-role-parameters-file>"
```

Bash:

```bash
az deployment group create --subscription "<subscription-id>" --resource-group "<development-resource-group>" --template-file infra/runtime-roles.bicep --parameters "@<external-role-parameters-file>"
```

The role template intentionally gives the single development identity Cosmos
Data Contributor across the designated database and Blob Data Contributor for
seeding. It does not grant Azure management Contributor/Owner. Separate ingestor
and runtime identities with narrower container access for hosted use; see
[security](security.md#least-privilege-azure-data-access).

Templates do not revoke inherited privileges of a local provisioning user.
Use distinct provisioner and workload identities when isolation is required.

`Owner` tags are explicit on supported resource groups, accounts, Foundry project,
and model deployments; they are not inherited. Cosmos SQL database/container
schemas expose generic tag fields, but the service does not retain independent
child tags and the ARM tag extension targets the parent account. Blob service/
container, role assignments, and ARM deployment records are not independently
taggable here. See the [tag-support inventory](https://learn.microsoft.com/azure/azure-resource-manager/management/tag-support).

Read your resource endpoints from the Azure portal or resource properties and
fill the settings below. The templates do not emit a ready-to-use `.env`, secrets,
or a model-deployment readiness result.

## Prepare embedding inference

Follow the official
[embedding generation setup](https://learn.microsoft.com/azure/cosmos-db/gen-ai/generate-embedding)
to register `Microsoft.InferenceService`, create the companion account in the
Cosmos resource group, and assign **Embedding User** to the calling identity.
Skip creation if you already deployed the companion account with the template.
Use its inference endpoint for `EMBEDDING_ENDPOINT`, not the Cosmos endpoint.
Resolve the built-in role ID from your subscription rather than copying an
unverified GUID.

The caller embedding images must also read private blobs and obtain a
user-delegation key. The application signs a read-only SAS for each request using
that same identity; no SAS belongs in `.env`, evidence records, or public output.
Check both identity permissions and embedding-service network reachability.

## Prepare Foundry and native memory

Select deployments with these capabilities:

1. A reasoning deployment supporting tool calling and image input.
2. A supported chat deployment for native-memory extraction.
3. A native text-embedding deployment producing the native container's configured
   1,536-dimensional vectors.

The model template creates two deployments: use its reasoning deployment for
both `REASONING_DEPLOYMENT` and `MEMORY_DEPLOYMENT`, and the separate text-embedding
deployment for `MEMORY_EMBEDDING_DEPLOYMENT`. A separately selected extraction
deployment is also possible; duplicating reasoning deployments is not required.
Check current catalog availability, retirement dates, and quota for each model
version before provisioning.

The project endpoint and native OpenAI resource endpoint are distinct.
The adapter normalizes the native endpoint for the pinned toolkit; see
[compatibility](compatibility.md#native-memory-and-foundry-endpoints).
Microsoft Embedding 1 evidence embeddings are not a replacement for the native
toolkit's text-embedding deployment.

Assign the Foundry identity's required inference access and the scoped Cosmos/
Blob roles in [security](security.md#least-privilege-azure-data-access). Native
memory access includes base, turns, summaries, and counter, not just the base store.

## Configure the application

Copy the template without copying any real credentials into source:

PowerShell:

```powershell
Copy-Item .env.example .env
```

macOS/Linux:

```bash
cp .env.example .env
```

Fill these settings in the private `.env` used by your command's working directory:

| Settings | Value to provide |
| --- | --- |
| `COSMOS_ENDPOINT`, `COSMOS_DATABASE` | Your NoSQL account URI and prepared database |
| `COSMOS_OPERATIONS_CONTAINER`, `COSMOS_EVIDENCE_CONTAINER`, `COSMOS_MEMORY_CONTAINER` | Prepared container names; memory setting is the base name |
| `COSMOS_AUTH_MODE` | `entra` by default |
| `EMBEDDING_ENDPOINT` | Companion inference account endpoint |
| `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` | `microsoft-embedding-1-large`, `1536` |
| `BLOB_ACCOUNT_URL`, `BLOB_CONTAINER` | Private evidence storage account HTTPS URL and container |
| `FOUNDRY_ENDPOINT` | Foundry project endpoint |
| `REASONING_DEPLOYMENT` | Tool-calling/image-capable deployment name |
| `MEMORY_DEPLOYMENT` | Extraction deployment name |
| `MEMORY_EMBEDDING_DEPLOYMENT` | Native text-embedding deployment name |
| `APP_ENV`, `APP_HOST`, `APP_PORT` | For local walkthrough: `development`, `127.0.0.1`, `8000` |
| `SCOPE_ID`, `SERVICE_ID` | For supplied fixture commands: `demo-payments-development`, `payments` |
| `ALLOW_AZURE_USE`, `ALLOW_DEMO_RESET` | Enable intentionally for live work and fixture injection |

The fixture scope is an application namespace, not an Azure subscription,
resource group, or identity. You choose your own Azure resource names.

Authenticate locally:

```text
az login
az account set --subscription "<subscription-id>"
```

Then seed and start:

```text
uv run python -m scripts.seed --confirm-azure-use
uv run python -m app.cli
```

The seed uploads supplied synthetic images, embeds text/images, and initializes
service/control records. It does not provision containers or model deployments.
Initialization is idempotent; it is not a destructive reset.

Open <http://127.0.0.1:8000>. Inspect setup/status before using
[the walkthrough](demo.md). A liveness response only means the HTTP process is
alive; it does not establish database, inference, private-image, or memory access.

## Hosted identity and optional CI

For Azure hosting, configure the managed identity's scoped data access and use a
protected authenticated ingress. Do not expose development identity headers
remotely. Hosted settings additionally require `OPERATOR_TENANT_ID`,
`OPERATOR_AUDIENCE`, `OPERATOR_MEMBERSHIPS_JSON`, and correctly secured
`TRUSTED_AUTH_PROXY`; see [identity boundaries](security.md#local-and-hosted-identity).

The optional [live integration workflow](../.github/workflows/live-integration.yml)
requires its named protected development environment, reviewers, repository-bound
Entra federation, configured endpoint/deployment variables, and fixture scope.
Review the workflow inputs and permissions before dispatch. It is an optional
test path, not an infrastructure deployment command or evidence that a complete
agent walkthrough succeeded.
