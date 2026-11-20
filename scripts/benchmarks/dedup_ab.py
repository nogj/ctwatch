"""Replay identical real CT observations through production Engine and a no-dedup baseline."""

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from common import counters, metrics, watchlist
from cryptography import x509

from ctwatch.ct import Entry
from ctwatch.engine import Engine
from ctwatch.matcher import analyze_batch, canonical


class NoDedupEngine(Engine):
    async def _analyze(self, entries):
        self.misses += len(entries)
        return await asyncio.get_running_loop().run_in_executor(
            self.pool, analyze_batch, [entry.der for entry in entries]
        )


def ready():
    time.sleep(0.05)
    return os.getpid()


def load(args):
    db = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
    # Recorded sequence, not shuffled or synthetically repeated. Same slice for every trial.
    rows = db.execute(
        """SELECT o.idx,o.accepted_ms,c.der FROM observations o
                      JOIN certificates c ON c.sha=o.sha ORDER BY o.n LIMIT ?""",
        (args.limit,),
    )
    entries = [Entry(index, int(stamp), der, "certificate") for index, stamp, der in rows]
    db.close()
    if len(entries) < 1000:
        raise ValueError("capture too small for a meaningful replay")
    document = watchlist()
    controls = set()
    for entry in entries[:256]:
        try:
            certificate = x509.load_der_x509_certificate(entry.der)
            names = certificate.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value.get_values_for_type(x509.DNSName)
            for name in names:
                try:
                    controls.add(canonical(name)[0])
                except ValueError:
                    continue
                if len(controls) >= 32:
                    break
        except x509.ExtensionNotFound:
            continue
        if len(controls) >= 32:
            break
    document["domains"].extend(
        {"domain": name, "include_subdomains": True} for name in sorted(controls)
    )
    return entries, document, len(controls)


async def trial(args):
    entries, document, controls = load(args)
    cls = Engine if args.mode == "on" else NoDedupEngine
    engine = cls(document, "data/confusables.txt", workers=args.workers, cache_size=200000)
    # Start ALL workers before timing; both variants start with empty analysis/name caches.
    loop = asyncio.get_running_loop()
    pids = set()
    while len(pids) < args.workers:
        pids.update(
            await asyncio.gather(
                *[loop.run_in_executor(engine.pool, ready) for _ in range(args.workers * 2)]
            )
        )
    before = counters()
    digest, matches = hashlib.sha256(), 0
    for offset in range(0, len(entries), 4096):
        batch = entries[offset : offset + 4096]
        values = await engine.analyze(batch)
        for value in values:
            body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            digest.update(len(body).to_bytes(4, "big") + body)
            matches += len(value)
    result = metrics(before)
    # All work completed above; shutdown is outside the timed analysis window.
    await asyncio.to_thread(engine.pool.shutdown, wait=True)
    engine.close()
    result.update(
        mode=args.mode,
        entries=len(entries),
        workers=args.workers,
        certificate_cache_hits=engine.hits,
        certificate_cache_misses=engine.misses,
        output_sha256=digest.hexdigest(),
        match_observations=matches,
        positive_control_domains=controls,
        watch_domains=len(document["domains"]),
        ruleset_version=engine.version,
    )
    print(json.dumps(result), flush=True)


def main(args):
    if args.mode:
        asyncio.run(trial(args))
        return
    trials = []
    for index, mode in enumerate(["off", "on", "on", "off", "off", "on"]):
        command = [
            sys.executable,
            __file__,
            "--mode",
            mode,
            "--database",
            str(args.database),
            "--limit",
            str(args.limit),
            "--workers",
            str(args.workers),
        ]
        process = subprocess.run(command, check=True, capture_output=True, text=True)
        value = json.loads(process.stdout)
        trials.append(value)
        print(json.dumps({"trial": index + 1, **value}), flush=True)
    if len({value["output_sha256"] for value in trials}) != 1:
        raise RuntimeError("A/B outputs differ")
    import statistics

    summary = {}
    for mode in ["off", "on"]:
        rows = [value for value in trials if value["mode"] == mode]
        summary[mode] = {
            key: statistics.median(row[key] for row in rows)
            for key in ["cpu_seconds", "elapsed_seconds", "memory_peak_bytes"]
        }
    report = {
        "trials": trials,
        "medians": summary,
        "outputs_identical": True,
        "cpu_reduction_fraction": 1 - summary["on"]["cpu_seconds"] / summary["off"]["cpu_seconds"],
        "wall_reduction_fraction": 1
        - summary["on"]["elapsed_seconds"] / summary["off"]["elapsed_seconds"],
        "limitations": [
            "Offline CPU analysis only; excludes network, DynamoDB and SQS.",
            "Name caches are enabled in both variants; certificate dedup alone is toggled.",
            "Positive controls add 32 captured domains to the same synthetic watchlist.",
            "One batch at a time: isolates dedup and does not test maximum parallel throughput.",
            "Cgroup memory peak is cumulative across trials; not an isolated per-trial peak.",
        ],
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "trials"}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("/results/direct.sqlite"))
    parser.add_argument("--limit", type=int, default=100000)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--mode", choices=["on", "off"])
    parser.add_argument("--output", type=Path, default=Path("/results/dedup-ab.json"))
    main(parser.parse_args())
