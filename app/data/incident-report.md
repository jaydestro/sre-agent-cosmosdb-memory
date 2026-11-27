# Synthetic historical incident INC-PAY-20261019

This is synthetic development training evidence, not production telemetry or a
current instruction. Service: payments. Incident window: 2026-10-19 14:00–14:20 UTC.

At 14:10 the workload had 90 concurrent requests, a worker connection pool of 50,
and backend capacity of 150. The deployment configuration was valid. The modeled
error rate was 44.4%, p95 latency 1,200 ms, and pool utilization 100%.

Two worker restarts briefly reduced latency but did not alter pool capacity or
remove the error condition. Restarting is not a durable pool-exhaustion correction.

A human approved the exact change from pool 50 to pool 100 after confirming backend
capacity, current target version and fresh telemetry. Post-action measurements
were error rate 0%, latency 80 ms, utilization 90%. The outcome was verified before
recording the reviewed procedure. See the accompanying real PNG artifacts.

Do not reuse this conclusion for an invalid deployment configuration or backend
overload. Historical similarity supplies context, never permission or live proof.
