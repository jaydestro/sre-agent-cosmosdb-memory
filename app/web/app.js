"use strict";

const $ = (id) => document.getElementById(id);
let current = null;
let polling = null;
let busy = false;
const imageURLs = new Set();

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}

function printable(value) {
  if (value === undefined || value === null) return "Not recorded";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function fields(container, values) {
  for (const [name, value] of Object.entries(values)) {
    container.append(node("dt", name), node("dd", printable(value)));
  }
}

function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").className = error ? "notice error" : "notice";
}

async function api(path, body) {
  const options = {credentials: "same-origin", headers: {}};
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.headers["X-Requested-With"] = "IncidentMemory";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) {
    if (result.available === false) showSetup(result);
    throw new Error(result.error || result.setup || "Request unavailable.");
  }
  return result;
}

function showSetup(result) {
  $("setup").hidden = false;
  $("workspace").hidden = true;
  $("setup-detail").textContent = result.setup || "Live services are unavailable.";
  if (result.operator) $("identity").textContent = result.operator.subject;
  if (polling) clearInterval(polling);
}

async function act(work) {
  if (busy) return;
  busy = true;
  try {
    notice("Working…");
    await work();
    notice("Operation completed. The console shows the latest persisted state.");
  } catch (error) {
    notice(error.message, true);
  } finally {
    busy = false;
  }
}

function can(role) {
  const roles = current?.operator?.roles || [];
  return roles.includes("admin") || roles.includes(role);
}

function renderStatus(value) {
  current = value;
  $("setup").hidden = true;
  $("workspace").hidden = false;
  $("identity").textContent = `${value.operator.subject} · ${value.operator.roles.join(", ")}`;
  const live = value.live || {};
  const metrics = live.metrics || {};
  $("metrics").replaceChildren();
  const displayed = {
    "Error rate": metrics.error_rate === undefined ? null : `${(metrics.error_rate * 100).toFixed(1)}%`,
    "Latency": metrics.latency_ms === undefined ? null : `${metrics.latency_ms} ms`,
    "Pool utilization": metrics.pool_utilization === undefined ? null :
      `${(metrics.pool_utilization * 100).toFixed(0)}%`,
    "Pool capacity": live.pool_size,
    "Observed condition": live.root_cause,
  };
  for (const [label, data] of Object.entries(displayed)) {
    const card = node("div", null, "metric");
    card.append(node("span", label), node("strong", printable(data)));
    $("metrics").append(card);
  }
  $("observed").textContent = `Observed ${printable(live.observed_at)} · service version ${
    printable(live.version)} · ${value.snapshot_cache_seconds}s snapshot cache`;
  const control = value.control || {};
  $("mode-description").textContent = `Mode: ${printable(control.mode)} · control version ${
    printable(control.version)}${control.suspended ? " · SUSPENDED" : ""}`;
  for (const button of document.querySelectorAll("[data-mode]")) {
    button.disabled = !can("admin");
    button.setAttribute("aria-pressed", String(button.dataset.mode === control.mode));
  }
  $("emergency-stop").disabled = !can("admin");
  for (const input of $("policy-form").elements) input.disabled = !can("admin");
  if (!$("policy-form").contains(document.activeElement)) {
    const policy = control.policy || {};
    const expiry = new Date(policy.expires_at);
    if (!Number.isNaN(expiry.getTime())) $("policy-expiry").value = expiry.toISOString().slice(0, 16);
    $("policy-actions").value = policy.max_actions ?? 10;
    $("policy-attempts").value = policy.max_attempts ?? 3;
    $("policy-rollback").checked = policy.allow_rollback === true;
  }
  for (const input of $("investigation-form").elements) input.disabled = !can("operator");
  $("policy").replaceChildren();
  fields($("policy"), {
    "Policy": control.policy,
    "Attempts / actions": `${control.attempts ?? 0} / ${control.actions ?? 0}`,
    "Worker running": value.worker?.running,
    "Worker interval (seconds)": value.worker?.interval_seconds,
    "Browser required": value.worker?.browser_required,
  });
  $("scenario-controls").hidden = !value.scenario_enabled;
  $("inject").disabled = !can("operator");
  const selected = $("incident").value;
  $("incident").replaceChildren(node("option", "Create a new incident"));
  $("incident").firstChild.value = "";
  for (const incident of value.incidents || []) {
    const option = node("option", `${incident.id} · ${incident.status || "open"}`);
    option.value = incident.id;
    $("incident").append(option);
  }
  $("incident").value = selected;
  renderProposals(value.proposals || []);
  const history = [...(value.history || []), ...(value.memory_reviews || [])];
  history.sort((a, b) => (Date.parse(b.observed_at || b.reviewed_at || b.created_at) || 0) -
    (Date.parse(a.observed_at || a.reviewed_at || a.created_at) || 0));
  renderHistory(history);
}

