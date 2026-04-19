#!/usr/bin/env python3
"""token-sync: Encrypted cross-machine token synchronization for NVIDIA AI CLIs.

Single-file, stdlib-only, Python 3.6+. Stores tokens in an encrypted vault
(AES-256-CBC via openssl) in a private GitLab repo. Merges across machines
using per-token newest-mtime-wins strategy.
"""

from __future__ import print_function

import argparse
import base64
import glob
import json
import os
import socket
import subprocess
import sys
import time

__version__ = "0.1.0"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SYNC_DIR = os.path.expanduser("~/.token-sync")
CONFIG_FILE = os.path.join(SYNC_DIR, "config.json")
REPO_DIR = os.path.join(SYNC_DIR, "repo")
VAULT_FILE = "vault.enc"
PASSPHRASE_FILE = os.path.join(SYNC_DIR, ".passphrase")
LOG_FILE = os.path.join(SYNC_DIR, "sync.log")

# Token manifest: (vault_key, filesystem_path_relative_to_HOME)
TOKEN_MANIFEST = [
    ("ai-pim-utils/confluence/token", ".ai-pim-utils/confluence/token"),
    ("ai-pim-utils/helios/token", ".ai-pim-utils/helios/token"),
    ("ai-pim-utils/nvbugs/token", ".ai-pim-utils/nvbugs/token"),
    ("ai-pim-utils/auth-cache", ".ai-pim-utils/auth-cache"),
    ("ai-pim-utils/token-cache-ai-pim-utils", ".ai-pim-utils/token-cache-ai-pim-utils"),
    ("atlassian-cli/email", ".atlassian-cli/email"),
    ("atlassian-cli/url", ".atlassian-cli/url"),
]

# Glob-based manifest: (vault_key_prefix, dir_relative_to_HOME, pattern)
TOKEN_GLOB_MANIFEST = [
    ("config/callisto/tokens", ".config/callisto/tokens", "*.json"),
]

# ---------------------------------------------------------------------------
# ANSI colors
# ---------------------------------------------------------------------------

def _supports_color():
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

_COLOR = _supports_color()

def _c(code, text):
    if _COLOR:
        return "\033[%sm%s\033[0m" % (code, text)
    return str(text)

def green(t):  return _c("32", t)
def red(t):    return _c("31", t)
def yellow(t): return _c("33", t)
def cyan(t):   return _c("36", t)
def bold(t):   return _c("1", t)
def dim(t):    return _c("2", t)

# ---------------------------------------------------------------------------
# Passphrase
# ---------------------------------------------------------------------------

def get_passphrase(confirm=False):
    """Get passphrase from env var or interactive prompt."""
    pp = os.environ.get("TOKEN_SYNC_PASSPHRASE", "")
    if not pp and os.path.isfile(PASSPHRASE_FILE):
        try:
            with open(PASSPHRASE_FILE, "r") as f:
                pp = f.read().strip()
        except Exception:
            pass
    if not pp:
        try:
            import getpass
            pp = getpass.getpass("Vault passphrase: ")
            if confirm:
                pp2 = getpass.getpass("Confirm passphrase: ")
                if pp != pp2:
                    print(red("Passphrases do not match."))
                    sys.exit(1)
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
    if len(pp) < 12:
        print(yellow("Warning: passphrase is shorter than 12 characters."))
    return pp

# ---------------------------------------------------------------------------
# Encryption (openssl AES-256-CBC + PBKDF2)
# ---------------------------------------------------------------------------

def encrypt_vault(plaintext, passphrase):
    """Encrypt bytes using openssl. Returns ciphertext bytes."""
    r = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "310000",
         "-salt", "-pass", "pass:" + passphrase],
        input=plaintext, capture_output=True,
    )
    if r.returncode != 0:
        print(red("Encryption failed: " + r.stderr.decode("utf-8", errors="replace")))
        sys.exit(1)
    return r.stdout


def decrypt_vault(ciphertext, passphrase):
    """Decrypt bytes using openssl. Returns plaintext bytes or None on failure."""
    r = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "310000",
         "-d", "-salt", "-pass", "pass:" + passphrase],
        input=ciphertext, capture_output=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout

# ---------------------------------------------------------------------------
# Vault I/O
# ---------------------------------------------------------------------------

def new_vault():
    return {"version": 1, "tokens": {}}


def load_vault(data):
    """Parse decrypted JSON bytes into vault dict."""
    return json.loads(data.decode("utf-8"))


def dump_vault(vault):
    """Serialize vault dict to JSON bytes."""
    return json.dumps(vault, indent=2, sort_keys=True).encode("utf-8")

# ---------------------------------------------------------------------------
# Token collection from filesystem
# ---------------------------------------------------------------------------

