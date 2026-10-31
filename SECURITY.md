# Security Policy

## Supported Versions

Only the latest release receives security fixes.

## Reporting a Vulnerability

Please report security issues **privately** via GitHub:

1. Open the **Security** tab of this repository
2. Click **Report a vulnerability**
3. Describe the issue, affected version/commit, and reproduction steps

**Do not open a public issue or pull request for security problems.**

This project is maintained by a single author; responses are
best-effort. The project follows coordinated disclosure and will publish a
GitHub Security Advisory once a fix is available. Reporters are
credited unless they prefer to remain anonymous.

## Deploying releases safely (operators)

`infra/update.sh` and `infra/install.sh` check out the newest `vX.Y.Z` tag of
your clone's `origin` and run that release's `install.sh` as root, so whoever
can push a tag there can run code as root on your server.

- Keep `origin` on `https://` or `ssh`; both scripts warn before deploying from
  an `http://`, `git://` or `ftp://` origin.
- Run the scripts from your own clone, not from `/opt/wsi` (they refuse that).
- To require a signature, import the maintainer's public key into the keyring
  of the user you run `sudo` from and set `WSI_VERIFY_RELEASE_TAGS=1`; a tag
  without a valid signature then stops the update before anything changes.
  Any key in that keyring counts unless you pin one with
  `WSI_RELEASE_SIGNING_KEY=<fingerprint>`. This
  only helps once releases are tagged with `git tag -s`; check with
  `git tag -v vX.Y.Z` before turning it on.
- Maintainers: restrict who may create `v*` tags with a GitHub tag ruleset
  (Settings → Rules) and keep release tags immutable.
