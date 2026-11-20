import time
from collections import OrderedDict


class TTLCache:
    """Exact bounded LRU. Expiration/eviction may repeat work, never discard unseen data."""

    def __init__(self, capacity: int, ttl: float, clock=time.monotonic):
        self.capacity = capacity
        self.ttl = ttl
        self.clock = clock
        self.items = OrderedDict()

    def get(self, key):
        item = self.items.pop(key, None)
        if item is None:
            return None
        expires, value = item
        if expires <= self.clock():
            return None
        self.items[key] = item
        return value

    def put(self, key, value):
        self.items.pop(key, None)
        self.items[key] = (self.clock() + self.ttl, value)
        while len(self.items) > self.capacity:
            self.items.popitem(last=False)
