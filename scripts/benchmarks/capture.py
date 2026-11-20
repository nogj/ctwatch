"""Two independent collectors share an absolute time window; never use AWS."""

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

from common import connection, counters, match_names, metrics, names_matcher, watchlist
from websockets.asyncio.client import ClientConnection, connect

from ctwatch.engine import Engine
from ctwatch.registry import Registry
from ctwatch.source import Sources


class CountingConnection(ClientConnection):
    wire_bytes = 0

    def data_received(self, data):
        self.wire_bytes += len(data)
        super().data_received(data)


async def direct(args, db):
    sources = Sources(16)
    registry = Registry()
    logs = [log for log in await registry.refresh(sources) if log.state in {"usable", "qualified"}]
    engine = Engine(watchlist(), "data/confusables.txt", workers=2, cache_size=100000)
    stats = {
        log.log_id: {
            "name": log.name,
            "kind": log.kind,
            "entries": 0,
            "errors": [],
            "head_ok": False,
        }
        for log in logs
    }
    counts = {"entries": 0, "matches": 0, "body_bytes": 0}
    wire_get = sources.get

    async def metered_get(url, params=None):
        response = await wire_get(url, params)
        counts["body_bytes"] += response.num_bytes_downloaded
        return response

    sources.get = metered_get

    async def tail(log):
        row = stats[log.log_id]
        position = None
        while time.time() < args.end:
            try:
                size, _ = await sources.head(log)
                row["head_ok"] = True
                if position is None:
                    position = size  # No seed: compare newly observed entries only.
                    row["initial_head"] = size
                row["head"] = size
                row["position"] = position
                while position < size and time.time() < args.end:
                    entries = await sources.read_range(log, position, size, limit=1024)
                    values = await engine.analyze(entries)
                    seen = time.time() * 1000
                    db.executemany(
                        "INSERT INTO observations VALUES(NULL,?,?,?,?,?,?,?)",
                        [
                            (
                                entry.fingerprint,
                                log.log_id,
                                entry.index,
                                entry.timestamp_ms,
                                seen,
                                None,
                                len(entry.der),
                            )
                            for entry in entries
                        ],
                    )
                    # Limit raw certificates for offline replay, keeping ALL identity observations.
                    if counts["entries"] < args.corpus_limit:
                        db.executemany(
                            "INSERT OR IGNORE INTO certificates VALUES(?,?)",
                            [(entry.fingerprint, entry.der) for entry in entries],
                        )
                    db.commit()
                    counts["entries"] += len(entries)
                    counts["matches"] += sum(map(len, values))
                    row["entries"] += len(entries)
                    position += len(entries)
                    row["position"] = position
                await asyncio.sleep(min(15, max(0, args.end - time.time())))
            except Exception as error:
                detail = f"{type(error).__name__}: {str(error)[:200]}"
                if detail not in row["errors"] and len(row["errors"]) < 5:
                    row["errors"].append(detail)
                await asyncio.sleep(min(10, max(0, args.end - time.time())))

    tasks = [asyncio.create_task(tail(log)) for log in logs]
    try:
        _, pending = await asyncio.wait(tasks, timeout=max(0, args.end - time.time()) + 20)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await asyncio.to_thread(engine.pool.shutdown, wait=True, cancel_futures=True)
        engine.close()
        await sources.close()
    counts.update(
        cache_hits=engine.hits,
        cache_misses=engine.misses,
        logs=list(stats.values()),
        catalogs=registry.report,
    )
    return counts


async def websocket(args, db):
    matcher, cache = names_matcher()
    counts = {
        "entries": 0,
        "matches": 0,
        "json_bytes": 0,
        "wire_bytes": 0,
        "invalid_sha": 0,
        "errors": [],
        "sources": {},
    }
    await asyncio.sleep(max(0, args.start - time.time()))
    try:
        async with connect(
            args.websocket,
            compression="deflate",
            max_size=4 * 1024 * 1024,
            max_queue=128,
            open_timeout=20,
            create_connection=CountingConnection,
            proxy=None,
        ) as ws:
            counts["negotiated_extensions"] = [str(item) for item in ws.protocol.extensions]
            while time.time() < args.end:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=max(0.01, args.end - time.time())
                    )
                except TimeoutError:
                    break
                seen = time.time() * 1000
                event = json.loads(raw)
                if event.get("message_type") != "certificate_update":
                    continue
                data = event["data"]
                cert = data["leaf_cert"]
                sha = cert.get("sha256", "").replace(":", "").lower()
                if len(sha) != 64:
                    counts["invalid_sha"] += 1
                    continue
                names = cert.get("all_domains", [])
                values = match_names(names, matcher, cache)
                source = data.get("source", {}).get("name", "unknown")
                counts["sources"][source] = counts["sources"].get(source, 0) + 1
                db.execute(
                    "INSERT INTO observations VALUES(NULL,?,?,?,?,?,?,?)",
                    (
                        sha,
                        source,
                        data.get("cert_index"),
                        data.get("submission_timestamp", data.get("seen", 0)) * 1000,
                        seen,
                        len(names),
                        len(raw.encode() if isinstance(raw, str) else raw),
                    ),
                )
                counts["entries"] += 1
                counts["matches"] += len(values)
                counts["json_bytes"] += len(raw.encode() if isinstance(raw, str) else raw)
                if counts["entries"] % 1000 == 0:
                    db.commit()
            counts["wire_bytes"] = ws.wire_bytes
    except Exception as error:
        counts["errors"].append(f"{type(error).__name__}: {str(error)[:300]}")
    db.commit()
    return counts


async def run(args):
    await asyncio.sleep(max(0, args.start - time.time()))
    before = counters()
    db = connection(args.output / (args.mode + ".sqlite"))
    result = await (direct(args, db) if args.mode == "direct" else websocket(args, db))
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
    result.update(metrics(before))
    result.update(
        mode=args.mode, start=args.start, end=args.end, watch_domains=10002, poll_seconds=15
    )
    (args.output / (args.mode + ".json")).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "logs"}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["direct", "websocket"])
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--end", type=float, required=True)
    parser.add_argument("--websocket", default="ws://ctwatch-bench-server:8080/")
    parser.add_argument("--output", type=Path, default=Path("/results"))
    parser.add_argument("--corpus-limit", type=int, default=100000)
    logging.basicConfig(level=logging.ERROR)
    args = parser.parse_args()
    if args.end <= args.start:
        raise SystemExit("end must follow start")
    asyncio.run(run(args))
