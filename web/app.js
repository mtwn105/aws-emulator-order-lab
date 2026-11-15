const $ = (selector) => document.querySelector(selector);

const state = {
  limit: 5,
  selected: null,
  detailSignature: null,
  ordersSignature: null,
  orders: [],
  flash: null,
  busy: false,
  timer: null,
  systemTimer: null,
};

const STATUS = {
  EVENT_ACCEPTED: { label: "Event accepted", tone: "completed" },
  INVOICED: { label: "Invoiced", tone: "invoiced" },
  FAILED: { label: "Failed", tone: "failed" },
  ACCEPTED: { label: "Accepted", tone: "accepted" },
};

const MODES = {
  none: "no injected failure",
  once: "fails the first attempt",
  always: "fails every attempt",
};

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child == null) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function chipFor(row) {
  if (row.status === "ACCEPTED" && row.executionStatus === "RUNNING") {
    return el("span", { class: "chip running", text: "Running" });
  }
  const status = STATUS[row.status] || { label: row.status || "Unknown", tone: "accepted" };
  return el("span", { class: `chip ${status.tone}`, text: status.label });
}

function relativeTime(iso) {
  if (!iso) return "—";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

function money(cents) {
  return (cents / 100).toFixed(2);
}

async function api(path, options) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) {
    const error = new Error(payload.error || `Request failed (${response.status})`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function setConnection(status, label) {
  const node = $("#connection");
  node.dataset.state = status;
  node.lastChild.textContent = label;
}

function showError(message, hint) {
  const banner = $("#error-banner");
  banner.replaceChildren(el("strong", { text: "The dashboard cannot reach the lab" }), message);
  if (hint) banner.append(document.createTextNode(` ${hint}`));
  banner.hidden = false;
  setConnection("offline", "Lab unreachable");
}

function clearError() {
  $("#error-banner").hidden = true;
}

function setNote(text, tone) {
  const note = $("#form-note");
  note.textContent = text;
  if (tone) note.dataset.tone = tone;
  else delete note.dataset.tone;
}

function applyMeta(lab) {
  state.limit = lab.limit;
  $("#meta-prefix").textContent = lab.prefix;
  $("#meta-endpoint").textContent = lab.endpoint.replace("https://", "");
  $("#meta-limit").textContent = `${lab.limit} units`;
  const quantity = $("#quantity");
  quantity.max = String(lab.limit);
  if (Number(quantity.value) > lab.limit) quantity.value = String(lab.limit);
  $("#count-total").textContent = lab.counts.total;
  $("#count-succeeded").textContent = lab.counts.succeeded;
  $("#count-failed").textContent = lab.counts.failed;
  $("#count-invoices").textContent = lab.counts.invoices;
  setConnection("online", `Connected · ${lab.prefix}`);
}

function renderCounts(rows) {
  $("#count-total").textContent = rows.length;
  $("#count-succeeded").textContent = rows.filter((r) => r.executionStatus === "SUCCEEDED").length;
  $("#count-failed").textContent = rows.filter((r) => r.executionStatus === "FAILED").length;
  $("#count-invoices").textContent = rows.filter((r) => r.invoiceCents != null).length;
}

function renderOrders(rows) {
  const signature = JSON.stringify(rows);
  if (signature === state.ordersSignature) {
    const byId = new Map(rows.map((row) => [row.id, row]));
    for (const tr of document.querySelectorAll("#orders tr")) {
      const row = byId.get(tr.dataset.id);
      const cell = tr.lastElementChild;
      if (row && cell) cell.textContent = relativeTime(row.startedAt);
    }
    return;
  }
  state.ordersSignature = signature;
  const body = $("#orders");
  const known = new Set(state.orders.map((row) => row.id));
  state.orders = rows;
  body.replaceChildren();

  for (const row of rows) {
    const tr = el("tr", {
      tabindex: "0",
      "aria-selected": String(row.id === state.selected),
      dataset: { id: row.id },
    });
    if (row.id === state.flash && !known.has(row.id)) tr.classList.add("is-new");
    tr.append(
      el("td", { class: "id", text: row.id, title: row.id }),
      el("td", { class: "num mono", text: row.quantity ?? "—" }),
      el("td", { class: "num attempts", text: row.attempts ?? "—" }),
      el("td", {}, chipFor(row)),
      el("td", { class: "num mono", text: row.invoiceCents == null ? "—" : money(row.invoiceCents) }),
      el("td", { class: "num time", title: row.startedAt || "", text: relativeTime(row.startedAt) }),
    );
    tr.addEventListener("click", () => select(row.id));
    tr.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        select(row.id);
      }
    });
    body.append(tr);
  }

  $("#empty").hidden = rows.length > 0;
  renderCounts(rows);
}

async function loadLab() {
  try {
    const lab = await api("/api/lab");
    applyMeta(lab);
    clearError();
    return true;
  } catch (error) {
    showError(error.message, "Start the emulator and run bootstrap.py, then reload.");
    return false;
  }
}

