"""In-memory caching with configurable expiration."""
import time


class Cache:
    def __init__(self, ttl=60):
        self.ttl = ttl
        self.entries = {}

    def put(self, key, value):
        self.entries[key] = (value, time.time())

    def get(self, key):
        entry = self.entries.get(key)
        if entry is None:
            return None
        value, created_at = entry
        if time.time() - created_at > self.ttl:
            return None
        return value

    def clear(self):
        for key in self.entries:
            del self.entries[key]
