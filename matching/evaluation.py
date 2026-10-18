from __future__ import annotations

import math
from statistics import mean
from typing import Iterable


def precision_at(labels: list[int], k: int, relevant: int = 2) -> float:
    sample = labels[:k]
    return sum(value >= relevant for value in sample) / k if k else 0.0


def recall_at(labels: list[int], total_relevant: int, k: int, relevant: int = 2) -> float:
    return sum(value >= relevant for value in labels[:k]) / total_relevant if total_relevant else 0.0


def reciprocal_rank(labels: list[int], relevant: int = 2) -> float:
    return next((1.0 / rank for rank, value in enumerate(labels, 1) if value >= relevant), 0.0)


def ndcg_at(labels: list[int], k: int) -> float:
    def dcg(values: list[int]) -> float:
        return sum((2 ** value - 1) / math.log2(index + 2) for index, value in enumerate(values))
    actual = dcg(labels[:k])
    ideal = dcg(sorted(labels, reverse=True)[:k])
    return actual / ideal if ideal else 0.0


def evaluate(queries: Iterable[tuple[list[int], int]]) -> dict[str, float]:
    rows = list(queries)
    if not rows: return {}
    return {
        "precision@5": mean(precision_at(labels, 5) for labels, _ in rows),
        "precision@10": mean(precision_at(labels, 10) for labels, _ in rows),
        "recall@20": mean(recall_at(labels, total, 20) for labels, total in rows),
        "recall@50": mean(recall_at(labels, total, 50) for labels, total in rows),
        "mrr": mean(reciprocal_rank(labels) for labels, _ in rows),
        "ndcg@10": mean(ndcg_at(labels, 10) for labels, _ in rows),
        "ndcg@20": mean(ndcg_at(labels, 20) for labels, _ in rows),
    }