async function loadOrders() {
  try {
    const data = await api("/api/orders");
    renderOrders(data.orders);
    clearError();
    if (state.selected) await loadDetail(state.selected, { quiet: true });
  } catch (error) {
    showError(error.message, "Is the emulator still running?");
  }
}

function timelineStatus(status, executionStatus, attempts, invoice) {
  const failed = status === "FAILED";
  const done = status === "EVENT_ACCEPTED" || invoice != null;
  const running = executionStatus === "RUNNING";
  return [
    {
      title: "Order accepted",
      meta: "Record stored in DynamoDB",
      state: failed || done || running || attempts > 0 ? "done" : "active",
    },
    {
      title: "Fulfilment",
      meta: attempts > 0
        ? `${attempts} attempt${attempts === 1 ? "" : "s"}${failed ? ", last one failed" : ""}`
        : "Waiting for the workflow to start",
      state: failed ? "failed" : done ? "done" : running || status === "ACCEPTED" ? "active" : "",
    },
    {
      title: "Invoice",
      meta: invoice
        ? `${invoice.quantity} units · ${money(invoice.totalCents)} USD`
        : failed ? "No invoice was written" : "Written after a successful attempt",
      state: invoice ? "done" : failed ? "failed" : "",
    },
    {
      title: "Completion event",
      meta: status === "EVENT_ACCEPTED"
        ? "EventBridge accepted the entry"
        : failed ? "Workflow failed before publication" : "Published after the invoice",
      state: status === "EVENT_ACCEPTED" ? "done" : failed ? "failed" : invoice && running ? "active" : "",
    },
  ];
}

function renderDetail(data) {
  const order = data.order;
  const execution = data.execution;
  const invoice = data.invoice;
  const status = order?.status;
  const id = order?.id || (execution && state.selected);
  const delivery = data.inboxes || {};

  const head = el("div", { class: "detail-head" },
    el("div", {},
      el("h2", { text: id || "Order" }),
      el("p", {
        class: "detail-sub",
        text: order
          ? `${order.quantity} units · ${MODES[order.failure] || order.failure}`
          : "No stored order record",
      }),
    ),
    chipFor({ status, executionStatus: execution?.status }),
    el("button", { class: "close", type: "button", "aria-label": "Close details", text: "×" }),
  );

  const steps = timelineStatus(status, execution?.status, order?.attempts || 0, invoice);
  const timeline = el("ol", { class: "timeline" });
  for (const step of steps) {
    timeline.append(el("li", { class: step.state || "" },
      el("span", { class: "step-title", text: step.title }),
      el("span", { class: "step-meta", text: step.meta }),
    ));
  }

  const deliveryList = el("div", { class: "inboxes" });
  for (const name of ["customer", "audit"]) {
    const seen = Boolean(delivery[name]);
    deliveryList.append(el("span", {
      class: `inbox ${seen ? "seen" : "unseen"}`,
      text: `${name} inbox · ${seen ? "observed" : "not observed"}`,
    }));
  }

  const logs = el("ol", { class: "order-logs" });
  const entries = (data.logs || []).slice(-6).reverse();
  if (entries.length === 0) logs.append(el("li", { class: "muted" }, el("span", {}, ""), "No log entries for this order"));
  for (const entry of entries) {
    let label = entry.message;
    try {
      label = JSON.parse(entry.message).stage || label;
    } catch {
      /* keep the raw message */
    }
    logs.append(el("li", {},
      el("time", { text: new Date(entry.time).toLocaleTimeString([], { hour12: false }) }),
      el("span", { text: label }),
    ));
  }

  const invoiceBlock = el("div", { class: "panel-block" }, el("h3", { text: "Invoice" }));
  if (invoice) {
    invoiceBlock.append(
      el("dl", { class: "facts" },
        el("dt", { text: "Total" }), el("dd", { text: `${money(invoice.totalCents)} USD` }),
        el("dt", { text: "Units" }), el("dd", { text: String(invoice.quantity) }),
        el("dt", { text: "Object" }), el("dd", { text: `${invoice.orderId}.json` }),
      ),
      el("details", { class: "raw" },
        el("summary", { text: "Raw invoice" }),
        el("pre", { text: JSON.stringify(invoice, null, 2) }),
      ),
    );
  } else {
    invoiceBlock.append(el("p", { class: "hint", text: "No invoice object exists for this order." }));
  }

  const body = el("div", {},
    head,
    timeline,
    invoiceBlock,
    el("div", { class: "panel-block" }, el("h3", { text: "Notification inboxes" }), deliveryList,
      el("p", { class: "hint", text: "Observed by peeking at the queues. The test suite consumes messages when it runs." })),
    el("div", { class: "panel-block" }, el("h3", { text: "Order log" }), logs),
  );

  const detail = $("#detail-body");
  detail.replaceChildren(body);
  detail.hidden = false;
  $("#detail-empty").hidden = true;
  detail.querySelector(".close").addEventListener("click", clearSelection);
}

