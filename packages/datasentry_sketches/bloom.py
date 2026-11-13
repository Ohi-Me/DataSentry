"""Bloom filter over the baseline vocabulary.

``new_category_count`` asks a narrow question: how many values in this cycle
were never seen during the baseline window? A set of every historical value
would answer it and would also be a copy of the customer's data. A Bloom
filter answers it in a few kilobytes, with false positives only in the
direction that *under*-reports novelty — a new value can be wrongly judged
familiar, but a familiar value is never wrongly flagged as new.

That asymmetry is the right one: it costs recall on a weak signal rather than
manufacturing false alarms on 200k columns.
"""

from __future__ import annotations

import base64
import hashlib
import math
import zlib
from collections.abc import Iterable


def _positions(value: object, k: int, m: int) -> list[int]:
    digest = hashlib.blake2b(str(value).encode("utf-8"), digest_size=16).digest()
    a = int.from_bytes(digest[:8], "big")
    b = int.from_bytes(digest[8:], "big") | 1
    return [(a + i * b) % m for i in range(k)]


class BloomFilter:
    __slots__ = ("added", "bits", "k", "m")

    def __init__(self, capacity: int = 10000, error_rate: float = 0.01) -> None:
        capacity = max(capacity, 8)
        m = math.ceil(-capacity * math.log(error_rate) / (math.log(2) ** 2))
        self.m = max(m, 64)
        self.k = max(1, round((self.m / capacity) * math.log(2)))
        self.bits = bytearray((self.m + 7) // 8)
        self.added = 0

    def add(self, value: object) -> None:
        for pos in _positions(value, self.k, self.m):
            self.bits[pos >> 3] |= 1 << (pos & 7)
        self.added += 1

    def add_all(self, values: Iterable[object]) -> BloomFilter:
        for v in values:
            self.add(v)
        return self

    def __contains__(self, value: object) -> bool:
        return all(
            self.bits[pos >> 3] & (1 << (pos & 7))
            for pos in _positions(value, self.k, self.m)
        )

    def merge(self, other: BloomFilter) -> BloomFilter:
        if (other.m, other.k) != (self.m, self.k):
            raise ValueError("cannot merge Bloom filters with different geometry")
        out = BloomFilter.__new__(BloomFilter)
        out.m, out.k = self.m, self.k
        out.bits = bytearray(a | b for a, b in zip(self.bits, other.bits, strict=True))
        out.added = self.added + other.added
        return out

    def count_novel(self, values: Iterable[object]) -> int:
        return sum(1 for v in values if v not in self)

    def encode(self) -> str:
        header = self.m.to_bytes(4, "big") + self.k.to_bytes(1, "big")
        return base64.b64encode(header + zlib.compress(bytes(self.bits), 6)).decode("ascii")

    @classmethod
    def decode(cls, text: str | None) -> BloomFilter | None:
        if not text:
            return None
        try:
            raw = base64.b64decode(text)
            out = cls.__new__(cls)
            out.m = int.from_bytes(raw[:4], "big")
            out.k = raw[4]
            out.bits = bytearray(zlib.decompress(raw[5:]))
            out.added = 0
            return out
        except Exception:  # noqa: BLE001 - a corrupt stored sketch is a cache miss, not a crash
            return None
