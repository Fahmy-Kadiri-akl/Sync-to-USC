# Akeyless Folder-to-USC Sync

Sync all static and/or rotated secrets under an Akeyless folder path to a Universal Secret Connector (USC) — such as AWS Secrets Manager — with a single command. Includes drift detection to identify when remote values have been changed outside of Akeyless.

## Problem

Akeyless does not natively support folder/path-level sync to a USC. Sync must be configured per-secret using `static-secret-sync` or `rotated-secret-sync`. For customers managing hundreds or thousands of secrets, this creates significant operational overhead.

## Solution

`sync-folder-to-usc.sh` wraps the Akeyless CLI to provide folder-level sync by:

1. Listing all secrets under a given path via `list-items`
2. Checking each secret for an existing sync association via `describe-item`
3. Creating new sync associations or re-syncing existing ones via `static-secret-sync` / `rotated-secret-sync`
4. Optionally detecting value drift between Akeyless and the remote secret store

## Prerequisites

- **Akeyless CLI** installed and authenticated (`akeyless configure` or `akeyless auth`)
- **Gateway URL** configured in the CLI profile (`akeyless configure --gateway-url <url>`) — required for drift detection via `usc get`
- **USC** already created and linked to a target (e.g., AWS Secrets Manager via an AWS Target)
- **Permissions**: Read access on the secrets, Read/Update on the USC, and Read on the associated target
- **jq** installed for JSON parsing
- **bash 4+** (uses associative arrays)

---

## Usage

### Interactive Mode

```bash
./sync-folder-to-usc.sh
```

The script will list available folders and USCs, then prompt you to select them.

### CLI Arguments

```bash
# Sync all static secrets under a folder to AWS
./sync-folder-to-usc.sh \
  --folder /team/app/prod \
  --usc /9-USC/AWS-TEST-Folder-Sync

# Dry run — preview without making changes
./sync-folder-to-usc.sh \
  --folder /team/app/prod \
  --usc /9-USC/AWS-TEST-Folder-Sync \
  --dry-run

# Sync both static and rotated secrets
./sync-folder-to-usc.sh \
  --folder /team/app/prod \
  --usc /9-USC/AWS-TEST-Folder-Sync \
  --types static-secret,rotated-secret

# Check for drift between Akeyless and AWS
./sync-folder-to-usc.sh \
  --folder /team/app/prod \
  --usc /9-USC/AWS-TEST-Folder-Sync \
  --check-drift

# Only sync secrets in the immediate folder (no recursion)
./sync-folder-to-usc.sh \
  --folder /team/app/prod \
  --usc /9-USC/AWS-TEST-Folder-Sync \
  --recursive false

# Add a prefix to remote secret names
./sync-folder-to-usc.sh \
  --folder /team/app/prod \
  --usc /9-USC/AWS-TEST-Folder-Sync \
  --remote-prefix "prod-"

# Specify a custom CLI binary path
./sync-folder-to-usc.sh \
  --folder /team/app/prod \
  --usc /9-USC/AWS-TEST-Folder-Sync \
  --cli /usr/local/bin/akeyless
```

### Config File Mode

```bash
cp sync-folder-to-usc.conf.example sync.conf
# Edit sync.conf with your values
./sync-folder-to-usc.sh --config sync.conf
```

**Config file format** (`key=value`):

```ini
# Akeyless folder path containing secrets to sync
FOLDER=/team/app/prod

# Universal Secret Connector name/path
USC_NAME=/9-USC/AWS-TEST-Folder-Sync

# Secret types to sync (comma-separated)
TYPES=static-secret

# Recurse into subfolders
RECURSIVE=true

# Prefix for remote secret names (leave empty for no prefix)
REMOTE_PREFIX=

# Preview mode
DRY_RUN=false

# Drift detection mode (requires gateway URL configured in CLI profile)
CHECK_DRIFT=false
```

> **Note:** CLI arguments take precedence over config file values. For example, `--dry-run` on the command line overrides `DRY_RUN=false` in the config file.

---

## Options Reference

| Option | Description | Default |
|---|---|---|
| `--folder PATH` | Akeyless folder path containing secrets to sync | *(prompted)* |
| `--usc NAME` | USC item name/path | *(prompted)* |
| `--config FILE` | Load settings from a config file | |
| `--types TYPES` | Comma-separated: `static-secret`, `rotated-secret` | `static-secret` |
| `--recursive BOOL` | Recurse into subfolders | `true` |
| `--remote-prefix PFX` | Prefix for secret names on the remote side | *(none)* |
| `--dry-run` | Preview without making changes | `false` |
| `--check-drift` | Compare values between Akeyless and remote | `false` |
| `--cli PATH` | Path to akeyless CLI binary | `akeyless` |
| `-h`, `--help` | Show usage and exit | |

You can also set `AKEYLESS_CLI` as an environment variable instead of using `--cli`.

---

## Modes of Operation

### Sync (default)

Creates sync associations for secrets that don't have one, and re-syncs secrets that already have an existing association. The script is **idempotent** — safe to run repeatedly.

