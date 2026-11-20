import asyncio
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

from .cache import TTLCache
from .matcher import Matcher, analyze_batch, init_worker


class Engine:
    def __init__(self, document, confusables_path, workers=4, cache_size=500_000):
        from .caa import issuer_mapping

        self.caa_issuers = issuer_mapping(document)
        self.version = Matcher(document, confusables_path).version
        self.cache = TTLCache(cache_size, 3600)
        self.inflight = {}
        self.pool = ProcessPoolExecutor(
            max_workers=workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=init_worker,
            initargs=(document, confusables_path),
        )
        self.slots = asyncio.Semaphore(workers * 2)
        self.hits = 0
        self.misses = 0
        self.active = 0
        self.retiring = False

    async def analyze(self, entries):
        # Limit queued CPU jobs, certificates retained in memory and IPC overhead.
        self.active += 1
        try:
            async with self.slots:
                return await self._analyze(entries)
        finally:
            self.active -= 1
            if self.retiring and self.active == 0:
                self.pool.shutdown(wait=False, cancel_futures=False)

    async def _analyze(self, entries):
        loop = asyncio.get_running_loop()
        futures, new = [], []
        for entry in entries:
            fingerprint = entry.fingerprint
            value = self.cache.get(fingerprint)
            if value is not None:
                future = loop.create_future()
                future.set_result(value)
                self.hits += 1
            elif fingerprint in self.inflight:
                future = self.inflight[fingerprint]
                self.hits += 1
            else:
                future = loop.create_future()
                # Retrieve errors even if its owning log task gets cancelled.
                future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
                self.inflight[fingerprint] = future
                new.append((fingerprint, entry.der, future))
                self.misses += 1
            futures.append(future)
        if new:
            try:
                results = await loop.run_in_executor(
                    self.pool, analyze_batch, [der for _, der, _ in new]
                )
                for (fingerprint, _, future), result in zip(new, results, strict=True):
                    self.cache.put(fingerprint, result)
                    future.set_result(result)
            except BaseException as error:
                for _, _, future in new:
                    if not future.done():
                        future.set_exception(error)
                raise
            finally:
                for fingerprint, _, _ in new:
                    self.inflight.pop(fingerprint, None)
        return await asyncio.gather(*(asyncio.shield(future) for future in futures))

    def close(self):
        self.retiring = True
        if self.active == 0:
            self.pool.shutdown(wait=False, cancel_futures=False)
