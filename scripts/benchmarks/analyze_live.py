"""Compare observed identities, conditional arrival times and complete-stack resource usage."""

import argparse
import json
import sqlite3
from pathlib import Path


def percentile(values):
    if not values:
        return None
    ordered = sorted(values)
    return {
        "n": len(ordered),
        **{
            name: ordered[int((len(ordered) - 1) * fraction)]
            for name, fraction in [("p50", 0.5), ("p95", 0.95)]
        },
    }


def observations(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    records = {
        sha: {"seen_ms": seen, "accepted_ms": accepted}
        for sha, seen, accepted in db.execute(
            "SELECT sha,MIN(seen_ms),MIN(accepted_ms) FROM observations GROUP BY sha"
        )
    }
    db.close()
    return records


def compare(direct, websocket, start, end):
    shared = direct.keys() & websocket.keys()
    union = direct.keys() | websocket.keys()
    delta = [(websocket[sha]["seen_ms"] - direct[sha]["seen_ms"]) / 1000 for sha in shared]
    # Exclude startup/end boundaries AND observations with old submission timestamps.
    left = {
        sha for sha, row in direct.items() if start + 30 <= row["accepted_ms"] / 1000 <= end - 30
    }
    right = {
        sha for sha, row in websocket.items() if start + 30 <= row["accepted_ms"] / 1000 <= end - 30
    }
    return {
        "direct_unique": len(direct),
        "websocket_unique": len(websocket),
        "shared_unique": len(shared),
        "union_unique": len(union),
        "direct_only": len(direct.keys() - websocket.keys()),
        "websocket_only": len(websocket.keys() - direct.keys()),
        "jaccard_fraction": len(shared) / len(union) if union else None,
        "ws_first_fraction_on_shared": sum(value < 0 for value in delta) / len(delta)
        if delta
        else None,
        "ws_minus_direct_seconds_on_shared": percentile(delta),
        "mature_window": {
            "direct_unique": len(left),
            "websocket_unique": len(right),
            "shared": len(left & right),
            "direct_only": len(left - right),
            "websocket_only": len(right - left),
        },
    }


def main(directory):
    raw = {
        name: json.loads((directory / (name + ".json")).read_text())
        for name in ["direct", "websocket", "aggregator"]
    }
    direct, ws, server = raw["direct"], raw["websocket"], raw["aggregator"]
    result = compare(
        observations(directory / "direct.sqlite"),
        observations(directory / "websocket.sqlite"),
        direct["start"],
        direct["end"],
    )
    result.update(
        resources={
            name: {
                key: value
                for key, value in item.items()
                if key
                in {
                    "cpu_seconds",
                    "elapsed_seconds",
                    "memory_peak_bytes",
                    "sampled_peak_memory_bytes",
                    "network_rx_bytes",
                    "network_tx_bytes",
                    "wire_bytes",
                    "json_bytes",
                    "body_bytes",
                    "entries",
                    "negotiated_extensions",
                    "matches",
                    "errors",
                    "invalid_sha",
                }
            }
            for name, item in raw.items()
        },
        websocket_stack_cpu_seconds=ws["cpu_seconds"] + server["cpu_seconds"],
        websocket_stack_ingress_bytes=ws["network_rx_bytes"] + server["network_rx_bytes"],
        direct_logs_head_ok=sum(row["head_ok"] for row in direct["logs"]),
        direct_logs_with_data=sum(row["entries"] > 0 for row in direct["logs"]),
        websocket_sources_with_data=len(ws["sources"]),
        limitations=[
            "Self-hosted Rust + Python lite client; not a hosted SLA or pure transport test.",
            "Different catalogs, parsers, pacing and startup behavior can affect coverage.",
            "Concurrent collectors share a public IP and can compete for rate limits.",
            "WebSocket is plain local Docker with optional deflate; direct uses HTTPS.",
            "CPU includes recording and initialization; final-drain windows may differ.",
            "Timing is conditional on shared identities; unseen entries have no latency.",
            "Precertificates remain separate; observed union is not ground truth.",
        ],
    )
    (directory / "comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    main(parser.parse_args().directory)
