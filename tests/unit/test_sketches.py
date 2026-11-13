"""Sketch accuracy and, more importantly, mergeability.

Mergeability is the property the whole profiling design rests on: a 30-cycle
baseline has to be a fold over 30 small objects rather than a rescan. If a
merge stopped agreeing with a direct computation, profiling 200k columns would
quietly become unaffordable and nothing would look broken.
"""

from __future__ import annotations

import random
import statistics

from datasentry_sketches import BloomFilter, CountMinTopK, HyperLogLog, TDigest


def test_tdigest_quantiles_track_the_true_distribution():
    rng = random.Random(7)
    values = [rng.gauss(100, 15) for _ in range(20000)]
    digest = TDigest.of(values)
    ordered = sorted(values)

    assert abs(digest.quantile(0.5) - statistics.median(values)) < 1.0
    assert abs(digest.quantile(0.99) - ordered[int(0.99 * len(ordered))]) < 2.0
    assert abs(digest.quantile(0.01) - ordered[int(0.01 * len(ordered))]) < 2.0


def test_tdigest_merge_matches_direct_computation():
    rng = random.Random(11)
    values = [rng.lognormvariate(4, 0.8) for _ in range(12000)]
    direct = TDigest.of(values)
    merged = TDigest.of(values[:4000]).merge(TDigest.of(values[4000:8000])) \
                                      .merge(TDigest.of(values[8000:]))

    for q in (0.1, 0.5, 0.9, 0.99):
        assert abs(merged.quantile(q) - direct.quantile(q)) / max(direct.quantile(q), 1) < 0.05


def test_tdigest_weighted_add_equals_repeated_add():
    """The optimisation that collapses repeats must not change the answer."""
    repeated = TDigest.of([3.0] * 500 + [9.0] * 250 + [1.0] * 250)
    weighted = TDigest()
    weighted.add(3.0, 500)
    weighted.add(9.0, 250)
    weighted.add(1.0, 250)

    assert abs(repeated.quantile(0.5) - weighted.quantile(0.5)) < 0.01
    assert repeated.count == weighted.count


def test_tdigest_survives_a_round_trip():
    digest = TDigest.of([float(i) for i in range(5000)])
    restored = TDigest.decode(digest.encode())
    assert restored is not None
    assert abs(restored.quantile(0.5) - digest.quantile(0.5)) < 1.0


def test_hyperloglog_error_stays_inside_its_stated_bound():
    sketch = HyperLogLog.of(range(50000), p=14)
    error = abs(sketch.count() - 50000) / 50000
    assert error < 0.02, "p=14 claims roughly 0.8% error; 2% is a generous ceiling"


def test_hyperloglog_merges_by_register_max():
    left = HyperLogLog.of(range(25000))
    right = HyperLogLog.of(range(25000, 50000))
    assert abs(left.merge(right).count() - 50000) / 50000 < 0.02


def test_hyperloglog_small_range_correction():
    """Without it, a nearly empty column reports a floor of hundreds."""
    assert HyperLogLog.of(["a", "b", "c"]).count() <= 5


def test_countmin_reports_mass_not_values():
    sketch = CountMinTopK(k=10, salt="pepper")
    for i in range(1000):
        sketch.add("status_" + str(i % 4))
    mass = sketch.mass()

    assert abs(sum(mass.values()) - 1.0) < 0.01
    assert len(mass) == 4
    # The plaintext category never appears in what leaves the sketch.
    assert not any("status_" in label for label in mass)


def test_bloom_never_reports_a_known_value_as_novel():
    """The asymmetry is the point: under-report novelty, never invent it."""
    bloom = BloomFilter(capacity=2000, error_rate=0.01)
    known = ["value_" + str(i) for i in range(500)]
    bloom.add_all(known)

    assert bloom.count_novel(known) == 0
    novel = ["unseen_" + str(i) for i in range(200)]
    assert bloom.count_novel(novel) > 190  # a few false "familiar" is acceptable
