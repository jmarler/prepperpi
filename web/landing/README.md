# web/landing

PrepperPi's captive-portal landing page. Pure HTML + CSS, no JavaScript — works on every phone, no build step, no Node on the device.

Four tiles (Library / Maps / Admin / USB) show placeholder "not installed" states. Dynamic tile generation (real ZIM / MBTiles / USB inventory) lands in E2-S1, E3-S1, E4, and E2-S2 respectively.

Deployed to `/opt/prepperpi/web/landing/` by `services/prepperpi-web/setup.sh`. Caddy serves it at `http://prepperpi.local/` on the AP subnet.

## Design constraints (from E1-S4 ACs)

- **AC-3:** Works without JavaScript. Progressive enhancement only.
- **AC-4:** Renders at 320 px width (small phones) and up. Mobile-first CSS with a single-column layout on narrow screens, 2-column at 32 rem, 4-column at 60 rem.

## Editing

Edit `index.html` / `style.css`, rerun `sudo services/prepperpi-web/setup.sh`, and `sudo systemctl restart prepperpi-web`.
