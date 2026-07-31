# Hardware dumps

Verbatim `gphoto2` output captured off real bodies. `tests/fakes/dump.py` parses
these into `FakeWidget` trees so tests assert against what a camera actually
publishes instead of against a hand-written guess.

| File | Body | Captured |
|---|---|---|
| `ilce_7m4.txt` | Sony ILCE-7M4 — `Sony Alpha-A7 IV (PC Control)`, firmware 4.00 | 2026-07-30 |

These are **inputs, not expectations**. Don't hand-edit them. If the tree looks
wrong, re-capture.

## Why this exists

`camera/sony.py` addresses widgets by name, and a wrong name is invisible until
it reaches hardware — `get_single_config` raises `[-2] Bad parameters` at
runtime, and nothing earlier catches it.

The a7 IV's AF-point quirk was `changeafarea` (a Canon EOS name) for months. The
hand-written fake published a `changeafarea` widget *because the quirk table
asked for one*, so `/api/afpoint` had green tests and could never have worked.
A fake built from the quirk table cannot falsify the quirk table.

## Adding a camera

Run this on the Pi, with the body connected, awake, and in PC Remote mode:

```bash
./tools/camera-dump.sh              # auto-name from the model
./tools/camera-dump.sh nikon_z6.txt # name it yourself
KEEP_SERIAL=1 ./tools/camera-dump.sh
```

That is the whole workflow. The script stops the `pathfinder` service (it holds
the USB claim, and `gphoto2` would otherwise fail with `Could not claim the USB
device`), captures the dump, redacts the serial number, names the file after the
PTP model, and restarts the service on every exit path including Ctrl-C.

Run it from anywhere — paths resolve against the script, not the working
directory. If the executable bit didn't survive the SFTP sync to the Pi,
`bash tools/camera-dump.sh` works just as well.

Commit the result and the suite covers it on the next run. **There is nothing to
register.** Discovery reads the model out of each file's `Abilities for camera`
header, so `EveryDumpedBodyMatchesItsQuirks` picks up new bodies automatically
and the filename is cosmetic.

A dump that no vendor module in `gp2.VENDORS` claims is reported as a skip
naming the model — that is the signal to write a quirks module, not a failure.

### Naming, and re-capturing

Re-capturing a body that already has a fixture **replaces that file in place**,
whatever it is called, rather than creating a second one under the auto-generated
name. That is the refine-and-replace path: run the script again, get a fresh
dump, same filename, review it as a diff. A genuinely new body is named after its
PTP model (`ILCE-7M4` → `ilce_7m4.txt`), falling back to the driver's model
string. Two dumps of one body is a state the suite rejects, so this matters.

### When it goes wrong

Three behaviours are deliberate, and each exists because the obvious version is
wrong:

- **The service is restored on every exit path**, including failure and Ctrl-C.
  Leaving it stopped would take the whole device offline for a typo.
- **`INT`/`TERM` exit rather than running a handler and continuing.** A trap that
  restores the service without exiting would restart `pathfinder` *mid-dump* and
  then carry on, with the service and `gphoto2` both reaching for the USB claim.
  So cleanup hangs off `EXIT` alone and the signal traps just `exit`.
- **A failed capture keeps its raw output** at `/tmp/camera-dump-failed.txt`.
  The temp file is deleted by the `EXIT` trap, so an error message naming it
  would otherwise point at a path that no longer exists — precisely when the
  output is needed.

A failure never touches the existing fixture.

## What's in a dump, and what isn't

Four `gphoto2` actions in **one invocation** — each run re-inits the camera, and
repeated init cycles are what provoke the `[-52]` USB re-enumeration this body is
prone to:

| Section | Used by | For |
|---|---|---|
| `--list-all-config` | the parser | the widget tree: names, types, choices, ranges, readonly |
| `--abilities` | humans | capture/file/folder operations, which are *not* in the config tree |
| `--summary` | humans | PTP property codes and their rw flags |
| `--list-config` | humans | the flat path list, for skimming |

The parser only reads `--list-all-config` blocks (a `/main/...` path followed by
`Label:`); that test is what makes it skip the flat listing. The rest is kept
because it answers questions the config tree can't — `--abilities` is the only
place that says `Trigger Capture` is supported, for instance.

**Not included: app state.** An earlier dump of this body also carried
`curl` output from `/api/settings` and `/api/telemetry`. That was dropped — it
records what the app happened to expose on one day, which rots, and the tests
derive the same thing from the dump instead (`TheFixtureIsARealDump` computes the
settable surface via `gp2._settable_widgets`). A fixture should hold hardware
truth only.

**Not included: the serial number.** It identifies a specific physical camera and
nothing reads it. Redacted in two places — the `--summary` line and the
`Current:` of any widget whose path ends in `serialnumber`.
