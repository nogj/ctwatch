import json

from scripts.resource_probe import ResourceMonitor, snapshot, summarize


def point(seconds, cpu, memory):
    return {
        "monotonic_seconds": seconds,
        "cpu_seconds": cpu,
        "cpu_user_seconds": cpu * 0.75,
        "cpu_system_seconds": cpu * 0.25,
        "cpu_periods": seconds * 10,
        "cpu_throttled_periods": seconds,
        "cpu_throttled_seconds": seconds * 0.01,
        "network_rx_bytes": seconds * 1000,
        "network_tx_bytes": seconds * 100,
        "memory_current_bytes": memory,
        "memory_anon_bytes": memory - 10,
        "memory_file_bytes": 10,
    }


def test_counters_are_deltas_and_memory_is_weighted_by_elapsed_time():
    result = summarize([point(10, 20, 100), point(11, 22, 300), point(14, 25, 100)])
    assert result["elapsed_seconds"] == 4
    assert result["cpu_seconds"] == 5
    assert result["average_cpu_cores"] == 1.25
    assert result["sampled_cpu_cores_max"] == 2
    assert result["memory_time_weighted_average_bytes"] == 200
    assert result["memory_sampled_peak_bytes"] == 300
    assert result["network_rx_bytes"] == 4000
    assert result["cpu_throttled_periods"] == 4
    assert result["maximum_sampling_gap_seconds"] == 3


def test_no_samples_interval_is_unknown_cpu_not_zero_usage():
    result = summarize([point(10, 20, 100)])
    assert result["average_cpu_cores"] is None
    assert result["sampled_cpu_cores_p95"] is None
    assert result["memory_time_weighted_average_bytes"] is None
    assert result["memory_sampled_peak_bytes"] == 100


def test_cgroup_snapshot_includes_oom_pressure_and_excludes_loopback(tmp_path):
    files = {
        "cpu.stat": "usage_usec 2500000\nuser_usec 2000000\nsystem_usec 500000\n",
        "memory.stat": "anon 100\nfile 20\nkernel 10\n",
        "memory.current": "130",
        "memory.peak": "200",
        "memory.events": "oom 2\noom_kill 1\nmax 3\n",
        "cpu.pressure": "some avg10=1.00 avg60=2.00 avg300=3.00 total=42000\n",
    }
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    network = tmp_path / "net"
    network.write_text(
        "header\nheader\n"
        "lo: 999 0 0 0 0 0 0 0 999 0 0 0 0 0 0 0\n"
        "eth0: 123 0 0 0 0 0 0 0 456 0 0 0 0 0 0 0\n"
    )
    result = snapshot(tmp_path, network)
    assert result["cpu_seconds"] == 2.5
    assert result["network_rx_bytes"] == 123
    assert result["network_tx_bytes"] == 456
    assert result["memory_events"]["oom_kill"] == 1
    assert result["pressure"]["cpu_some"]["total"] == "42000"


def test_sample_journal_retains_partial_measurements(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.resource_probe.snapshot", lambda: {"cpu_seconds": 3})
    journal = tmp_path / "nested" / "series.jsonl"
    monitor = ResourceMonitor(extra=lambda: {"entries": 12}, journal=journal)
    monitor.sample()
    monitor.sample()
    assert [json.loads(line) for line in journal.read_text().splitlines()] == [
        {"cpu_seconds": 3, "entries": 12},
        {"cpu_seconds": 3, "entries": 12},
    ]
