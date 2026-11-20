# CCADB CAA identifiers

`ccadb-caa.json` contains a snapshot of the Common CA Database (CCADB) report
“List of CAA Identifiers”, retrieved from the official CSV endpoint:

https://ccadb.my.salesforce-sites.com/ccadb/AllCAAIdentifiersReportCSVV2

Attribution: Common CA Database (CCADB). The data is distributed under
CDLA-Permissive-2.0; see the accompanying [license](CCADB-LICENSE.txt).
Source, fetch time and checksum are recorded in the snapshot. The checksum verifies
the saved content; it is not a CCADB digital signature or a trust-store endorsement.

Reports and usage terms: https://www.ccadb.org/resources and
https://www.ccadb.org/rootstores/usage.

Refresh from the repository root with:

```bash
PYTHONPATH=src .venv/bin/python scripts/fetch_caa_catalog.py
```

The service refreshes its runtime copy separately. Updating it does not rewrite this
bundled snapshot. Unknown or malformed disclosures are not interpreted as permissions.
