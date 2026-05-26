# Contributing

Thanks for taking the time to contribute!

## Reporting issues

- Use the [issue tracker](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues) and pick the matching template.
- Include camera model + hardware version (Gen1 `INDOOR`/`OUTDOOR`, Gen2 `HOME_Eyes_Indoor`/`HOME_Eyes_Outdoor`), HA core version, and integration version (`manifest.json`).
- For bugs, attach DEBUG-level logs filtered to `custom_components.bosch_shc_camera`.

## Pull requests

1. Fork the repo and create a feature branch.
2. Keep changes focused — one logical change per PR.
3. Add or update tests in `tests/` for behavioral changes.
4. Run the existing test suite locally before opening the PR.
5. Update `CHANGELOG.md` under a new `## [Unreleased]` section (or the target version).

## Developer Certificate of Origin (DCO)

This project uses the [Developer Certificate of Origin](https://developercertificate.org/) instead of a Contributor License Agreement. By signing off on a commit, you certify that you wrote the code (or have the right to submit it) under the project's MIT license.

**Every commit in a PR must be signed off.** This is enforced by the `DCO` GitHub Action.

Add the sign-off automatically with `-s`:

```bash
git commit -s -m "fix: short description"
```

This appends a trailer line to the commit message:

```
Signed-off-by: Your Name <your.email@example.com>
```

The name + email must match your `git config user.name` / `user.email`.

### Fixing missing sign-offs

If the DCO check fails on your PR:

- **One commit:** `git commit --amend -s --no-edit && git push --force-with-lease`
- **Multiple commits:** `git rebase --signoff main && git push --force-with-lease`

### Set sign-off as default

Save typing `-s` every time:

```bash
git config --global format.signOff true
```

## Code style

- Python: follow the existing style in the codebase (no formal linter pinned yet — match what's around).
- One file, one purpose. Avoid spreading unrelated changes across multiple files in the same PR.
- Prefer adding regression tests for any bug fix.

## Releases

Releases are cut by the maintainer. Bump `manifest.json` version + add a `CHANGELOG.md` entry as part of the release commit (separate from feature PRs).
