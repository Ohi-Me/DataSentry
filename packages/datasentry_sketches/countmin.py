"""Count-Min Sketch with a top-k heap, for categorical mass.

The categorical detector needs the shape of a column's value distribution
without keeping the values. Count-Min gives bounded-error frequency estimates
in fixed space; the accompanying heap tracks which keys are worth reporting.

Only the top-k **hashed** labels leave this structure. The plaintext category
never reaches a profile row, so a new enum value shows up as "a category that
was not in the baseline vocabulary" rather than as the value itself. That is
the difference between a detector and a data leak.
"""

from __future__ import annotations

import base64
import hashlib
import json
import zlib
from collections.abc import Iterable, Mapping


def _hashes(value: object, depth: int, width: int) -> list[int]:
    digest = hashlib.blake2b(str(value).encode("utf-8"), digest_size=16).digest()
    base_a = int.from_bytes(digest[:8], "big")
    base_b = int.from_bytes(digest[8:], "big") | 1
    return [(base_a + i * base_b) % width for i in range(depth)]


def label_of(value: object, salt: str = "") -> str:
    """The stable, non-reversible label a category is stored under."""
    return hashlib.blake2b(
        (salt + "|" + str(value)).encode("utf-8"), digest_size=6
    ).hexdigest()


class CountMinTopK:
    __slots__ = ("depth", "k", "salt", "table", "top", "total", "width")

    def __init__(self, depth: int = 4, width: int = 2048, k: int = 50, salt: str = "") -> None:
        self.depth = depth
        self.width = width
        self.k = k
        self.salt = salt
        self.table = [[0] * width for _ in range(depth)]
        self.top: dict[str, int] = {}
        self.total = 0

    def add(self, value: object, count: int = 1) -> None:
        label = label_of(value, self.salt)
        self.total += count
        estimate = None
        for row, idx in enumerate(_hashes(label, self.depth, self.width)):
            self.table[row][idx] += count
            cell = self.table[row][idx]
            estimate = cell if estimate is None else min(estimate, cell)
        self.top[label] = int(estimate or count)
        if len(self.top) > self.k * 4:
            self._trim()

    def add_all(self, values: Iterable[object]) -> CountMinTopK:
        for v in values:
            self.add(v)
        return self

    @classmethod
    def of(cls, values: Iterable[object], **kwargs: object) -> CountMinTopK:
        return cls(**kwargs).add_all(values)  # type: ignore[arg-type]

    def _trim(self) -> None:
        ordered = sorted(self.top.items(), key=lambda kv: kv[1], reverse=True)
        self.top = dict(ordered[: self.k])

    def estimate(self, value: object) -> int:
        label = label_of(value, self.salt)
        return min(self.table[row][idx] for row, idx in enumerate(_hashes(label, self.depth, self.width)))

    def top_k(self, k: int | None = None) -> dict[str, int]:
        self._trim()
        limit = k or self.k
        return dict(sorted(self.top.items(), key=lambda kv: kv[1], reverse=True)[:limit])

    def mass(self, k: int | None = None) -> dict[str, float]:
        """Top-k as a probability distribution — what the drift detectors compare."""
        counts = self.top_k(k)
        total = float(self.total) or 1.0
        return {label: n / total for label, n in counts.items()}

    def merge(self, other: CountMinTopK) -> CountMinTopK:
        if (other.depth, other.width) != (self.depth, self.width):
            raise ValueError("cannot merge sketches with different geometry")
        out = CountMinTopK(self.depth, self.width, self.k, self.salt)
        out.table = [
            [a + b for a, b in zip(rowa, rowb, strict=True)]
            for rowa, rowb in zip(self.table, other.table, strict=True)
        ]
        out.total = self.total + other.total
        merged: dict[str, int] = dict(self.top)
        for label, n in other.top.items():
            merged[label] = merged.get(label, 0) + n
        out.top = merged
        out._trim()
        return out

    @classmethod
    def merge_all(cls, sketches: Iterable[CountMinTopK]) -> CountMinTopK | None:
        out: CountMinTopK | None = None
        for s in sketches:
            out = s if out is None else out.merge(s)
        return out

    def vocabulary(self) -> set[str]:
        return set(self.top.keys())

    def encode(self) -> str:
        doc = {
            "d": self.depth,
            "w": self.width,
            "k": self.k,
            "t": self.total,
            "top": self.top_k(),
        }
        return base64.b64encode(
            zlib.compress(json.dumps(doc, separators=(",", ":")).encode("utf-8"), 6)
        ).decode("ascii")

    @classmethod
    def decode(cls, text: str | None) -> CountMinTopK | None:
        if not text:
            return None
        try:
            doc = json.loads(zlib.decompress(base64.b64decode(text)).decode("utf-8"))
        except Exception:  # noqa: BLE001 - a corrupt stored sketch is a cache miss, not a crash
            return None
        out = cls(doc["d"], doc["w"], doc["k"])
        out.total = doc["t"]
        out.top = dict(doc["top"])
        return out

    @classmethod
    def from_mapping(cls, counts: Mapping[str, int]) -> CountMinTopK:
        """Rehydrate from a stored ``categorical_topk`` map (already hashed)."""
        out = cls()
        out.top = dict(counts)
        out.total = sum(counts.values())
        return out
