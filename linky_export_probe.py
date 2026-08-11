#!/usr/bin/env python3
"""Read-only probe for Linky's optional streamer CSV export."""

from __future__ import annotations

import argparse
import json
import os

from linky_export import (ExportValidationError, download_export,
    request_export_url, validate_streamer_export)
from linky_fetch import _authenticated_call


DEFAULT_TOKENS = "/home/ubuntu/.config/nova/linky-guild-tokens.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--guild", required=True)
    parser.add_argument("--business-date", required=True)
    parser.add_argument("--tokens", default=os.getenv("LINKE_GUILD_TOKENS", DEFAULT_TOKENS))
    args = parser.parse_args(argv)
    call = _authenticated_call(args.guild, args.tokens)
    path = (f"/api/guild/streamer_stat?begin={args.business_date}&end={args.business_date}"
        "&page_num=1&page_size=1&type=0")
    summary = call(path)
    try:
        expected_count = int(summary["total"])
        expected_amount = summary["total_item"]["total_earns"]
    except (KeyError, TypeError, ValueError):
        print(json.dumps({"status": "REJECTED", "code": "INVALID_API_SUMMARY"}))
        return 2
    try:
        url = request_export_url(args.guild, args.business_date, tokens_path=args.tokens)
        raw = download_export(url)
        _, evidence = validate_streamer_export(raw, business_date=args.business_date,
            expected_row_count=expected_count, expected_amount=expected_amount)
    except ExportValidationError as error:
        output = {"status": "REJECTED", "code": error.code}
        if error.evidence is not None:
            output["evidence"] = error.evidence.as_dict()
        print(json.dumps(output, sort_keys=True))
        return 2
    print(json.dumps({"status": "PASSED", "evidence": evidence.as_dict()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

