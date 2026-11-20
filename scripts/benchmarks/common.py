import json
import sqlite3
import time
from pathlib import Path

from ctwatch.matcher import Matcher


def watchlist():
    doc = json.loads(Path("config/watchlist.example.json").read_text())
    doc["domains"].extend({"domain": f"ctwatch-load-{i:05d}.invalid"} for i in range(10000))
    return doc


def connection(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA cache_size=-4096")
    db.executescript("""
    CREATE TABLE observations (
        n INTEGER PRIMARY KEY, sha TEXT, log TEXT, idx INTEGER,
        accepted_ms REAL, seen_ms REAL, domain_count INTEGER, payload_bytes INTEGER
    );
    CREATE INDEX obs_sha ON observations(sha);
    CREATE TABLE certificates (sha TEXT PRIMARY KEY, der BLOB);
    """)
    return db


def counters():
    cpu = {}
    for line in Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines():
        key, value = line.split()
        cpu[key] = int(value)
    network = 0
    for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
        name, fields = line.split(":")
        if name.strip() != "lo":
            network += int(fields.split()[0])
    return {
        "cpu_seconds": cpu["usage_usec"] / 1e6,
        "network_rx_bytes": network,
        "memory_current_bytes": int(Path("/sys/fs/cgroup/memory.current").read_text()),
        "memory_peak_bytes": int(Path("/sys/fs/cgroup/memory.peak").read_text()),
        "wall_time": time.time(),
    }


def metrics(before):
    after = counters()
    elapsed = after["wall_time"] - before["wall_time"]
    return {
        "elapsed_seconds": elapsed,
        "cpu_seconds": after["cpu_seconds"] - before["cpu_seconds"],
        "average_cpu_cores": (after["cpu_seconds"] - before["cpu_seconds"]) / elapsed,
        "network_rx_bytes": after["network_rx_bytes"] - before["network_rx_bytes"],
        "memory_peak_bytes": after["memory_peak_bytes"],
    }


def names_matcher():
    from ctwatch.cache import TTLCache

    return Matcher(watchlist(), "data/confusables.txt"), TTLCache(100000, 3600)


def match_names(names, matcher, cache):
    result = []
    for name in sorted(set(names)):
        value = cache.get(name)
        if value is None:
            try:
                value = matcher.match(name)
            except (ValueError, UnicodeError):
                value = []
            cache.put(name, value)
        result.extend(value)
    return result
