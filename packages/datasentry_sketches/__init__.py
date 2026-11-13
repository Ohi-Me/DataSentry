"""Mergeable summary structures.

Every profile DataSentry stores is built from these. Nothing here retains a
raw value, which is what makes ADR-010 an architectural property rather than
a policy: there is no code path from a stored profile back to a customer's
data, because the data was never kept.

The other reason these specific structures were chosen is **mergeability**.
A 30-day baseline is the merge of 30 daily sketches, not a rescan of 30 days
of rows. That single property is what makes profiling 200k columns affordable,
and it is why the answer is sketches rather than stored histograms.

All four are pure Python and serialise to compact strings so a profile row
stays around 3 KB per column per cycle.
"""

from datasentry_sketches.bloom import BloomFilter
from datasentry_sketches.countmin import CountMinTopK
from datasentry_sketches.hyperloglog import HyperLogLog
from datasentry_sketches.tdigest import TDigest

__all__ = ["BloomFilter", "CountMinTopK", "HyperLogLog", "TDigest"]
