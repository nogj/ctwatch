import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import dns.exception
import dns.name
import dns.resolver
import dns.rrset
import pytest

from ctwatch.caa import CAAValidator, evaluate, issuer_mapping
from ctwatch.ct import Entry, Log
from ctwatch.pipeline import process_range
from ctwatch.store import make_event


def record(value, tag="issue", flags=0):
    return {"flags": flags, "tag": tag, "value": value}


@pytest.mark.parametrize(
    "records,wildcard,identities,status,reason",
    [
        ([], False, [], "unrestricted", "no_applicable_issuance_restriction"),
        (
            [record("mailto:x@example.com", "iodef")],
            False,
            [],
            "unrestricted",
            "no_applicable_issuance_restriction",
        ),
        ([record("ca.example")], False, ["ca.example"], "authorized", "issuer_identity_listed"),
        (
            [record("other.example")],
            False,
            ["ca.example"],
            "unauthorized",
            "issuer_identity_not_listed",
        ),
        ([record(";")], False, [], "unauthorized", "issuance_denied_for_all_cas"),
        ([record("ca.example")], False, [], "unknown", "issuer_unmapped"),
        (
            [record("ca.example; accounturi=https://ca.example/a")],
            False,
            ["ca.example"],
            "unknown",
            "issuer_parameters_not_evaluated",
        ),
        (
            [record("ca.example"), record(";", "issuewild")],
            True,
            ["ca.example"],
            "unauthorized",
            "issuance_denied_for_all_cas",
        ),
        (
            [record("ca.example"), record(";", "issuewild")],
            False,
            ["ca.example"],
            "authorized",
            "issuer_identity_listed",
        ),
        ([record("ca.example")], True, ["ca.example"], "authorized", "issuer_identity_listed"),
        (
            [record("x", "custom", 128)],
            False,
            ["ca.example"],
            "unknown",
            "unsupported_critical_property",
        ),
        (
            [record("x", "custom", 0)],
            False,
            [],
            "unrestricted",
            "no_applicable_issuance_restriction",
        ),
        ([record("not a domain")], False, ["ca.example"], "unknown", "malformed_issuance_property"),
    ],
)
def test_policy_evaluation(records, wildcard, identities, status, reason):
    assert evaluate(records, wildcard, identities) == (status, reason)


def answer(name, *records):
    rrset = dns.rrset.from_text(name, 120, "IN", "CAA", *records) if records else None
    return (
        SimpleNamespace(canonical_name=dns.name.from_text(name), rrset=rrset, __iter__=None)
        if rrset is None
        else DNSAnswer(rrset)
    )


class DNSAnswer:
    def __init__(self, rrset):
        self.rrset = rrset
        self.canonical_name = rrset.name

    def __iter__(self):
        return iter(self.rrset)


def test_alias_lookup_then_original_parent_inheritance():
    resolver = SimpleNamespace(
        resolve=AsyncMock(
            side_effect=[
                answer("alias.other.net."),
                answer("example.com.", '0 issue "ca.example"'),
            ]
        )
    )
    validator = CAAValidator(resolver)
    result = asyncio.run(validator.lookup("www.example.com"))
    assert result["policy_domain"] == "example.com"
    assert result["records"] == [record("ca.example")]
    assert result["ttl"] == 120
    queried = [call.args[0].to_text() for call in resolver.resolve.call_args_list]
    assert queried == ["www.example.com.", "example.com."]
    assert all(call.kwargs["search"] is False for call in resolver.resolve.call_args_list)
    assert isinstance(resolver.cache, dns.resolver.LRUCache)


def test_caa_found_at_alias_target_is_used_without_climbing():
    resolver = SimpleNamespace(
        resolve=AsyncMock(return_value=answer("alias.other.net.", '0 issue "ca.example"'))
    )
    result = asyncio.run(CAAValidator(resolver).lookup("www.example.com"))
    assert result["policy_domain"] == "alias.other.net"
    assert resolver.resolve.await_count == 1


def test_nxdomain_climbs_to_parent_but_never_queries_root():
    resolver = SimpleNamespace(resolve=AsyncMock(side_effect=dns.resolver.NXDOMAIN()))
    result = asyncio.run(CAAValidator(resolver).lookup("www.example.com"))
    assert result["lookup_status"] == "absent"
    assert [call.args[0].to_text() for call in resolver.resolve.call_args_list] == [
        "www.example.com.",
        "example.com.",
        "com.",
    ]


