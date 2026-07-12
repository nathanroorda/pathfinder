/* Interactive exposure triangle.
 * Reads the camera's real iso / f-number / shutterspeed choices, lets the user
 * step each in stops, and (with Lock on) auto-compensates another axis to hold
 * total exposure constant. Every change is written to the camera via the API.
 *
 * Light is measured in "stops" (a doubling = +1). Higher stop = brighter:
 *   ISO       stop = log2(iso / 100)
 *   shutter   stop = log2(seconds)          (longer = brighter)
 *   aperture  stop = -2 * log2(f-number)    (smaller f = brighter)
 */
(function () {
  const root = document.getElementById("triangle");
  if (!root) return;

  const AXES = ["iso", "f-number", "shutterspeed"];
  const NAME = { iso: "ISO", "f-number": "Aperture", shutterspeed: "Shutter" };
  const VERT = { iso: [150, 26], "f-number": [34, 214], shutterspeed: [266, 214] };

  const toStop = {
    iso: (l) => { const m = String(l).match(/(\d+)/); return m ? Math.log2(+m[1] / 100) : null; },
    "f-number": (l) => { const m = String(l).match(/([\d.]+)/); const f = m ? +m[1] : 0; return f > 0 ? -2 * Math.log2(f) : null; },
    shutterspeed: (l) => {
      const s = String(l).trim();
      let sec;
      if (s.includes("/")) { const [a, b] = s.split("/").map(Number); sec = a / b; }
      else sec = parseFloat(s.replace(/[^\d.]/g, ""));
      return isFinite(sec) && sec > 0 ? Math.log2(sec) : null;
    },
  };

  const state = {};      // axis -> { value, scale:[{label,stop}], index, missing }
  let baseline = null;   // total light that defines "balanced"

  async function api(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  }

  function buildScale(axis, choices) {
    return choices
      .map((label) => ({ label, stop: toStop[axis](label) }))
      .filter((c) => c.stop !== null)
      .sort((a, b) => a.stop - b.stop);
  }

  function nearestIndex(scale, stop) {
    let best = 0, bestD = Infinity;
    scale.forEach((c, i) => { const d = Math.abs(c.stop - stop); if (d < bestD) { bestD = d; best = i; } });
    return best;
  }

  // Adopt a fresh settings list (from our own fetch or another panel's change).
  function sync(items) {
    const byName = Object.fromEntries(items.map((s) => [s.name, s]));
    for (const axis of AXES) {
      const d = byName[axis];
      if (!d || !d.choices) { state[axis] = { missing: true }; continue; }
      const scale = buildScale(axis, d.choices);
      let index = scale.findIndex((c) => c.label === d.value);
      const autoValue = index < 0;               // e.g. "Auto ISO" — off the numeric scale
      if (index < 0) index = Math.floor(scale.length / 2);
      state[axis] = { value: d.value, scale, index, autoValue };
    }
    if (baseline === null) baseline = totalLight();
    render();
  }

  function totalLight() {
    let sum = 0;
    for (const axis of AXES) {
      const s = state[axis];
      if (s && s.scale && s.scale[s.index]) sum += s.scale[s.index].stop;
    }
    return sum;
  }

  const compSelect = () => root.querySelector("#tri-comp");
  const lockCheck = () => root.querySelector("#tri-lock");

  function compAxisFor(changed) {
    let comp = compSelect() ? compSelect().value : "shutterspeed";
    if (comp === changed || !state[comp] || state[comp].missing) {
      comp = AXES.find((a) => a !== changed && state[a] && !state[a].missing);
    }
    return comp;
  }

  async function writeAxis(axis, index) {
    const s = state[axis];
    index = Math.max(0, Math.min(s.scale.length - 1, index));
    const label = s.scale[index].label;
    s.index = index; s.value = label; s.autoValue = false;
    render();
    const items = await api(`/api/settings/${encodeURIComponent(axis)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: label }),
    });
    window.dispatchEvent(new CustomEvent("pf:settings", { detail: items }));
  }

  async function step(axis, dir) {
    const s = state[axis];
    if (!s || s.missing) return;
    const target = s.index + dir;
    if (target < 0 || target >= s.scale.length) return;
    const delta = s.scale[target].stop - s.scale[s.index].stop;   // stops of light added
    try {
      await writeAxis(axis, target);
      if (lockCheck() && lockCheck().checked) {
        const comp = compAxisFor(axis);
        if (comp) {
          const cs = state[comp];
          const want = cs.scale[cs.index].stop - delta;            // cancel the change
          const ci = nearestIndex(cs.scale, want);
          if (ci !== cs.index) await writeAxis(comp, ci);
        }
      }
    } catch (e) {
      note(`Couldn't set ${NAME[axis]}: ${e.message}`);
    }
  }

  function note(msg) {
    const n = root.querySelector("#tri-note");
    if (n) n.textContent = msg || "";
  }

  // --- rendering ----------------------------------------------------------
  function render() {
    const ev = totalLight() - (baseline ?? totalLight());
    const balanced = Math.abs(ev) < 0.17;                          // within ~1/6 stop
    const evTxt = (ev >= 0 ? "+" : "") + ev.toFixed(1) + " EV";
    const dotColor = balanced ? "var(--ok)" : (ev > 0 ? "var(--warm)" : "var(--cool)");

    const vertSvg = AXES.map((axis) => {
      const [x, y] = VERT[axis];
      const s = state[axis];
      const val = s && !s.missing ? (s.value || "—") : "n/a";
      const ty = axis === "iso" ? y - 8 : y + 22;
      return `
        <circle cx="${x}" cy="${y}" r="6" fill="${s && !s.missing ? "currentColor" : "var(--muted)"}"/>
        <text x="${x}" y="${axis === "iso" ? y - 16 : y + 16}" text-anchor="middle" class="tri-axis">${NAME[axis]}</text>
        <text x="${x}" y="${ty}" text-anchor="middle" class="tri-val">${val}</text>`;
    }).join("");

    root.querySelector("#tri-svg").innerHTML = `
      <polygon points="150,26 34,214 266,214" class="tri-edge"/>
      <circle cx="150" cy="151" r="16" fill="${dotColor}" opacity="0.9"/>
      <text x="150" y="156" text-anchor="middle" class="tri-ev">${evTxt}</text>
      ${vertSvg}`;

    // steppers
    root.querySelectorAll(".tri-row").forEach((row) => {
      const axis = row.dataset.axis;
      const s = state[axis];
      const label = row.querySelector(".tri-row-val");
      const dn = row.querySelector('[data-dir="-1"]');
      const up = row.querySelector('[data-dir="1"]');
      if (!s || s.missing) {
        label.textContent = "not adjustable in this mode";
        dn.disabled = up.disabled = true;
      } else {
        label.textContent = s.value + (s.autoValue ? " (auto)" : "");
        dn.disabled = s.index <= 0;
        up.disabled = s.index >= s.scale.length - 1;
      }
    });
  }

  function shell() {
    root.innerHTML = `
      <h2 class="tri-title">Exposure Triangle</h2>
      <svg id="tri-svg" viewBox="0 0 300 240" class="tri-canvas" aria-label="exposure triangle"></svg>
      <div class="tri-controls">
        <label class="tri-lock">
          <input type="checkbox" id="tri-lock" checked> Lock exposure
        </label>
        <label class="tri-compwrap">compensate with
          <select id="tri-comp">
            <option value="shutterspeed">Shutter</option>
            <option value="iso">ISO</option>
            <option value="f-number">Aperture</option>
          </select>
        </label>
      </div>
      ${AXES.map((axis) => `
        <div class="tri-row" data-axis="${axis}">
          <span class="tri-row-name">${NAME[axis]}</span>
          <span class="tri-row-val">…</span>
          <span class="tri-steppers">
            <button data-dir="-1" title="darker">−</button>
            <button data-dir="1" title="brighter">＋</button>
          </span>
        </div>`).join("")}
      <p id="tri-note" class="tri-note"></p>`;

    root.querySelectorAll(".tri-row").forEach((row) => {
      const axis = row.dataset.axis;
      row.querySelector('[data-dir="-1"]').addEventListener("click", () => step(axis, -1));
      row.querySelector('[data-dir="1"]').addEventListener("click", () => step(axis, 1));
    });
    // Re-baseline when the user explicitly rebalances via the header dot.
    root.querySelector("#tri-svg").addEventListener("dblclick", () => { baseline = totalLight(); render(); });
  }

  // Stay in sync with the dropdown panel and vice versa.
  window.addEventListener("pf:settings", (e) => sync(e.detail));

  shell();
  api("/api/settings").then(sync).catch(() => note("camera not connected"));
})();