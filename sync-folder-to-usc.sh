#!/usr/bin/env bash
# Sync all static/rotated secrets under an Akeyless folder to a USC.
# Usage: ./sync-folder-to-usc.sh [--config FILE] [--folder PATH] [--usc NAME] [--dry-run] [--check-drift] [--types static-secret,rotated-secret] [--recursive false] [--remote-prefix PFX] [--cli PATH]
set -euo pipefail

AKEYLESS_CLI="${AKEYLESS_CLI:-akeyless}" FOLDER="" USC_NAME="" DRY_RUN=false
TYPES="static-secret" RECURSIVE=true REMOTE_PREFIX="" CONFIG_FILE="" CHECK_DRIFT=false

die() { echo "Error: $1" >&2; exit 1; }

# ── Parse arguments ──
while [[ $# -gt 0 ]]; do
  case "$1" in
    --folder)        FOLDER="$2"; shift 2 ;;
    --usc)           USC_NAME="$2"; shift 2 ;;
    --config)        CONFIG_FILE="$2"; shift 2 ;;
    --types)         TYPES="$2"; shift 2 ;;
    --recursive)     RECURSIVE="$2"; shift 2 ;;
    --remote-prefix) REMOTE_PREFIX="$2"; shift 2 ;;
    --dry-run)       DRY_RUN=true; shift ;;
    --check-drift)   CHECK_DRIFT=true; shift ;;
    --cli)           AKEYLESS_CLI="$2"; shift 2 ;;
    -h|--help)       sed -n '2,3p' "$0"; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

# ── Load config file ──
if [[ -n "$CONFIG_FILE" ]]; then
  [[ -f "$CONFIG_FILE" ]] || die "Config file not found: $CONFIG_FILE"
  while IFS='=' read -r key value; do
    [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
    key=$(echo "$key" | xargs); value=$(echo "$value" | xargs)
    case "$key" in
      FOLDER|USC_NAME|TYPES|RECURSIVE|REMOTE_PREFIX|DRY_RUN|CHECK_DRIFT) printf -v "$key" '%s' "$value" ;;
    esac
  done < "$CONFIG_FILE"
fi

# ── Interactive prompts if values missing ──
if [[ -z "$FOLDER" ]]; then
  echo "Available folders:"
  "$AKEYLESS_CLI" list-items --path '/' --json true --minimal-view true 2>/dev/null \
    | jq -r '[.items[]? | select(.item_type != "USC") | .item_name | split("/")[1]] | unique | .[]' 2>/dev/null \
    | sort | sed 's/^/  \//'
  read -rp $'\nEnter the Akeyless folder path to sync: ' FOLDER
fi

if [[ -z "$USC_NAME" ]]; then
  echo -e "\nAvailable USCs:"
  "$AKEYLESS_CLI" list-items --path '/' --json true --minimal-view true 2>/dev/null \
    | jq -r '.items[]? | select(.item_type == "USC") | .item_name' 2>/dev/null \
    | sort | sed 's/^/  /'
  read -rp $'\nEnter the USC name to sync to: ' USC_NAME
fi

[[ -n "$FOLDER" && -n "$USC_NAME" ]] || die "folder and USC name are required."

if [[ -t 0 && "$DRY_RUN" == "false" ]]; then
  read -rp "Include rotated secrets? (y/N): " ans
  [[ "$ans" =~ ^[Yy] ]] && TYPES="static-secret,rotated-secret"
  read -rp "Dry run first? (Y/n): " ans
  [[ ! "$ans" =~ ^[Nn] ]] && DRY_RUN=true
fi

# ── Collect secrets ──
LIST_FLAGS=(--path "$FOLDER" --minimal-view true --json true)
[[ "$RECURSIVE" == "false" ]] && LIST_FLAGS+=(--current-folder true)

cat <<EOF

══════════════════════════════════════════════════
  Akeyless Folder → USC Sync
  Folder: $FOLDER  |  USC: $USC_NAME
  Types: $TYPES  |  Recursive: $RECURSIVE  |  Dry Run: $DRY_RUN  |  Drift: $CHECK_DRIFT
══════════════════════════════════════════════════

EOF

SECRET_NAMES=() SECRET_TYPES=()
IFS=',' read -ra TYPE_ARRAY <<< "$TYPES"

for stype in "${TYPE_ARRAY[@]}"; do
  echo "Listing ${stype} secrets under ${FOLDER}..."
  output=$("$AKEYLESS_CLI" list-items "${LIST_FLAGS[@]}" --type "$stype" 2>/dev/null) || true
  [[ "$output" == "{}" || -z "$output" ]] && { echo "  None found."; continue; }
  while IFS= read -r name; do
    [[ -z "$name" ]] && continue
    SECRET_NAMES+=("$name"); SECRET_TYPES+=("$stype")
  done < <(echo "$output" | jq -r '.items[]?.item_name // empty' 2>/dev/null)
done

