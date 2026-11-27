# Costs and operating limits

Real Azure setup incurs charges even though the payments workload is simulated.
Check current regional prices, currency, quotas, and billing before provisioning.

## Dedicated Cosmos throughput

The development setup uses **autoscale with an initial maximum of 1,000 RU/s per
required container**, not serverless or shared database throughput. Autoscale has
an idle billing floor: the nominal minimum is 10% of the configured maximum,
and billing depends on the highest scaled throughput in the hour and the account's
pricing configuration.

Count all required containers: operations, evidence, native memory base, native
turns, native summaries, and the toolkit counter. At an initial 1,000 RU/s maximum
each, six containers have a combined nominal idle floor of 600 RU/s before
regional/pricing factors.
Do not budget for only a single memory container or assume pausing the application
stops provisioned throughput charges.

Storage, vector indexes, replication, backup choices, and any increase in autoscale
maximum add costs. The application does not scale down or delete resources for you.

## Inference and storage

[Microsoft Embedding 1 pricing](https://learn.microsoft.com/azure/cosmos-db/gen-ai/microsoft-embedding-1#pricing)
lists text/query input at $0.12 per million tokens and image input at $0.40 per
million tokens. Use current service pricing and reported usage, not a per-image
token assumption. At those rates, 100,000 text/query tokens and 100,000 image tokens
would cost `$0.012 + $0.040 = $0.052` for this embedding component alone.

Other charges include:

- Foundry reasoning with image input and tool calls.
- Native-memory extraction and its separate text-embedding deployment.
- Private Blob capacity, transactions, and applicable egress.
- Hosting and any separately configured monitoring or logging.

A 1,536-dimensional float32 vector occupies 6,144 raw bytes, excluding JSON,
metadata, indexes, and replicas. The synthetic dataset has fewer than 1,000
vectors, so quantizedFlat/DiskANN scan fallback affects RU and latency.

## Bound work and measure honestly

- `ALLOW_AZURE_USE=false` disables ordinary application Azure use. CLI seeding,
  reset, and evaluation also require explicit confirmation.
- Keep one worker and bounded concurrency. Defaults are 12 model calls,
  15 diagnostic calls, 120 seconds, and 4,096 generated tokens per investigation.
- The generated-token cap does not bound input tokens or all charges. Historical
  excerpts, image input, provider processing, and extraction add work.
- Default evidence and memory retrieval limits are five hits. Idempotent ingestion
  reuses unchanged payload embeddings.
- Autonomous policy defaults bound attempts to three, actions to ten, concurrency
  to one, and cooldown to 30 seconds; policy also expires.
- Pause after use and stop the server to stop polling/model work. Pause does not
  cancel already incurred costs or Cosmos autoscale billing floors.

Investigation responses expose model usage and available evidence request charges.
Native-memory RU is unavailable when the toolkit does not expose it; `null` means
unknown, not free. Use Azure billing/metrics for total service spend.

[Evaluation](demo.md#compare-investigations) records actual usage and decision
results. Memory may help diagnosis, but lower token use, lower cost, or faster
recovery is not guaranteed.

References: [Cosmos pricing](https://azure.microsoft.com/pricing/details/cosmos-db/),
[autoscale billing](https://learn.microsoft.com/azure/cosmos-db/provision-throughput-autoscale),
and [vector limits](https://learn.microsoft.com/azure/cosmos-db/vector-search).
