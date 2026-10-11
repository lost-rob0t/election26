"""Opt-in bridge to an existing external FEC target CLI, never a collector."""
import argparse
import os
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="election26-fec-target",
        description="Forward explicit arguments to an operator-supplied FEC target interface. "
                    "This does not implement or verify raw FEC archive acquisition.",
        epilog="Set ELECTION26_FEC_EXECUTABLE to an existing absolute executable path. "
               "Use -- before the external tool's arguments. No tool is fetched or installed.")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    forwarded = args.arguments
    if not forwarded or forwarded[0] != "--" or len(forwarded) == 1:
        parser.error("provide explicit external arguments after -- (use -- --help to inspect that tool)")
    executable = os.environ.get("ELECTION26_FEC_EXECUTABLE", "")
    if not executable or not Path(executable).is_absolute():
        print("Set ELECTION26_FEC_EXECUTABLE to an existing absolute executable path; "
              "the optional FEC target tool is not bundled.", file=sys.stderr)
        return 2
    if not Path(executable).is_file() or not os.access(executable, os.X_OK):
        print("The operator-supplied FEC target executable is missing or not executable.", file=sys.stderr)
        return 2
    try:
        # No shell, argument rewriting, server defaults, credentials or downloads.
        os.execv(executable, [executable, *forwarded[1:]])
    except OSError:
        print("Could not start the operator-supplied FEC target executable.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
