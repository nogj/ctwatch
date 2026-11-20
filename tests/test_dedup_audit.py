from types import SimpleNamespace

from ctwatch.cache import TTLCache
from scripts.dedup_audit import RECORD, DedupAudit


def test_audit_distinguishes_cache_inflight_and_reanalysis_without_changing_results(tmp_path):
    engine = SimpleNamespace(cache=TTLCache(1, 3600), inflight={})
    path = tmp_path / "audit.bin"
    audit = DedupAudit(engine, path)
    first, other = "ab" * 32, "cd" * 32
    assert engine.cache.get(first) is None
    engine.inflight[first] = True
    assert engine.cache.get(first) is None
    engine.inflight.clear()
    engine.cache.put(first, [])
    assert engine.cache.get(first) == []
    engine.cache.put(other, ["match"])
    assert engine.cache.get(first) is None
    assert engine.cache.get(other) == ["match"]
    audit.close()
    data = path.read_bytes()
    records = [RECORD.unpack_from(data, offset) for offset in range(0, len(data), RECORD.size)]
    assert [r[1] for r in records] == [0, 2, 1, 0, 1]
    assert [r[2] for r in records] == [0, 0, 1, 1, 1]
    assert records[-1][3] == bytes.fromhex(other)


def test_exact_reanalyses_and_popularity_include_zero_hit_entries(tmp_path):
    from scripts.analyze_dedup_audit import analyze

    first, other = bytes.fromhex("ab" * 32), bytes.fromhex("cd" * 32)
    path = tmp_path / "audit.bin"
    rows = [
        (0, 0, 0, first),
        (1, 2, 0, first),
        (2, 1, 1, first),
        (3, 0, 1, other),
        (4, 0, 1, first),
        (5, 0, 1, other),
    ]
    path.write_bytes(b"".join(RECORD.pack(*row) for row in rows))
    result = analyze(path, [1, 2])
    assert result["unique_der_fingerprints_entire_window"] == 2
    assert result["observed_phases"]["before_full"]["reanalysis_calls"] == 0
    assert result["observed_phases"]["after_full"]["reanalysis_calls"] == 2
    assert result["cache_hits_per_certificate"]["buckets"]["0"] == 1
    assert result["cache_hits_per_certificate"]["buckets"]["1"] == 1
    assert result["ideal_lru_capacity_curve"][0]["reanalyses"] == 2
    assert result["ideal_lru_capacity_curve"][1]["reanalyses"] == 0


def test_capacity_simulation_does_not_extend_ttl_on_hit(tmp_path):
    from scripts.analyze_dedup_audit import analyze

    first, other = bytes.fromhex("ab" * 32), bytes.fromhex("cd" * 32)
    path = tmp_path / "audit.bin"
    rows = [(0, 0, 0, first), (1, 0, 1, other), (2, 1, 1, first), (5, 0, 1, first)]
    path.write_bytes(b"".join(RECORD.pack(*row) for row in rows))
    curve = analyze(path, [2], ttl=4)["ideal_lru_capacity_curve"][0]
    assert curve["hits"] == 1
    assert curve["reanalyses"] == 1


def test_cli_reconciles_trace_with_engine_and_rejects_incomplete_accounting(tmp_path, monkeypatch):
    import json

    import pytest

    from scripts.analyze_dedup_audit import main

    trace, probe, output = (tmp_path / name for name in ("trace.bin", "probe.json", "out.json"))
    trace.write_bytes(RECORD.pack(1, 0, 0, bytes.fromhex("ab" * 32)))
    report = dict(
        certificate_cache_misses=1,
        certificate_cache_hits=0,
        match_event_repetitions_before_persistence={},
        started_utc="2026-11-19T00:00:00Z",
        elapsed_seconds=1,
        configured_seconds=1,
        workers=1,
        watch_domains=1,
        certificate_cache_capacity=2,
        initial_seed_entries_per_log=0,
    )
    probe.write_text(json.dumps(report))
    monkeypatch.setattr(
        "sys.argv",
        [
            "audit",
            str(trace),
            "--output",
            str(output),
            "--probe-report",
            str(probe),
            "--capacities",
            "2",
        ],
    )
    main()
    result = json.loads(output.read_text())
    assert result["trace_counters_reconcile_with_probe"] is True
    assert result["trace_records"] == 1
    assert len(result["trace_sha256"]) == 64
    report["certificate_cache_misses"] = 2
    probe.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="reconcile"):
        main()
