# Simulation walkthrough

Run this with your own configured Azure development resources. It investigates a
simulated payments workload backed by real Cosmos state, historical synthetic
evidence, model calls, and native memory. No production payments service is changed.

Complete [Azure setup](azure-setup.md), seed the supplied artifacts, and start one
worker. Set `ALLOW_AZURE_USE=true` and `ALLOW_DEMO_RESET=true` only for this
designated development fixture. The browser is a console, not the monitor worker.

## 1. Start in guarded mode

For a repeatable starting state:

```text
uv run python -m scripts.reset --scenario pool_exhaustion --confirm-azure-use
uv run python -m app.cli
```

Reset pauses the control, invalidates earlier version-bound approvals, and
injects pool exhaustion. It retains durable incident/action/memory history.
If the server is already running, do not start a second worker.

Open <http://127.0.0.1:8000>. The default local identity is administrator
`engineer-a`; the console displays the configured identity, not a sign-in or
identity selector. Switch from paused to **guarded**. Inspect the live
simulation snapshot:

| Field | Pool-exhaustion fixture |
| --- | --- |
| Pool | 50 |
| Demand | 90 |
| Backend capacity | 150 |
| Configuration valid | true |
| Error rate | 0.4444 |
| Latency | 1,200 ms |
| Healthy | false |

These are deterministic simulated values, not live production telemetry.

## 2. Investigate and inspect evidence

Use the default investigation question:

> Why are payments failing?

### Supported questions

The educational investigator accepts these registered questions:

- `Why are payments failing?`
- `Investigate payment health with fresh evidence.`
- `Investigate payments connection pool exhaustion.`
- `Investigate the payments deployment configuration.`
- `Compare current payments diagnostics with verified historical memory.`
- `Recall verified payments recovery procedures and check current health.`

Case, whitespace, and terminal `?`/`.` are normalized. Other wording, general chat,
and appended instructions return HTTP 422 before investigation work. This is a
limited explicit prompt contract, not general natural-language topic detection.
Historical evidence can still be untrusted; prompt acceptance does not authorize
any action.

Inspect the response's historical evidence, retrieved image artifacts, source/
version metadata, native-memory section, and actual tool activity. Images are
synthetic historical diagrams/charts; they are not current telemetry.

The investigator must obtain fresh `health`, `config`, `backend`, and `metrics`
observations to support a pool recommendation. Missing or contradictory checks
must not produce an executable correction. A model can return a factual summary
without a proposal.

Inspect the exact proposed adjustment and its digest, target version, policy
version, expiry, and evidence. The narrow fixture correction is pool **50 to 100**:
100 covers demand 90 and remains below backend capacity 150.
Do not approve a different action simply because a historical record recommends it.

## 3. Approve the exact action and verify recovery

Use the console's approval control as the authenticated operator/administrator.
Typing approval into the investigation question does not authorize anything.
The server queues the exact approved proposal and its outbox intent atomically.

Observe the monitor processing it, then inspect current state and the durable
resolution. The expected successful fixture state is pool 100, error rate 0,
latency 80 ms, and healthy true. An HTTP write or model statement alone is not
recovery evidence.

If state/policy changed, expiry elapsed, or proof became stale, refresh and
investigate again. Do not keep retrying an obsolete approval card.

## 4. Inspect verified native memory

Wait for memory processing and inspect the memory section. A verified outcome
must be processed into the native store and confirmed by persisted scoped
content/vector readback. Check the resolution reference/provenance rather than
accepting a chat recollection or pending extraction as learning.

Review/invalidate an inappropriate lesson with an explicit reason using the
authenticated memory controls. Review cannot revive a durable invalidation
tombstone. Missing extraction/embedding/readback remains an error or pending
retry, not a successful memory result.

## 5. Recur under a bounded autonomous policy

After inspecting the verified lesson:

1. As administrator, enable **autonomous** mode with the narrow expiring policy.
   Keep the default action/attempt/concurrency/cooldown bounds.
2. Wait for the prior correction's cooldown before inducing another incident.
3. Stop the server, set `LOCAL_OPERATOR_ID=engineer-b` in your private `.env`,
   restart the single server process, and reopen the console. The synthetic
   operator shares only the server-authorized service scope. Each agent analysis
   creates a new session; do not reuse the prior transcript as proof of memory.
4. Inject **pool_exhaustion** through the development scenario control.
5. Do not ask the agent to monitor or recover. Observe the application worker
   detect the unhealthy state, recall authorized shared memory, investigate fresh
   telemetry, and queue only a policy-authorized action.

The recurrence should reference the verified native lesson while still producing
fresh tool proof. Policy, not the memory, grants bounded autonomous authority.
The operator cannot switch policy into autonomy.

A verified-memory prerequisite is part of this walkthrough's sequence, not a
separate enforced "memory exists" condition in the current authorization code.
Do not interpret the sequence as a guarantee that model recall always succeeds.

