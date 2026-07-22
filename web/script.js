const statusEl = document.getElementById("status");
const resultEl = document.getElementById("result");
const shootBtn = document.getElementById("shoot");
const recordBtn = document.getElementById("record");
const settingsEl = document.getElementById("settings");
const liveviewEl = document.getElementById("liveview");
const previewImg = document.getElementById("preview");

async function api(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

let wasConnected = false;
let connected = false;
let recording = false;

function startLiveview() {
  previewImg.src = "/api/liveview?t=" + Date.now();  // cache-buster forces a fresh stream
  liveviewEl.classList.remove("offline");
}

function stopLiveview() {
  previewImg.removeAttribute("src");
  liveviewEl.classList.add("offline");
}

function updateLiveview() {
  const shouldStream = connected && !recording;
  const streaming = previewImg.hasAttribute("src");
  if (shouldStream && !streaming) startLiveview();
  else if (!shouldStream && streaming) stopLiveview();
}

previewImg.addEventListener("error", stopLiveview);

// Reflect recording state in the UI. Stills and video are mutually exclusive on
// the camera, so the capture button is disabled while recording is in progress.
function setRecording(on) {
  recording = on;
  recordBtn.textContent = on ? "Stop recording" : "Record";
  recordBtn.classList.toggle("recording", on);
  shootBtn.disabled = on;
  updateLiveview();
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    statusEl.textContent = s.connected ? `Connected: ${s.model}` : "No camera connected";
    statusEl.className = "status " + (s.connected ? "connected" : "offline");
    if (s.connected && !wasConnected) loadSettings();
    connected = s.connected;
    setRecording(s.connected && s.recording);  // also reconciles the liveview
    wasConnected = s.connected;
  } catch {
    statusEl.textContent = "Server unreachable";
    statusEl.className = "status offline";
    connected = false;
    updateLiveview();
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
