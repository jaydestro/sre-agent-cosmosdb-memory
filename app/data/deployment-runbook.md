# Synthetic deployment configuration error runbook

Revision 2, reviewed 2026-10-25. Contradictory historical case.

The worker pool can have spare capacity while payments fail because config_valid
is false after a deployment. The deterministic development scenario has demand
90, pool 100 and backend capacity 150, but error rate 80% and latency 1,800 ms.

A pool-exhaustion memory is misleading for this condition. Reject a proposed
pool adjustment even if the historical dashboard looks similar. A restart may
temporarily reduce latency but cannot correct the invalid deployment.

Escalate to the deployment owner with current telemetry and configuration validity.
The demo's narrow policy does not authorize arbitrary deployment edits, shell
commands, backend changes or rollback based only on model text.

Pause execution when uncertain. Preserve the denied policy decision and escalation
in durable history. Invalidate any recalled procedure that asserts pool expansion
fixes invalid configuration; retain its provenance and review rationale.
