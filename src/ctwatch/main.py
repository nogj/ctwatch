import asyncio
import json
import logging
import os
import random
import signal
import time
import uuid
from pathlib import Path

import boto3

from .caa import CAAValidator
from .config import DEFAULT_POLL_SECONDS, positive_int
from .engine import Engine
from .issuer_catalog import IssuerCatalog
from .pipeline import process_range
from .registry import APPLE_LOG_LIST_URL, CHROME_LOG_LIST_URL, Registry
from .source import Sources
from .store import Store

LOGGER = logging.getLogger("ctwatch")


def emit(message, level=logging.INFO, **fields):
    LOGGER.log(level, json.dumps({"message": message, **fields}))


def load_document(location):
    if location.startswith("s3://"):
        bucket, key = location[5:].split("/", 1)
        response = boto3.client("s3").get_object(Bucket=bucket, Key=key)
        return json.loads(response["Body"].read())
    return json.loads(Path(location).read_text())


async def run():
    poll = positive_int(os.getenv("POLL_SECONDS", str(DEFAULT_POLL_SECONDS)))
    store = Store(os.environ["STATE_TABLE"], str(uuid.uuid4()))
    sources = Sources(int(os.getenv("HTTP_CONCURRENCY", "16")))
    issuer_catalog = IssuerCatalog()
    caa = CAAValidator(catalog=issuer_catalog)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    confusables = os.getenv("CONFUSABLES_PATH", "data/confusables.txt")
    location = os.getenv("WATCHLIST_URI", "config/watchlist.example.json")
    document = await asyncio.to_thread(load_document, location)
    engine = Engine(
        document,
        confusables,
        int(os.getenv("WORKERS", "6")),
        int(os.getenv("CERT_CACHE_ENTRIES", "500000")),
    )
    start_mode = os.getenv("START_MODE", "head")
    if start_mode not in {"head", "beginning"}:
        raise ValueError("START_MODE must be head or beginning")
    registry = Registry(
        os.getenv("LOG_LIST_URL", CHROME_LOG_LIST_URL),
        os.getenv("APPLE_LOG_LIST_URL", APPLE_LOG_LIST_URL),
    )
    logs, tasks = {}, {}

    async def sleep(seconds):
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def tail(log_id):
        failures = 0
        while not stop.is_set():
            log = logs[log_id]
            try:
                state = await asyncio.to_thread(store.acquire, log_id)
                if state is None:
                    await sleep(10)
                    continue
                # Do not start historical retired/rejected logs on a fresh deployment.
                if "next_index" not in state and log.state in {"retired", "rejected"}:
                    await sleep(3600)
                    continue
                if log.state == "readonly" and state.get("drained", False):
                    await sleep(3600)
                    continue
                size, root = await sources.head(log)
                if "next_index" not in state:
                    initial = size if start_mode == "head" else 0
                    await asyncio.to_thread(store.initialize, log_id, initial, root)
                    state["next_index"] = initial
                    emit("cursor_initialized", log_id=log_id, position=initial, mode=start_mode)
                position = int(state["next_index"])
                if size < position or size < int(state.get("observed_tree_size", 0)):
                    raise ValueError("tree size regressed; refusing cursor movement")
                if size == int(state.get("observed_tree_size", -1)) and root != state.get(
                    "tree_root"
                ):
                    raise ValueError("tree root changed at same size")
                emitted, processed = 0, 0
                # Bounded rounds keep lease renewal and telemetry regular during catch-up.
                for _ in range(32):
                    if position >= size or stop.is_set():
                        break
                    state = await asyncio.to_thread(store.acquire, log_id)
                    if state is None or int(state["next_index"]) != position:
                        raise RuntimeError("log ownership changed")
                    entries = await sources.read_range(log, position, size)
                    current_engine = engine
                    new_position, observations = await process_range(
                        entries, current_engine, store, log, position, size, root, caa=caa
                    )
                    position = new_position
                    processed += len(entries)
                    emitted += observations
                if (
                    position == size
                    and not processed
                    and (
                        size != int(state.get("observed_tree_size", -1)) or log.state == "readonly"
                    )
                ):
                    await asyncio.to_thread(
                        store.commit,
                        log_id,
                        position,
                        position,
                        size,
                        root,
                        log.state == "readonly",
                    )
                emit(
                    "log_progress",
                    log_id=log_id,
                    position=position,
                    tree_size=size,
                    backlog=size - position,
                    processed=processed,
                    match_observations=emitted,
                )
                failures = 0
                await sleep(poll if position == size else 0)
            except Exception:
                failures += 1
                LOGGER.exception(json.dumps({"message": "log_error", "log_id": log_id}))
                await sleep(min(300, 2 ** min(failures, 8)) + random.random())

    async def supervise():
        nonlocal engine, document
        refreshed = 0
        while not stop.is_set():
            try:
                retry_refresh = any(row["stale"] for row in registry.report.values())
                if time.monotonic() - refreshed >= (60 if retry_refresh else 3600) or not logs:
                    for log in await registry.refresh(sources):
                        logs[log.log_id] = log
                        if log.log_id not in tasks:
                            tasks[log.log_id] = asyncio.create_task(tail(log.log_id))
                    # Existing logs remain monitored if temporarily omitted from registry.
                    refreshed = time.monotonic()
                    emit(
                        "registry_refreshed",
                        level=logging.WARNING
                        if any(row["stale"] for row in registry.report.values())
                        else logging.INFO,
                        logs=len(logs),
                        catalogs=registry.report,
                    )
                new_document = await asyncio.to_thread(load_document, location)
                if new_document != document:
                    replacement = Engine(
                        new_document,
                        confusables,
                        int(os.getenv("WORKERS", "6")),
                        int(os.getenv("CERT_CACHE_ENTRIES", "500000")),
                    )
                    old, engine, document = engine, replacement, new_document
                    old.close()  # queued work completes against its original ruleset
                    emit("ruleset_reloaded", version=engine.version)
            except Exception:
                LOGGER.exception(json.dumps({"message": "configuration_error"}))
            await sleep(60)

    async def heartbeat():
        while not stop.is_set():
            Path("/tmp/ctwatch-health").touch()
            emit(
                "heartbeat",
                logs=len(logs),
                cache_entries=len(engine.cache.items),
                certificate_cache_hits=engine.hits,
                certificate_cache_misses=engine.misses,
                ruleset_version=engine.version,
                caa_catalog=issuer_catalog.report(),
            )
            await sleep(30)

    async def refresh_issuers():
        while not stop.is_set():
            updated = await issuer_catalog.refresh(sources)
            report = issuer_catalog.report()
            emit(
                "caa_catalog_refreshed",
                updated=updated,
                level=logging.WARNING if report["last_error"] or report["stale"] else logging.INFO,
                **report,
            )
            await sleep(3600)

    background = [
        asyncio.create_task(supervise()),
        asyncio.create_task(heartbeat()),
        asyncio.create_task(refresh_issuers()),
    ]
    await stop.wait()
    emit("shutdown_requested")
    try:
        await asyncio.wait_for(asyncio.gather(*tasks.values(), *background), timeout=90)
    except TimeoutError:
        emit("shutdown_deadline", level=logging.WARNING)
    finally:
        engine.close()
        await sources.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run())


if __name__ == "__main__":
    main()
