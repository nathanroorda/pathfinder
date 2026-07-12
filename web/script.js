const statusEl = document.getElementById("status");
const resultEl = document.getElementById("result");
const shootBtn = document.getElementById("shoot");
const settingsEl = document.getElementById("settings");

async function api(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

// --- status ---------------------------------------------------------------
async function refreshStatus() {
  try {
    const s = await api("/api/status");
    statusEl.textContent = s.connected ? `Connected: ${s.model}` : "No camera connected";
    statusEl.className = "status " + (s.connected ? "connected" : "offline");
  } catch {
    statusEl.textContent = "Server unreachable";
    statusEl.className = "status offline";
  }
}

// --- capture --------------------------------------------------------------
shootBtn.addEventListener("click", async () => {
  shootBtn.disabled = true;
  resultEl.textContent = "Firing…";
  try {
    await api("/api/capture", { method: "POST" });
    resultEl.textContent = "Shot taken \u2713";
  } catch (e) {
    resultEl.textContent = `Error: ${e.message}`;
  } finally {
    shootBtn.disabled = false;
  }
});

// --- settings -------------------------------------------------------------
// One renderer per control kind. Add a kind here to support a new type.
const renderers = {
  choice: (s, apply) => {
    const sel = document.createElement("select");
    for (const c of s.choices) sel.add(new Option(c, c, false, c === s.value));
    sel.addEventListener("change", () => apply(sel.value));
    return sel;
  },
  toggle: (s, apply) => {
    const btn = document.createElement("button");
    btn.className = "toggle";
    const on = Number(s.value) === 1;
    btn.dataset.on = on ? "1" : "0";
    btn.textContent = on ? "On" : "Off";
    btn.addEventListener("click", () => apply(on ? 0 : 1));
    return btn;
  },
  range: (s, apply) => {
    const wrap = document.createElement("div");
    wrap.style.cssText = "display:flex;align-items:center;flex:1 1 auto";
    const input = document.createElement("input");
    input.type = "range";
    input.min = s.min; input.max = s.max; input.step = s.step || 1;
    input.value = s.value;
    const out = document.createElement("output");
    out.textContent = s.value;
    input.addEventListener("input", () => (out.textContent = input.value));
    input.addEventListener("change", () => apply(input.value));
    wrap.append(input, out);
    return wrap;
  },
  text: (s, apply) => {
    const input = document.createElement("input");
    input.type = "text";
    input.value = s.value;
    input.addEventListener("change", () => apply(input.value));
    return input;
  },
};

function renderSettings(items) {
  settingsEl.replaceChildren();
  for (const s of items) {
    const render = renderers[s.type];
    if (!render) continue;
    const row = document.createElement("div");
    row.className = "setting";
    const label = document.createElement("label");
    label.textContent = s.label;
    row.append(label, render(s, (value) => applySetting(s.name, value)));
    settingsEl.append(row);
  }
}

// Share fresh settings with every panel (dropdowns + exposure triangle).
function broadcast(items) {
  renderSettings(items);
  window.dispatchEvent(new CustomEvent("pf:settings", { detail: items }));
}

async function loadSettings() {
  try {
    broadcast(await api("/api/settings"));
  } catch {
    settingsEl.replaceChildren();
  }
}

async function applySetting(name, value) {
  try {
    // The server returns the full refreshed set, since one change can affect others.
    broadcast(await api(`/api/settings/${encodeURIComponent(name)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    }));
  } catch (e) {
    resultEl.textContent = `Setting failed: ${e.message}`;
  }
}

// When another panel (the triangle) changes a setting, refresh the dropdowns.
window.addEventListener("pf:settings", (e) => renderSettings(e.detail));

// --- init -----------------------------------------------------------------
refreshStatus();
loadSettings();
setInterval(refreshStatus, 5000);