async function loadDetail(id, { quiet = false } = {}) {
  try {
    const data = await api(`/api/orders/${encodeURIComponent(id)}`);
    const signature = JSON.stringify(data);
    if (quiet && signature === state.detailSignature) return;
    state.detailSignature = signature;
    renderDetail(data);
  } catch (error) {
    if (!quiet) showError(error.message);
  }
}

async function select(id) {
  state.selected = id;
  document.querySelectorAll("#orders tr").forEach((row) => {
    row.setAttribute("aria-selected", String(row.dataset.id === id));
  });
  await loadDetail(id);
  if (window.innerWidth <= 1180) {
    const panel = $("#detail");
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    panel.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
  }
}

function clearSelection() {
  state.selected = null;
  state.detailSignature = null;
  $("#detail-body").hidden = true;
  $("#detail-empty").hidden = false;
  $("#detail-title").textContent = "Select an order";
  document.querySelectorAll("#orders tr").forEach((row) => row.setAttribute("aria-selected", "false"));
}

async function loadSystem() {
  try {
    const data = await api("/api/system");
    const max = Math.max(1, ...data.queues.map((q) => q.depth));
    const queues = $("#queues");
    queues.replaceChildren();
    for (const queue of data.queues) {
      queues.append(el("div", { class: "queue" },
        el("span", { class: "queue-name", text: queue.name }),
        el("span", { class: "queue-count", text: queue.inFlight ? `${queue.depth} + ${queue.inFlight}` : String(queue.depth) }),
        el("span", { class: "queue-bar" }, el("span", { style: `width:${(queue.depth / max) * 100}%` })),
      ));
    }
    const logs = $("#logs");
    logs.replaceChildren();
    if (data.logs.length === 0) logs.append(el("li", { class: "muted", text: "No entries yet." }));
    for (const entry of data.logs.slice(0, 8)) {
      let label = entry.message;
      try {
        const parsed = JSON.parse(entry.message);
        if (parsed.orderId && parsed.stage) label = `${parsed.orderId} · ${parsed.stage}`;
      } catch {
        /* keep the raw message */
      }
      logs.append(el("li", {},
        el("time", { text: new Date(entry.time).toLocaleTimeString([], { hour12: false }) }),
        el("span", { text: label, title: entry.message }),
      ));
    }
    $("#parameter").textContent = `${data.parameter.name} = ${data.parameter.value}`;
  } catch {
    /* the banner already explains a lab-wide failure */
  }
}

async function submit(event) {
  event.preventDefault();
  if (state.busy) return;
  const quantity = Number($("#quantity").value);
  if (!Number.isInteger(quantity) || quantity < 1 || quantity > state.limit) {
    setNote(`Quantity must be a whole number from 1 to ${state.limit}.`, "bad");
    return;
  }
  const failure = document.querySelector("input[name=failure]:checked").value;
  const button = $(".primary");
  state.busy = true;
  button.disabled = true;
  setNote("Placing the order…");
  try {
    const result = await api("/api/orders", {
      method: "POST",
      body: JSON.stringify({ quantity, failure }),
    });
    state.flash = result.orderId;
    state.selected = result.orderId;
    setNote(`Order ${result.orderId} placed. Watching its workflow.`, "good");
    await loadOrders();
    await select(result.orderId);
  } catch (error) {
    setNote(error.payload?.hint ? `${error.message} ${error.payload.hint}` : error.message, "bad");
    if (!error.status || error.status >= 500) showError(error.message, error.payload?.hint);
  } finally {
    state.busy = false;
    button.disabled = false;
  }
}

function bindStepper() {
  for (const button of document.querySelectorAll(".step")) {
    button.addEventListener("click", () => {
      const input = $("#quantity");
      const next = Number(input.value) + Number(button.dataset.step);
      input.value = String(Math.min(state.limit, Math.max(1, Number.isInteger(next) ? next : 1)));
    });
  }
  $("#quantity").addEventListener("change", () => {
    const input = $("#quantity");
    const value = Math.round(Number(input.value));
    input.value = String(Math.min(state.limit, Math.max(1, Number.isFinite(value) ? value : 1)));
  });
}

function startPolling() {
  const tick = async () => {
    if (document.hidden) return;
    await loadOrders();
  };
  state.timer = setInterval(tick, 3000);
  state.systemTimer = setInterval(() => {
    if (!document.hidden) loadSystem();
  }, 6000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) tick();
  });
}

async function init() {
  bindStepper();
  $("#order-form").addEventListener("submit", submit);
  $("#refresh").addEventListener("click", async () => {
    await loadLab();
    await loadOrders();
    await loadSystem();
  });
  const ready = await loadLab();
  if (ready) {
    await loadOrders();
    await loadSystem();
  }
  startPolling();
}

init();