## 6. Contradictory deployment and pause

Inject **deployment_error** while observing autonomous monitoring. The fixture
sets invalid deployment configuration, error rate 0.8, and latency 1,800 ms.
The historical pool lesson must not override this contradiction. Expect an
escalated incident and no pool correction.

To return to administrator controls, stop the server, set
`LOCAL_OPERATOR_ID=engineer-a`, and restart. Switch to **paused**, then inject
**pool_exhaustion**.
No new correction should be authorized. Inspect pending work and current state;
pause does not reverse an already committed change.

Reset from the command line when needed:

```text
uv run python -m scripts.reset --scenario healthy --confirm-azure-use
```

This also leaves control paused. Stop the server after inspecting pending work.
Reset does not clear history/native memory or delete resources. Never replace
fixture reset with broad database/container/partition deletion.

All module commands above also have installed entrypoints:
`incident-memory`, `incident-memory-reset`, `incident-memory-seed`, and
`incident-memory-evaluate`. Pass the same options without `uv run python -m`.

## Local API examples

These examples apply only to loopback development. Hosted callers must use the
configured authenticated ingress, not synthetic identity headers.
The HTTP mutation boundary requires both same-origin headers shown below.

PowerShell:

```powershell
$base = 'http://127.0.0.1:8000'
$headers = @{
    Origin = $base
    'X-Requested-With' = 'IncidentMemory'
    'X-Demo-Operator' = 'engineer-a'
}
$status = Invoke-RestMethod "$base/api/status" -Headers $headers
$mode = @{ mode = 'guarded'; expected_version = $status.control.version } | ConvertTo-Json
Invoke-RestMethod "$base/api/mode" -Method Post -Headers $headers -ContentType 'application/json' -Body $mode
$body = @{ question = 'Why are payments failing?'; memory_enabled = $true } | ConvertTo-Json
$result = Invoke-RestMethod "$base/api/investigate" -Method Post -Headers $headers -ContentType 'application/json' -Body $body
$result
```

Review `$result.proposal` before executing an approval:

```powershell
if ($null -eq $result.proposal) { throw 'No supported current-tool proposal to approve.' }
$approval = @{ digest = $result.proposal.digest } | ConvertTo-Json
Invoke-RestMethod "$base/api/proposals/$($result.proposal.id)/approve" -Method Post -Headers $headers -ContentType 'application/json' -Body $approval
```

macOS/Linux Bash (start in guarded mode through the console first):

```bash
curl --fail-with-body http://127.0.0.1:8000/api/status \
  -H 'X-Demo-Operator: engineer-a'
curl --fail-with-body http://127.0.0.1:8000/api/investigate \
  -H 'Origin: http://127.0.0.1:8000' \
  -H 'X-Requested-With: IncidentMemory' \
  -H 'X-Demo-Operator: engineer-a' \
  -H 'Content-Type: application/json' \
  --data '{"question":"Why are payments failing?","memory_enabled":true}'
```

Use the console to approve the returned exact card, not a placeholder digest.
`GET /api/memories` shows authorized native memory; `GET /api/status` shows
current control, worker, incident, and history information.

## Compare investigations

Stop the application server before evaluation so no worker changes its fixture
while measurements run:

```text
uv run python -m scripts.evaluate --confirm-azure-use --output data/evaluation-results.json
```

Evaluation changes the designated fixture, creates an isolated native-memory
namespace, records a verified baseline correction, and compares repeated
pool-exhaustion and contradictory-deployment investigations with memory off/on.
The output records actual model usage, tool order, evidence, duration, diagnosis/
decision accuracy, and authorization behavior. It leaves mode paused, including
on failure, and retains its isolated namespace for inspection.

The output must be a JSON file beneath the current working directory's `data`
folder, not the packaged `artifacts.json`. Keep results private if they contain
deployment details. Missing live support fails rather than inventing savings.

The supplied historical synthetic assets are installed application data.
Optional offline image regeneration writes new PNGs into the current directory's
`data` folder; it does not overwrite installed package resources:

```text
uv run python -m scripts.seed --generate-images
```

## Failure checks to inspect

- Wrong membership/scope, forged remote identity headers, cross-origin mutations:
  deny without side effects.
- Altered/expired/replayed approval, state or policy version change: conflict or
  deny; do not apply an old action to a new fixture.
- Missing endpoint, incompatible vector schema, inaccessible private image:
  unavailable/error, never substitute fabricated evidence.
- Missing fresh tool proof or deployment contradiction: no authorized correction.
- Failed native processing or vector readback: pending/retry/error, not learning.
- Restart with pending outbox work: stable operation identity and actual health
  readback prevent duplicate corrections.

Inspect operation IDs, state/policy versions, before/after simulation health,
evidence provenance, and native-memory confirmation. Do not expose secrets or SAS.
