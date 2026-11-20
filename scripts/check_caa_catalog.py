"""Offline CCADB lookup audit against captured certificate DER; does not query DNS/AWS."""

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from cryptography import x509

from ctwatch.issuer_catalog import IssuerCatalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--output", type=Path, default=Path(".deploy/ccadb-lookup.json"))
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    catalog = IssuerCatalog(cache="/tmp/ctwatch-ccadb-offline-unused.json")
    db = sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)
    reasons, issuers = Counter(), Counter()
    for (der,) in db.execute("SELECT der FROM certificates ORDER BY sha LIMIT ?", (args.limit,)):
        certificate = x509.load_der_x509_certificate(der)
        result = catalog.identify(certificate)
        reasons[result["issuer_identity_reason"]] += 1
        issuers[certificate.issuer.rfc4514_string()] += 1
    db.close()
    report = {
        "catalog": catalog.report(),
        "certificates_checked": sum(reasons.values()),
        "identity_reasons": dict(reasons),
        "issuers": dict(issuers.most_common()),
        "limitations": [
            "Recorded sample ordered by SHA-256, not a uniform ecosystem sample.",
            "AKI/SKI and full subject lookup only; no issuer signature or chain verification.",
            "No DNS CAA policy was queried by this audit.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
