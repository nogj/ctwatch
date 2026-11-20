"""Run a bounded comparison using an already-started, isolated Certstream container."""

import argparse
import json
import subprocess
import time
from pathlib import Path

import docker

ROOT = Path(__file__).resolve().parents[2]


def snapshot(container):
    value = container.stats(stream=False, one_shot=True)
    return {
        "cpu_seconds": value["cpu_stats"]["cpu_usage"]["total_usage"] / 1e9,
        "memory_bytes": value.get("memory_stats", {}).get("usage", 0),
        "network_rx_bytes": sum(item["rx_bytes"] for item in value.get("networks", {}).values()),
        "network_tx_bytes": sum(item["tx_bytes"] for item in value.get("networks", {}).values()),
        "time": time.time(),
    }


def main(out):
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o777)
    if list(out.glob("*.sqlite")):
        raise SystemExit("choose a fresh output directory; existing captures are preserved")
    endpoint = subprocess.check_output(
        ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], text=True
    ).strip()
    client = docker.DockerClient(base_url=endpoint)
    server = client.containers.get("ctwatch-bench-server")
    if server.status != "running":
        server.start()
        time.sleep(20)
    start, duration = time.time() + 15, 120
    end = start + duration
    containers = []
    try:
        for mode, cpus, memory in [("direct", 2, "3g"), ("websocket", 0.5, "1g")]:
            container = client.containers.run(
                "ctwatch:bench",
                ["python", "/bench/capture.py", mode, "--start", str(start), "--end", str(end)],
                name="ctwatch-bench-" + mode,
                network="ctwatch-bench-net",
                detach=True,
                nano_cpus=int(cpus * 1e9),
                mem_limit=memory,
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                tmpfs={"/tmp": "rw,nosuid,nodev,size=128m,mode=1777"},
                volumes={
                    str(ROOT / "scripts/benchmarks"): {"bind": "/bench", "mode": "ro"},
                    str(out): {"bind": "/results", "mode": "rw"},
                },
            )
            containers.append(container)
        time.sleep(max(0, start - time.time()))
        before = snapshot(server)
        peak = before["memory_bytes"]
        while time.time() < end:
            time.sleep(min(20, max(0, end - time.time())))
            sample = snapshot(server)
            peak = max(peak, sample["memory_bytes"])
            print(
                json.dumps(
                    {
                        "elapsed": round(time.time() - start),
                        "aggregator_cpu_seconds": sample["cpu_seconds"] - before["cpu_seconds"],
                        "aggregator_memory_bytes": sample["memory_bytes"],
                    }
                ),
                flush=True,
            )
        after = snapshot(server)
        report = {
            key: after[key] - before[key]
            for key in ["cpu_seconds", "network_rx_bytes", "network_tx_bytes"]
        }
        report.update(
            elapsed_seconds=after["time"] - before["time"],
            sampled_peak_memory_bytes=peak,
            image=server.image.attrs.get("RepoDigests"),
            start=start,
            end=end,
        )
        (out / "aggregator.json").write_text(json.dumps(report, indent=2) + "\n")
        failures = []
        for container in containers:
            outcome = container.wait(timeout=90)
            container.reload()
            (out / (container.name + ".log")).write_bytes(container.logs())
            (out / (container.name + "-state.json")).write_text(
                json.dumps(container.attrs["State"], indent=2) + "\n"
            )
            if outcome["StatusCode"] != 0:
                failures.append(container.name)
        if failures:
            raise RuntimeError(f"{failures} failed; inspect captured state/logs")
        print(f"Both captures completed; results in {out}", flush=True)
    finally:
        for container in containers:
            container.reload()
            if container.status == "running":
                container.stop(timeout=10)
            container.remove(v=True)
        server.stop(timeout=15)
        (out / "aggregator.log").write_bytes(server.logs(tail=200))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".deploy/ab-live")
    main(parser.parse_args().output.resolve())
