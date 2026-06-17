"""Token resolution for API providers — never stored in api-models.json."""

import os
import re

from camc_pkg.api_store import TOKEN_ENV_FILE

LEGACY_TOKEN_ENV = os.path.expanduser("~/.cam/inference-hub.env")
MY_TOKENS_YAML = os.path.expanduser("~/.my_tokens.yaml")


def _read_token_env(path):
    if not os.path.isfile(path):
        return {}
    out = {}
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip("'\"")
    return out


def _read_yaml_flat(path):
    if not os.path.isfile(path):
        return {}
    out = {}
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip().strip("'\"")
    return out


def _normalize_key(key):
    return re.sub(r"[-\s]+", "_", str(key or "").lower())


def resolve_token(auth_key, env_names, cli_token=None):
    """Return (token, source_label). First hit wins."""
    if cli_token:
        return cli_token.strip(), "cli"

    names = list(env_names or [])
    ak = _normalize_key(auth_key)
    if ak and ak not in [_normalize_key(n) for n in names]:
        names.append(auth_key)

    for name in names:
        val = os.environ.get(name, "").strip()
        if val:
            return val, "env:%s" % name

    for path, label in ((TOKEN_ENV_FILE, "token.env"), (LEGACY_TOKEN_ENV, "inference-hub.env")):
        file_vals = _read_token_env(path)
        for name in names:
            if name in file_vals and file_vals[name]:
                return file_vals[name], "%s:%s" % (label, name)
        if ak in file_vals and file_vals[ak]:
            return file_vals[ak], "%s:%s" % (label, ak)

    yaml_vals = _read_yaml_flat(MY_TOKENS_YAML)
    for key in (auth_key, ak):
        if key in yaml_vals and yaml_vals[key]:
            return yaml_vals[key], "my_tokens.yaml:%s" % key

    return "", "none"
