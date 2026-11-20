"""Refresh the bundled CCADB CAA snapshot from its public CSV (no AWS)."""

import argparse
import json
from pathlib import Path

import httpx

from ctwatch.issuer_catalog import CCADB_URL, snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Use a previously downloaded official CSV")
    parser.add_argument("--output", type=Path, default=Path("data/ccadb-caa.json"))
    args = parser.parse_args()
    if args.input:
        text = args.input.read_text(encoding="utf-8-sig")
    else:
        response = httpx.get(CCADB_URL, timeout=60, follow_redirects=True)
        response.raise_for_status()
        text = response.content.decode("utf-8-sig")
    data = snapshot(text)
    args.output.write_text(json.dumps(data, ensure_ascii=False) + "\n")
    print(json.dumps({key: value for key, value in data.items() if key != "csv"}, indent=2))


if __name__ == "__main__":
    main()