def _read_token(abs_path):
    """Read a token file, return (base64_data, mtime_iso) or None."""
    if not os.path.isfile(abs_path):
        return None
    try:
        with open(abs_path, "rb") as f:
            raw = f.read()
        mtime = os.path.getmtime(abs_path)
        mtime_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime))
        return {
            "data": base64.b64encode(raw).decode("ascii"),
            "mtime": mtime_iso,
            "source_host": socket.gethostname(),
        }
    except Exception as e:
        print(yellow("  Warning: cannot read %s: %s" % (abs_path, e)))
        return None


def collect_local_tokens():
    """Read all tracked tokens from filesystem into a vault dict."""
    vault = new_vault()
    home = os.path.expanduser("~")

    for key, rel_path in TOKEN_MANIFEST:
        abs_path = os.path.join(home, rel_path)
        entry = _read_token(abs_path)
        if entry:
            vault["tokens"][key] = entry

    for prefix, rel_dir, pattern in TOKEN_GLOB_MANIFEST:
        full_dir = os.path.join(home, rel_dir)
        for filepath in sorted(glob.glob(os.path.join(full_dir, pattern))):
            filename = os.path.basename(filepath)
            key = "%s/%s" % (prefix, filename)
            entry = _read_token(filepath)
            if entry:
                vault["tokens"][key] = entry

    return vault

# ---------------------------------------------------------------------------
# Token deployment to filesystem
# ---------------------------------------------------------------------------

def _key_to_path(key):
    """Convert vault key to absolute filesystem path."""
    home = os.path.expanduser("~")
    return os.path.join(home, "." + key)


def deploy_tokens(vault, dry_run=False):
    """Write merged tokens to filesystem. Returns list of (key, action) tuples."""
    changes = []
    home = os.path.expanduser("~")

    for key, entry in vault.get("tokens", {}).items():
        abs_path = _key_to_path(key)
        new_data = base64.b64decode(entry["data"])

        # Check if different from current
        current_data = None
        if os.path.isfile(abs_path):
            try:
                with open(abs_path, "rb") as f:
                    current_data = f.read()
            except Exception:
                pass

        if current_data == new_data:
            continue

        action = "update" if current_data is not None else "create"
        changes.append((key, action, entry.get("mtime", ""), entry.get("source_host", "")))

        if dry_run:
            continue

        # Write
        parent = os.path.dirname(abs_path)
        if not os.path.isdir(parent):
            os.makedirs(parent, mode=0o700, exist_ok=True)

        with open(abs_path, "wb") as f:
            f.write(new_data)
        os.chmod(abs_path, 0o600)

        # Restore mtime
        try:
            import calendar
            t = calendar.timegm(time.strptime(entry["mtime"], "%Y-%m-%dT%H:%M:%SZ"))
            os.utime(abs_path, (t, t))
        except Exception:
            pass

    return changes

# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def _parse_mtime(s):
    """Parse ISO 8601 timestamp to epoch seconds."""
    try:
        import calendar
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return 0


def merge_vaults(local, remote):
    """Per-token newest-wins merge. Returns merged vault."""
    merged = new_vault()
    all_keys = set(local.get("tokens", {}).keys()) | set(remote.get("tokens", {}).keys())

    for key in sorted(all_keys):
        local_entry = local.get("tokens", {}).get(key)
        remote_entry = remote.get("tokens", {}).get(key)

        if local_entry is None:
            merged["tokens"][key] = remote_entry
        elif remote_entry is None:
            merged["tokens"][key] = local_entry
        else:
            local_t = _parse_mtime(local_entry.get("mtime", ""))
            remote_t = _parse_mtime(remote_entry.get("mtime", ""))
            if local_t >= remote_t:
                merged["tokens"][key] = local_entry
            else:
                merged["tokens"][key] = remote_entry

    return merged

# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------

def _git(args, cwd=None):
    """Run a git command, return (success, stdout, stderr)."""
    if cwd is None:
        cwd = REPO_DIR
    r = subprocess.run(
        ["git"] + args, cwd=cwd,
        capture_output=True, text=True,
    )
    return r.returncode == 0, r.stdout.strip(), r.stderr.strip()


def git_clone(url, dest):
    ok, out, err = _git(["clone", url, dest], cwd=os.path.dirname(dest))
    if not ok:
        print(red("git clone failed: " + err))
        sys.exit(1)


def git_pull():
    ok, out, err = _git(["pull", "--rebase"])
    return ok


def git_push(message="Update vault"):
    vault_path = os.path.join(REPO_DIR, VAULT_FILE)
    _git(["add", VAULT_FILE])
    _git(["commit", "-m", message])
    ok, out, err = _git(["push"])
    return ok

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    if os.path.isfile(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}


