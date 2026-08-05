#!/usr/bin/env python3
"""Compatibility wrapper for one-guild current-day Linky refresh."""

from __future__ import annotations

import datetime as dt
import sys

from linky_sync_runner import main as runner_main


def main(argv: list[str] | None = None) -> int:
    values = list(argv if argv is not None else sys.argv[1:])
    guild = values[0] if values else "Permata-Indonesia"
    utc_date = dt.datetime.now(dt.timezone.utc).date()
    if len(values) > 1:
        utc_date = dt.datetime.strptime(values[1], "%Y%m%d").date()
    args = ["--job-name", "linky-live-compat", "--mode", "target", "--guild", guild,
        "--business-date", utc_date.strftime("%Y%m%d"), "--target-write-live"]
    if utc_date != dt.datetime.now(dt.timezone.utc).date():
        args.append("--target-no-ledger")
    return runner_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
