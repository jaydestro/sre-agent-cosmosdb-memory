# IncidentMemory

IncidentMemory is an educational incident-investigation application built with
Azure Cosmos DB, Microsoft Embedding 1, and Microsoft Agent Framework native
memory. It demonstrates how to retrieve historical text and images, investigate
fresh telemetry, approve a narrowly scoped correction, and recall verified
outcomes during a later incident.

**This is not a production controller.** The payments service and its failures
are simulated state in Cosmos DB. Database operations, private Blob evidence,
embedding inference, and agent memory use real Azure services when configured.
There is no emulator-backed or offline agent mode; offline tests use doubles.

## What you will learn

- Separate operational state, multimodal evidence, and native agent memory.
- Combine document versioning, ETags, transactional outbox, and idempotent work.
- Retrieve historical images with text queries using one evidence vector space.
- Keep model suggestions separate from human approval and deterministic policy.
- Compare a first investigation with fresh-session recall of a verified outcome.
- Observe escalation, pause, usage, and recovery without granting a model Azure
  management privileges.

The patterns are useful beyond this workload, but this repository implements
Azure integrations only. It does not claim an implemented port to another cloud.

## Prerequisites

- Python 3.11 or later and [uv](https://docs.astral.sh/uv/).
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) for local
  Microsoft Entra sign-in.
- An Azure subscription with permission to provision a dedicated development
  resource group and assign the required data roles, or an administrator who
  prepares those resources for you.
- Azure Cosmos DB for NoSQL, a companion Microsoft InferenceService embedding
  account, private Blob Storage, and Foundry model deployments. See
  [Azure setup](docs/azure-setup.md) for the resource and configuration contract.
- A reasoning deployment supporting tool calling and image input, a native-memory
  extraction deployment, and a separate native-memory text-embedding deployment.
  Confirm availability, quota, lifecycle, and network access for your region.

Using real services incurs charges, including idle provisioned Cosmos throughput.
See [cost controls](docs/costs.md) before provisioning or running the simulation.

## Install and start

From the repository root, on Windows PowerShell, macOS, or Linux:

```text
uv sync --extra dev
uv run python -m app.cli --help
uv run python -m app.cli
```

For a standard installed package, use an activated virtual environment:

```text
python -m pip install .
incident-memory --help
incident-memory
```

Use `python -m pip install ".[dev]"` for contributor tooling. After installation,
the `incident-memory`, `incident-memory-seed`, `incident-memory-reset`, and
`incident-memory-evaluate` commands work outside the checkout; public UI and
synthetic fixtures are package resources. Configure environment variables or a
private `.env` in the directory where you run the command.

Open <http://127.0.0.1:8000>. The application serves its console and
`GET /health/live` without Azure access. Investigation remains unavailable until
live configuration and seeding are complete. Keep the loopback binding and
one worker: the monitor runs in the application process, not in the browser.

### Configure your Azure resources

1. Follow [Azure setup](docs/azure-setup.md); runtime startup does not provision
   resources or assign roles.
2. Copy `.env.example` to `.env` and fill in your own resource endpoints and
   deployment names. `.env` is private local configuration, not source code.
3. Authenticate and choose your subscription:

   ```text
   az login
   az account set --subscription "<subscription-id>"
   ```

4. Set `ALLOW_AZURE_USE=true` only when ready to make data and inference calls.
   For the fixture walkthrough also set `ALLOW_DEMO_RESET=true`, keep
   `APP_ENV=development`, `SCOPE_ID=demo-payments-development`, and
   `SERVICE_ID=payments`.
5. Seed the supplied synthetic historical evidence and service records:

   ```text
   uv run python -m scripts.seed --confirm-azure-use
   uv run python -m app.cli
   ```

   Installed equivalents are `incident-memory-seed --confirm-azure-use` and
   `incident-memory`.

Local development uses Azure CLI credentials through `DefaultAzureCredential`;
Azure hosting uses a configured managed identity. Cosmos account-key mode is an
explicit alternative, not an automatic fallback when Entra access fails.

Follow the [simulation walkthrough](docs/demo.md) for the first human-approved
pool correction, verified memory, bounded autonomous recurrence, deployment-error
escalation, and safe pause/reset.

## Validate changes

These checks do not establish live Azure connectivity or model behavior:

```text
uv run ruff check .
uv run ruff format --check .
uv run pytest tests/unit tests/security
uv run mypy app scripts
uv run pip-audit
```

The [ordinary CI workflow](.github/workflows/ci.yml) runs without cloud credentials.
The optional [live integration workflow](.github/workflows/live-integration.yml)
uses a manually dispatched protected environment and workload identity federation.
Its integration checks are not a substitute for observing the agent walkthrough.

## Documentation

| Guide | Purpose |
| --- | --- |
| [Azure setup](docs/azure-setup.md) | Resources, roles, model deployments, and settings |
| [Architecture](docs/architecture.md) | Data model, patterns, tools, memory, and monitoring |
| [Simulation walkthrough](docs/demo.md) | Runnable investigation, approval, recurrence, and reset |
| [Security](docs/security.md) | Prompt boundaries, identity, authority, and private evidence |
| [Compatibility](docs/compatibility.md) | Version-specific SDK, model, and vector requirements |
| [Costs](docs/costs.md) | Throughput floors, inference costs, and bounded usage |

Design-pattern references:
[Azure-Samples/cosmos-db-design-patterns](https://github.com/Azure-Samples/cosmos-db-design-patterns).
The application is [MIT licensed](LICENSE); preserve upstream notices when
copying dependency or sample code.
