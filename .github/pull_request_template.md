## What and why

<!-- What changes, and why. Link the issue: "Closes #123". -->

## How it was checked

<!-- Tests added or changed, a live check on your own server, screenshots below for UI changes. -->

## Checklist

- [ ] `uv run ruff check src tests && uv run ruff format --check src tests && uv run pytest -q` passes
- [ ] Tests cover the new behaviour (and the refusals, for security-sensitive areas)
- [ ] Every new UI string is a catalog key with an English string in `en.json` (and `ru.json` if you can)
- [ ] UI changes: screenshots in light and dark, from `scripts/shoot.mjs`, with a clean console
- [ ] Docs updated: user guide, configuration or security, as the change needs
- [ ] No secrets or personal data in code, fixtures, screenshots or logs

## Screenshots

<!-- Light and dark, for UI changes. -->
