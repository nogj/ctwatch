import asyncio

from ctwatch.ct import Entry
from ctwatch.engine import Engine


def test_real_spawned_process_pool(document, certificate):
    async def exercise():
        engine = Engine(document, "data/confusables.txt", workers=2, cache_size=10)
        try:
            entry = Entry(0, 1234, certificate, "certificate")
            values = await engine.analyze([entry, entry])
            assert values[0] == values[1]
            assert any(match["match_type"] == "exact" for match in values[0])
            assert engine.misses == 1
            assert engine.hits == 1
            assert await engine.analyze([entry]) == values[:1]
        finally:
            engine.close()
            engine.pool.shutdown(wait=True)

    asyncio.run(exercise())
