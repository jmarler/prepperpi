# tests/

Automated tests.

- `lint/` — shellcheck, yamllint, ruff.
- `unit/` — pytest for the FastAPI admin backend.
- `integration/` — spin up the AP service in a Linux VM (qemu / vagrant), assert an attached client gets an IP and resolves the captive portal.

Run everything via `make test` (or CI's `.github/workflows/test.yml`).
