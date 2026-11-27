# Public contributor standards

- Keep this an educational simulated payments workload, not a production
  controller. Do not represent simulated telemetry as production measurements.
- Ground SDK changes in the declared package versions and official release
  interfaces. Keep endpoint, schema, partition, vector, and credential contracts
  explicit; reject incompatible stores rather than migrating them at startup.
- Separate operational state, Microsoft Embedding 1 evidence, and provider-owned
  native memory. Equal vector dimensions do not imply model compatibility.
- Use native Agent Framework memory hooks. Only verified outcomes with scoped
  provenance and persisted readback may become trusted resolution references.
- Models, retrieved artifacts, and memory cannot grant authority. Preserve scoped
  roles, exact proposal digests, current policy, diagnostic proof, ETags, bounded
  targets, expiry, and idempotent outbox operations.
- Keep local synthetic identities loopback-only. Hosted operation requires
  authenticated ingress and server-owned membership checks; never trust arbitrary
  caller identity headers.
- Keep Azure access off by default. Runtime startup must not provision resources,
  seed state, assign roles, fetch keys, or silently broaden authentication.
- Keep Blob evidence private. Never commit or print secrets, SAS, bearer tokens,
  personal deployment details, or customer incident data.
- Preserve single-worker bounded monitoring, fail-closed escalation, and pause
  semantics. Cleanup must be fixture-owned, explicit, and never broad deletion.
- Package public UI and synthetic evidence as application resources. Installed
  commands must not rely on the repository working directory or private assets.
- Use existing lint/type/security/unit tests for changes. Keep offline checks free
  of cloud credentials and inference. Live integration needs its separate
  protected workload-identity path.
- Document actual behavior and runnable commands. Distinguish source contracts,
  offline checks, live service results, and benchmarks; none substitutes for the
  others. Do not claim an unimplemented cloud port or production integration.
- Keep README and documentation usable independently. Include public technical
  constraints and source citations, not conversation notes, private status,
  machine-specific paths, or event-specific instructions.
