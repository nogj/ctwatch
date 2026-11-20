import asyncio
import csv
import datetime
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from ctwatch.caa import CAAValidator
from ctwatch.ct import Entry
from ctwatch.issuer_catalog import CCADB_URL, IssuerCatalog, parse_csv, snapshot

STAMP = 1800000000
KEY_ID = b"a" * 20


def document(*domains, subject="CN=Issuer; O=Example; C=ES"):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["Subject", "Subject Key Identifier (Hex)", "Recognized CAA Domains"])
    for value in domains:
        writer.writerow([subject, KEY_ID.hex().upper(), value])
    return stream.getvalue()


def certificate(aki=KEY_ID, issuer_cn="Issuer"):
    key = ec.generate_private_key(ec.SECP256R1())
    issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "ES"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Example"),
            x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn),
        ]
    )
    now = datetime.datetime.now(datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(issuer)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
    )
    if aki is not None:
        builder = builder.add_extension(x509.AuthorityKeyIdentifier(aki, None, None), False)
    return builder.sign(key, hashes.SHA256())


def catalog(tmp_path, text=None, stamp=STAMP):
    bundled = tmp_path / "bundled.json"
    bundled.write_text(json.dumps(snapshot(text or document("ca.example"), now=stamp)))
    return IssuerCatalog(bundled, tmp_path / "cache.json", clock=lambda: STAMP)


def test_automatic_identity_requires_both_aki_and_full_subject(tmp_path):
    value = catalog(tmp_path)
    result = value.identify(certificate())
    assert result["issuer_caa_identities"] == ["ca.example"]
    assert result["issuer_identity_source"] == "ccadb_aki_ski_subject"
    assert not result["issuer_signature_verified"]
    assert value.identify(certificate(issuer_cn="Impostor"))["issuer_identity_reason"] == (
        "issuer_subject_not_matched"
    )
    assert value.identify(certificate(aki=b"b" * 20))["issuer_identity_reason"] == (
        "issuer_not_in_catalog"
    )
    assert value.identify(certificate(aki=None))["issuer_identity_reason"] == "issuer_aki_missing"


@pytest.mark.parametrize(
    "text,reason",
    [
        (document("ca.example", "other.example"), "issuer_catalog_ambiguous"),
        (document(""), "issuer_caa_identities_not_disclosed"),
        (document("N/A"), "issuer_caa_identities_invalid"),
        (document("ca.example, admin@domain; master@domain"), "issuer_caa_identities_invalid"),
    ],
)
def test_ambiguous_missing_or_invalid_disclosures_are_unknown(tmp_path, text, reason):
    result = catalog(tmp_path, text).identify(certificate())
    assert result["issuer_caa_identities"] == []
    assert result["issuer_identity_reason"] == reason


def test_duplicate_rows_and_multiple_identities_are_not_ambiguous(tmp_path):
    result = catalog(tmp_path, document("CA.EXAMPLE., other.example", "other.example, ca.example"))
    assert result.identify(certificate())["issuer_caa_identities"] == [
        "ca.example",
        "other.example",
    ]


def test_old_snapshot_cannot_authorize_or_deny_an_issuer(tmp_path):
    result = catalog(tmp_path, stamp=STAMP - 8 * 86400).identify(certificate())
    assert result["issuer_caa_identities"] == []
    assert result["issuer_identity_reason"] == "issuer_catalog_stale_or_missing"


def test_cache_checksum_and_schema_failures_fall_back_to_bundle(tmp_path):
    value = catalog(tmp_path)
    good_hash = value.data["sha256"]
    bad = snapshot(document("other.example"), now=STAMP)
    bad["csv"] = document("evil.example")
    for data in [bad, {"csv": []}, {"csv": "<html>error</html>"}]:
        value.cache.write_text(json.dumps(data))
        loaded = IssuerCatalog(tmp_path / "bundled.json", value.cache, clock=lambda: STAMP)
        assert loaded.data["sha256"] == good_hash


