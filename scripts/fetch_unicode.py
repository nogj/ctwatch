"""Fetch pinned Unicode data. No implicit updates during application startup."""

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

URL = "https://www.unicode.org/Public/security/16.0.0/confusables.txt"
ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    path = ROOT / "data/confusables.txt"
    manifest = ROOT / "data/unicode.json"
    if args.verify:
        expected = json.loads(manifest.read_text())["sha256"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise SystemExit("Unicode data checksum mismatch")
        print("Unicode data checksum OK")
        return
    with urllib.request.urlopen(URL, timeout=30) as response:
        data = response.read()
    path.write_bytes(data)
    manifest.write_text(
        json.dumps(
            {"version": "16.0.0", "url": URL, "sha256": hashlib.sha256(data).hexdigest()}, indent=2
        )
        + "\n"
    )
    with urllib.request.urlopen("https://www.unicode.org/license.txt", timeout=30) as response:
        (ROOT / "data/UNICODE-LICENSE.txt").write_bytes(response.read())


if __name__ == "__main__":
    main()
