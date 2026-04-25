# installer/

Top-level PrepperPi installer. Pure bash, idempotent, safe to re-run.

## What it does

1. **Preflight**: verifies the hardware is a Raspberry Pi 4 or 5 and the OS is Debian Bookworm (12) or later. Exits with a clear message on anything else.
2. **Confirms with the operator** that it's OK to reboot the device when installation finishes. If stdin isn't a terminal (e.g. you're piping `curl | bash`), pass `--yes` to skip the prompt or `--no-reboot` to stay in place.
3. **Logs every step** to `/var/log/prepperpi/install.log` via `tee`. Nothing is uploaded anywhere.
4. **Creates the `prepperpi` service account** (`useradd --system`) if it doesn't already exist.
5. **Creates the state tree** at `/srv/prepperpi/{zim,maps,media,user-usb,config,cache,backups}`, owned by `prepperpi:prepperpi`. Existing content is preserved.
6. **Runs each `services/*/setup.sh`** in a fixed order:
   1. `prepperpi-ap` — Wi-Fi access point + DHCP/DNS
   2. `prepperpi-events` — shared event-emitter helper used by later services
   3. `prepperpi-web` — Caddy front door + landing page
   4. `prepperpi-admin` — FastAPI/uvicorn admin console
   5. `prepperpi-aria2c` — aria2c download daemon for the catalog page (E2-S3); installed after admin so it can chown its RPC secret to the `prepperpi-admin` group
   6. `prepperpi-kiwix` — kiwix-serve + ZIM library indexer
   7. `prepperpi-usb` — USB hot-plug mounting + Markdown renderer

   Each per-service script is responsible for apt installs, rendering systemd units, enabling services, etc.
7. **Reboots** into AP mode.

## Usage

From a fresh Raspberry Pi OS Lite (64-bit, Bookworm or Trixie) install:

```bash
git clone https://github.com/jmarler/prepperpi.git
cd prepperpi
sudo installer/install.sh
```

The script asks for confirmation once at the top (it will reboot at the end), then runs unattended. Expect ~5–15 minutes depending on network speed and Pi model.

### Flags

| Flag | Behavior |
|---|---|
| `--yes` / `-y` | Skip the confirmation prompt. Required for `curl | bash` or other non-interactive runs. |
| `--no-reboot` | Install everything but don't reboot at the end. Useful for testing. |
| `--help` / `-h` | Print the usage comment and exit. |

### Re-running

`install.sh` is idempotent. On a second run:

- The preflight still runs. Still passes.
- The `prepperpi` user and `/srv/prepperpi` tree are left alone.
- Each `services/*/setup.sh` re-executes — apt is a no-op, systemd unit files are re-written in place, `systemctl enable` is idempotent, and **each daemon is `systemctl restart`ed at the end** so the new code is active when the installer exits. No reboot required for re-installs over SSH.
- If `--yes` isn't passed, you'll be asked to confirm the reboot again.

Use this to pick up new services after pulling updates from the repo.

## Logs

Full install log: `/var/log/prepperpi/install.log`. Appended per-run; oldest-first. No telemetry, no upload.

If something goes wrong, the log plus `sudo journalctl -u 'prepperpi-*'` is the entire debugging surface.

## Adding a new service

1. Drop a `services/<name>/setup.sh` that's idempotent and executable.
2. Add `<name>` to the `SERVICE_ORDER` array in `install.sh` at the correct position. Dependency rule of thumb: `prepperpi-ap` first (network), then `prepperpi-events` (the event-emitter helper that later services call), then `prepperpi-web` (which seeds `/opt/prepperpi/web/landing/`), then any service that writes fragment files into the landing root or proxies through Caddy.
3. The installer logs a WARN if it finds a `services/*/setup.sh` that isn't in `SERVICE_ORDER`, so you get caught if you forget step 2.
