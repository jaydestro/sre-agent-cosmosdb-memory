# Synthetic payments pool correction runbook

Revision 3, reviewed 2026-10-20. Historical procedure, not autonomous authority.

1. Inspect fresh service state and current control policy. Confirm payments is the
   exact target, deployment configuration is valid and root cause is pool exhaustion.
2. Confirm the current pool is exactly 50, demand is greater than 50 and at most 100,
   and backend capacity is at least 100 and no less than demand.
3. Retrieve the historical dashboard, utilization chart and architecture image.
   Compare their provenance and capture dates with current measurements.
4. Propose only the bounded pool adjustment 50 to 100. Bind incident, current target
   version, control/policy version, evidence, expiry and rollback linkage in an
   immutable digest. Display before/current/proposed values and risks to the human.
5. In guarded mode require exact human approval. In autonomous mode require fresh
   narrow policy authorization, cooldown, concurrency one and remaining budgets.
6. Revalidate immediately before conditional execution. Expiry, emergency stop,
   changed target, contradictory telemetry or scope mismatch cancels authority.
7. Verify persisted post-action telemetry before recording procedural memory.
   A failed verification escalates. Rollback is a new exact proposal and requires
   new authorization; it must never silently reuse the original approval.

Expected development impact: error rate 44.4% to 0%, latency 1,200 ms to 80 ms.
Risk: more backend connections; never apply if capacity is unknown or insufficient.