```
[SYNC]    /folder/new-secret     → creates association + pushes value to remote
[RE-SYNC] /folder/existing       → pushes current Akeyless value to remote
```

### Dry Run (`--dry-run`)

Previews what would happen without making any changes.

### Drift Check (`--check-drift`)

Compares the Akeyless secret value against the remote (USC) value for each secret.

| Status | Meaning |
|---|---|
| `OK` | Values match |
| `DRIFT DETECTED` | Values differ — remote was changed outside Akeyless |
| `NOT SYNCED` | Secret has no sync association to this USC |
| `NO REMOTE ID` | Association exists but remote secret ID is missing |
| `REMOTE READ FAILED` | Could not read the remote value |

---

## Drift Behavior

Drift **only occurs in one direction**:

- **Akeyless → Remote**: Changes auto-propagate in real-time via the sync association. No drift possible.
- **Remote → Akeyless**: No auto-propagation. Changes made directly on AWS/Azure/GCP are NOT reflected back to Akeyless.

To **fix drift**, re-run the script without `--check-drift`. The re-sync will push Akeyless values back to the remote, overwriting any direct changes. Akeyless is always the source of truth.

---

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | All operations succeeded / no drift |
| `N > 0` | Number of failures (sync mode) or drifted secrets (drift mode) |

This makes the script CI/CD-friendly — use the exit code in pipelines to gate deployments.

---

## Example Output

### Sync

```
══════════════════════════════════════════════════
  Akeyless Folder → USC Sync
  Folder: /2-Static_Secrets/folder-sync-test  |  USC: /9-USC/AWS-TEST-Folder-Sync
  Types: static-secret  |  Recursive: true  |  Dry Run: false  |  Drift: false
══════════════════════════════════════════════════

Listing static-secret secrets under /2-Static_Secrets/folder-sync-test...

Found 11 secret(s) to sync.

[RE-SYNC] /2-Static_Secrets/folder-sync-test/api-key-stripe       OK
[RE-SYNC] /2-Static_Secrets/folder-sync-test/db-password          OK
[SYNC   ] /2-Static_Secrets/folder-sync-test/new-secret           OK
[RE-SYNC] /2-Static_Secrets/folder-sync-test/redis-auth-token     OK
...

══════════════════════════════════════════════════
  Total: 11  |  OK: 11  |  Failed: 0
  Folder: /2-Static_Secrets/folder-sync-test  →  USC: /9-USC/AWS-TEST-Folder-Sync
══════════════════════════════════════════════════
```

- `SYNC` — new association created and value pushed to remote
- `RE-SYNC` — existing association found, value pushed again to remote

### Drift Check

```
══════════════════════════════════════════════════
  Akeyless Folder → USC Sync
  Folder: /2-Static_Secrets/folder-sync-test  |  USC: /9-USC/AWS-TEST-Folder-Sync
  Types: static-secret  |  Recursive: true  |  Dry Run: false  |  Drift: true
══════════════════════════════════════════════════

Listing static-secret secrets under /2-Static_Secrets/folder-sync-test...

Found 10 secret(s) to sync.

[CHECK] /2-Static_Secrets/folder-sync-test/api-key-stripe       OK
[CHECK] /2-Static_Secrets/folder-sync-test/db-password          DRIFT DETECTED
[CHECK] /2-Static_Secrets/folder-sync-test/redis-auth-token     OK
...

══════════════════════════════════════════════════
  Drift Check Summary
  Total: 10  |  Match: 9  |  Drifted: 1  |  Not Synced: 0  |  Errors: 0
  Folder: /2-Static_Secrets/folder-sync-test  →  USC: /9-USC/AWS-TEST-Folder-Sync
══════════════════════════════════════════════════

Drifted secrets:
  /2-Static_Secrets/folder-sync-test/db-password
```

---

## Underlying Akeyless API Commands

The script uses these CLI commands under the hood:

| Command | Purpose |
|---|---|
| `akeyless list-items --path <folder> --type <type>` | List all secrets under a folder |
| `akeyless describe-item --name <secret>` | Check for existing sync associations |
| `akeyless static-secret-sync --name <secret> --usc-name <usc> --remote-secret-name <name>` | Create new sync association |
| `akeyless static-secret-sync --name <secret>` | Re-sync existing association |
| `akeyless rotated-secret-sync` | Same as above for rotated secrets |
| `akeyless get-secret-value --name <secret>` | Read Akeyless value (for drift check) |
| `akeyless usc get --usc-name <usc> --secret-id <arn>` | Read remote value (for drift check) |

---

## Scheduling

Run on a schedule to keep secrets in sync and detect drift:

```bash
# Cron — sync every hour
0 * * * * /path/to/sync-folder-to-usc.sh --config /path/to/sync.conf

# Cron — drift check daily, alert on failure
0 6 * * * /path/to/sync-folder-to-usc.sh --config /path/to/drift.conf --check-drift || notify-team.sh
```

---

## Supported USC Providers

The script works with any USC type:

- AWS Secrets Manager
- Azure Key Vault
- GCP Secret Manager
- HashiCorp Vault
- Kubernetes Secrets
