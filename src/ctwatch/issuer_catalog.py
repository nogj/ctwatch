"""CCADB CAA identities, refreshed outside ingestion; no issuer-name heuristics."""

import csv
import hashlib
import io
import json
import os
import re
import time
from pathlib import Path

from cryptography import x509

from .matcher import canonical

CCADB_URL = "https://ccadb.my.salesforce-sites.com/ccadb/AllCAAIdentifiersReportCSVV2"
MAX_BYTES = 8 * 1024 * 1024


def subject_key(text):
    # CCADB uses '; ' between attributes; X.509 RFC4514 escapes punctuation.
    # Unsupported/ambiguous representations fail closed (no guessed CA owner).
    fields = []
    for part in re.split(r"; (?=[A-Za-z0-9.]+=)", text):
        key, separator, value = part.partition("=")
        if not separator:
            return None
        value = re.sub(r'\\([,+=<>#;"\\])', r"\1", value)
        fields.append((key.lower(), value))
    return tuple(fields)


def parse_csv(text):
    if len(text.encode()) > MAX_BYTES:
        raise ValueError("CCADB snapshot too large")
    reader = csv.DictReader(io.StringIO(text))
    required = {"Subject", "Subject Key Identifier (Hex)", "Recognized CAA Domains"}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError("unexpected CCADB CAA CSV schema")
    by_ski, rows = {}, 0
    for row in reader:
        if any(not isinstance(row.get(key), str) for key in required):
            raise ValueError("truncated CCADB row")
        ski = row["Subject Key Identifier (Hex)"].strip().lower()
        if not ski or len(ski) % 2 or not re.fullmatch(r"[0-9a-f]+", ski):
            raise ValueError("invalid CCADB subject key identifier")
        identities, invalid = [], False
        for value in row["Recognized CAA Domains"].split(","):
            value = value.strip()
            if not value:
                continue
            try:
                name, _, wildcard = canonical(value)
                if wildcard or ";" in value:
                    raise ValueError("invalid CCADB CAA identity")
                identities.append(name.rstrip("."))
            except (ValueError, UnicodeError):
                invalid = True
        subject = subject_key(row["Subject"])
        if subject is None:
            raise ValueError("invalid CCADB subject")
        by_ski.setdefault(ski, []).append(
            (subject, None if invalid else tuple(sorted(set(identities))))
        )
        rows += 1
    if not rows:
        raise ValueError("empty CCADB catalog")
    return by_ski, rows


def snapshot(text, now=None):
    parse_csv(text)
    return {
        "source_url": CCADB_URL,
        "attribution": "Common CA Database (CCADB)",
        "license": "CDLA-Permissive-2.0",
        "fetched_at_ms": int((time.time() if now is None else now) * 1000),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "csv": text,
    }


class IssuerCatalog:
    def __init__(
        self,
        bundled="data/ccadb-caa.json",
        cache="/tmp/ctwatch-ccadb-caa.json",
        max_age=7 * 86400,
        clock=time.time,
    ):
        self.cache = Path(cache)
        self.clock, self.max_age = clock, max_age
        self.data, self.index, self.rows = None, {}, 0
        self.last_error = None
        self.retry_after = 0
        # Prefer the newest VALID copy; a broken cache cannot hide the bundled copy.
        candidates = []
        for path in (Path(bundled), self.cache):
            try:
                if path.stat().st_size > MAX_BYTES * 2:
                    raise ValueError("CCADB cache too large")
                data = json.loads(path.read_text())
                index, rows = self.validate(data)
                candidates.append((data, index, rows))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        if candidates:
            self.data, self.index, self.rows = max(
                candidates, key=lambda item: item[0]["fetched_at_ms"]
            )

    def validate(self, data):
        if not isinstance(data, dict) or not isinstance(data.get("csv"), str):
            raise ValueError("invalid CCADB snapshot")
        if data["source_url"] != CCADB_URL:
            raise ValueError("unexpected CCADB source")
        if data["sha256"] != hashlib.sha256(data["csv"].encode()).hexdigest():
            raise ValueError("CCADB checksum mismatch")
        stamp = data["fetched_at_ms"]
        if not isinstance(stamp, int) or stamp <= 0 or stamp > (self.clock() + 300) * 1000:
            raise ValueError("invalid CCADB fetch timestamp")
        return parse_csv(data["csv"])

    def report(self):
        return {
            "source_url": CCADB_URL,
            "rows": self.rows,
            "sha256": self.data["sha256"] if self.data else None,
            "fetched_at_ms": self.data["fetched_at_ms"] if self.data else None,
            "stale": not self.data
            or self.clock() - self.data["fetched_at_ms"] / 1000 > self.max_age,
            "last_error": self.last_error,
        }

    async def refresh(self, sources):
        now = self.clock()
        if now < self.retry_after or (
            self.data and now - self.data["fetched_at_ms"] / 1000 < 86400
        ):
            return False
        try:
            response = await sources.get(CCADB_URL)
            data = snapshot(response.content.decode("utf-8-sig"), now=self.clock())
            index, rows = self.validate(data)
            # Publish only after a full parse and atomic write, preserving last good data.
            temporary = self.cache.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False) + "\n")
            os.replace(temporary, self.cache)
            self.data, self.index, self.rows = data, index, rows
            self.last_error = None
            return True
        except Exception as error:
            self.last_error = f"{type(error).__name__}: {str(error)[:200]}"
            self.retry_after = self.clock() + 3600
            return False

    def identify(self, certificate):
        result = {
            "issuer_caa_identities": [],
            "issuer_identity_source": "ccadb_aki_ski_subject",
            "issuer_signature_verified": False,
            "issuer_catalog_sha256": self.data["sha256"] if self.data else None,
            "issuer_catalog_fetched_at_ms": self.data["fetched_at_ms"] if self.data else None,
        }

        def unknown(reason):
            return {**result, "issuer_identity_reason": reason}

        if self.report()["stale"]:
            return unknown("issuer_catalog_stale_or_missing")
        try:
            aki = certificate.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
        except x509.ExtensionNotFound:
            return unknown("issuer_aki_missing")
        if not aki.key_identifier:
            return unknown("issuer_aki_missing")
        result["issuer_key_identifier"] = aki.key_identifier.hex()
        subject = subject_key(
            "; ".join(rdn.rfc4514_string() for rdn in reversed(certificate.issuer.rdns))
        )
        candidates = self.index.get(aki.key_identifier.hex(), [])
        matches = {identities for name, identities in candidates if name == subject}
        if not matches:
            return unknown(
                "issuer_not_in_catalog" if not candidates else "issuer_subject_not_matched"
            )
        if len(matches) != 1:
            return unknown("issuer_catalog_ambiguous")
        disclosed = next(iter(matches))
        if disclosed is None:
            return unknown("issuer_caa_identities_invalid")
        identities = list(disclosed)
        if not identities:
            return unknown("issuer_caa_identities_not_disclosed")
        return {
            **result,
            "issuer_caa_identities": identities,
            "issuer_identity_reason": "catalog_match",
        }
