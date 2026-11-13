"""HyperLogLog: approximate distinct counting in fixed space.

Used for ``distinct_estimate``, which is how the cardinality detector sees a
dedup break: rows stay flat while the distinctness ratio falls off a cliff.
Exact distinct counting would mean holding every value, which is precisely
what ADR-010 forbids, so the approximation is not a compromise here — it is
the containment mechanism.

At p=14 the register array is 16 KB in memory, serialises to well under 1 KB
after run-length packing, and carries about 0.8% relative error. Registers
merge with ``max``, so baselines compose the same way t-digests do.
"""

from __future__ import annotations

import base64
import hashlib
import math
import zlib
from collections.abc import Iterable


def _hash64(value: object) -> int:
    digest = hashlib.blake2b(str(value).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


class HyperLogLog:
    __slots__ = ("m", "p", "registers")

    def __init__(self, p: int = 14) -> None:
        if not 4 <= p <= 18:
            raise ValueError("p must be between 4 and 18")
        self.p = p
        self.m = 1 << p
        self.registers = bytearray(self.m)

    def add(self, value: object) -> None:
        h = _hash64(value)
        index = h >> (64 - self.p)
        remainder = (h << self.p) & ((1 << 64) - 1)
        rank = 1
        if remainder == 0:
            rank = 64 - self.p + 1
        else:
            while remainder & (1 << 63) == 0:
                remainder <<= 1
                rank += 1
        if rank > self.registers[index]:
            self.registers[index] = min(rank, 255)

    def add_all(self, values: Iterable[object]) -> HyperLogLog:
        for v in values:
            self.add(v)
        return self

    @classmethod
    def of(cls, values: Iterable[object], p: int = 14) -> HyperLogLog:
        return cls(p).add_all(values)

    def _alpha(self) -> float:
        if self.m == 16:
            return 0.673
        if self.m == 32:
            return 0.697
        if self.m == 64:
            return 0.709
        return 0.7213 / (1.0 + 1.079 / self.m)

    def count(self) -> int:
        raw = self._alpha() * self.m * self.m / sum(2.0 ** -r for r in self.registers)
        zeros = self.registers.count(0)
        if raw <= 2.5 * self.m and zeros:
            # Small-range correction. Without it a nearly empty column reports
            # a floor of a few hundred distinct values, which would make the
            # cardinality detector fire on every young table.
            return round(self.m * math.log(self.m / zeros))
        return round(raw)

    def merge(self, other: HyperLogLog) -> HyperLogLog:
        if other.p != self.p:
            raise ValueError("cannot merge HyperLogLogs with different precision")
        out = HyperLogLog(self.p)
        out.registers = bytearray(
            max(a, b) for a, b in zip(self.registers, other.registers, strict=True)
        )
        return out

    @classmethod
    def merge_all(cls, sketches: Iterable[HyperLogLog], p: int = 14) -> HyperLogLog:
        out = cls(p)
        for s in sketches:
            out = out.merge(s)
        return out

    def encode(self) -> str:
        return base64.b64encode(
            bytes([self.p]) + zlib.compress(bytes(self.registers), 6)
        ).decode("ascii")

    @classmethod
    def decode(cls, text: str | None) -> HyperLogLog | None:
        if not text:
            return None
        try:
            raw = base64.b64decode(text)
            out = cls(raw[0])
            out.registers = bytearray(zlib.decompress(raw[1:]))
            return out
        except Exception:  # noqa: BLE001 - a corrupt stored sketch is a cache miss, not a crash
            return None

    def __len__(self) -> int:
        return self.count()

    def __repr__(self) -> str:  # pragma: no cover
        return "HyperLogLog(p=" + str(self.p) + ", n~=" + str(self.count()) + ")"
