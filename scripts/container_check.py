"""Offline check of the production image, including its real multiprocessing workers."""

import asyncio
import datetime
import hashlib
import json
import os
import platform
import sys
import tempfile
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from ctwatch.config import DEFAULT_POLL_SECONDS
from ctwatch.ct import Entry
from ctwatch.engine import Engine
from ctwatch.issuer_catalog import IssuerCatalog
from ctwatch.matcher import Matcher
from ctwatch.source import Sources, retry_after_seconds


async def check():
    assert sys.version_info[:2] == (3, 12), "production runtime must use Python 3.12"
    assert os.getuid() == 10001, "container must run as the non-root application user"
    assert DEFAULT_POLL_SECONDS == 15
    assert retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT", 1445412470) == 10
    source = Sources()
    await source.client.aclose()
    clock = [0.0]
    source._clock = lambda: clock[0]

    async def wait(delay):
        clock[0] += delay

    source._sleep = wait
    statuses = iter([429, 200])
    source.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(next(statuses), headers={"Retry-After": "15"})
        )
    )
    try:
        try:
            await source.get("https://offline.example/checkpoint")
        except httpx.HTTPStatusError as error:
            assert error.response.status_code == 429
        else:
            raise AssertionError("expected throttling error")
        await source.get("https://offline.example/tile/data/000")
        assert clock[0] == 15
    finally:
        await source.close()
    with tempfile.NamedTemporaryFile(dir="/tmp") as scratch:
        scratch.write(b"writable scratch volume")
        scratch.flush()
    manifest = json.loads(Path("data/unicode.json").read_text())
    issuer_catalog = IssuerCatalog()
    assert issuer_catalog.data is not None and issuer_catalog.rows > 0
    assert (
        hashlib.sha256(Path("data/confusables.txt").read_bytes()).hexdigest() == manifest["sha256"]
    )
    document = json.loads(Path("config/watchlist.example.json").read_text())
    matcher = Matcher(document, "data/confusables.txt")
    assert matcher.match("example.com")[0]["match_type"] == "exact"
    assert matcher.match("pаypal.com")[0]["match_type"] == "confusable"  # Cyrillic a
    assert not matcher.match("example.com.attacker.net")
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "example.com")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("example.com")]), False)
        .sign(key, hashes.SHA256())
    )
    entry = Entry(0, 1234, certificate.public_bytes(serialization.Encoding.DER), "certificate")
    engine = Engine(document, "data/confusables.txt", workers=2, cache_size=10)
    try:
        values = await engine.analyze([entry, entry])
        assert values[0] == values[1]
        assert values[0][0]["match_type"] == "exact"
        assert engine.misses == 1 and engine.hits == 1
        assert await engine.analyze([entry]) == values[:1]
        assert engine.hits == 2
    finally:
        engine.close()
        engine.pool.shutdown(wait=True)
    print(
        json.dumps(
            {
                "status": "ok",
                "python": platform.python_version(),
                "architecture": platform.machine(),
                "uid": os.getuid(),
                "unicode_checksum": "ok",
                "matching": "ok",
                "spawned_workers": "ok",
                "deduplication": "ok",
                "scratch_volume": "writable",
                "poll_seconds": DEFAULT_POLL_SECONDS,
                "retry_after": "ok",
                "ccadb_snapshot_checksum": "ok",
                "ccadb_rows": issuer_catalog.rows,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(check())
