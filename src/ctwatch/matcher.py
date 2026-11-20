import hashlib
import json
import unicodedata
from pathlib import Path

import idna
from cryptography import x509


def canonical(name: str) -> tuple[str, str, bool]:
    name = name.strip().rstrip(".")
    wildcard = name.startswith("*.")
    if wildcard:
        name = name[2:]
    ascii_name = idna.encode(name, uts46=True, std3_rules=True).decode("ascii").lower()
    return ascii_name, idna.decode(ascii_name), wildcard


class Matcher:
    def __init__(self, document: dict, confusables_path: str):
        if document.get("schema_version") != 1 or not document.get("domains"):
            raise ValueError("watchlist needs schema_version=1 and nonempty domains")
        self.version = hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
            + Path(confusables_path).read_bytes()
            + f"ctwatch-skeleton-v1:{unicodedata.unidata_version}:{idna.__version__}".encode()
        ).hexdigest()
        self.mapping = {}
        for line in Path(confusables_path).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            source, target, *_ = line.split(";")
            points = source.split()
            if len(points) != 1:
                raise ValueError("unsupported multi-codepoint confusable source")
            self.mapping[chr(int(points[0], 16))] = "".join(
                chr(int(part, 16)) for part in target.split()
            )
        self.exact: dict[str, list[dict]] = {}
        self.skeletons: dict[str, list[str]] = {}
        self.variants: dict[str, list[str]] = {}
        for rule in document["domains"]:
            if not isinstance(rule.get("include_subdomains", False), bool):
                raise ValueError("include_subdomains must be boolean")
            if not isinstance(rule.get("variants", []), list):
                raise ValueError("variants must be an array")
            watched = canonical(rule["domain"])[0]
            self.exact.setdefault(watched, []).append(rule)
            self.skeletons.setdefault(self.skeleton(canonical(watched)[1]), []).append(watched)
            for variant in rule.get("variants", []):
                self.variants.setdefault(canonical(variant)[0], []).append(watched)

    def skeleton(self, name: str) -> str:
        decomposed = unicodedata.normalize("NFD", name)
        return unicodedata.normalize(
            "NFD", "".join(self.mapping.get(char, char) for char in decomposed)
        )

    def match(self, name: str) -> list[dict]:
        ascii_name, unicode_name, wildcard = canonical(name)
        found: dict[tuple[str, str], dict] = {}

        def add(watched: str, kind: str):
            found[(watched, kind)] = {
                "watched_domain": watched,
                "observed_domain_ascii": ascii_name,
                "observed_domain_unicode": unicode_name,
                "wildcard": wildcard,
                "match_type": kind,
            }

        if ascii_name in self.exact:
            add(ascii_name, "exact")
        labels = ascii_name.split(".")
        for index in range(1, len(labels)):
            parent = ".".join(labels[index:])
            if any(rule.get("include_subdomains", False) for rule in self.exact.get(parent, [])):
                add(parent, "subdomain")
        for watched in self.skeletons.get(self.skeleton(unicode_name), []):
            if watched != ascii_name:
                add(watched, "confusable")
        for watched in self.variants.get(ascii_name, []):
            if watched != ascii_name:
                add(watched, "variant")
        return list(found.values())


_matcher = None
_name_cache = None


def init_worker(document: dict, confusables_path: str):
    from .cache import TTLCache

    global _matcher, _name_cache
    _matcher = Matcher(document, confusables_path)
    _name_cache = TTLCache(100_000, 3600)


def analyze_batch(certificates: list[bytes]) -> list[list[dict]]:
    results = []
    for der in certificates:
        certificate = x509.load_der_x509_certificate(der)
        try:
            names = certificate.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value.get_values_for_type(x509.DNSName)
        except x509.ExtensionNotFound:
            names = []
        matches = []
        for name in sorted(set(names)):
            try:
                value = _name_cache.get(name)
                if value is None:
                    value = _matcher.match(name)
                    _name_cache.put(name, value)
                matches.extend(value)
            except (idna.IDNAError, UnicodeError):
                # Invalid DNS SAN is ignored; certificate parsing failures halt its log.
                continue
        results.append(matches)
    return results