async function refresh() {
  renderStatus(await api("/api/status"));
}

function actionButton(text, callback, disabled = false, className = "") {
  const button = node("button", text, className);
  button.type = "button";
  button.disabled = disabled;
  button.addEventListener("click", () => act(callback));
  return button;
}

function renderProposals(items) {
  $("proposals").replaceChildren();
  if (!items.length) $("proposals").append(node("p", "No proposals recorded."));
  for (const proposal of items) {
    const card = node("article", null, "proposal");
    card.append(node("h3", proposal.operation || "Proposed action"),
      node("p", `${proposal.id} · ${proposal.status || "pending"}`, "muted"));
    const details = node("dl");
    fields(details, {
      "Bound before value": proposal.before?.pool_size ?? proposal.current_value,
      "Live current value": current?.live?.pool_size,
      "Proposed value": proposal.proposed_value,
      "Target / version": `${printable(proposal.service_id)} / ${printable(proposal.target_version)}`,
      "Risk": proposal.risk,
      "Expected impact": proposal.expected_impact,
      "Expiry": proposal.expires_at,
      "Rollback controls": proposal.rollback || proposal.rollback_controls,
      "Evidence provenance": proposal.evidence,
      "Exact approval digest": proposal.digest,
      "Policy decision": proposal.policy_decision,
      "Verification": proposal.verification,
    });
    card.append(details);
    const disabled = !can("operator") || proposal.status !== "proposed" ||
      !proposal.digest || Date.parse(proposal.expires_at) <= Date.now();
    const id = encodeURIComponent(proposal.id);
    card.append(actionButton("Approve exact action", async () => {
      if (!window.confirm(`Approve ${proposal.operation}: ${proposal.current_value} → ${
        proposal.proposed_value}, target version ${proposal.target_version}?\nDigest: ${
        proposal.digest}`)) return;
      await api(`/api/proposals/${id}/approve`, {digest: proposal.digest});
      await refresh();
    }, disabled));
    card.append(actionButton("Reject", async () => {
      await api(`/api/proposals/${id}/reject`, {digest: proposal.digest});
      await refresh();
    }, disabled, "secondary"));
    $("proposals").append(card);
  }
}

function renderHistory(items) {
  $("history").replaceChildren();
  if (!items.length) $("history").append(node("li", "No durable events recorded."));
  for (const event of items) {
    const item = node("li");
    item.append(node("strong", event.action || event.event || event.type),
      node("p", printable(event.summary || event.reason || event.decision ||
        event.error || event.mode || event.status || event.source)),
      node("small", `${printable(event.observed_at || event.reviewed_at ||
        event.created_at || event.timestamp)} · ${
        printable(event.subject || event.reviewed_by || event.actor || event.principal)} · ${
        printable(event.proposal_id || event.memory_id || event.incident_id)}`));
    $("history").append(item);
  }
}

async function renderEvidence(result) {
  for (const url of imageURLs) URL.revokeObjectURL(url);
  imageURLs.clear();
  $("evidence").replaceChildren();
  const retrieval = result.evidence || {};
  const records = Array.isArray(retrieval) ? retrieval : retrieval.hits || [];
  $("evidence").append(node("p", `Retrieval model: ${printable(retrieval.model)} · latency ${
    printable(retrieval.latency_ms)} ms · RU ${printable(retrieval.ru)}`, "muted"));
  for (const hit of records) {
    const card = node("article", null, "evidence-card");
    card.append(node("h4", hit.title || hit.id || "Historical evidence"));
    const details = node("dl");
    fields(details, {
      "Source": hit.source || hit.source_name || hit.source_incident_id,
      "Capture time": hit.captured_at || hit.observed_at,
      "Score / vector distance": hit.score ?? hit.distance,
      "Model": hit.model || retrieval.model,
      "Summary": hit.summary || hit.text,
    });
    card.append(details);
    $("evidence").append(card);
    if (hit.id && (hit.kind === "image" || hit.content_type?.startsWith("image/") ||
      hit.image_ref || hit.image_id || hit.image_available)) {
      try {
        const response = await fetch(`/api/evidence/${encodeURIComponent(hit.image_id || hit.id)}/image`,
          {credentials: "same-origin"});
        if (!response.ok) throw new Error("Historical image unavailable.");
        const blob = await response.blob();
        if (!["image/png", "image/jpeg", "image/webp"].includes(blob.type)) {
          throw new Error("Unsupported historical image.");
        }
        const url = URL.createObjectURL(blob);
        imageURLs.add(url);
        const image = node("img");
        image.src = url;
        image.alt = String(hit.title || "Historical incident evidence");
        image.loading = "lazy";
        card.prepend(image);
      } catch (error) {
        card.append(node("p", error.message, "muted"));
      }
    }
  }
}

