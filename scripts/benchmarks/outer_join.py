"""Audit the full outer join of recorded SHA-256 identities, without new CT traffic."""

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path


def load(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = db.execute(
        "SELECT sha, seen_ms, accepted_ms, log FROM observations ORDER BY n"
    ).fetchall()
    db.close()
    first = {}
    for sha, seen, accepted, source in rows:
        if sha not in first or seen < first[sha]["seen_ms"]:
            first[sha] = {"seen_ms": seen, "accepted_ms": accepted, "source": source}
    return rows, first


def counts(left, right):
    left, right = set(left), set(right)
    union = left | right
    return {
        "both": len(left & right),
        "direct_only": len(left - right),
        "websocket_only": len(right - left),
        "full_outer": len(union),
        "direct_unique": len(left),
        "websocket_unique": len(right),
    }


def main(directory, output):
    raw, direct = load(directory / "direct.sqlite")
    ws_raw, ws = load(directory / "websocket.sqlite")
    metadata = json.loads((directory / "direct.json").read_text())
    start, end = metadata["start"] * 1000, metadata["end"] * 1000
    groups = {
        "both": direct.keys() & ws.keys(),
        "direct_only": direct.keys() - ws.keys(),
        "websocket_only": ws.keys() - direct.keys(),
    }
    boundaries = {}
    for name, keys in groups.items():
        # One common reference timestamp per identity, never separate cohort filters.
        stamps = [
            min(feed[key]["accepted_ms"] for feed in (direct, ws) if key in feed) for key in keys
        ]
        boundaries[name] = dict(
            Counter(
                "before_start"
                if stamp < start
                else "first_30s"
                if stamp < start + 30000
                else "central_60s"
                if stamp <= end - 30000
                else "last_30s"
                if stamp <= end
                else "after_end"
                for stamp in stamps
            )
        )
    direct_cutoff = {sha for sha, row in direct.items() if row["seen_ms"] <= end}
    ws_cutoff = {sha for sha, row in ws.items() if row["seen_ms"] <= end}
    central_keys = {
        sha
        for sha in direct.keys() | ws.keys()
        if start + 30000
        <= min(feed[sha]["accepted_ms"] for feed in (direct, ws) if sha in feed)
        <= end - 30000
    }
    multiplicity = Counter(sha for sha, *_ in raw)
    result = {
        "join_key": "SHA-256 of certificate DER; precertificate and final remain distinct",
        "all_recorded": counts(direct, ws),
        "same_reception_deadline": counts(direct_cutoff, ws_cutoff),
        "central_acceptance_cohort": counts(direct.keys() & central_keys, ws.keys() & central_keys),
        "acceptance_bins_using_min_timestamp_across_both_feeds": boundaries,
        "direct_observations": len(raw),
        "websocket_observations": len(ws_raw),
        "direct_repeated_observations": len(raw) - len(direct),
        "direct_multiplicity_histogram": dict(sorted(Counter(multiplicity.values()).items())),
        "direct_first_seen_after_end": len(direct) - len(direct_cutoff),
        "direct_only_first_seen_after_end": len(groups["direct_only"] - direct_cutoff),
        "ws_only_by_first_reported_source": dict(
            Counter(ws[sha]["source"] for sha in groups["websocket_only"]).most_common()
        ),
        "limitations": [
            "Full outer union is observed evidence, not ecosystem ground truth.",
            "Missing within a finite capture does not prove permanent loss.",
            "Submission timestamps differ by log; common cohort uses minimum recorded timestamp.",
            "WebSocket source is the first reported source after upstream dedup, not all sources.",
            "Shared public IP, different catalogs, startup and draining confound comparison.",
        ],
    }
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    main(args.directory, args.output)
