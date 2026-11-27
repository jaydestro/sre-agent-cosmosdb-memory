# Security and AI guardrails

This educational application is not a hardened production controller. Its narrow
simulation is intended to make authority boundaries visible and inspectable.

## Model suggestions are not permissions

Historical documents, images, tool output, and native memories are untrusted
content. The investigator's instructions explicitly treat them as references,
ignore embedded permission/action requests, and require fresh configuration,
backend, metrics, and health checks. The model proposes; it never approves or
executes, grants a role, changes policy, or runs arbitrary commands.

The fixed diagnostic tool set is documented in [architecture](architecture.md).
No shell, resource deletion, role assignment, or general Azure administration
tool is registered. Prompt text is not a security boundary: deterministic checks
still govern every proposal and action.

An accepted pool recommendation must be grounded in all four required current
tool observations, with matching version and freshness. The application binds
that proof to the proposal digest and validates it again before execution.
Confidence text, a remembered success, or model-supplied flags cannot replace it.

The application accepts only the registered payments questions in
[the walkthrough](demo.md#supported-questions), normalizing case, whitespace, and
terminal question marks/periods. Other wording and mixed appended instructions
return HTTP 422 before incident/model/tool work. This is a small deterministic prompt
contract, not a general semantic classifier or complete prompt-injection defense.
Retrieved content still needs the independent authority checks above.

## Application roles and policy

| Role | Application access |
| --- | --- |
| Viewer | Scoped status, evidence, and memory reads |
| Operator | Investigation, exact proposal approval/rejection, memory review/invalidation, and enabled development scenario injection |
| Administrator | Operator rights plus versioned mode/policy changes |

The monitor has a server-owned scoped service identity, not a human identity.
Its operator role does not permit changing policy. In guarded mode only an exact
human approval authorizes a proposal. Autonomous mode requires current,
administrator-configured, narrow policy, plus fresh proof, state preconditions,
ETags, expiry, and bounded work. Paused mode blocks new remediation. Invalid
deployment evidence escalates rather than increasing the pool.

Approvals are tied to a particular proposal digest and state/policy versions.
They are neither chat messages nor durable permission to act on future incidents.
Policy updates and scenario injection can invalidate earlier approvals.

## Local and hosted identity

Local mode trusts `X-Demo-Operator` only when configured for development and
loopback host/client addresses. The default synthetic `engineer-a` identity is an
administrator; `engineer-b` is an operator. These labels are fixture identities,
not Azure user accounts. Do not expose this mode on a public interface.

Hosted mode requires a protected Entra-authenticated ingress that strips
caller-supplied identity headers and prevents bypassing authentication.
`TRUSTED_AUTH_PROXY=true` alone does not secure a public application port.
The application checks tenant/audience and server-owned memberships; an encoded
`X-MS-Client-Principal` header is not itself a signed credential.

Every mutation also requires matching `Origin` and
`X-Requested-With: IncidentMemory`. Every scoped read/write requires membership;
client-provided role claims and arbitrary native-memory IDs cannot grant access.

## Least-privilege Azure data access

Provisioning identities need management-plane permissions; the running
application needs only the relevant data-plane permissions.

| Resource | Required role and scope |
| --- | --- |
| Operations and native base/turns/summaries/counter Cosmos containers | Cosmos DB Built-in Data Contributor scoped to required containers |
| Evidence Cosmos container | Runtime Data Reader; seeding/ingestion Data Contributor |
| Inference account | Embedding User at that account |
| Private evidence Blob container | Retrieval Data Reader; seeding Data Contributor |
| Storage account delegation key | Storage Blob Delegator, in addition to the required container access |
| Foundry project/model endpoints | Inference/extraction/embedding access for the configured identity and deployment |

Cosmos built-in data role IDs are
`00000000-0000-0000-0000-000000000001` (Reader) and
`00000000-0000-0000-0000-000000000002` (Contributor). These are Cosmos SQL
data roles, not Azure control-plane Contributor. Resolve **Embedding User** from
the subscription's role definitions; never invent its GUID.

Entra is the default. Local Azure CLI and Azure managed identity credentials are
selected through `DefaultAzureCredential`. Use a separate ingestor identity where
practical so runtime retrieval does not inherit upload/write privileges.

The supplied development [role template](../infra/runtime-roles.bicep) uses one
identity for seeding and runtime, granting Cosmos Data Contributor at the dedicated
database scope and Blob Data Contributor at the evidence container. This is
broader than the split-identity container-scoped recommendation above.

`COSMOS_AUTH_MODE=key` requires an explicitly supplied protected `COSMOS_KEY`.
It is not a fallback for failed Entra authorization. Account keys provide broader
data access than scoped container RBAC; use only an isolated environment if
testing this option. The application does not fetch or enable keys. Foundry and
Blob still use Entra.

Token audiences differ: Cosmos uses `https://cosmos.azure.com/.default`,
embedding inference uses `https://dbinference.azure.com/.default`, and Blob uses
`https://storage.azure.com/.default`. Foundry follows its SDK's configured
audience. A token for one service cannot be reused as authorization for another.

## Private images and secrets

Keep Blob anonymous/public access disabled. Microsoft Embedding 1 receives a
plain HTTPS Blob URL plus a separate short-lived, read-only, HTTPS-only
user-delegation SAS signed by the same caller identity. An account-key SAS is
not interchangeable. Firewall and service reachability checks remain necessary.

The application validates storage/result hosts, MIME type, size, vector shape,
finite values, model, and dimensions. A linked embedding result is not permission
to download an arbitrary URL.

Never commit `.env`, credentials, SAS, bearer tokens, customer incident content,
or private evaluation outputs. Do not expose them in logs, screenshots, browser
responses, or model-visible text. Use synthetic evidence and define retention,
backup, and deletion policies separately before introducing sensitive data.

## Safe operations and cleanup

Startup does not create resources or seed records. Development seed/reset/evaluate
commands require explicit Azure use and the designated fixture scope. Reset first
creates a paused control version, then injects fixture state; it retains durable
history and does not delete containers or all memory.

Pause and inspect pending work before stopping the server. Any later cleanup must
use explicit fixture-owned item/blob IDs, not broad account, database, container,
partition, or memory-store deletion. Operational audit reports are append-only
application records, not tamperproof against privileged database writers.

Ordinary CI has no cloud credentials. Optional live integration uses a protected,
manually dispatched workload-identity environment. Optional key coverage must
have its own supplied environment secret; absence skips that branch rather than
enabling or retrieving keys.

References: [Cosmos data RBAC](https://learn.microsoft.com/azure/cosmos-db/how-to-setup-rbac),
[user-delegation SAS](https://learn.microsoft.com/azure/storage/blobs/storage-blob-user-delegation-sas-create-python),
and [embedding access](https://learn.microsoft.com/azure/cosmos-db/gen-ai/generate-embedding).
