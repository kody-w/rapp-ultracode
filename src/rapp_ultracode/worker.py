from __future__ import annotations

import argparse

from .cli import _execute


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model")
    parser.add_argument("--effort", required=True)
    parser.add_argument("--budget", type=float, required=True)
    parser.add_argument("--max-agents", type=int, required=True)
    parser.add_argument("--lease-token", required=True)
    parser.add_argument("--allow-host-checks", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    args.json = False
    return _execute(args, args.run_id, resume=args.resume)


if __name__ == "__main__":
    raise SystemExit(main())