def test_refresh_is_daily_atomic_and_survives_restart(tmp_path):
    value = catalog(tmp_path, stamp=STAMP - 86401)
    sources = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(content=document("new.example").encode()))
    )
    assert asyncio.run(value.refresh(sources))
    assert value.identify(certificate())["issuer_caa_identities"] == ["new.example"]
    assert not asyncio.run(value.refresh(sources))
    sources.get.assert_awaited_once_with(CCADB_URL)
    loaded = IssuerCatalog(tmp_path / "bundled.json", value.cache, clock=lambda: STAMP)
    assert loaded.data == value.data


def test_failed_refresh_preserves_snapshot_and_throttles_retries(tmp_path):
    value = catalog(tmp_path, stamp=STAMP - 86401)
    old = value.data
    sources = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(content=b"<html>error</html>"))
    )
    assert not asyncio.run(value.refresh(sources))
    assert value.data is old
    assert value.report()["last_error"]
    assert not asyncio.run(value.refresh(sources))
    assert sources.get.await_count == 1
    assert value.identify(certificate())["issuer_caa_identities"] == ["ca.example"]


def test_unwritable_cache_does_not_replace_last_good_copy(tmp_path):
    value = catalog(tmp_path, stamp=STAMP - 86401)
    value.cache = tmp_path / "missing" / "cache.json"
    sources = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(content=document("new.example").encode()))
    )
    assert not asyncio.run(value.refresh(sources))
    assert value.identify(certificate())["issuer_caa_identities"] == ["ca.example"]


def test_enrichment_uses_catalog_and_manual_override_takes_precedence(tmp_path):
    cert = certificate()
    entry = Entry(1, 1, cert.public_bytes(serialization.Encoding.DER), "certificate")
    validator = CAAValidator(SimpleNamespace(), catalog=catalog(tmp_path))
    validator.lookup = AsyncMock(
        return_value={
            "lookup_status": "found",
            "records": [{"flags": 0, "tag": "issue", "value": "ca.example"}],
        }
    )

    def event():
        return {
            "match_type": "exact",
            "observed_domain_ascii": "example.com",
            "certificate_sha256": entry.fingerprint,
            "wildcard": False,
        }

    first = event()
    asyncio.run(validator.enrich([first], {entry.fingerprint: entry}, {}))
    assert first["caa"]["status"] == "authorized"
    assert first["caa"]["issuer_identity_source"] == "ccadb_aki_ski_subject"
    second = event()
    asyncio.run(
        validator.enrich(
            [second], {entry.fingerprint: entry}, {cert.issuer.rfc4514_string(): ["manual.example"]}
        )
    )
    assert second["caa"]["status"] == "unauthorized"
    assert second["caa"]["issuer_identity_source"] == "configured_issuer_dn"


def test_catalog_ambiguity_is_explained_in_event(tmp_path):
    cert = certificate()
    entry = Entry(1, 1, cert.public_bytes(serialization.Encoding.DER), "certificate")
    validator = CAAValidator(
        SimpleNamespace(), catalog=catalog(tmp_path, document("a.example", "b.example"))
    )
    validator.lookup = AsyncMock(
        return_value={
            "lookup_status": "found",
            "records": [{"flags": 0, "tag": "issue", "value": "a.example"}],
        }
    )
    event = {
        "match_type": "exact",
        "observed_domain_ascii": "example.com",
        "certificate_sha256": entry.fingerprint,
        "wildcard": False,
    }
    asyncio.run(validator.enrich([event], {entry.fingerprint: entry}, {}))
    assert event["caa"]["status"] == "unknown"
    assert event["caa"]["reason"] == "issuer_catalog_ambiguous"


@pytest.mark.parametrize(
    "text",
    [
        "<html>error</html>",
        document(),
        "Subject,Subject Key Identifier (Hex),Recognized CAA Domains\nx,XYZ,ca.example",
        "Subject,Subject Key Identifier (Hex),Recognized CAA Domains\nx",
    ],
)
def test_malformed_catalog_is_rejected(text):
    with pytest.raises(ValueError):
        parse_csv(text)