def test_servfail_is_error_not_absent_or_unrestricted():
    resolver = SimpleNamespace(resolve=AsyncMock(side_effect=dns.resolver.NoNameservers()))
    result = asyncio.run(CAAValidator(resolver).lookup("example.com"))
    assert result["lookup_status"] == "error"
    assert resolver.resolve.await_count == 1


def test_slow_dns_is_bounded():
    async def slow(*args, **kwargs):
        await asyncio.Event().wait()

    resolver = SimpleNamespace(resolve=slow)
    result = asyncio.run(CAAValidator(resolver, timeout=0.01).lookup("example.com"))
    assert result["lookup_status"] == "error"
    assert result["error"] == "TimeoutError"


def test_only_exact_is_enriched_and_dns_is_shared_per_domain(certificate):
    entry = Entry(1, 1, certificate, "certificate")
    events = [
        {
            "match_type": kind,
            "observed_domain_ascii": "example.com",
            "wildcard": wild,
            "certificate_sha256": entry.fingerprint,
        }
        for kind, wild in [
            ("exact", False),
            ("exact", True),
            ("subdomain", False),
            ("variant", False),
            ("confusable", False),
        ]
    ]
    validator = CAAValidator(SimpleNamespace())
    validator.lookup = AsyncMock(
        return_value={
            "lookup_status": "found",
            "records": [record("ca.example"), record(";", "issuewild")],
        }
    )
    asyncio.run(
        validator.enrich(events, {entry.fingerprint: entry}, {"CN=example.com": ["ca.example"]})
    )
    assert [event.get("caa", {}).get("status") for event in events] == [
        "authorized",
        "unauthorized",
        None,
        None,
        None,
    ]
    assert validator.lookup.await_count == 1


def test_dns_error_still_saves_event_then_commits_cursor(certificate):
    entry = Entry(1, 1, certificate, "certificate")
    match = {
        "watched_domain": "example.com",
        "observed_domain_ascii": "example.com",
        "wildcard": False,
        "match_type": "exact",
    }
    engine = SimpleNamespace(
        analyze=AsyncMock(return_value=[[match]]), version="v1", caa_issuers={}
    )
    validator = CAAValidator(SimpleNamespace())
    validator.lookup = AsyncMock(return_value={"lookup_status": "error", "records": []})
    store = Mock()
    log = Log("id", "test", "https://log.example/", b"", "static", "usable")
    assert asyncio.run(process_range([entry], engine, store, log, 1, 2, "root", validator)) == (
        2,
        1,
    )
    event = store.save_events.call_args.args[0][0]
    assert event["caa"]["status"] == "unknown"
    assert event["caa"]["reason"] == "dns_lookup_error"
    assert [call[0] for call in store.mock_calls] == ["save_events", "commit"]


def test_mapping_is_explicit_and_validated():
    assert issuer_mapping({}) == {}
    assert issuer_mapping({"caa_issuers": {"CN=Issuer": ["CA.EXAMPLE"]}}) == {
        "CN=Issuer": ["ca.example"]
    }
    for mapping in [
        [],
        {"CN=Issuer": "ca.example"},
        {"CN=Issuer": []},
        {"CN=Issuer": ["ca.example; account=1"]},
    ]:
        with pytest.raises(ValueError):
            issuer_mapping({"caa_issuers": mapping})


def test_no_dns_for_non_exact_matches():
    validator = CAAValidator(SimpleNamespace())
    validator.lookup = AsyncMock()
    events = [{"match_type": kind} for kind in ("subdomain", "confusable", "variant")]
    asyncio.run(validator.enrich(events, {}, {}))
    validator.lookup.assert_not_called()
    assert all("caa" not in event for event in events)


def test_caa_snapshot_does_not_change_event_identity(certificate):
    entry = Entry(1, 1, certificate, "certificate")
    log = Log("id", "test", "https://log.example/", b"", "static", "usable")
    match = {
        "watched_domain": "example.com",
        "observed_domain_ascii": "example.com",
        "match_type": "exact",
        "wildcard": False,
    }
    original = make_event(entry, log, match, "version")
    enriched = make_event(entry, log, {**match, "caa": {"status": "unknown"}}, "version")
    assert original["event_id"] == enriched["event_id"]
