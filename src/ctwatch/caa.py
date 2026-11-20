"""Post-match CAA policy observation, not historical issuance or chain validation."""

import asyncio
import time

import dns.asyncresolver
import dns.exception
import dns.name
import dns.resolver
from cryptography import x509

from .matcher import canonical


def issuer_mapping(document):
    mapping = document.get("caa_issuers", {})
    if not isinstance(mapping, dict):
        raise ValueError("caa_issuers must map issuer RFC4514 names to CAA identities")
    result = {}
    for issuer, identities in mapping.items():
        if not isinstance(issuer, str) or not issuer or not isinstance(identities, list):
            raise ValueError("invalid CAA issuer mapping")
        if not identities or any(
            not isinstance(value, str) or ";" in value for value in identities
        ):
            raise ValueError("CAA issuer identities must be nonempty domain lists")
        normalized = [canonical(value) for value in identities]
        if any(wildcard for _, _, wildcard in normalized):
            raise ValueError("CAA issuer identities cannot be wildcards")
        result[issuer] = sorted({value[0].rstrip(".") for value in normalized})
    return result


def evaluate(records, wildcard, identities):
    if any(
        row["flags"] & 128 and row["tag"] not in {"issue", "issuewild", "iodef"} for row in records
    ):
        return "unknown", "unsupported_critical_property"
    tag = "issuewild" if wildcard and any(row["tag"] == "issuewild" for row in records) else "issue"
    relevant = [row["value"] for row in records if row["tag"] == tag]
    if not relevant:
        return "unrestricted", "no_applicable_issuance_restriction"
    grants, malformed = [], False
    for value in relevant:
        domain, *parameters = value.split(";")
        domain = domain.strip().lower()
        try:
            normalized = canonical(domain)[0].rstrip(".") if domain else ""
            if "*" in domain:
                raise ValueError("invalid issuer domain")
        except (ValueError, UnicodeError):
            malformed = True
            continue
        grants.append((normalized, any(part.strip() for part in parameters)))
    if malformed:
        return "unknown", "malformed_issuance_property"
    if not any(domain for domain, _ in grants):
        return "unauthorized", "issuance_denied_for_all_cas"
    if not identities:
        return "unknown", "issuer_unmapped"
    matching = [conditional for domain, conditional in grants if domain in identities]
    if any(not conditional for conditional in matching):
        return "authorized", "issuer_identity_listed"
    if matching:
        return "unknown", "issuer_parameters_not_evaluated"
    return "unauthorized", "issuer_identity_not_listed"


class CAAValidator:
    def __init__(self, resolver=None, timeout=3.0, concurrency=16, catalog=None):
        self.catalog = catalog
        self.resolver = resolver if resolver is not None else dns.asyncresolver.Resolver()
        self.resolver.cache = dns.resolver.LRUCache(max_size=10000)
        self.timeout = timeout
        self.slots = asyncio.Semaphore(concurrency)

    async def lookup(self, domain):
        base = {"queried_domain": domain, "scope": "current_dns_policy", "dnssec_validated": False}
        try:
            # Includes queueing: DNS cannot indefinitely hold up cursor advancement.
            async with asyncio.timeout(self.timeout):
                async with self.slots:
                    name = dns.name.from_text(domain)
                    while name != dns.name.root:
                        try:
                            answer = await self.resolver.resolve(
                                name,
                                "CAA",
                                search=False,
                                raise_on_no_answer=False,
                                lifetime=self.timeout,
                            )
                        except dns.resolver.NXDOMAIN:
                            answer = None
                        if answer is not None and answer.rrset is not None:
                            return {
                                **base,
                                "lookup_status": "found",
                                "policy_domain": answer.canonical_name.to_text().rstrip("."),
                                "ttl": int(answer.rrset.ttl),
                                "records": [
                                    {
                                        "flags": int(record.flags),
                                        "tag": record.tag.decode("ascii").lower(),
                                        "value": record.value.decode("ascii"),
                                    }
                                    for record in answer
                                ],
                                "checked_at_ms": int(time.time() * 1000),
                            }
                        # After chasing aliases without finding CAA, climb the ORIGINAL name.
                        name = name.parent()
                    return {
                        **base,
                        "lookup_status": "absent",
                        "records": [],
                        "checked_at_ms": int(time.time() * 1000),
                    }
        except (TimeoutError, dns.exception.DNSException, UnicodeError, OSError) as error:
            return {
                **base,
                "lookup_status": "error",
                "records": [],
                "error": type(error).__name__,
                "checked_at_ms": int(time.time() * 1000),
            }

    async def enrich(self, events, entries, mapping):
        selected = [event for event in events if event["match_type"] == "exact"]
        domains = {event["observed_domain_ascii"] for event in selected}
        if not domains:
            return
        policies = dict(
            zip(domains, await asyncio.gather(*(self.lookup(d) for d in domains)), strict=True)
        )
        issuers = {}
        for event in selected:
            fingerprint = event["certificate_sha256"]
            if fingerprint not in issuers:
                certificate = x509.load_der_x509_certificate(entries[fingerprint].der)
                issuer = certificate.issuer.rfc4514_string()
                if issuer in mapping:
                    identity = {
                        "issuer_caa_identities": mapping[issuer],
                        "issuer_identity_source": "configured_issuer_dn",
                        "issuer_identity_reason": "manual_override",
                        "issuer_signature_verified": False,
                    }
                elif self.catalog is not None:
                    identity = self.catalog.identify(certificate)
                else:
                    identity = {
                        "issuer_caa_identities": [],
                        "issuer_identity_source": "unmapped",
                        "issuer_identity_reason": "issuer_catalog_missing",
                        "issuer_signature_verified": False,
                    }
                issuers[fingerprint] = issuer, identity
            issuer, identity = issuers[fingerprint]
            identities = identity["issuer_caa_identities"]
            policy = policies[event["observed_domain_ascii"]]
            status, reason = (
                ("unknown", "dns_lookup_error")
                if policy["lookup_status"] == "error"
                else evaluate(policy["records"], event["wildcard"], identities)
            )
            if reason == "issuer_unmapped":
                reason = identity["issuer_identity_reason"]
            event["caa"] = {
                **policy,
                "status": status,
                "reason": reason,
                "issuer_rfc4514": issuer,
                **identity,
                "chain_validated": False,
            }
