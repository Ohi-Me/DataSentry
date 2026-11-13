"""Isolation Forest, in about a hundred lines.

The intuition is that anomalies are easy to separate: build random binary
trees by picking a random feature and a random split point, and a point that
does not belong ends up isolated near the root. Average path length over many
trees, normalise by the expected path length of an unsuccessful BST search,
and you have a score in [0, 1] where higher means stranger.

Writing it out rather than importing scikit-learn is a deliberate trade. The
model is 5 features and a few dozen trees; the dependency would cost more in
container size and cold start than the algorithm costs to maintain, and every
line of it is inspectable by whoever has to defend a score to an analyst.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass
class _Node:
    feature: int = -1
    split: float = 0.0
    left: _Node | None = None
    right: _Node | None = None
    size: int = 0

    @property
    def is_leaf(self) -> bool:
        return self.left is None and self.right is None


def _expected_path(n: int) -> float:
    """c(n): average path length of an unsuccessful search in a BST of n nodes."""
    if n <= 1:
        return 0.0
    if n == 2:
        return 1.0
    euler = 0.5772156649015329
    return 2.0 * (math.log(n - 1) + euler) - 2.0 * (n - 1) / n


class IsolationForest:
    def __init__(self, n_trees: int = 64, sample_size: int = 128, seed: int = 0) -> None:
        self.n_trees = n_trees
        self.sample_size = sample_size
        self.seed = seed
        self._trees: list[_Node] = []
        self._trained_on = 0
        self._height_limit = 0

    def stale(self, available_rows: int) -> bool:
        """Retrain when the history has grown by half since the last fit."""
        return not self._trees or available_rows > self._trained_on * 1.5

    def fit(self, rows: Sequence[Sequence[float]]) -> IsolationForest:
        data = [list(map(float, r)) for r in rows if r]
        if not data:
            return self
        rng = random.Random(self.seed)
        sample_size = min(self.sample_size, len(data))
        self._height_limit = max(1, math.ceil(math.log2(max(sample_size, 2))))
        self._trees = []
        for _ in range(self.n_trees):
            sample = rng.sample(data, sample_size) if sample_size < len(data) else list(data)
            self._trees.append(self._build(sample, 0, rng))
        self._trained_on = len(data)
        return self

    def _build(self, rows: list[list[float]], depth: int, rng: random.Random) -> _Node:
        if depth >= self._height_limit or len(rows) <= 1:
            return _Node(size=len(rows))
        n_features = len(rows[0])
        candidates = list(range(n_features))
        rng.shuffle(candidates)
        for feature in candidates:
            values = [r[feature] for r in rows]
            lo, hi = min(values), max(values)
            if hi - lo <= 1e-12:
                continue
            split = rng.uniform(lo, hi)
            left = [r for r in rows if r[feature] < split]
            right = [r for r in rows if r[feature] >= split]
            if not left or not right:
                continue
            return _Node(
                feature=feature,
                split=split,
                left=self._build(left, depth + 1, rng),
                right=self._build(right, depth + 1, rng),
                size=len(rows),
            )
        # Every feature is constant across this sample: nothing left to split.
        return _Node(size=len(rows))

    def _path_length(self, node: _Node, row: Sequence[float], depth: int = 0) -> float:
        if node.is_leaf:
            return depth + _expected_path(node.size)
        value = row[node.feature] if node.feature < len(row) else 0.0
        child = node.left if value < node.split else node.right
        return self._path_length(child or node, row, depth + 1)

    def anomaly_score(self, row: Sequence[float]) -> float:
        """Score in [0, 1]. Above ~0.6 is conventionally 'anomalous'."""
        if not self._trees:
            return 0.0
        avg = sum(self._path_length(t, row) for t in self._trees) / len(self._trees)
        norm = _expected_path(min(self.sample_size, max(self._trained_on, 2)))
        if norm <= 0:
            return 0.0
        return 2.0 ** (-avg / norm)
