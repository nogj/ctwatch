import asyncio

from .store import make_event


async def process_range(entries, engine, store, log, expected, size, root, caa=None):
    if not entries or entries[0].index != expected:
        raise ValueError("range must start at cursor")
    if any(entry.index != expected + offset for offset, entry in enumerate(entries)):
        raise ValueError("range must be contiguous")
    matches = await engine.analyze(entries)
    events = [
        make_event(entry, log, match, engine.version)
        for entry, values in zip(entries, matches, strict=True)
        for match in values
    ]
    if caa is not None:
        await caa.enrich(
            events, {entry.fingerprint: entry for entry in entries}, engine.caa_issuers
        )
    await asyncio.to_thread(store.save_events, events)
    position = expected + len(entries)
    await asyncio.to_thread(
        store.commit,
        log.log_id,
        expected,
        position,
        size,
        root,
        log.state == "readonly" and position == size,
    )
    return position, len(events)
