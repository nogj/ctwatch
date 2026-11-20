"""RFC6962 and Static CT framing. Checkpoint signatures verified; see trust model docs."""

import base64
import hashlib
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa


class Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, size: int) -> bytes:
        if size < 0 or self.pos + size > len(self.data):
            raise ValueError("truncated CT entry")
        result = self.data[self.pos : self.pos + size]
        self.pos += size
        return result

    def number(self, size: int) -> int:
        return int.from_bytes(self.take(size), "big")

    def vector(self, width: int) -> bytes:
        return self.take(self.number(width))


@dataclass(frozen=True)
class Entry:
    index: int
    timestamp_ms: int
    der: bytes
    entry_type: str

    @property
    def fingerprint(self):
        return hashlib.sha256(self.der).hexdigest()


def timestamped(reader: Reader):
    timestamp = reader.number(8)
    kind = reader.number(2)
    if kind == 0:
        signed = reader.vector(3)
    elif kind == 1:
        reader.take(32)  # issuer_key_hash; followed by TBSCertificate
        signed = reader.vector(3)
    else:
        raise ValueError(f"unknown CT entry type {kind}")
    reader.vector(2)  # extensions
    return timestamp, kind, signed


def parse_rfc(index: int, item: dict) -> Entry:
    leaf = Reader(base64.b64decode(item["leaf_input"], validate=True))
    if leaf.take(2) != b"\x00\x00":
        raise ValueError("unsupported MerkleTreeLeaf version/type")
    timestamp, kind, signed = timestamped(leaf)
    if leaf.pos != len(leaf.data):
        raise ValueError("trailing bytes in leaf")
    if kind:
        extra = Reader(base64.b64decode(item["extra_data"], validate=True))
        der = extra.vector(3)
        extra.vector(3)  # precertificate_chain
        if extra.pos != len(extra.data):
            raise ValueError("trailing precertificate data")
    else:
        der = signed
    return Entry(index, timestamp, der, "precertificate" if kind else "certificate")


def parse_tile(data: bytes, first_index: int) -> list[Entry]:
    reader = Reader(data)
    entries = []
    while reader.pos < len(data):
        timestamp, kind, signed = timestamped(reader)
        der = reader.vector(3) if kind else signed
        chain = reader.vector(2)
        if len(chain) % 32:
            raise ValueError("invalid issuer fingerprint vector")
        entries.append(
            Entry(
                first_index + len(entries),
                timestamp,
                der,
                "precertificate" if kind else "certificate",
            )
        )
    return entries


def tile_path(number: int) -> str:
    parts = [f"{number % 1000:03d}"]
    while number >= 1000:
        number //= 1000
        parts.insert(0, f"x{number % 1000:03d}")
    return "/".join(parts)


@dataclass(frozen=True)
class Log:
    log_id: str
    name: str
    url: str
    key: bytes
    kind: str
    state: str
    origin: str = ""


def discover(document: dict) -> list[Log]:
    found = {}
    for operator in document["operators"]:
        for field, kind, url_field in [
            ("logs", "rfc6962", "url"),
            ("tiled_logs", "static", "monitoring_url"),
        ]:
            for item in operator.get(field, []):
                state = next(iter(item.get("state", {})), "unknown")
                if state not in {"usable", "qualified", "readonly", "retired", "rejected"}:
                    continue
                key = base64.b64decode(item["key"], validate=True)
                log_id = base64.b64decode(item["log_id"], validate=True).hex()
                if hashlib.sha256(key).hexdigest() != log_id:
                    raise ValueError("log identity does not match public key")
                url = item[url_field].rstrip("/") + "/"
                if not url.startswith("https://"):
                    raise ValueError("CT sources must use HTTPS")
                origin = item.get("submission_url", url).removeprefix("https://").rstrip("/")
                found[log_id] = Log(log_id, item["description"], url, key, kind, state, origin)
    return list(found.values())


def verify_sth(key_der: bytes, timestamp: int, size: int, root: bytes, signature: bytes):
    if len(root) != 32 or size < 0 or timestamp < 0:
        raise ValueError("invalid tree head")
    reader = Reader(signature)
    hash_algorithm, algorithm = reader.number(1), reader.number(1)
    value = reader.vector(2)
    if hash_algorithm != 4 or reader.pos != len(signature):
        raise ValueError("unsupported STH signature")
    payload = b"\x00\x01" + timestamp.to_bytes(8, "big") + size.to_bytes(8, "big") + root
    key = serialization.load_der_public_key(key_der)
    if algorithm == 3 and isinstance(key, ec.EllipticCurvePublicKey):
        key.verify(value, payload, ec.ECDSA(hashes.SHA256()))
    elif algorithm == 1 and isinstance(key, rsa.RSAPublicKey):
        key.verify(value, payload, padding.PKCS1v15(), hashes.SHA256())
    else:
        raise ValueError("unsupported public key/signature")


def static_head(body: str, log: Log) -> tuple[int, str]:
    lines = body.splitlines()
    if len(lines) < 5 or lines[0] != log_origin(log):
        raise ValueError("invalid checkpoint origin")
    size, root = int(lines[1]), base64.b64decode(lines[2], validate=True)
    key_id = hashlib.sha256(lines[0].encode() + b"\n\x05" + bytes.fromhex(log.log_id)).digest()[:4]
    for line in lines[4:]:
        parts = line.split()
        if len(parts) != 3 or parts[0] != "—" or parts[1] != lines[0]:
            continue
        signature = base64.b64decode(parts[2], validate=True)
        if signature[:4] == key_id:
            verify_sth(log.key, int.from_bytes(signature[4:12], "big"), size, root, signature[12:])
            return size, lines[2]
    raise ValueError("no valid log signature on checkpoint")


def log_origin(log: Log) -> str:
    # Static monitoring URL can differ from submission origin. Set by fetch_head caller.
    return log.origin or log.url.removeprefix("https://").rstrip("/")