def save_config(cfg):
    os.makedirs(SYNC_DIR, mode=0o700, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG_FILE, 0o600)

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args):
    """Initialize token-sync repo."""
    repo_url = args.repo
    os.makedirs(SYNC_DIR, mode=0o700, exist_ok=True)

    if os.path.isdir(os.path.join(REPO_DIR, ".git")):
        print(yellow("Repo already exists at %s" % REPO_DIR))
    elif repo_url:
        print("Cloning %s ..." % repo_url)
        git_clone(repo_url, REPO_DIR)
        print(green("Cloned."))
    else:
        os.makedirs(REPO_DIR, mode=0o700, exist_ok=True)
        _git(["init"], cwd=REPO_DIR)
        # Create .gitignore
        gi_path = os.path.join(REPO_DIR, ".gitignore")
        with open(gi_path, "w") as f:
            f.write("*.json\n*.dec\n*.tmp\n*.plaintext\n")
        _git(["add", ".gitignore"], cwd=REPO_DIR)
        _git(["commit", "-m", "init"], cwd=REPO_DIR)
        print(green("Initialized local repo at %s" % REPO_DIR))
        print("Add remote: git -C %s remote add origin <url>" % REPO_DIR)

    # Test encrypt/decrypt
    pp = get_passphrase(confirm=True)
    test_data = b"token-sync-test"
    enc = encrypt_vault(test_data, pp)
    dec = decrypt_vault(enc, pp)
    if dec != test_data:
        print(red("Encrypt/decrypt test failed!"))
        sys.exit(1)
    print(green("Encryption test passed."))

    save_config({"repo_url": repo_url or "", "repo_dir": REPO_DIR})
    print(green("Config saved to %s" % CONFIG_FILE))


def cmd_push(args):
    """Collect local tokens, merge with remote, encrypt, push."""
    pp = get_passphrase()

    print("Collecting local tokens...")
    local_vault = collect_local_tokens()
    count = len(local_vault["tokens"])
    print("  Found %d token(s)" % count)

    if count == 0:
        print(yellow("No tokens found. Nothing to push."))
        return

    # Pull latest
    print("Pulling latest from remote...")
    git_pull()

    vault_path = os.path.join(REPO_DIR, VAULT_FILE)
    if os.path.isfile(vault_path):
        print("Decrypting remote vault...")
        with open(vault_path, "rb") as f:
            enc_data = f.read()
        dec_data = decrypt_vault(enc_data, pp)
        if dec_data is None:
            print(red("Failed to decrypt remote vault. Wrong passphrase?"))
            sys.exit(1)
        remote_vault = load_vault(dec_data)
        print("  Remote has %d token(s)" % len(remote_vault.get("tokens", {})))

        print("Merging (newest-wins)...")
        merged = merge_vaults(local_vault, remote_vault)
    else:
        print("  No remote vault found. Creating new.")
        merged = local_vault

    print("  Merged vault has %d token(s)" % len(merged["tokens"]))

    # Encrypt and write
    enc = encrypt_vault(dump_vault(merged), pp)
    with open(vault_path, "wb") as f:
        f.write(enc)
    os.chmod(vault_path, 0o600)

    # Push with retry
    host = socket.gethostname()
    msg = "Update vault from %s at %s" % (host, time.strftime("%Y-%m-%d %H:%M:%S"))
    for attempt in range(3):
        if git_push(msg):
            print(green("Pushed successfully."))
            return
        print(yellow("Push failed (attempt %d/3), pulling and retrying..." % (attempt + 1)))
        git_pull()
        # Re-merge after pull
        if os.path.isfile(vault_path):
            with open(vault_path, "rb") as f:
                enc_data = f.read()
            dec_data = decrypt_vault(enc_data, pp)
            if dec_data:
                remote_vault = load_vault(dec_data)
                merged = merge_vaults(local_vault, remote_vault)
                enc = encrypt_vault(dump_vault(merged), pp)
                with open(vault_path, "wb") as f:
                    f.write(enc)

    print(red("Push failed after 3 attempts."))
    sys.exit(1)


