# installer/

Pure-bash installer and helper scripts. Idempotent; safe to re-run. Detects the Pi model, installs apt dependencies, lays down systemd units, and creates `/srv/prepperpi`.

The entry point is `install.sh`. Individual service setups live in `../services/` and are sourced by the installer.
