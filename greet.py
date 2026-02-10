#!/usr/bin/env python3
"""Simple greeting script that takes a name as an argument."""

import sys


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <name>")
        sys.exit(1)

    name = " ".join(sys.argv[1:])
    print(f"Hello, {name}!")


if __name__ == "__main__":
    main()
