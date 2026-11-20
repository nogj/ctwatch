"""Bounded multi-log ingestion probe. No boto3, durable cursors or event publication."""

import argparse
import asyncio
import datetime
import json
import logging
import platform
import resource
import time
from collections import Counter
from pathlib import Path

import httpx
from cryptography import x509
from dedup_audit import DedupAudit
from resource_probe import ResourceMonitor

from ctwatch.caa import CAAValidator
from ctwatch.config import DEFAULT_POLL_SECONDS, positive_int
from ctwatch.engine import Engine
from ctwatch.issuer_catalog import IssuerCatalog
from ctwatch.matcher import canonical
from ctwatch.registry import APPLE_LOG_LIST_URL, CHROME_LOG_LIST_URL, Registry
from ctwatch.source import Sources
from ctwatch.store import make_event


class MeteredSources(Sources):
    def __init__(self, concurrency):
        super().__init__(concurrency)
        self.requests = 0
        self.downloaded_bytes = 0
        self.statuses = Counter()

    async def get(self, url, params=None):
        self.requests += 1
        try:
            response = await super().get(url, params)
        except httpx.HTTPStatusError as error:
            self.statuses[str(error.response.status_code)] += 1
            self.downloaded_bytes += error.response.num_bytes_downloaded
            raise
        else:
            self.statuses[str(response.status_code)] += 1
            self.downloaded_bytes += response.num_bytes_downloaded
            return response


def percentiles(values):
    if not values:
        return None
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        **{
            name: round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * p))], 3)
            for name, p in [("p50", 0.5), ("p95", 0.95), ("p99", 0.99)]
        },
    }