def cmd_pull(args):
    """Pull remote vault, merge, deploy to filesystem."""
    pp = get_passphrase()

    print("Pulling latest from remote...")
    git_pull()

    vault_path = os.path.join(REPO_DIR, VAULT_FILE)
    if not os.path.isfile(vault_path):
        print(yellow("No vault found in repo. Run 'token-sync push' first."))
        return

    print("Decrypting vault...")
    with open(vault_path, "rb") as f:
        enc_data = f.read()
    dec_data = decrypt_vault(enc_data, pp)
    if dec_data is None:
        print(red("Failed to decrypt vault. Wrong passphrase?"))
        sys.exit(1)
    remote_vault = load_vault(dec_data)
    print("  Remote has %d token(s)" % len(remote_vault.get("tokens", {})))

    print("Collecting local tokens...")
    local_vault = collect_local_tokens()

    print("Merging (newest-wins)...")
    merged = merge_vaults(local_vault, remote_vault)

    dry_run = getattr(args, "dry_run", False)
    changes = deploy_tokens(merged, dry_run=dry_run)

    if not changes:
        print(green("All tokens are up to date."))
    else:
        prefix = "[DRY RUN] " if dry_run else ""
        for key, action, mtime, host in changes:
            color = green if action == "update" else cyan
            print("  %s%s %s  (%s from %s)" % (prefix, color(action), key, dim(mtime), dim(host)))
        print("%s%d token(s) %s." % (prefix, len(changes), "would change" if dry_run else "deployed"))


def cmd_status(args):
    """Show token freshness: local vs remote timestamps."""
    pp = get_passphrase()

    local_vault = collect_local_tokens()

    vault_path = os.path.join(REPO_DIR, VAULT_FILE)
    remote_vault = new_vault()
    if os.path.isfile(vault_path):
        git_pull()
        with open(vault_path, "rb") as f:
            enc_data = f.read()
        dec_data = decrypt_vault(enc_data, pp)
        if dec_data:
            remote_vault = load_vault(dec_data)

    all_keys = sorted(
        set(local_vault.get("tokens", {}).keys())
        | set(remote_vault.get("tokens", {}).keys())
    )

    if not all_keys:
        print("No tokens tracked.")
        return

    # Header
    print("%-45s  %-22s  %-22s  %s" % (bold("Token"), bold("Local"), bold("Remote"), bold("Winner")))
    print("-" * 110)

    for key in all_keys:
        local_e = local_vault.get("tokens", {}).get(key)
        remote_e = remote_vault.get("tokens", {}).get(key)

        local_mt = local_e["mtime"] if local_e else "-"
        remote_mt = remote_e["mtime"] if remote_e else "-"

        if local_e and not remote_e:
            winner = green("local")
        elif remote_e and not local_e:
            winner = cyan("remote")
        elif local_e and remote_e:
            lt = _parse_mtime(local_e["mtime"])
            rt = _parse_mtime(remote_e["mtime"])
            if lt >= rt:
                winner = green("local")
            else:
                winner = cyan("remote")
        else:
            winner = dim("none")

        # Shorten key for display
        display_key = key if len(key) <= 44 else "..." + key[-41:]
        print("%-45s  %-22s  %-22s  %s" % (display_key, local_mt, remote_mt, winner))


def cmd_diff(args):
    """Dry-run pull showing what would change."""
    args.dry_run = True
    cmd_pull(args)


def cmd_list(args):
    """List all tracked token paths and their existence."""
    home = os.path.expanduser("~")
    print(bold("Tracked tokens:"))
    print()

    for key, rel_path in TOKEN_MANIFEST:
        abs_path = os.path.join(home, rel_path)
        exists = os.path.isfile(abs_path)
        status = green("exists") if exists else red("missing")
        print("  %-45s  %s  %s" % (key, status, dim(abs_path)))

    for prefix, rel_dir, pattern in TOKEN_GLOB_MANIFEST:
        full_dir = os.path.join(home, rel_dir)
        files = sorted(glob.glob(os.path.join(full_dir, pattern)))
        if files:
            for fp in files:
                fn = os.path.basename(fp)
                key = "%s/%s" % (prefix, fn)
                print("  %-45s  %s  %s" % (key, green("exists"), dim(fp)))
        else:
            print("  %-45s  %s  %s" % (prefix + "/" + pattern, red("no files"), dim(full_dir)))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="token-sync",
        description="Encrypted cross-machine token sync for NVIDIA AI CLIs",
    )
    parser.add_argument("--version", action="version", version="%%(prog)s %s" % __version__)

    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="Initialize token sync repo")
    p_init.add_argument("--repo", default="", help="GitLab repo URL to clone")

    sub.add_parser("push", help="Push local tokens to encrypted vault")
    sub.add_parser("pull", help="Pull and deploy tokens from vault")
    sub.add_parser("status", help="Show local vs remote token timestamps")
    sub.add_parser("diff", help="Dry-run pull showing what would change")
    sub.add_parser("list", help="List all tracked token paths")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # Ensure repo exists for commands that need it
    if args.command in ("push", "pull", "status", "diff"):
        if not os.path.isdir(os.path.join(REPO_DIR, ".git")):
            print(red("Not initialized. Run: token-sync init --repo <url>"))
            sys.exit(1)

    commands = {
        "init": cmd_init,
        "push": cmd_push,
        "pull": cmd_pull,
        "status": cmd_status,
        "diff": cmd_diff,
        "list": cmd_list,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
