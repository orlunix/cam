"""Entry point for `python -m camc_pkg`."""

from camc_pkg.fast_capture import early_capture_main

_fast_capture_exit = early_capture_main()
if _fast_capture_exit is not None:
    raise SystemExit(_fast_capture_exit)

from camc_pkg.cli import main

if __name__ == "__main__":
    main()