async def probe(args):
    sources = MeteredSources(args.concurrency)
    engine = None
    audit = None
    stats = {}
    stages = {name: [] for name in ("read", "analyze_including_queue", "enrich")}

    def telemetry():
        return {
            "certificate_cache_entries": len(engine.cache.items) if engine else 0,
            "certificate_cache_hits": engine.hits if engine else 0,
            "certificate_cache_misses": engine.misses if engine else 0,
            "analysis_active_calls": engine.active if engine else 0,
            "analysis_inflight_certificates": len(engine.inflight) if engine else 0,
            "entries_processed": sum(row["entries"] for row in stats.values()),
            "backlog_last_observed_heads": sum(row.get("backlog", 0) for row in stats.values()),
            "http_requests": sources.requests,
            "http_statuses": dict(sources.statuses),
            "logs": {
                key: {
                    field: row.get(field)
                    for field in ("entries", "backlog", "head_observed_monotonic", "error_count")
                }
                for key, row in stats.items()
            },
        }

    monitor = (
        ResourceMonitor(
            extra=telemetry,
            journal=args.output.with_suffix(".resources.jsonl"),
        )
        if args.measure_resources
        else None
    )
    try:
        if monitor is not None:
            monitor.start()
        registry = Registry(args.registry, args.apple_registry)
        logs = [
            log for log in await registry.refresh(sources) if log.state in {"usable", "qualified"}
        ]
        document = json.loads(args.watchlist.read_text())
        for i in range(args.synthetic_domains):
            document["domains"].append({"domain": f"ctwatch-load-{i:05d}.invalid"})
        controls, control_names, control_log = [], set(), None
        if args.positive_controls:
            control_log = next(log for log in logs if log.kind == "static")
            size, _ = await sources.head(control_log)
            controls = await sources.entries(
                control_log, max(0, size - args.positive_controls), size
            )
            for entry in controls:
                certificate = x509.load_der_x509_certificate(entry.der)
                try:
                    names = certificate.extensions.get_extension_for_class(
                        x509.SubjectAlternativeName
                    ).value.get_values_for_type(x509.DNSName)
                except x509.ExtensionNotFound:
                    continue
                for name in names:
                    try:
                        control_names.add(canonical(name)[0])
                    except (ValueError, UnicodeError):
                        continue
                    if len(control_names) >= 32:
                        break
                if len(control_names) >= 32:
                    break
            if not control_names:
                raise ValueError("no DNS SAN names available for positive controls")
            document["domains"].extend({"domain": name} for name in sorted(control_names))
        engine = Engine(
            document, "data/confusables.txt", workers=args.workers, cache_size=args.cache_entries
        )
        catalog = IssuerCatalog() if args.caa else None
        caa = CAAValidator(catalog=catalog) if args.caa else None
        caa_stats = {"live": Counter(), "controls": Counter()}
        caa_reasons = {"live": Counter(), "controls": Counter()}
        unique_events = {"live": {}, "controls": {}}
        event_repetitions = {
            phase: Counter() for phase in ("before_cache_full", "after_cache_full")
        }

        async def enrich(entries, values, log, phase):
            events = [
                make_event(entry, log, match, engine.version)
                for entry, matches in zip(entries, values, strict=True)
                for match in matches
            ]
            if caa is not None:
                await caa.enrich(
                    events, {entry.fingerprint: entry for entry in entries}, engine.caa_issuers
                )
            for event in events:
                if phase == "live":
                    cache_phase = (
                        "after_cache_full"
                        if len(engine.cache.items) >= args.cache_entries
                        else "before_cache_full"
                    )
                    event_repetitions[cache_phase]["observations"] += 1
                    event_repetitions[cache_phase]["repeated_event_ids"] += int(
                        event["event_id"] in unique_events[phase]
                    )
                if "caa" in event:
                    caa_stats[phase][event["caa"]["status"]] += 1
                    caa_reasons[phase][event["caa"]["reason"]] += 1
                unique_events[phase].setdefault(event["event_id"], event)

        if controls:
            await enrich(controls, await engine.analyze(controls), control_log, "controls")
        # Control preparation must not warm certificate/name caches in the timed run.
        if controls:
            engine.close()
            await asyncio.to_thread(engine.pool.shutdown, wait=True)
            engine = Engine(
                document,
                "data/confusables.txt",
                workers=args.workers,
                cache_size=args.cache_entries,
            )
            caa = CAAValidator(catalog=catalog) if args.caa else None
        stats = {
            log.log_id: {
                "name": log.name,
                "url": log.url,
                "kind": log.kind,
                "state": log.state,
                "entries": 0,
                "new_entries": 0,
                "matches": 0,
                "error_count": 0,
                "errors": [],
                "head_ok": False,
                "read_ok": False,
            }
            for log in logs
        }
        started = time.monotonic()
        if args.audit_dedup:
            audit = DedupAudit(engine, args.output.with_suffix(".dedup.bin"))
        if monitor is not None:
            monitor.mark("ingestion_start")
        started_utc = datetime.datetime.now(datetime.UTC).isoformat()
        deadline = started + args.seconds
        ages = []

        async def tail(log):
            row = stats[log.log_id]
            position, previous_size, previous_root = None, None, None
            failures = 0
            while time.monotonic() < deadline:
                try:
                    size, root = await sources.head(log)
                    row["head_ok"] = True
                    if previous_size is not None and (
                        size < previous_size or (size == previous_size and root != previous_root)
                    ):
                        raise ValueError("inconsistent tree head")
                    previous_size, previous_root = size, root
                    row["observed_head"] = size
                    row["head_observed_monotonic"] = time.monotonic()
                    if position is None:
                        row["initial_head"] = size
                        position = max(0, size - args.initial_entries)
                    # Record backlog even when the very first download fails or is throttled.
                    row["position"] = position
                    row["backlog"] = size - position
                    while position < size and time.monotonic() < deadline:
                        stage_start = time.monotonic()
                        entries = await sources.read_range(log, position, size, limit=4096)
                        stages["read"].append(time.monotonic() - stage_start)
                        stage_start = time.monotonic()
                        values = await engine.analyze(entries)
                        stages["analyze_including_queue"].append(time.monotonic() - stage_start)
                        stage_start = time.monotonic()
                        await enrich(entries, values, log, "live")
                        stages["enrich"].append(time.monotonic() - stage_start)
                        now_ms = time.time() * 1000
                        ages.extend(
                            (now_ms - entry.timestamp_ms) / 1000
                            for entry in entries[::32]
                            if entry.index >= row["initial_head"]
                        )
                        row["entries"] += len(entries)
                        row["new_entries"] += sum(
                            entry.index >= row["initial_head"] for entry in entries
                        )
                        row["matches"] += sum(map(len, values))
                        row["read_ok"] = True
                        position += len(entries)
                        row["position"] = position
                        row["backlog"] = size - position
                    row["position"] = position
                    row["backlog"] = size - position
                    failures = 0
                    await asyncio.sleep(min(args.poll_seconds, max(0, deadline - time.monotonic())))
                except Exception as error:
                    failures += 1
                    row["error_count"] += 1
                    detail = f"{type(error).__name__}: {str(error)[:400]}"
                    if detail not in row["errors"] and len(row["errors"]) < 5:
                        row["errors"].append(detail)
                        print(json.dumps({"log": log.name, "error": detail}), flush=True)
                    await asyncio.sleep(
                        min(30, 2 ** min(failures, 5), max(0, deadline - time.monotonic()))
                    )

        tasks = [asyncio.create_task(tail(log)) for log in logs]
        print(
            json.dumps(
                {
                    "event": "started",
                    "logs": len(logs),
                    "protocols": dict(Counter(log.kind for log in logs)),
                    "states": dict(Counter(log.state for log in logs)),
                    "watch_domains": len(document["domains"]),
                    "seconds": args.seconds,
                    "workers": args.workers,
                }
            ),
            flush=True,
        )
        while time.monotonic() < deadline:
            await asyncio.sleep(min(20, max(0, deadline - time.monotonic())))
            print(
                json.dumps(
                    {
                        "event": "progress",
                        "elapsed_seconds": round(time.monotonic() - started),
                        "heads_ok": sum(row["head_ok"] for row in stats.values()),
                        "logs_with_entries": sum(row["read_ok"] for row in stats.values()),
                        "entries": sum(row["entries"] for row in stats.values()),
                        "cache_hits": engine.hits,
                        "cache_misses": engine.misses,
                        "cache_entries": len(engine.cache.items),
                        "backlog_last_observed_heads": sum(
                            row.get("backlog", 0) for row in stats.values()
                        ),
                        "analysis_active_calls": engine.active,
                        "http_downloaded_bytes": sources.downloaded_bytes,
                    }
                ),
                flush=True,
            )
        _, pending = await asyncio.wait(tasks, timeout=30)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(engine.pool.shutdown, wait=True, cancel_futures=True)
        engine.close()
        elapsed = time.monotonic() - started
        entries = sum(row["entries"] for row in stats.values())
        new_entries = sum(row["new_entries"] for row in stats.values())
        total_cache = engine.hits + engine.misses
        resources = (
            await monitor.finish(active_seconds=args.seconds) if monitor is not None else None
        )
        report = {
            "started_utc": started_utc,
            "elapsed_seconds": round(elapsed, 3),
            "configured_seconds": args.seconds,
            "python": platform.python_version(),
            "architecture": platform.machine(),
            "workers": args.workers,
            "certificate_cache_capacity": args.cache_entries,
            "certificate_cache_entries_at_end": len(engine.cache.items),
            "resources": resources,
            "http_concurrency": args.concurrency,
            "poll_seconds": args.poll_seconds,
            "dedup_audit_enabled": args.audit_dedup,
            "batch_stage_wall_seconds": {
                name: percentiles(values) for name, values in stages.items()
            },
            "watch_domains": len(document["domains"]),
            "synthetic_watch_domains": args.synthetic_domains,
            "initial_seed_entries_per_log": args.initial_entries,
            "ruleset_version": engine.version,
            "registry_timestamp": registry.report.get("chrome", {}).get("timestamp"),
            "catalogs": registry.report,
            "caa_enabled": args.caa,
            "caa_catalog": catalog.report() if catalog is not None else None,
            "positive_control_certificates": len(controls),
            "positive_control_domains": len(control_names),
            "caa_match_observation_statuses": caa_stats,
            "caa_match_observation_reasons": caa_reasons,
            "unique_match_events": {phase: len(values) for phase, values in unique_events.items()},
            "match_event_repetitions_before_persistence": event_repetitions,
            "caa_unique_event_statuses": {
                phase: dict(
                    Counter(event["caa"]["status"] for event in values.values() if "caa" in event)
                )
                for phase, values in unique_events.items()
            },
            "caa_event_samples": {
                phase: list(values.values())[:10] for phase, values in unique_events.items()
            },
            "logs_selected": len(logs),
            "logs_head_ok": sum(row["head_ok"] for row in stats.values()),
            "logs_with_entries": sum(row["read_ok"] for row in stats.values()),
            "entries_processed": entries,
            "new_entries_processed": new_entries,
            "entries_per_second": round(entries / elapsed, 2),
            "new_entries_per_second": round(new_entries / elapsed, 2),
            "certificate_cache_hits": engine.hits,
            "certificate_cache_misses": engine.misses,
            "cache_hit_fraction": round(engine.hits / total_cache, 4) if total_cache else None,
            "http_requests": sources.requests,
            "http_statuses": dict(sources.statuses),
            "http_body_downloaded_bytes": sources.downloaded_bytes,
            "sampled_acceptance_age_seconds": percentiles(ages),
            "parent_max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "children_max_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
            "logs": list(stats.values()),
            "limitations": [
                "No AWS integration, persistence or output publication; local Docker resources.",
                "Acceptance age includes log publication delay; it is not pipeline latency.",
                "No arrivals on a log during this interval does not mean it is faulty.",
                "A short run and synthetic watchlist do not establish production capacity or SLA.",
                "Backlog is relative to the last observed signed head, not the latest live head.",
                "Batch stage wall times include waiting and overlap across concurrent logs.",
                "Cache misses cease to count globally unique certificates after LRU eviction.",
                "Positive controls use recent real CT certificates and additional watched names.",
                "Live counts include the configured seed; controls are outside the timed window.",
                "CAA is current DNS policy; issuer lookup does not verify certificate signatures.",
                "HTTP counters include catalogs and positive-control preparation outside timing.",
            ],
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {
                    key: value
                    for key, value in report.items()
                    if key not in {"logs", "resources", "caa_event_samples"}
                },
                indent=2,
            ),
            flush=True,
        )
    finally:
        if audit is not None:
            audit.close()
        if monitor is not None and monitor.task is not None:
            await monitor.finish()
        if engine is not None:
            engine.close()
            engine.pool.shutdown(wait=True, cancel_futures=True)
        await sources.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=120, choices=range(10, 3601))
    parser.add_argument("--workers", type=int, default=3, choices=range(1, 17))
    parser.add_argument("--cache-entries", type=positive_int, default=200000)
    parser.add_argument("--measure-resources", action="store_true")
    parser.add_argument("--audit-dedup", action="store_true")
    parser.add_argument("--concurrency", type=int, default=16, choices=range(1, 65))
    parser.add_argument("--poll-seconds", type=positive_int, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--initial-entries", type=int, default=256, choices=range(0, 257))
    parser.add_argument("--synthetic-domains", type=int, default=10000, choices=range(0, 100001))
    parser.add_argument("--caa", action="store_true")
    parser.add_argument("--positive-controls", type=int, default=0, choices=range(0, 9))
    parser.add_argument("--output", type=Path, default=Path(".deploy/live-probe.json"))
    parser.add_argument("--watchlist", type=Path, default=Path("config/watchlist.example.json"))
    parser.add_argument("--registry", default=CHROME_LOG_LIST_URL)
    parser.add_argument("--apple-registry", default=APPLE_LOG_LIST_URL)
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(probe(parser.parse_args()))
