"""Token resolution for API providers — never stored in api-models.json."""

import os
import re

from camc_pkg.api_store import TOKEN_ENV_FILE

LEGACY_TOKEN_ENV = os.path.expanduser("~/.cam/inference-hub.env")
MY_TOKENS_YAML = os.path.expanduser("~/.my_tokens.yaml")


def _normalize_key(key):
    """Canonical key for case/hyphen-insensitive lookup."""
    return re.sub(r"[-\s]+", "_", str(key or "").lower().strip())


def _strip_quotes(value):
    return str(value or "").strip().strip("'\"")


def _read_kv_file(path, sep):
    """Read simple KEY<sep>VALUE lines from a dotenv or flat-yaml file."""
    if not os.path.isfile(path):
        return {}
    out = {}
    with open(path, "r") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if sep not in line:
                continue
            k, _, v = line.partition(sep)
            key = k.strip()
            val = _strip_quotes(v)
            if key and val:
                out[key] = val
    return out


def _read_token_env(path):
    return _read_kv_file(path, "=")


def _read_yaml_flat(path):
    return _read_kv_file(path, ":")


def _index_by_normalized_key(flat_dict):
    """Map normalized key -> (original_key, value). First entry wins."""
    index = {}
    for key, value in flat_dict.items():
        nk = _normalize_key(key)
        if nk and nk not in index:
            index[nk] = (key, value)
    return index


def _lookup_in_index(index, label, *candidates):
    """Case/hyphen-insensitive lookup; returns (token, source_label)."""
    for candidate in candidates:
        if not candidate:
            continue
        hit = index.get(_normalize_key(candidate))
        if hit:
            orig, val = hit
            val = _strip_quotes(val)
            if val:
                return val, "%s:%s" % (label, orig)
    return "", ""


def _lookup_in_environ(env_names, auth_key):
    """Resolve from process environment (exact name first, then case-insensitive)."""
    for name in env_names or []:
        val = os.environ.get(name, "").strip()
        if val:
            return val, "env:%s" % name

    norm_env = {}
    for key, value in os.environ.items():
        nk = _normalize_key(key)
        if nk and nk not in norm_env:
            norm_env[nk] = (key, value)

    for candidate in list(env_names or []) + [auth_key]:
        if not candidate:
            continue
        hit = norm_env.get(_normalize_key(candidate))
        if hit:
            orig, val = hit
            val = val.strip()
            if val:
                return val, "env:%s" % orig
    return "", ""


def resolve_token(auth_key, env_names, cli_token=None):
    """Return (token, source_label). First hit wins."""
    if cli_token:
        return cli_token.strip(), "cli"

    names = list(env_names or [])
    ak = _normalize_key(auth_key)
    if ak:
        for name in names:
            if _normalize_key(name) == ak:
                break
        else:
            names.append(auth_key)

    token, src = _lookup_in_environ(names, auth_key)
    if token:
        return token, src

    candidates = []
    seen = set()
    for name in names:
        nk = _normalize_key(name)
        if nk and nk not in seen:
            seen.add(nk)
            candidates.append(name)
    if ak and ak not in seen:
        candidates.append(auth_key)

    for path, label in ((TOKEN_ENV_FILE, "token.env"), (LEGACY_TOKEN_ENV, "inference-hub.env")):
        token, src = _lookup_in_index(_index_by_normalized_key(_read_token_env(path)), label, *candidates)
        if token:
            return token, src

    token, src = _lookup_in_index(
        _index_by_normalized_key(_read_yaml_flat(MY_TOKENS_YAML)),
        "my_tokens.yaml",
        *candidates,
    )
    if token:
        return token, src

    return "", "none"
