# Frontend

`web/` is the entire client: a single static page served by `app.py`'s
`StaticFiles` mount. There is no build step, no framework, and no dependencies —
just hand-written HTML, CSS, and vanilla JS that the Pi serves as-is. It's built
for one usage pattern: a phone joined to the Pi's own WiFi AP, pointed at
`http://10.42.0.1:8080` (see **`README.md`** for the device-side setup).

The frontend holds **no camera-specific knowledge**. It renders whatever the
backend reports and posts changes back; the settings panel is built entirely from
the widget descriptors the API returns, so plugging in a different camera rebuilds
the UI with no code change. The API surface it consumes is defined in **`app.md`**
(the HTTP table) and the descriptor shape it renders comes from `gp2._describe`,
documented in **`camera.md`**.

## Files

- **`index.html`** — the page shell. A single `<main>` with the elements the
  script drives by `id`: `#status` (connection line), `#liveview` (the preview
  box, wrapping the `#preview` `<img>` the MJPEG stream feeds), `#telemetry` (the
  read-only battery/storage/lens chip strip), `#focus` (the
  focus control group: `#af` plus `#focusNear`/`#focusStep`/`#focusFar`), `#shoot`
  (Capture button), `#record` (Record/Stop button), `#result` (last-action
  feedback), and `#settings` (the dynamic settings panel). The liveview sits
  directly above the telemetry strip, the focus group, and the two buttons. Loads `style.css` in `<head>` and
  `script.js` at the end of `<body>`. The favicon is a `data:,` no-op so the
  browser doesn't fire a 404 for `/favicon.ico`.
- **`script.js`** — all behavior: status polling, the liveview stream, the
  capture/record and focus controls, telemetry polling, and dynamic settings
  rendering.
- **`style.css`** — styling only; no logic. Mobile-first, single-column, and
  theme-aware.

## `script.js`

### The `api()` helper

Every backend call goes through one wrapper:

```js
async function api(url, opts) { ... }   // fetch → throw on !ok → return parsed JSON
```

On a non-2xx response it throws an `Error` carrying the backend's `detail` field
(FastAPI's `HTTPException` body) when present, falling back to the HTTP status
text. That means the 503/400/409 responses described in **`app.md`** surface to
the user as readable messages rather than silent failures — callers just
`try/catch` and drop `e.message` into `#result`.

### Status polling loop

`refreshStatus()` runs once on load and then every **5s** (`setInterval`). It
`GET`s `/api/status` and:

- updates `#status` to `Connected: <model>` or `No camera connected`, with a
  matching CSS class for color;
- on a **transition** into the connected state (tracked by the `wasConnected`
  flag), calls `loadSettings()` *and* `loadTelemetry()` — so both panels populate
  the moment a camera is plugged in, without re-fetching on every poll;
- reflects the camera's `recording` flag into the UI via `setRecording()`;
- on a thrown error (server unreachable), shows `Server unreachable` and marks
  the status offline.

This poll is the entire "liveness" mechanism: because `/api/status` never touches
hardware (see **`app.md`**), it's cheap to hit every 5s, and it's what makes the
UI recover on its own after the backend self-heals a dropped USB connection.

### Telemetry strip

`loadTelemetry()` `GET`s `/api/telemetry` and renders the read-only
battery/storage/lens fields as compact `#telemetry` chips (`renderTelemetry`
drops any chip whose value is `null`/empty). Unlike status, telemetry *does* touch
hardware — each read takes the camera lock for a `get_config()` — so it polls on a
**gentler 15s cadence** and `loadTelemetry()` early-returns when disconnected or
recording, leaving the lock to the movie session. The strip is cleared on
disconnect; on a transient read failure it keeps the last-known chips rather than
blanking.

### Liveview stream

The preview is an **MJPEG stream**, not a poll loop: pointing `#preview`'s `src`
at `/api/liveview` opens one long-lived `multipart/x-mixed-replace` connection
that the browser decodes frame by frame, swapping the `<img>` in place. Clearing
`src` tears the connection down (which also signals the server generator to stop,
via `request.is_disconnected()`).

`updateLiveview()` reconciles the stream toward a desired on-state of **`connected
&& !recording`** and is idempotent — it only opens a stream when one isn't already
running and only tears down when one is, so it's safe to call on every 5s status
poll without restarting a healthy feed. It's invoked from `setRecording()` (so
starting/stopping a recording toggles the preview) and from `refreshStatus()`'s
error path (server unreachable → stop). The stream is deliberately **off while
recording**: the backend refuses `preview()` mid-recording to avoid extra PTP
traffic on the bus while the movie is rolling (see **`camera.md`**), so the client
doesn't request frames it would only get errors for. A stream error (camera
hiccup, or the server generator ending when the camera drops) fires the `<img>`
`error` handler, which clears `src`; the next status poll re-opens it if the
camera is still connected — the same self-healing pattern the settings panel and
status line already rely on. `startLiveview()` appends a `?t=<now>` cache-buster
so re-opening always starts a fresh stream rather than a cached/aborted one.

### Capture and Record buttons

Both follow the same shape: disable the button, write a pending message to
`#result`, `await` the POST, write success/failure, and re-enable in a `finally`.

