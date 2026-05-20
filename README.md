# sb-presets

Sound Blaster preset TOML files and preset logos.

This repository is asset-only. It does not contain Rust code or publish a crate.

## Layout

- `assets/presets/*.toml`
- `assets/logos/*.png`

## Bundle

Every push to `master` validates the assets, builds a zip bundle, publishes it as
a GitHub Release asset, and uploads the same files to WebDAV.

The bundle format is:

```text
sb-presets-<build>.zip
  manifest.json
  presets/*.toml
  logos/*.png
```

`<build>` is the total commit count in the current branch:

```powershell
git rev-list --count HEAD
```

The workflow also writes `latest.json` with the build number, bundle URL,
SHA-256, asset counts, and commit hash.
