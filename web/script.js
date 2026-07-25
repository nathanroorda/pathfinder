const statusEl = document.getElementById("status");
const resultEl = document.getElementById("result");
const shootBtn = document.getElementById("shoot");
const recordBtn = document.getElementById("record");
const settingsEl = document.getElementById("settings");
const liveviewEl = document.getElementById("liveview");
const previewImg = document.getElementById("preview");
const telemetryEl = document.getElementById("telemetry");
const afBtn = document.getElementById("af");
const focusNearBtn = document.getElementById("focusNear");
const focusFarBtn = document.getElementById("focusFar");
const focusStepEl = document.getElementById("focusStep");
const bulbBtn = document.getElementById("bulbBtn");
const bulbSecondsEl = document.getElementById("bulbSeconds");
const afMarker = document.getElementById("afMarker");

async function api(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

let wasConnected = false;
let connected = false;
let recording = false;
let bulbing = false;

function startLiveview() {
  previewImg.src = "/api/liveview?t=" + Date.now();
  liveviewEl.classList.remove("offline");
}

function stopLiveview() {
  previewImg.removeAttribute("src");
  liveviewEl.classList.add("offline");
}

function updateLiveview() {
  const shouldStream = connected && !recording && !bulbing;
  const streaming = previewImg.hasAttribute("src");
  if (shouldStream && !streaming) startLiveview();
  else if (!shouldStream && streaming) stopLiveview();
}

previewImg.addEventListener("error", stopLiveview);

function setRecording(on) {
  recording = on;
  recordBtn.textContent = on ? "Stop recording" : "Record";
  recordBtn.classList.toggle("recording", on);
  shootBtn.disabled = on || bulbing;  // don't let a status poll re-enable capture mid-bulb
  updateLiveview();
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    statusEl.textContent = s.connected ? `Connected: ${s.model}` : "No camera connected";
    statusEl.className = "status " + (s.connected ? "connected" : "offline");
    connected = s.connected;
    if (s.connected && !wasConnected) { loadSettings(); loadTelemetry(); }
    if (!s.connected) telemetryEl.replaceChildren();
    setRecording(s.connected && s.recording);  // also reconciles the liveview
    wasConnected = s.connected;
  } catch {
    statusEl.textContent = "Server unreachable";
    statusEl.className = "status offline";
    connected = false;
    telemetryEl.replaceChildren();
    updateLiveview();
  }
}

function renderTelemetry(items) {
  telemetryEl.replaceChildren();
  for (const item of items) {
    if (item.value === null || item.value === "") continue;
    const chip = document.createElement("span");
    chip.className = "chip";
    const k = document.createElement("span");
    k.className = "k";
    k.textContent = item.label;
    const v = document.createElement("span");
    v.className = "v";
    v.textContent = item.value;
    chip.append(k, v);
    telemetryEl.append(chip);
  }
}

async function loadTelemetry() {
  if (!connected || recording || bulbing) return;
  try {
    renderTelemetry(await api("/api/telemetry"));
  } catch {
    /* transient (e.g. camera busy); keep the last-known chips */
  }
}

shootBtn.addEventListener("click", async () => {
  shootBtn.disabled = true;
  resultEl.textContent = "Firing…";
  try {
    await api("/api/capture", { method: "POST" });
    resultEl.textContent = "Shot taken ✓";
  } catch (e) {
    resultEl.textContent = `Error: ${e.message}`;
  } finally {
    shootBtn.disabled = false;
  }
});

recordBtn.addEventListener("click", async () => {
  const start = !recording;
  recordBtn.disabled = true;
  resultEl.textContent = start ? "Starting recording…" : "Stopping recording…";
  try {
    const s = await api(start ? "/api/record/start" : "/api/record/stop", { method: "POST" });
    setRecording(s.recording);
    resultEl.textContent = s.recording ? "Recording ●" : "Recording stopped ✓";
  } catch (e) {
    resultEl.textContent = `Error: ${e.message}`;
  } finally {
    recordBtn.disabled = false;
  }
});

let settingsRefreshTimer;
function scheduleSettingsRefresh() {
  clearTimeout(settingsRefreshTimer);
  settingsRefreshTimer = setTimeout(loadSettings, 400);
}

afBtn.addEventListener("click", async () => {
  resultEl.textContent = "Focusing…";
  try {
    await api("/api/autofocus", { method: "POST" });
    resultEl.textContent = "Focused ✓";
    scheduleSettingsRefresh();
  } catch (e) {
    resultEl.textContent = `Error: ${e.message}`;
  }
});