- **`#shoot`** → `POST /api/capture`. On success, `Shot taken ✓`.
- **`#record`** → toggles on the client-side `recording` flag: `POST
  /api/record/start` when not recording, `/api/record/stop` when recording. The
  response's `recording` field is fed back through `setRecording()`, keeping the
  button label/state authoritative from the server rather than assumed.

`setRecording(on)` also **disables `#shoot` while recording** — stills and video
are mutually exclusive on the body, so the UI enforces that up front instead of
waiting for the backend's 409. (The 409 still exists as the real guard; this is
just a nicer front door.)

> **Deploy note — endpoint naming and content blockers.** The `/api/record/*`
> paths collide with tracker/ad filter lists (e.g. EasyPrivacy, which targets
> session-*record*ing analytics endpoints). Brave Shields and uBlock-style
> blockers can cancel those requests **client-side** — the button then looks dead
> with *nothing in the server log*, since the request never leaves the browser
> (`net::ERR_BLOCKED_BY_CLIENT`). Capture is unaffected. If you rename these
> routes, avoid `record`/`track`/`analytics`-style tokens, and update both
> `app.py` and the fetch URLs here.

### Focus controls

Sits right under the liveview (where you're framing) and is the client half of
the `/api/autofocus` and `/api/focus` routes:

- **`#af`** → `POST /api/autofocus`, a one-shot autofocus.
- **`#focusNear` / `#focusFar`** → `POST /api/focus` with `{steps}` from the
  shared `driveFocus(steps)` helper. Sign encodes direction (near = negative,
  far = positive) and the `#focusStep` `<select>` (Fine/Med/Coarse → `1`/`3`/`6`,
  within the body's `-7..7` `manualfocus` range) sets magnitude, so `focusNear`
  sends `-step` and `focusFar` sends `+step`.

Unlike `#shoot`, these are **not** disabled while recording — the backend permits
focus writes mid-take (rack focus), so the UI leaves them live. Like the other
buttons they don't self-disable on disconnect; a click while offline just surfaces
the backend's 503 in `#result`.

Because the backend may switch the body's focus mode to reach the motor (AF needs
an AF mode, the nudge needs Manual — see **`camera.md`**), a focus action can leave
the settings panel's `focusmode` row stale. On success both handlers call
`scheduleSettingsRefresh()`, a **debounced** (`400ms`) `loadSettings()` — a burst
of nudges coalesces into one reload, and it's a *generic* settings re-fetch rather
than reaching in to patch a named widget, so the frontend keeps its no-camera-
knowledge invariant. (The endpoints also return the effective `focusmode`, but the
client ignores it for exactly that reason — using it would mean hardcoding the
widget name here.)

### Dynamic settings rendering

This is the camera-agnostic core. `loadSettings()` `GET`s `/api/settings` and
hands the descriptor list to `renderSettings()`, which clears `#settings` and
builds one labeled row per descriptor. Each descriptor's `type` selects a builder
from the `settingRenderers` map:

| descriptor `type` | control built | fires `apply` on | value sent |
|---|---|---|---|
| `choice` | `<select>` of `choices` | `change` | selected string |
| `toggle` | on/off `<button>` | `click` | `0` / `1` |
| `range` | `<input type=range>` + live `<output>` | `change` (commit) | slider value |
| `text` | `<input type=text>` | `change` | text |

An unknown `type` is skipped, so a new backend widget kind degrades to "not shown"
rather than a broken row. The `range` control updates its `<output>` on every
`input` event for live feedback but only `apply`s on `change`, so the backend
isn't hammered with a write per pixel of slider drag.

Every control's change handler calls `applySetting(name, value)`, which `POST`s to
`/api/settings/{name}` with `{value}`. Crucially, the backend responds with the
**full, re-read settings list**, and `applySetting` renders that response — so if
changing one setting shifts another as a side effect (e.g. aperture limits moving
with ISO), the whole panel reflects reality after every edit. A failed write lands
in `#result` as `Setting failed: <detail>` and leaves the panel as-is.

## `style.css`

Pure presentation, a few intentional choices worth noting:

- **`color-scheme: light dark`** plus `system-ui` fonts and `currentColor`
  borders — the page follows the phone's light/dark setting for free, with no
  media queries. Only three explicit accent colors exist: a green `--ok` (custom
  property) for connected/on states and a red for offline/recording.
- **Mobile-first, thumb-sized targets** — full-width buttons with generous
  padding, `max-width: 22rem` centered column. `button:active { transform:
  scale(.98) }` gives tactile press feedback on touch.
- **State is driven by classes/attributes the script toggles**:
  `.status.connected`/`.status.offline`, `#record.recording` (turns the button
  red), `button:disabled` (dims it), and `.toggle[data-on="1"]` (green when on).
  The CSS never queries the DOM — it just styles whatever state `script.js` has
  set, keeping the two loosely coupled.
- The `range` `<output>` uses `font-variant-numeric: tabular-nums` so the live
  value doesn't jitter horizontally as digits change.
- **`.liveview`** is a fixed `aspect-ratio: 3 / 2` box (matching the α7 IV
  sensor/liveview frame, so the JPEG fills it without letterboxing) with a black
  background; the `<img>` uses `object-fit: contain`. When the script adds the
  `.offline` class (no camera, or recording), the image is hidden and a `"no
  preview"` label shows via `::after` — the same class-driven, script-decides /
  CSS-styles split used everywhere else.
