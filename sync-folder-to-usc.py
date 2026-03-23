#!/usr/bin/env python3
"""Sync all static/rotated secrets under an Akeyless folder to a USC.

Usage: ./sync-folder-to-usc.py [--config FILE] [--folder PATH] [--usc NAME]
       [--dry-run] [--check-drift] [--types static-secret,rotated-secret]
       [--recursive false] [--remote-prefix PFX] [--quiet] [--cli PATH]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from typing import NoReturn

# ─── Constants ────────────────────────────────────────────

_UNSET = object()

SYNC_CMDS = {
    "static-secret": "static-secret-sync",
    "rotated-secret": "rotated-secret-sync",
}

# (attr_name, default) — default type distinguishes bool from string fields
CONFIG_FIELDS = {
    "FOLDER": ("folder", ""),
    "USC_NAME": ("usc_name", ""),
    "TYPES": ("types", "static-secret"),
    "RECURSIVE": ("recursive", "true"),
    "REMOTE_PREFIX": ("remote_prefix", ""),
    "DRY_RUN": ("dry_run", False),
    "CHECK_DRIFT": ("check_drift", False),
}


# ─── Helpers ──────────────────────────────────────────────


def die(msg: str) -> NoReturn:
    """Print error to stderr and exit."""
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def print_banner(
    *lines: str,
    details_title: str = "",
    details: list[str] | None = None,
) -> None:
    """Print a separator-boxed banner with optional detail list."""
    sep = "═" * 50
    print(f"\n{sep}")
    for line in lines:
        if line:
            print(f"  {line}")
    print(sep)
    if details:
        print(f"\n{details_title}:")
        print("\n".join(details))


def pick_from_list(prompt: str, items: list[str]) -> str:
    """Interactive selection from a numbered list."""
    for i, item in enumerate(items, 1):
        print(f"  {i}) {item}", file=sys.stderr)
    print(file=sys.stderr)
    while True:
        choice = input(f"{prompt} (number or path): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(items):
            return items[int(choice) - 1]
        if choice:
            return choice
        print("Invalid selection. Try again.", file=sys.stderr)


def load_config(path: str) -> dict[str, str]:
    """Parse a key=value config file. Skips comments and blank lines."""
    cfg: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                cfg[key.strip()] = value.strip()
    return cfg


# ─── CLI Wrapper ──────────────────────────────────────────


class AkeylessCLI:
    """Wrapper around the akeyless CLI binary."""

    def __init__(self, binary: str, quiet: bool = False) -> None:
        self.bin = binary
        self.quiet = quiet

    def log(self, msg: str) -> None:
        """Print a progress message unless --quiet."""
        if not self.quiet:
            print(msg)

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        """Run a CLI command. Returns CompletedProcess."""
        return subprocess.run(
            [self.bin, *args], capture_output=True, text=True,
        )

    def json(self, args: list[str]) -> dict:
        """Run a CLI command with --json true, parse output. Returns {} on failure."""
        result = self.run([*args, "--json", "true"])
        if result.returncode != 0:
            return {}
        raw = result.stdout.strip()
        if not raw or raw == "{}":
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def list_items(self, base_args: list[str]) -> list[dict]:
        """List items with explicit auto-pagination."""
        data = self.json(["list-items", "--auto-pagination", "enabled", *base_args])
        return data.get("items") or []

    def list_items_with_folders(self, base_args: list[str]) -> tuple[list[dict], list[str]]:
        """List items and subfolders. Returns (items, folders)."""
        data = self.json(["list-items", "--auto-pagination", "enabled", *base_args])
        return data.get("items") or [], data.get("folders") or []

    def find_association(self, name: str, usc_name: str) -> dict | None:
        """Describe an item and return its USC sync association, or None."""
        desc = self.json(["describe-item", "--name", name])
        for a in desc.get("usc_sync_associated_items") or []:
            if a.get("item_name") == usc_name:
                return a
        return None


# ─── Operations ───────────────────────────────────────────


def discover_interactive(
    ak: AkeylessCLI, folder: str, usc_name: str,
) -> tuple[str, str]:
    """Prompt for folder/usc if missing and stdin is a terminal."""
    is_tty = sys.stdin.isatty()

    root_data = None
    if not folder or not usc_name:
        root_data = ak.json(["list-items", "--path", "/"])

    if not folder:
        if not is_tty:
            die("--folder is required in non-interactive mode")
        ak.log("Discovering folders...")
        folders = sorted(root_data.get("folders") or [])
        if folders:
            ak.log("\nAvailable folders:")
            folder = pick_from_list("Select folder", folders)
        else:
            folder = input("No folders discovered. Enter the Akeyless folder path manually: ").strip()

    if not usc_name:
        if not is_tty:
            die("--usc is required in non-interactive mode")
        ak.log("\nDiscovering USCs...")
        uscs = sorted(
            item["item_name"]
            for f in (root_data.get("folders") or [])
            for item in ak.list_items(["--path", f, "--minimal-view", "true"])
            if item.get("item_type") == "USC" and item.get("item_name")
        )
        if uscs:
            ak.log("\nAvailable USCs:")
            usc_name = pick_from_list("Select USC", uscs)
        else:
            usc_name = input("No USCs discovered. Enter the USC name manually: ").strip()

    return folder, usc_name


def collect_secrets(
    ak: AkeylessCLI, folder: str, types: list[str], recursive: bool,
) -> list[tuple[str, str]]:
    """List secrets under folder. Returns list of (name, type).

    When recursive=True, manually walks subfolders since the CLI's
    --current-folder flag does not reliably control recursion.
    """
    secrets: list[tuple[str, str]] = []
    folders_to_visit = [folder]

    while folders_to_visit:
        current = folders_to_visit.pop(0)
        for stype in types:
            ak.log(f"Listing {stype} secrets under {current}...")
            items, subfolders = ak.list_items_with_folders(
                ["--path", current, "--minimal-view", "true", "--type", stype],
            )
            if not items:
                ak.log("  None found.")
            for item in items:
                name = item.get("item_name")
                if name:
                    secrets.append((name, stype))

            # Only queue subfolders on the first type to avoid duplicates
            if recursive and stype == types[0]:
                for sf in subfolders:
                    if sf not in folders_to_visit:
                        ak.log(f"  Found subfolder: {sf}")
                        folders_to_visit.append(sf)

    return secrets


def check_drift(
    ak: AkeylessCLI, secrets: list[tuple[str, str]], usc_name: str, folder: str,
) -> int:
    """Compare Akeyless values against remote USC values. Returns exit code."""
    match = drifted = not_synced = errors = 0
    drift_details: list[str] = []

    for name, _ in secrets:
        print(f"[CHECK] {name:<55} ", end="", flush=True)

        assoc = ak.find_association(name, usc_name)
        if not assoc:
            print("NOT SYNCED")
            not_synced += 1
            continue

        remote_id = (assoc.get("attributes") or {}).get("secret_id")
        if not remote_id:
            print("NO REMOTE ID")
            errors += 1
            continue

        result = ak.run(["get-secret-value", "--name", name])
        if result.returncode != 0:
            print("AKEYLESS READ FAILED")
            errors += 1
            continue
        akeyless_val = result.stdout.strip()

        remote_data = ak.json(["usc", "get", "--usc-name", usc_name, "--secret-id", remote_id])
        remote_b64 = remote_data.get("value", "")
        if not remote_b64:
            print("REMOTE READ FAILED")
            errors += 1
            continue

        try:
            remote_val = base64.b64decode(remote_b64).decode()
        except (ValueError, UnicodeDecodeError):
            print("REMOTE DECODE FAILED")
            errors += 1
            continue

        if akeyless_val == remote_val:
            print("OK")
            match += 1
        else:
            print("DRIFT DETECTED")
            drifted += 1
            drift_details.append(f"  {name}")

    print_banner(
        "Drift Check Summary",
        f"Total: {len(secrets)}  |  Match: {match}  |  Drifted: {drifted}  |  Not Synced: {not_synced}  |  Errors: {errors}",
        f"Folder: {folder}  →  USC: {usc_name}",
        details_title="Drifted secrets", details=drift_details,
    )
    return min(drifted + errors, 125)


def sync_secrets(
    ak: AkeylessCLI,
    secrets: list[tuple[str, str]],
    usc_name: str,
    remote_prefix: str,
    dry_run: bool,
    folder: str,
) -> int:
    """Sync or re-sync secrets to USC. Returns exit code."""
    success = failed = 0
    error_details: list[str] = []

    for name, stype in secrets:
        sync_cmd = SYNC_CMDS.get(stype)
        if not sync_cmd:
            print(f"[SKIP] {name}")
            continue

        assoc = ak.find_association(name, usc_name)
        if assoc:
            sync_args = ["--name", name]
            action = "RE-SYNC"
        else:
            remote_name = remote_prefix + name.rsplit("/", 1)[-1]
            sync_args = ["--name", name, "--usc-name", usc_name, "--remote-secret-name", remote_name]
            action = "SYNC"

        if dry_run:
            print(f"[DRY-RUN] [{action}] {name} → {usc_name}")
            success += 1
            continue

        print(f"[{action:<7}] {name:<55} ", end="", flush=True)
        result = ak.run([sync_cmd, *sync_args])
        if result.returncode == 0:
            print("OK")
            success += 1
        else:
            print("FAILED")
            error_details.append(f"  {name}: {result.stderr.strip() or result.stdout.strip()}")
            failed += 1

    print_banner(
        f"Total: {len(secrets)}  |  OK: {success}  |  Failed: {failed}",
        f"Folder: {folder}  →  USC: {usc_name}",
        "Mode: DRY RUN (no changes made)" if dry_run else "",
        details_title="Errors", details=error_details,
    )
    return min(failed, 125)


# ─── Argument Parsing & Config ────────────────────────────


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--folder", default=_UNSET)
    p.add_argument("--usc", dest="usc_name", default=_UNSET)
    p.add_argument("--config", dest="config_file", default="")
    p.add_argument("--types", default=_UNSET)
    p.add_argument("--recursive", default=_UNSET)
    p.add_argument("--remote-prefix", default=_UNSET)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--check-drift", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--cli", default=os.environ.get("AKEYLESS_CLI", "akeyless"))
    return p.parse_args()


def resolve_config(args: argparse.Namespace) -> None:
    """Merge config file values into args. Priority: CLI > config > defaults."""
    if args.config_file:
        if not os.path.isfile(args.config_file):
            die(f"Config file not found: {args.config_file}")
        cfg = load_config(args.config_file)
        for cfg_key, (attr, default) in CONFIG_FIELDS.items():
            if cfg_key not in cfg:
                continue
            current = getattr(args, attr)
            if isinstance(default, bool):
                if not current:  # False = not set by CLI (store_true)
                    setattr(args, attr, cfg[cfg_key].lower() == "true")
            elif current is _UNSET:
                setattr(args, attr, cfg[cfg_key])

    for _, (attr, default) in CONFIG_FIELDS.items():
        if getattr(args, attr) is _UNSET:
            setattr(args, attr, default)


# ─── Main ─────────────────────────────────────────────────


def main() -> None:
    """Entry point: parse args, resolve config, discover, collect, sync or drift-check."""
    args = parse_args()
    resolve_config(args)

    ak = AkeylessCLI(args.cli, quiet=args.quiet)
    args.folder, args.usc_name = discover_interactive(ak, args.folder, args.usc_name)

    if not args.folder or not args.usc_name:
        die("folder and USC name are required.")

    if sys.stdin.isatty() and not args.dry_run and not args.check_drift:
        ans = input("Include rotated secrets? (y/N): ").strip()
        if ans.lower().startswith("y"):
            args.types = "static-secret,rotated-secret"
        ans = input("Dry run first? (Y/n): ").strip()
        if not ans.lower().startswith("n"):
            args.dry_run = True

    recursive = args.recursive.lower() != "false"
    types = [t.strip() for t in args.types.split(",")]
    invalid = [t for t in types if t not in SYNC_CMDS]
    if invalid:
        die(f"Unknown secret type(s): {', '.join(invalid)}. Valid: {', '.join(SYNC_CMDS)}")

    ak.log(f"""
{'═' * 50}
  Akeyless Folder → USC Sync
  Folder: {args.folder}  |  USC: {args.usc_name}
  Types: {args.types}  |  Recursive: {recursive}  |  Dry Run: {args.dry_run}  |  Drift: {args.check_drift}
{'═' * 50}
""")

    secrets = collect_secrets(ak, args.folder, types, recursive)
    ak.log(f"\nFound {len(secrets)} secret(s) to sync.\n")
    if not secrets:
        ak.log("Nothing to sync.")
        return

    if args.check_drift:
        sys.exit(check_drift(ak, secrets, args.usc_name, args.folder))
    else:
        sys.exit(sync_secrets(ak, secrets, args.usc_name, args.remote_prefix, args.dry_run, args.folder))


if __name__ == "__main__":
    main()
