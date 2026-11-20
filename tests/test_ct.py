import base64
import hashlib

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from ctwatch.ct import Log, discover, parse_rfc, parse_tile, static_head, tile_path, verify_sth


def vector(data, width):
    return len(data).to_bytes(width, "big") + data


def timestamped(der, kind=0):
    signed = vector(der, 3) if kind == 0 else b"i" * 32 + vector(b"tbs", 3)
    return (1234).to_bytes(8, "big") + kind.to_bytes(2, "big") + signed + b"\x00\x00"


@pytest.mark.parametrize("kind", [0, 1])
def test_both_wire_formats_identify_same_certificate(certificate, kind):
    leaf = timestamped(certificate, kind)
    extra = vector(certificate, 3) + b"\x00\x00\x00" if kind else b"\x00\x00\x00"
    rfc = parse_rfc(
        9,
        {
            "leaf_input": base64.b64encode(b"\x00\x00" + leaf).decode(),
            "extra_data": base64.b64encode(extra).decode(),
        },
    )
    tile = leaf + (vector(certificate, 3) if kind else b"") + vector(b"x" * 32, 2)
    static = parse_tile(tile, 9)[0]
    assert rfc == static
    assert rfc.fingerprint == hashlib.sha256(certificate).hexdigest()
    assert rfc.timestamp_ms == 1234


def test_truncated_tile_cannot_advance_cursor():
    with pytest.raises(ValueError, match="truncated"):
        parse_tile(b"\x00", 0)


@pytest.mark.parametrize(
    "number,path", [(0, "000"), (999, "999"), (1000, "x001/000"), (1234067, "x001/x234/067")]
)
def test_tile_paths(number, path):
    assert tile_path(number) == path


def test_signed_checkpoint_and_wrong_root():
    key = ec.generate_private_key(ec.SECP256R1())
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    root = b"r" * 32
    payload = b"\x00\x01" + (1234).to_bytes(8, "big") + (567).to_bytes(8, "big") + root
    sig = b"\x04\x03" + vector(key.sign(payload, ec.ECDSA(hashes.SHA256())), 2)
    verify_sth(der, 1234, 567, root, sig)
    with pytest.raises(InvalidSignature):
        verify_sth(der, 1234, 568, root, sig)
    origin = "submission.example/2026"
    log_id = hashlib.sha256(der).hexdigest()
    log = Log(log_id, "test", "https://monitoring.example/", der, "static", "usable", origin)
    key_id = hashlib.sha256(origin.encode() + b"\n\x05" + bytes.fromhex(log_id)).digest()[:4]
    note = base64.b64encode(key_id + (1234).to_bytes(8, "big") + sig).decode()
    body = f"{origin}\n567\n{base64.b64encode(root).decode()}\n\n— {origin} {note}\n"
    assert static_head(body, log)[0] == 567
    with pytest.raises(ValueError, match="origin"):
        static_head(body.replace(origin, "attacker"), log)


def test_registry_checks_identity_and_monitoring_url():
    key = b"key"
    item = {
        "key": base64.b64encode(key).decode(),
        "log_id": base64.b64encode(hashlib.sha256(key).digest()).decode(),
        "description": "test",
        "state": {"usable": {}},
        "submission_url": "https://submission.example/log/",
        "monitoring_url": "https://monitoring.example/",
    }
    registry = {"operators": [{"tiled_logs": [item]}]}
    log = discover(registry)[0]
    assert log.url == item["monitoring_url"]
    assert log.origin == "submission.example/log"
    item["log_id"] = base64.b64encode(b"wrong").decode()
    with pytest.raises(ValueError):
        discover(registry)