TOTAL=${#SECRET_NAMES[@]}
echo -e "\nFound ${TOTAL} secret(s) to sync.\n"
[[ "$TOTAL" -eq 0 ]] && { echo "Nothing to sync."; exit 0; }

# ── Drift Check ──
if [[ "$CHECK_DRIFT" == "true" ]]; then
  MATCH=0 DRIFTED=0 NOT_SYNCED=0 ERR=0 DRIFT_DETAILS=()

  for i in "${!SECRET_NAMES[@]}"; do
    secret_name="${SECRET_NAMES[$i]}"
    printf "[CHECK] %-55s " "${secret_name}"

    # Get sync association to find remote secret ID
    assoc=$("$AKEYLESS_CLI" describe-item --name "$secret_name" --json true 2>/dev/null \
      | jq -r --arg usc "$USC_NAME" '.usc_sync_associated_items[]? | select(.item_name == $usc)' 2>/dev/null) || true

    if [[ -z "$assoc" || "$assoc" == "null" ]]; then
      echo "NOT SYNCED"; NOT_SYNCED=$((NOT_SYNCED + 1)); continue
    fi

    remote_id=$(echo "$assoc" | jq -r '.attributes.secret_id // empty')
    if [[ -z "$remote_id" ]]; then
      echo "NO REMOTE ID"; ERR=$((ERR + 1)); continue
    fi

    # Get both values
    akeyless_val=$("$AKEYLESS_CLI" get-secret-value --name "$secret_name" 2>/dev/null) || true
    remote_b64=$("$AKEYLESS_CLI" usc get --usc-name "$USC_NAME" --secret-id "$remote_id" --json true 2>/dev/null \
      | jq -r '.value // empty' 2>/dev/null) || true

    if [[ -z "$remote_b64" ]]; then
      echo "REMOTE READ FAILED"; ERR=$((ERR + 1)); continue
    fi

    remote_val=$(echo "$remote_b64" | base64 -d 2>/dev/null) || true

    if [[ "$akeyless_val" == "$remote_val" ]]; then
      echo "OK"; MATCH=$((MATCH + 1))
    else
      echo "DRIFT DETECTED"
      DRIFTED=$((DRIFTED + 1))
      DRIFT_DETAILS+=("  ${secret_name}")
    fi
  done

  cat <<EOF

══════════════════════════════════════════════════
  Drift Check Summary
  Total: ${TOTAL}  |  Match: ${MATCH}  |  Drifted: ${DRIFTED}  |  Not Synced: ${NOT_SYNCED}  |  Errors: ${ERR}
  Folder: ${FOLDER}  →  USC: ${USC_NAME}
══════════════════════════════════════════════════
EOF
  if [[ ${#DRIFT_DETAILS[@]} -gt 0 ]]; then
    echo -e "\nDrifted secrets:"; printf '%s\n' "${DRIFT_DETAILS[@]}"
  fi
  exit "$DRIFTED"
fi

# ── Sync ──
SUCCESS=0 FAILED=0 SKIPPED=0 ERRORS=()

for i in "${!SECRET_NAMES[@]}"; do
  secret_name="${SECRET_NAMES[$i]}"
  case "${SECRET_TYPES[$i]}" in
    static-secret)  sync_cmd="static-secret-sync" ;;
    rotated-secret) sync_cmd="rotated-secret-sync" ;;
    *) echo "[SKIP] ${secret_name}"; continue ;;
  esac

  # Check if sync association already exists
  has_assoc=$("$AKEYLESS_CLI" describe-item --name "$secret_name" --json true 2>/dev/null \
    | jq -r --arg usc "$USC_NAME" '[.usc_sync_associated_items[]? | select(.item_name == $usc)] | length' 2>/dev/null) || true

  if [[ "$has_assoc" -gt 0 ]]; then
    # Already synced — trigger a re-sync (only secret name, no usc-name)
    sync_args=(--name "$secret_name")
    action="RE-SYNC"
  else
    # New association — provide remote-secret-name
    remote_name="${REMOTE_PREFIX}$(basename "$secret_name")"
    sync_args=(--name "$secret_name" --usc-name "$USC_NAME" --remote-secret-name "$remote_name")
    action="SYNC"
  fi

  if [[ "$DRY_RUN" == "true" ]]; then
    echo "[DRY-RUN] [${action}] ${secret_name} → ${USC_NAME}"
    SUCCESS=$((SUCCESS + 1)); continue
  fi

  printf "[%-7s] %-55s " "${action}" "${secret_name}"
  if result=$("$AKEYLESS_CLI" "$sync_cmd" "${sync_args[@]}" 2>&1); then
    echo "OK"; SUCCESS=$((SUCCESS + 1))
  else
    echo "FAILED"; ERRORS+=("  ${secret_name}: ${result}"); FAILED=$((FAILED + 1))
  fi
done

# ── Summary ──
cat <<EOF

══════════════════════════════════════════════════
  Total: ${TOTAL}  |  OK: ${SUCCESS}  |  Failed: ${FAILED}
  Folder: ${FOLDER}  →  USC: ${USC_NAME}
$( [[ "$DRY_RUN" == "true" ]] && echo "  Mode: DRY RUN (no changes made)" )
══════════════════════════════════════════════════
EOF

[[ ${#ERRORS[@]} -gt 0 ]] && { echo -e "\nErrors:"; printf '%s\n' "${ERRORS[@]}"; }
exit "$FAILED"
