#!/usr/bin/env python3
"""Build script: merge camc_pkg/ package into a single-file camc for deployment.

Usage:
    python build_camc.py              # Output to dist/camc
    python build_camc.py --output X   # Output to X
    python build_camc.py --verify     # Build and compare with src/camc baseline

The single file is stdlib-only, Python 3.6+, zero dependencies.
"""

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime

# Module load order (respects dependency DAG)
MODULE_ORDER = [
    "utils",
    "adapters",
    "transport",
    "detection",
    "storage",
    "machine_store",
    "context_store",
    "remote",
    "migrate",
    "formatters",
    "monitor",
    "scheduler",
    "cli",
]

PKG_DIR = os.path.join(os.path.dirname(__file__), "src", "camc_pkg")
DIST_DIR = os.path.join(os.path.dirname(__file__), "dist")
TOML_DIR = os.path.join(os.path.dirname(__file__), "src", "cam", "adapters", "configs")


def read_module(name):
    path = os.path.join(PKG_DIR, "%s.py" % name)
    with open(path, "r") as f:
        src = f.read()
    if name == "adapters":
        src = _inject_embedded_configs(src)
    return src


def _inject_embedded_configs(src):
    """Replace empty _EMBEDDED_CONFIGS with TOML files from src/cam/adapters/configs/."""
    entries = []
    for fname in sorted(os.listdir(TOML_DIR)):
        if not fname.endswith(".toml"):
            continue
        with open(os.path.join(TOML_DIR, fname), "r") as f:
            content = f.read()
        entries.append('    "%s": r"""%s"""' % (fname, content))
    replacement = "_EMBEDDED_CONFIGS = {\n%s,\n}" % ",\n".join(entries)
    return src.replace(
        "_EMBEDDED_CONFIGS = {}  # populated by build_camc.py",
        replacement,
    )


def read_init():
    path = os.path.join(PKG_DIR, "__init__.py")
    with open(path, "r") as f:
        return f.read()


def strip_imports(source, module_name):
    """Remove intra-package imports (from camc_pkg... import ...) from module source.

    Handles both single-line and multi-line (parenthesized) imports.
    """
    lines = source.splitlines()
    result = []
    in_multiline_import = False
    for line in lines:
        stripped = line.strip()
        if in_multiline_import:
            # Skip until closing paren
            if ')' in stripped:
                in_multiline_import = False
            continue
        # Skip any import from camc_pkg
        if re.match(r'^from\s+camc_pkg', stripped) or re.match(r'^import\s+camc_pkg', stripped):
            # Check if it's a multi-line import (has opening paren but no closing)
            if '(' in stripped and ')' not in stripped:
                in_multiline_import = True
            continue
        result.append(line)
    return "\n".join(result)


def strip_docstring(source):
    """Strip the leading module docstring."""
    lines = source.splitlines()
    i = 0
    # Skip blank lines
    while i < len(lines) and not lines[i].strip():
        i += 1
    # Check for docstring
    if i < len(lines) and lines[i].strip().startswith('"""'):
        first = lines[i].strip()
        if first.count('"""') >= 2:
            # Single-line docstring
            i += 1
        else:
            # Multi-line docstring
            i += 1
            while i < len(lines) and '"""' not in lines[i]:
                i += 1
            if i < len(lines):
                i += 1
    # Skip blank lines after docstring
    while i < len(lines) and not lines[i].strip():
        i += 1
    return "\n".join(lines[i:])


def _is_top_level_import(line, lines, idx):
    """Check if an import line is at top-level (not inside try/except or function)."""
    stripped = line.strip()
    if not (re.match(r'^import\s+\w', stripped) or re.match(r'^from\s+(?!camc_pkg)\w', stripped)):
        return False
    # If the line has no indentation, it's top-level
    if line == stripped:
        # But check if previous non-blank line is 'try:' or 'except ...'
        for j in range(idx - 1, -1, -1):
            prev = lines[j].strip()
            if not prev:
                continue
            if prev.startswith("try:") or prev.startswith("except"):
                return False
            break
        return True
    return False


def collect_stdlib_imports(source):
    """Extract top-level stdlib import lines."""
    imports = set()
    lines = source.splitlines()
    for idx, line in enumerate(lines):
        if _is_top_level_import(line, lines, idx):
            imports.add(line.strip())
    return imports


def strip_stdlib_imports(source):
    """Remove top-level stdlib import lines (they'll be consolidated at the top)."""
    lines = source.splitlines()
    result = []
    for idx, line in enumerate(lines):
        if _is_top_level_import(line, lines, idx):
            continue
        result.append(line)
    return "\n".join(result)


