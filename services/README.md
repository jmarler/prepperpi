# services/

One subdirectory per systemd-managed service. Each subdirectory contains:

- A `setup.sh` that the top-level installer calls.
- Config files (hostapd.conf, Caddyfile, etc.).
- The `.service` unit(s) to drop into `/etc/systemd/system/`.
- A `README.md` describing what this service does and how to test it in isolation.
