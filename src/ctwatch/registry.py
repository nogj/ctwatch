"""Independent catalog refreshes, with one reader per cryptographic log identity."""

import asyncio

from .ct import discover

CHROME_LOG_LIST_URL = "https://www.gstatic.com/ct/log_list/v3/log_list.json"
APPLE_LOG_LIST_URL = "https://valid.apple.com/ct/log_list/current_log_list.json"


class Registry:
    def __init__(self, chrome_url=CHROME_LOG_LIST_URL, apple_url=APPLE_LOG_LIST_URL):
        self.urls = {
            name: url for name, url in [("chrome", chrome_url), ("apple", apple_url)] if url
        }
        self.snapshots = {}
        self.report = {}

    async def refresh(self, sources):
        async def fetch(name, url):
            try:
                document = (await sources.get(url)).json()
                logs = discover(document)
                if not logs:
                    raise ValueError("empty CT catalog")
                self.snapshots[name] = logs
                return name, {
                    "url": url,
                    "logs": len(logs),
                    "version": document.get("version"),
                    "timestamp": document.get("log_list_timestamp"),
                    "stale": False,
                }
            except Exception as error:
                return name, {
                    "url": url,
                    "error": f"{type(error).__name__}: {str(error)[:200]}",
                    "stale": True,
                    "retained_logs": len(self.snapshots.get(name, [])),
                }

        self.report = dict(await asyncio.gather(*(fetch(*item) for item in self.urls.items())))
        if not self.snapshots:
            raise ValueError(f"no CT catalogs available: {self.report}")
        found = {}
        # A log can be active in one program and rejected in the other. Follow it
        # while either program is active; Chrome wins equal-state endpoint choices.
        priority = {"usable": 3, "qualified": 3, "readonly": 2, "retired": 1, "rejected": 0}
        for name in self.urls:
            for log in self.snapshots.get(name, []):
                previous = found.get(log.log_id)
                if previous is None or priority[log.state] > priority[previous.state]:
                    found[log.log_id] = log
        return list(found.values())