def _git_info():
    """Get git short hash and check for dirty state."""
    repo = os.path.dirname(os.path.abspath(__file__))
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=repo, stderr=subprocess.DEVNULL).decode().strip()
        dirty = subprocess.call(
            ["git", "diff", "--quiet", "HEAD", "--", "src/camc_pkg/"],
            cwd=repo, stderr=subprocess.DEVNULL) != 0
        return sha + ("-dirty" if dirty else "")
    except Exception:
        return "unknown"


def _build_stamp():
    """Return build stamp: 'git-hash YYYY-MM-DD HH:MM'."""
    git = _git_info()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    return "%s %s" % (git, ts)


def build():
    # 1. Read __init__.py for version, constants, logging setup
    init_src = read_init()

    # Stamp __build__ with git hash + date
    stamp = _build_stamp()
    init_src = init_src.replace(
        '__build__ = ""  # populated by build_camc.py: "git-hash date"',
        '__build__ = "%s"' % stamp,
    )

    # 2. Collect all stdlib imports from all modules
    all_imports = set()
    all_imports.update(collect_stdlib_imports(init_src))

    module_sources = {}
    for mod in MODULE_ORDER:
        src = read_module(mod)
        all_imports.update(collect_stdlib_imports(src))
        module_sources[mod] = src

    # 3. Build the single file
    parts = []

    # Shebang and docstring
    parts.append('#!/usr/bin/env python3')
    parts.append('"""camc — Standalone coding agent manager (single-file build).')
    parts.append('')
    parts.append('Auto-generated from camc_pkg/ package. Do not edit directly.')
    parts.append('Edit the source in src/camc_pkg/ and rebuild with build_camc.py.')
    parts.append('"""')
    parts.append('')

    # Consolidated imports
    stdlib_imports = sorted(all_imports)
    for imp in stdlib_imports:
        parts.append(imp)
    parts.append('')

    # __init__.py content (version, constants) — strip imports and docstring
    init_body = strip_imports(init_src, "__init__")
    init_body = strip_stdlib_imports(init_body)
    init_body = strip_docstring(init_body)
    parts.append("# " + "=" * 75)
    parts.append("# Constants and logging")
    parts.append("# " + "=" * 75)
    parts.append('')
    parts.append(init_body.strip())
    parts.append('')

    # Each module in order
    for mod in MODULE_ORDER:
        src = module_sources[mod]
        body = strip_imports(src, mod)
        body = strip_stdlib_imports(body)
        body = strip_docstring(body)
        parts.append('')
        parts.append("# " + "=" * 75)
        parts.append("# %s" % mod)
        parts.append("# " + "=" * 75)
        parts.append('')
        parts.append(body.strip())
        parts.append('')

    # Entry point
    parts.append('')
    parts.append('if __name__ == "__main__":')
    parts.append('    main()')
    parts.append('')

    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Build single-file camc from camc_pkg/")
    parser.add_argument("--output", "-o", default=os.path.join(DIST_DIR, "camc"),
                        help="Output file path [default: dist/camc]")
    parser.add_argument("--verify", action="store_true",
                        help="Build and verify help output matches")
    args = parser.parse_args()

    output = build()

    # Ensure output directory exists
    out_dir = os.path.dirname(args.output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(args.output, "w") as f:
        f.write(output)
    os.chmod(args.output, 0o755)

    lines = output.count("\n")

    # Extract version from built output
    ver = "?"
    for line in output.splitlines():
        if line.startswith("__version__"):
            ver = line.split('"')[1]
            break
    stamp = _build_stamp()

    print("Built %s  v%s (%s, %d lines)" % (args.output, ver, stamp, lines))

    # Append to build log
    log_path = os.path.join(os.path.dirname(__file__), "dist", "BUILD_LOG.md")
    repo_root = os.path.dirname(os.path.abspath(__file__))
    try:
        git_log = subprocess.check_output(
            ["git", "log", "--oneline", "-5", "--", "src/camc_pkg/"],
            cwd=repo_root, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        git_log = "(no git)"
    entry = "## v%s  %s\n\n- Lines: %d\n- Output: %s\n- Recent changes:\n```\n%s\n```\n\n" % (
        ver, stamp, lines, args.output, git_log)
    with open(log_path, "a") as f:
        f.write(entry)

    if args.verify:
        # Compare help outputs
        for cmd in ["--help", "run --help", "list --help", "version"]:
            pkg_out = subprocess.check_output(
                [sys.executable, "-m", "camc_pkg"] + cmd.split(),
                stderr=subprocess.STDOUT).decode()
            built_out = subprocess.check_output(
                [sys.executable, args.output] + cmd.split(),
                stderr=subprocess.STDOUT).decode()
            if pkg_out != built_out:
                print("MISMATCH: camc_pkg %s vs built %s" % (cmd, cmd))
                print("--- package ---")
                print(pkg_out[:500])
                print("--- built ---")
                print(built_out[:500])
                sys.exit(1)
            print("  ✓ %s matches" % cmd)
        print("Verification passed!")


if __name__ == "__main__":
    main()
