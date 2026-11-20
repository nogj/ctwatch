import importlib.util
from pathlib import Path


def module():
    spec = importlib.util.spec_from_file_location(
        "benchmark_comparison", Path("scripts/benchmarks/analyze_live.py")
    )
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_comparison_keeps_unseen_records_and_conditional_timing_separate():
    compare = module().compare
    direct = {
        "shared": {"seen_ms": 50000, "accepted_ms": 40000},
        "direct-only": {"seen_ms": 50000, "accepted_ms": 40000},
    }
    ws = {
        "shared": {"seen_ms": 45000, "accepted_ms": 40000},
        "ws-only": {"seen_ms": 50000, "accepted_ms": 40000},
        "old": {"seen_ms": 50000, "accepted_ms": 0},
    }
    report = compare(direct, ws, 0, 120)
    assert report["shared_unique"] == 1
    assert report["union_unique"] == 4
    assert report["direct_only"] == 1
    assert report["websocket_only"] == 2
    assert report["ws_minus_direct_seconds_on_shared"]["p50"] == -5
    assert report["mature_window"]["websocket_only"] == 1


def test_empty_stream_does_not_report_fictitious_zero_latency():
    report = module().compare({}, {}, 0, 120)
    assert report["ws_minus_direct_seconds_on_shared"] is None
    assert report["ws_first_fraction_on_shared"] is None
