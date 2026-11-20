"""Offline exact duplicate accounting and idealized LRU sizing from a decision trace."""

import argparse
import hashlib
import json
from collections import Counter, OrderedDict
from pathlib import Path

if __package__:
    from .dedup_audit import RECORD
else:
    from dedup_audit import RECORD


def records(path):
    with path.open("rb") as source:
        while chunk := source.read(RECORD.size * 100000):
            if len(chunk) % RECORD.size:
                raise ValueError("truncated dedup trace")
            yield from RECORD.iter_unpack(chunk)


def percentiles(values):
    ordered = sorted(values)
    return {
        name: ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]
        if ordered
        else None
        for name, fraction in (("p50", 0.5), ("p90", 0.9), ("p95", 0.95), ("p99", 0.99), ("max", 1))
    }


def distribution(values):
    exact = Counter(values)
    buckets = Counter()
    for value, count in exact.items():
        label = (
            str(value)
            if value <= 2
            else "3-4"
            if value <= 4
            else "5-9"
            if value <= 9
            else "10-19"
            if value <= 19
            else "20-49"
            if value <= 49
            else "50-99"
            if value <= 99
            else "100+"
        )
        buckets[label] += count
    ordered = sorted(values, reverse=True)
    total = sum(values)
    top = max(1, (len(values) + 99) // 100)
    return {
        "population_entries": len(values),
        "total": total,
        "percentiles_including_zero": percentiles(values),
        "buckets": {
            k: buckets[k] for k in ("0", "1", "2", "3-4", "5-9", "10-19", "20-49", "50-99", "100+")
        },
        "exact_histogram": dict(sorted(exact.items())),
        "top_one_percent_entries": top,
        "top_one_percent_share": sum(ordered[:top]) / total if total else None,
    }


def analyze(path, capacities, ttl=3600):
    # Fingerprint -> [observations, cache hits, in-flight hits, analysis calls].
    entries = {}
    phases = {key: Counter() for key in ("before_full", "after_full")}
    minutes = {}
    first_time = None
    first_full = None
    for timestamp, outcome, full, key in records(path):
        if first_time is None:
            first_time = timestamp
        if full and first_full is None:
            first_full = timestamp
        phase = phases["after_full" if first_full is not None else "before_full"]
        minute = int((timestamp - first_time) // 60)
        counters = (phase, minutes.setdefault(minute, Counter()))
        previous = entries.get(key)
        repeat = previous is not None
        row = entries.setdefault(key, [0, 0, 0, 0])
        row[0] += 1
        row[{0: 3, 1: 1, 2: 2}[outcome]] += 1
        for counter in counters:
            counter["observations"] += 1
            counter["duplicate_observations"] += int(repeat)
            counter["analysis_calls"] += int(outcome == 0)
            counter["reanalysis_calls"] += int(outcome == 0 and repeat)
            counter["cache_hits"] += int(outcome == 1)
            counter["inflight_hits"] += int(outcome == 2)
    if not entries:
        raise ValueError("empty audit trace")
    for phase in phases.values():
        phase["reanalysis_fraction_of_analysis"] = (
            phase["reanalysis_calls"] / phase["analysis_calls"] if phase["analysis_calls"] else None
        )
        phase["duplicate_leakage_fraction"] = (
            phase["reanalysis_calls"] / phase["duplicate_observations"]
            if phase["duplicate_observations"]
            else None
        )
    unique = len(entries)
    result = {
        "trace_record_bytes": RECORD.size,
        "unique_der_fingerprints_entire_window": unique,
        "first_cache_full_seconds_from_first_lookup": first_full - first_time
        if first_full
        else None,
        "observed_phases": phases,
        "observed_minute_buckets": minutes,
        "cache_hits_per_certificate": distribution([row[1] for row in entries.values()]),
        "inflight_hits_per_certificate": distribution([row[2] for row in entries.values()]),
        "reanalyses_per_certificate": distribution(
            [max(0, row[3] - 1) for row in entries.values()]
        ),
        "observations_per_certificate": distribution([row[0] for row in entries.values()]),
        "ideal_lru_capacity_curve": [],
        "limitations": [
            "Observed outcomes are traced at the actual cache lookup, including in-flight hits.",
            "Reference set covers the complete capture and does not expire or evict entries.",
            "Capacity simulation inserts results immediately and uses lookup order; "
            "actual worker completions can reorder insertions.",
            "Simulation TTL is fixed from insertion; hits update LRU order but do not extend TTL.",
            "Curve measures avoided analyses, not CPU time or output event deduplication.",
            "The measured task includes trace I/O and page-cache overhead; "
            "the offline oracle does not.",
            "Short capture and changing source activity do not establish a universal optimal size.",
        ],
    }
    del entries
    for capacity in capacities:
        cache = OrderedDict()
        hits = misses = 0
        for timestamp, _, _, key in records(path):
            expiry = cache.pop(key, None)
            if expiry is not None and expiry > timestamp:
                hits += 1
                cache[key] = expiry
            else:
                misses += 1
                cache[key] = timestamp + ttl
                if len(cache) > capacity:
                    cache.popitem(last=False)
        result["ideal_lru_capacity_curve"].append(
            {
                "capacity": capacity,
                "hits": hits,
                "analysis_calls": misses,
                "reanalyses": misses - unique,
                "hit_fraction": hits / (hits + misses),
                "ttl_seconds": ttl,
            }
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--probe-report", type=Path)
    parser.add_argument(
        "--capacities", type=int, nargs="+", default=[100000, 200000, 500000, 1000000]
    )
    args = parser.parse_args()
    if any(capacity <= 0 for capacity in args.capacities):
        parser.error("capacities must be positive")
    result = analyze(args.trace, args.capacities)
    with args.trace.open("rb") as source:
        result["trace_sha256"] = hashlib.file_digest(source, "sha256").hexdigest()
    result["trace_records"] = sum(
        phase.get("observations", 0) for phase in result["observed_phases"].values()
    )
    if args.probe_report:
        probe = json.loads(args.probe_report.read_text())
        phases = result["observed_phases"].values()
        analyses = sum(p.get("analysis_calls", 0) for p in phases)
        hits = sum(p.get("cache_hits", 0) + p.get("inflight_hits", 0) for p in phases)
        if (analyses, hits) != (probe["certificate_cache_misses"], probe["certificate_cache_hits"]):
            raise ValueError("trace outcomes do not reconcile with the probe counters")
        result["trace_counters_reconcile_with_probe"] = True
        result["capture"] = {
            key: probe[key]
            for key in (
                "started_utc",
                "elapsed_seconds",
                "configured_seconds",
                "workers",
                "watch_domains",
                "certificate_cache_capacity",
                "initial_seed_entries_per_log",
            )
        }
        result["pre_persistence_event_repetition_counts"] = probe[
            "match_event_repetitions_before_persistence"
        ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: v
                for k, v in result.items()
                if k
                not in {
                    "observed_minute_buckets",
                    "cache_hits_per_certificate",
                    "inflight_hits_per_certificate",
                    "reanalyses_per_certificate",
                    "observations_per_certificate",
                }
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
