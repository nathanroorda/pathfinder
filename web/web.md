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

- **`index.html`** — the page shell. A single `<main>` with five elements the
  script drives by `id`: `#status` (connection line), `#shoot` (Capture button),
  `#record` (Record/Stop button), `#result` (last-action feedback), and
  `#settings` (the dynamic settings panel). Loads `style.css` in `<head>` and
  `script.js` at the end of `<body>`. The favicon is a `data:,` no-op so the
  browser doesn't fire a 404 for `/favicon.ico`.
- **`script.js`** — all behavior: status polling, the two capture buttons, and
  dynamic settings rendering.
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
  flag), calls `loadSettings()` — so the panel populates the moment a camera is
  plugged in, without re-fetching settings on every poll;
- reflects the camera's `recording` flag into the UI via `setRecording()`;
- on a thrown error (server unreachable), shows `Server unreachable` and marks
  the status offline.

This poll is the entire "liveness" mechanism: because `/api/status` never touches
hardware (see **`app.md`**), it's cheap to hit every 5s, and it's what makes the
UI recover on its own after the backend self-heals a dropped USB connection.

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
