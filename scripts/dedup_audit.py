"""Optional exact decision trace; evaluate its full-window history outside the measured task."""

import struct
import time

# Timestamp, outcome (0=analysis, 1=cache, 2=in-flight), full-cache flag, SHA256 DER.
RECORD = struct.Struct("<dBB32s")


class DedupAudit:
    def __init__(self, engine, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("wb", buffering=1024 * 1024)
        original = engine.cache.get

        def get(key):
            value = original(key)
            outcome = 1 if value is not None else 2 if key in engine.inflight else 0
            self.file.write(
                RECORD.pack(
                    time.monotonic(),
                    outcome,
                    int(len(engine.cache.items) >= engine.cache.capacity),
                    bytes.fromhex(key),
                )
            )
            return value

        engine.cache.get = get

    def close(self):
        self.file.close()
