# Security & data hygiene

## Reporting a vulnerability

Please open a **private** security advisory at
<https://github.com/V4n1X/ha-oclean-integration/security/advisories/new>
or email the maintainer instead of opening a public issue.

## What this repository must never contain

This is a **local-only** integration: it talks to the toothbrush over Bluetooth
and never contacts an Oclean server. The repository is therefore expected to be
free of every kind of personal or credential data. The following must **never**
be committed:

| Category | Examples |
|---|---|
| Credentials | Oclean account e-mail/password, `accessToken`, `refreshToken`, JWT strings |
| Keys | private keys (`*.pem`, `id_rsa*`), keystores (`*.jks`, `*.keystore`), service-account JSON |
| Home Assistant secrets | `secrets.yaml`, `.storage/`, database files |
| Personal device data | your own MAC address, `oclean_ble.log` dumps, captured BLE session hex from a real brush |
| Environment files | `.env`, `.env.*` |

`tools/oclean_api_test.py` and `tools/oclean_scheme_map.py` talk to the Oclean
cloud **only** to help research; they read the account password from an
interactive prompt / CLI argument and cache the resulting token in
`tools/.oclean_token.json`, which is git-ignored.

## How this is enforced

1. `.gitignore` blocks the obvious files and directories.
2. `.github/workflows/secret-scan.yml` runs on every push and pull request and
   fails the build if
   - a credential-looking file is tracked,
   - a hard-coded private key / token pattern appears in the source, or
   - a log file or captured-session directory is tracked.
   It also runs [gitleaks](https://github.com/gitleaks/gitleaks) over the full
   git history.
3. Nothing in `custom_components/oclean_ble/` contains a network client for the
   Oclean cloud – only the BLE stack is used.

## If a secret was committed by accident

1. Rotate the credential immediately (the Oclean account password / session).
2. Remove it from the working tree **and** from history
   (`git filter-repo` or the GitHub "remove sensitive data" flow) – deleting it
   in a follow-up commit is not enough.
3. Force-push the rewritten branch and tell anyone who has a clone.

## Scope of the integration

* No cloud connection, no account, no telemetry.
* The only external input is the BLE advertisement (`local_name: Oclean*` /
  service UUID `8082caa8-41a6-4021-91c6-56f9b954cc18`).
* Debug logs written to `<config_dir>/oclean_ble.log` contain BLE payload hex and
  the device MAC address – treat that file as personal data before sharing it in
  an issue.
