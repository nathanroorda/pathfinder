# Logging Architecture

## Components

- **`log.py`** — one function, `configure_logging()`, called once at the top
  of `run.py` before anything else is imported. It calls
  `logging.basicConfig()`, which sets up the **root logger**: a single
  `StreamHandler` writing to stdout, formatted as
  `%(asctime)s %(levelname)-8s %(name)s: %(message)s`.
- **Per-module loggers** — `app.py` and `camera/gp2.py` each get their own
  logger via `logging.getLogger(__name__)` and log through it
  (`log.info(...)`, `log.warning(...)`, `log.debug(...)`). These loggers have
  no handlers of their own; log records propagate up to the root logger's
  handler configured in `log.py`, which is what actually writes them out.
- **`run.py`** disables uvicorn's own logging config (`log_config=None`) so
  uvicorn's request/access logs also flow through the same root logger
  instead of uvicorn setting up a separate, conflicting handler.

There is no file handler anywhere in the app. All log output goes to stdout
only.

## Level control

Log level is set once, at process start, via an environment variable:

```
PATHFINDER_LOG_LEVEL=DEBUG|INFO|WARNING|ERROR   (default: INFO)
```

Read in `log.py`, converted to a `logging` level constant, and passed to
`basicConfig(level=...)`. Invalid/unset values silently fall back to `INFO`.
Because it's read once at startup, changing it requires a process restart —
there's no live reload.

## How stdout becomes durable: systemd + journald

Pathfinder runs as the `pathfinder` systemd service (installed by
`setup.sh`). systemd captures a managed service's stdout/stderr by default
and feeds it into `journald` — that's the *only* reason logs are visible
after the app leaves the foreground. Nothing in the app itself writes to a
log file or the journal directly; it's purely systemd's process supervision
picking up the stream.

```
app (stdout) → systemd (captures service stdout/stderr) → journald → journalctl
```

## Persistence caveat

`setup.sh` does not configure `journald` storage. journald's default
(`Storage=auto` in `/etc/systemd/journald.conf`) means:

- If `/var/log/journal/` exists → logs persist across reboots.
- If it doesn't (the case on a freshly-imaged Raspberry Pi OS) → logs go to
  `/run/log/journal/` (tmpfs) and are **wiped on every reboot/power cycle**.

Check which mode a given device is in:

```bash
journalctl --list-boots        # more than one boot listed = persisting already
ls -ld /var/log/journal 2>/dev/null && echo persistent || echo volatile-only
```

Make it persistent:

```bash
sudo mkdir -p /var/log/journal
sudo systemd-tmpfiles --create --prefix /var/log/journal
sudo systemctl restart systemd-journald
```

Worth doing on any device that runs off battery/USB power in the field,
since an unexpected power loss otherwise takes the logs with it. Not yet
wired into `setup.sh`.

## Viewing logs

```bash
journalctl -u pathfinder -f            # follow live
journalctl -u pathfinder -n 100        # last 100 lines
journalctl -u pathfinder --since today
journalctl -u pathfinder -p err        # errors only
```

## Changing the level at runtime (via systemd drop-in)

```bash
sudo mkdir -p /etc/systemd/system/pathfinder.service.d
sudo tee /etc/systemd/system/pathfinder.service.d/override.conf >/dev/null <<'EOF'
[Service]
Environment=PATHFINDER_LOG_LEVEL=DEBUG
EOF
sudo systemctl daemon-reload
sudo systemctl restart pathfinder
```

A drop-in is used instead of editing the generated unit file directly
because `setup.sh` regenerates `/etc/systemd/system/pathfinder.service` on
every provisioning run and would overwrite an in-place edit.

## Saving a log sequence to a file

Since there's no file handler in the app, exporting a sequence means
capturing journalctl's output:

```bash
journalctl -u pathfinder --since "10 min ago" > pathfinder.log
journalctl -u pathfinder -f | tee pathfinder.log     # live + save simultaneously
```

## Known limitations

- Volatile-by-default persistence (see above) unless manually configured
  per device.
- No log rotation/size cap configured beyond journald's own defaults —
  worth setting `SystemMaxUse=` in `journald.conf` if verbose `DEBUG`
  logging is left on for extended periods, to bound flash usage on the SD
  card.
- No file-based logging path independent of systemd — if the app is ever
  run outside systemd (e.g. directly via `python run.py` during
  development), logs only go to the terminal and aren't captured anywhere.
