"""Read-only bounded live probe. No AWS calls, cursor writes or event publication."""

import argparse
import asyncio
import json
import time

from ctwatch.matcher import Matcher, analyze_batch, init_worker
from ctwatch.registry import APPLE_LOG_LIST_URL, CHROME_LOG_LIST_URL, Registry
from ctwatch.source import Sources


async def probe(args):
    sources = Sources(4)
    try:
        registry = Registry(args.registry, args.apple_registry)
        logs = [
            log
            for log in await registry.refresh(sources)
            if log.state == "usable" and log.kind == args.kind
        ]
        if args.name:
            logs = [log for log in logs if args.name.lower() in log.name.lower()]
        if not logs:
            raise SystemExit("no matching usable CT log")
        log = logs[0]
        size, root = await sources.head(log)
        start = max(0, size - args.entries)
        entries = await sources.entries(log, start, size)
        document = json.loads(args.watchlist.read_text())
        init_worker(document, "data/confusables.txt")
        begin = time.perf_counter()
        matches = analyze_batch([entry.der for entry in entries])
        elapsed = time.perf_counter() - begin
        print(
            json.dumps(
                {
                    "log": log.name,
                    "kind": log.kind,
                    "tree_size": size,
                    "root": root,
                    "downloaded_entries": len(entries),
                    "der_bytes": sum(len(entry.der) for entry in entries),
                    "parse_match_seconds": elapsed,
                    "ruleset": Matcher(document, "data/confusables.txt").version,
                    "matches": sum(map(len, matches)),
                },
                indent=2,
            )
        )
    finally:
        await sources.close()


if __name__ == "__main__":
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=["static", "rfc6962"], default="static")
    parser.add_argument("--name")
    parser.add_argument("--entries", type=int, default=32, choices=range(1, 257))
    parser.add_argument("--registry", default=CHROME_LOG_LIST_URL)
    parser.add_argument("--apple-registry", default=APPLE_LOG_LIST_URL)
    parser.add_argument("--watchlist", type=Path, default=Path("config/watchlist.example.json"))
    asyncio.run(probe(parser.parse_args()))