async function driveFocus(steps) {
  resultEl.textContent = "Focusing…";
  try {
    await api("/api/focus", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ steps }),
    });
    resultEl.textContent = "Focus moved ✓";
    scheduleSettingsRefresh();
  } catch (e) {
    resultEl.textContent = `Error: ${e.message}`;
  }
}

focusNearBtn.addEventListener("click", () => driveFocus(-Number(focusStepEl.value)));
focusFarBtn.addEventListener("click", () => driveFocus(+Number(focusStepEl.value)));

bulbBtn.addEventListener("click", async () => {
  const seconds = Number(bulbSecondsEl.value);
  if (!(seconds > 0)) { resultEl.textContent = "Enter a bulb time > 0"; return; }
  bulbing = true;
  bulbBtn.disabled = shootBtn.disabled = recordBtn.disabled = true;
  updateLiveview();  // tear the preview down — the body owns the bus for the exposure
  let remaining = Math.ceil(seconds);
  resultEl.textContent = `Exposing ${remaining}s…`;
  const tick = setInterval(() => {
    remaining -= 1;
    resultEl.textContent = remaining > 0 ? `Exposing ${remaining}s…` : "Reading out…";
  }, 1000);
  try {
    await api("/api/bulb", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seconds }),
    });
    resultEl.textContent = "Bulb captured ✓";
  } catch (e) {
    resultEl.textContent = `Error: ${e.message}`;
  } finally {
    clearInterval(tick);
    bulbing = false;
    bulbBtn.disabled = recordBtn.disabled = false;
    shootBtn.disabled = recording;  // don't undo the recording lock-out
    updateLiveview();               // resume the preview
  }
});

previewImg.addEventListener("click", async (e) => {
  if (!connected || recording || bulbing || !previewImg.hasAttribute("src")) return;
  const rect = previewImg.getBoundingClientRect();
  const x = (e.clientX - rect.left) / rect.width;
  const y = (e.clientY - rect.top) / rect.height;
  pingAfMarker(x, y);
  try {
    await api("/api/afpoint", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ x, y }),
    });
    resultEl.textContent = "AF point set ✓";
  } catch (err) {
    resultEl.textContent = `Error: ${err.message}`;
  }
});

function pingAfMarker(x, y) {
  afMarker.style.left = `${x * 100}%`;
  afMarker.style.top = `${y * 100}%`;
  afMarker.classList.remove("ping");
  void afMarker.offsetWidth;  // reflow so re-adding the class restarts the animation
  afMarker.classList.add("ping");
}

const settingRenderers = {
  choice: (setting, apply) => {
    const select = document.createElement("select");
    for (const choice of setting.choices) {
      select.add(new Option(choice, choice, false, choice === setting.value));
    }
    select.addEventListener("change", () => apply(select.value));
    return select;
  },
  toggle: (setting, apply) => {
    const button = document.createElement("button");
    button.className = "toggle";
    const isOn = Number(setting.value) === 1;
    button.dataset.on = isOn ? "1" : "0";
    button.textContent = isOn ? "On" : "Off";
    button.addEventListener("click", () => apply(isOn ? 0 : 1));
    return button;
  },
  range: (setting, apply) => {
    const wrap = document.createElement("div");
    wrap.style.cssText = "display:flex;align-items:center;flex:1 1 auto";
    const input = document.createElement("input");
    input.type = "range";
    input.min = setting.min;
    input.max = setting.max;
    input.step = setting.step || 1;
    input.value = setting.value;
    const output = document.createElement("output");
    output.textContent = setting.value;
    input.addEventListener("input", () => (output.textContent = input.value));
    input.addEventListener("change", () => apply(input.value));
    wrap.append(input, output);
    return wrap;
  },
  text: (setting, apply) => {
    const input = document.createElement("input");
    input.type = "text";
    input.value = setting.value;
    input.addEventListener("change", () => apply(input.value));
    return input;
  },
};

function renderSettings(settings) {
  settingsEl.replaceChildren();
  for (const setting of settings) {
    const render = settingRenderers[setting.type];
    if (!render) continue;
    const row = document.createElement("div");
    row.className = "setting";
    const label = document.createElement("label");
    label.textContent = setting.label;
    row.append(label, render(setting, (value) => applySetting(setting.name, value)));
    settingsEl.append(row);
  }
}

async function loadSettings() {
  try {
    renderSettings(await api("/api/settings"));
  } catch {
    settingsEl.replaceChildren();
  }
}

async function applySetting(name, value) {
  try {
    renderSettings(await api(`/api/settings/${encodeURIComponent(name)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    }));
  } catch (e) {
    resultEl.textContent = `Setting failed: ${e.message}`;
  }
}

refreshStatus();
loadSettings();
setInterval(refreshStatus, 5000);
setInterval(loadTelemetry, 15000);
