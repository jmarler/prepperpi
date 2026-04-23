# Contributing to PrepperPi

Thanks for thinking about contributing. PrepperPi is a small project with a big surface area, and help is welcome.

## Ground rules

1. **Clean-room.** PrepperPi is an independent project modeled only on the *public* descriptions of commercial offline-library devices (marketing pages, FAQs, comparison charts, public reviews) and on upstream open-source projects. Do not reference, photograph, image-dump, or paraphrase from the shipped software of any commercial device. If you are not sure whether a source is acceptable, open an issue before you write code. See [`docs/clean-room-policy.md`](docs/clean-room-policy.md) when it lands.
2. **MIT-0 in; MIT-0 out.** By opening a pull request, you release your contribution under [MIT No Attribution](LICENSE). No CLA. No attribution required downstream.
3. **Be kind.** Follow [`CODE-OF-CONDUCT.md`](CODE-OF-CONDUCT.md).

## Workflow

1. **Open an issue first** for anything non-trivial — a design change, a new content source, a new service. A three-line issue saves a 300-line PR that gets rejected.
2. **Fork and branch off `main`.** Branch names use short kebab-case: `feat/wifi-ap`, `fix/kiwix-serve-path`, `docs/clarify-licensing`.
3. **Conventional commits.** Prefix commit subjects with `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, or `ci:`. Keep subjects ≤72 characters; put the "why" in the body.
4. **One topic per PR.** Split unrelated changes.
5. **Tests and linting pass** before you ask for review. Run `make test` (once it exists) locally.
6. **Docs land with the code.** If you add a new service, add its `README.md`. If you add a new content source, update [`CONTENT-LICENSES.md`](CONTENT-LICENSES.md).

## What's helpful

- Filing clear bug reports with the Pi model, the installer version, and the output of `systemctl status prepperpi-*`.
- Testing the installer on real hardware and reporting where it breaks.
- Adding support for a new language's Wikipedia ZIM, a new map region, or a new public-domain source.
- Improving the landing page or admin SPA's accessibility.
- Writing a plain-language doc for a specific audience (teachers, NGO workers, amateur radio operators, parents).

## What's not helpful

- Adding content that isn't freely redistributable.
- Adding telemetry, phone-home, or analytics of any kind.
- "Just a small refactor" PRs that touch twelve unrelated files.
- Shipping credentials, API keys, or personal tokens. If you need to authenticate the updater to a private source, add a config hook — do not commit the secret.

## Security

If you find a security issue, please open a [private security advisory on GitHub](https://github.com/jmarler/prepperpi/security/advisories/new).
