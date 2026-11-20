import copy

import pytest

from ctwatch.matcher import Matcher, analyze_batch, canonical, init_worker


def test_exact_subdomain_and_boundaries(document):
    matcher = Matcher(document, "data/confusables.txt")
    assert matcher.match("EXAMPLE.com.")[0]["match_type"] == "exact"
    assert matcher.match("api.example.com")[0]["match_type"] == "subdomain"
    assert not matcher.match("example.com.attacker.net")
    assert not matcher.match("notexample.com")
    assert not matcher.match("api.paypal.com")


def test_unicode_and_punycode_are_same_name(document):
    matcher = Matcher(document, "data/confusables.txt")
    unicode_name = "pаypal.com"  # U+0430 Cyrillic a
    ascii_name = canonical(unicode_name)[0]
    assert ascii_name.startswith("xn--")
    a, b = matcher.match(unicode_name), matcher.match(ascii_name)
    assert a == b
    assert a[0]["watched_domain"] == "paypal.com"
    assert a[0]["match_type"] == "confusable"
    assert matcher.match("paypal.com")[0]["match_type"] == "exact"


def test_wildcard_and_explicit_variant(document):
    matcher = Matcher(document, "data/confusables.txt")
    assert matcher.match("*.example.com")[0]["wildcard"] is True
    assert "variant" in {value["match_type"] for value in matcher.match("examplle.com")}


def test_version_invalidates_negative_and_positive_results(document):
    changed = copy.deepcopy(document)
    changed["domains"].append({"domain": "new-domain.net"})
    assert (
        Matcher(document, "data/confusables.txt").version
        != Matcher(changed, "data/confusables.txt").version
    )


def test_extract_san_and_ignore_unrelated(document, certificate):
    init_worker(document, "data/confusables.txt")
    values = analyze_batch([certificate])[0]
    assert {value["match_type"] for value in values} >= {"exact", "subdomain"}
    assert not any(value["observed_domain_ascii"] == "unrelated.net" for value in values)


def test_bad_certificate_fails_closed(document):
    init_worker(document, "data/confusables.txt")
    with pytest.raises(ValueError):
        analyze_batch([b"broken DER"])
