"""Read-only version query before CLI, tmux, bridge and storage startup."""

import os
import sys


def early_version_main(argv=None, version=None, build=None, config_names=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if (not args or args[-1] != "version" or
            any(arg not in ("--json", "--verbose", "-v") for arg in args[:-1])):
        return None
    if config_names is None:
        directory = os.path.normpath(os.path.join(
            os.path.dirname(__file__), "..", "cam", "adapters", "configs"))
        try:
            config_names = [name for name in os.listdir(directory)
                            if name.endswith(".toml") and
                            not name.endswith(".boot.toml")]
        except OSError:
            return None  # Unusual packaging: preserve the normal loader.
    if version is None:
        from camc_pkg import __version__, __build__
        version, build = __version__, __build__
    print("camc v%s (%s)" % (version, build or "dev"))
    print()
    print("Supported tools:")
    directory = os.path.expanduser("~/.cam/configs")
    for name in sorted(config_names):
        path = os.path.join(directory, name)
        suffix = " (config: %s)" % path if os.path.exists(path) else " (embedded)"
        print("  %s%s" % (name.replace(".toml", ""), suffix))
    return 0
