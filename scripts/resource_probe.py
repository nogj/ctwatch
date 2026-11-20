"""Linux cgroup v2 measurements for the complete probe, including all child workers."""

import asyncio
import time
from pathlib import Path


def fields(path):
    return {
        key: int(value) for key, value in (line.split() for line in path.read_text().splitlines())
    }


def snapshot(root=Path("/sys/fs/cgroup"), network=Path("/proc/net/dev")):
    cpu = fields(root / "cpu.stat")
    memory = fields(root / "memory.stat")
    rx = tx = 0
    for line in network.read_text().splitlines()[2:]:
        name, values = line.split(":")
        if name.strip() != "lo":
            values = values.split()
            rx += int(values[0])
            tx += int(values[8])
    pressure = {}
    for kind in ("cpu", "memory", "io"):
        path = root / f"{kind}.pressure"
        if path.exists():
            for line in path.read_text().splitlines():
                category, *values = line.split()
                pressure[f"{kind}_{category}"] = dict(item.split("=") for item in values)
    return {
        "monotonic_seconds": time.monotonic(),
        "cpu_seconds": cpu["usage_usec"] / 1e6,
        "cpu_user_seconds": cpu.get("user_usec", 0) / 1e6,
        "cpu_system_seconds": cpu.get("system_usec", 0) / 1e6,
        "cpu_periods": cpu.get("nr_periods", 0),
        "cpu_throttled_periods": cpu.get("nr_throttled", 0),
        "cpu_throttled_seconds": cpu.get("throttled_usec", 0) / 1e6,
        "memory_current_bytes": int((root / "memory.current").read_text()),
        "memory_kernel_peak_bytes": int((root / "memory.peak").read_text()),
        "memory_anon_bytes": memory.get("anon", 0),
        "memory_file_bytes": memory.get("file", 0),
        "memory_kernel_bytes": memory.get("kernel", 0),
        "network_rx_bytes": rx,
        "network_tx_bytes": tx,
        "memory_events": fields(root / "memory.events"),
        "pressure": pressure,
    }


def summarize(points):
    first, last = points[0], points[-1]
    elapsed = last["monotonic_seconds"] - first["monotonic_seconds"]
    intervals = []
    memory_area = 0
    for left, right in zip(points, points[1:], strict=False):
        seconds = right["monotonic_seconds"] - left["monotonic_seconds"]
        if seconds > 0:
            intervals.append((right["cpu_seconds"] - left["cpu_seconds"]) / seconds)
            memory_area += (
                (left["memory_current_bytes"] + right["memory_current_bytes"]) / 2 * seconds
            )
    rates = sorted(intervals)
    delta_keys = [
        "cpu_seconds",
        "cpu_user_seconds",
        "cpu_system_seconds",
        "cpu_periods",
        "cpu_throttled_periods",
        "cpu_throttled_seconds",
        "network_rx_bytes",
        "network_tx_bytes",
    ]
    return {
        "elapsed_seconds": elapsed,
        **{key: last[key] - first[key] for key in delta_keys},
        "average_cpu_cores": (last["cpu_seconds"] - first["cpu_seconds"]) / elapsed
        if elapsed
        else None,
        "sampled_cpu_cores_p95": rates[int((len(rates) - 1) * 0.95)] if rates else None,
        "sampled_cpu_cores_max": max(rates) if rates else None,
        "memory_time_weighted_average_bytes": memory_area / elapsed if elapsed else None,
        "memory_sampled_peak_bytes": max(row["memory_current_bytes"] for row in points),
        "memory_anon_sampled_peak_bytes": max(row["memory_anon_bytes"] for row in points),
        "memory_file_sampled_peak_bytes": max(row["memory_file_bytes"] for row in points),
        "memory_current_at_end_bytes": last["memory_current_bytes"],
        "samples": len(points),
        "maximum_sampling_gap_seconds": max(
            (
                right["monotonic_seconds"] - left["monotonic_seconds"]
                for left, right in zip(points, points[1:], strict=False)
            ),
            default=0,
        ),
    }


class ResourceMonitor:
    def __init__(self, extra=lambda: {}, journal=None):
        self.extra = extra
        self.points = []
        self.markers = {}
        self.stopped = asyncio.Event()
        self.task = None
        self.journal = journal

    def sample(self):
        point = {**snapshot(), **self.extra()}
        self.points.append(point)
        if self.journal is not None:
            import json

            self.journal.parent.mkdir(parents=True, exist_ok=True)
            with self.journal.open("a") as output:
                output.write(json.dumps(point) + "\n")
        return point

    def start(self):
        self.cpu_limit = Path("/sys/fs/cgroup/cpu.max").read_text().strip()
        self.memory_limit = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        self.sample()
        self.task = asyncio.create_task(self.run())

    def mark(self, name):
        self.sample()
        self.markers[name] = len(self.points) - 1

    async def run(self):
        while not self.stopped.is_set():
            try:
                await asyncio.wait_for(self.stopped.wait(), timeout=1)
            except TimeoutError:
                self.sample()

    async def finish(self, active_seconds=None):
        self.stopped.set()
        if self.task is not None:
            await self.task
            self.task = None
        self.sample()
        start = self.markers.get("ingestion_start", 0)
        active = self.points[start:]
        if active_seconds is not None:
            zero = active[0]["monotonic_seconds"]
            end = min(
                range(len(active)),
                key=lambda n: abs(active[n]["monotonic_seconds"] - zero - active_seconds),
            )
            active_summary = summarize(active[: end + 1])
        else:
            active_summary = None
        return {
            "method": "Linux cgroup v2: aggregate CPU/memory includes parent and all workers",
            "cpu_limit_cpu_max": self.cpu_limit,
            "memory_limit_bytes": self.memory_limit,
            "whole_probe": summarize(self.points),
            "ingestion_and_drain": summarize(self.points[start:]),
            "active_window_sampled_summary": active_summary,
            "memory_kernel_peak_whole_container_lifetime_bytes": self.points[-1][
                "memory_kernel_peak_bytes"
            ],
            "series": self.points,
            "limitations": [
                "Samples can miss short bursts; kernel memory peak covers the container lifetime.",
                "CPU p95/max describe sampled intervals, not instantaneous scheduler usage.",
                "Memory includes cgroup-charged processes, page cache, and kernel memory.",
                "Network measures container interfaces, not billable AWS traffic or stored data.",
                "Active-window summary uses the sample nearest the configured deadline.",
            ],
        }