async function investigate(event) {
  event.preventDefault();
  await act(async () => {
    const body = {question: $("question").value, memory_enabled: $("use-memory").checked};
    if ($("incident").value) body.incident_id = $("incident").value;
    const result = await api("/api/investigate", body);
    $("answer").replaceChildren(node("h3", "Investigation summary"),
      node("p", printable(result.summary)),
      node("small", `Model ${printable(result.model)} · usage ${printable(result.usage)} · incident ${
        printable(result.incident_id || (typeof result.incident === "string" ?
          result.incident : result.incident?.id))}`));
    $("tools").replaceChildren();
    for (const activity of result.tool_activity || []) {
      const item = node("li");
      item.append(node("strong", activity.tool || activity.name),
        node("p", printable(activity.summary || activity.status)),
        node("small", `Latency ${printable(activity.duration_ms ?? activity.latency_ms)} ms · ${
          printable(activity.source || activity.model || "")}`));
      $("tools").append(item);
    }
    const recalled = result.memory || {};
    $("recalled").replaceChildren(node("p", `Status: ${printable(recalled.processing_status)} · ${
      (recalled.memories || []).length} procedures · latency ${printable(recalled.latency_ms)} ms`));
    for (const memory of recalled.memories || []) {
      const detail = node("details");
      detail.append(node("summary", `Historical procedure ${memory.id} · ${memory.review}`),
        node("p", printable(memory.content)),
        node("small", `Source incident ${printable(memory.source_incident_id)} · resolution ${
          printable(memory.source_resolution_id)} · ${printable(memory.trust)}`));
      $("recalled").append(detail);
    }
    await renderEvidence(result);
    await refresh();
    const incidentId = result.incident_id || (typeof result.incident === "string" ?
      result.incident : result.incident?.id);
    if (incidentId) {
      $("incident").value = incidentId;
    }
  });
}

async function memories() {
  const response = await api("/api/memories");
  const items = Array.isArray(response) ? response : response.items || response.memories || [];
  $("memories").replaceChildren();
  if (!items.length) $("memories").append(node("p", "No procedural memories recorded."));
  for (const memory of items) {
    const card = node("article", null, "proposal");
    card.append(node("h3", memory.title || memory.id));
    const details = node("dl");
    fields(details, {
      "Procedure": memory.procedure || memory.content || memory.summary,
      "Status / review": memory.status || memory.review,
      "Processing status": memory.processing_status,
      "Reviewed by": memory.reviewed_by,
      "Provenance": memory.provenance || memory.source_ids || {
        incident: memory.source_incident_id, resolution: memory.source_resolution_id,
        thread: memory.source_thread_id, trust: memory.trust,
      },
      "Verification": memory.verification,
      "Validity / expiry": memory.expires_at,
      "Last review": memory.reviewed_at,
      "Review reason": memory.review_reason || memory.reason,
    });
    card.append(details);
    for (const action of ["review", "invalidate"]) {
      card.append(actionButton(action === "review" ? "Review" : "Invalidate", async () => {
        const reason = window.prompt(`Reason to ${action} this procedure (required):`);
        if (!reason?.trim()) return;
        await api(`/api/memories/${encodeURIComponent(memory.id)}/${action}`, {reason});
        await memories();
        await refresh();
      }, !can("operator") || !memory.id, "secondary"));
    }
    $("memories").append(card);
  }
}

async function changeMode(mode, policy) {
  const body = {mode, expected_version: current.control.version};
  if (policy) body.policy = policy;
  await api("/api/mode", body);
  await refresh();
}

$("refresh").addEventListener("click", () => act(refresh));
$("investigation-form").addEventListener("submit", investigate);
$("load-memories").addEventListener("click", () => act(memories));
for (const button of document.querySelectorAll("[data-mode]")) {
  button.addEventListener("click", () => act(async () => {
    if (button.dataset.mode === "autonomous" &&
      !window.confirm("Enable bounded autonomous execution under the current policy?")) return;
    await changeMode(button.dataset.mode);
  }));
}
$("emergency-stop").addEventListener("click", () => act(() => changeMode("paused")));
$("policy-form").addEventListener("submit", (event) => {
  event.preventDefault();
  act(() => changeMode(current.control.mode, {
    expires_at: new Date(`${$("policy-expiry").value}Z`).toISOString(),
    max_actions: Number($("policy-actions").value),
    max_attempts: Number($("policy-attempts").value),
    allow_rollback: $("policy-rollback").checked,
  }));
});
$("inject").addEventListener("click", () => act(async () => {
  if (!window.confirm("Change only the designated development payments workload?")) return;
  await api("/api/scenario", {scenario: $("scenario").value});
  await refresh();
}));
$("auto-refresh").addEventListener("change", () => {
  if (polling) clearInterval(polling);
  polling = $("auto-refresh").checked ? setInterval(() => {
    if (!document.hidden && !busy) act(refresh);
  }, 10000) : null;
});
act(async () => { await refresh(); await memories(); });
