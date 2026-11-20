"""Audit catalog overlap and optionally verify heads of Apple-only active logs. No AWS."""

import argparse
import asyncio
import json
from pathlib import Path

from ctwatch.registry import Registry
from ctwatch.source import Sources


async def check(args):
    sources = Sources(4)
    try:
        registry = Registry()
        logs = await registry.refresh(sources)
        if any(row["stale"] for row in registry.report.values()):
            raise RuntimeError(f"both catalogs required for comparison: {registry.report}")
        active = {
            name: {log.log_id for log in rows if log.state in {"usable", "qualified"}}
            for name, rows in registry.snapshots.items()
        }
        added = active["apple"] - active["chrome"]
        rows = []
        for log in logs:
            if log.log_id not in added:
                continue
            row = {"log_id": log.log_id, "name": log.name, "kind": log.kind, "url": log.url}
            if args.heads:
                try:
                    size, root = await asyncio.wait_for(sources.head(log), timeout=30)
                    row.update(head_verified=True, tree_size=size, root=root)
                except Exception as error:
                    row.update(head_verified=False, error=f"{type(error).__name__}: {error}")
            rows.append(row)
        report = {
            "catalogs": registry.report,
            "chrome_active": len(active["chrome"]),
            "apple_active": len(active["apple"]),
            "shared_active": len(active["chrome"] & active["apple"]),
            "union_active": len(active["chrome"] | active["apple"]),
            "apple_only_active": rows,
            "limitations": (
                "Verified heads are not evidence of sustained entry ingestion or coverage."
            ),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    finally:
        await sources.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--heads", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(".deploy/catalogs.json"))
    asyncio.run(check(parser.parse_args()))
