"""Standalone IR evaluation metrics for B12 LoCoMo benchmark.

Implements MRR, NDCG@k, and Precision@k without ranx/Numba dependencies.
All functions accept lists of retrieved items and sets of relevant items.

Correctness verified against hand-calculated test cases (see bottom).
"""

import math
import sys


def reciprocal_rank(retrieved: list, relevant: set) -> float:
    """Reciprocal rank: 1/rank of first relevant result.

    Args:
        retrieved: Ordered list of item IDs
        relevant: Set of relevant item IDs

    Returns:
        1/rank of first relevant result, or 0.0 if none found.
    """
    for i, item in enumerate(retrieved):
        if item in relevant:
            return 1.0 / (i + 1)
    return 0.0


def mean_reciprocal_rank(queries: list[tuple[list, set]]) -> float:
    """MRR averaged across multiple queries.

    Args:
        queries: List of (retrieved_list, relevant_set) tuples

    Returns:
        Average reciprocal rank across all queries.
    """
    if not queries:
        return 0.0
    return sum(reciprocal_rank(r, rel) for r, rel in queries) / len(queries)


def dcg_at_k(retrieved: list, relevant: set, k: int) -> float:
    """Discounted Cumulative Gain at k with binary relevance.

    DCG@k = sum_{i=1}^{k} rel_i / log2(i+1)

    Args:
        retrieved: Ordered list of item IDs
        relevant: Set of relevant item IDs
        k: Cutoff rank

    Returns:
        DCG score at rank k.
    """
    score = 0.0
    for i in range(min(k, len(retrieved))):
        if retrieved[i] in relevant:
            score += 1.0 / math.log2(i + 2)  # i+2 because log2(1)=0
    return score


def ndcg_at_k(retrieved: list, relevant: set, k: int) -> float:
    """Normalized DCG at k with binary relevance.

    NDCG@k = DCG@k / IDCG@k where IDCG is the ideal ordering.

    Args:
        retrieved: Ordered list of item IDs
        relevant: Set of relevant item IDs
        k: Cutoff rank

    Returns:
        NDCG score at rank k (0.0 to 1.0).
    """
    if not relevant:
        return 0.0

    actual_dcg = dcg_at_k(retrieved, relevant, k)

    # IDCG: all relevant items at the top
    ideal_k = min(k, len(relevant))
    ideal_dcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_k))

    if ideal_dcg == 0.0:
        return 0.0

    return actual_dcg / ideal_dcg


def precision_at_k(retrieved: list, relevant: set, k: int) -> float:
    """Precision@k: fraction of top-k results that are relevant.

    Args:
        retrieved: Ordered list of item IDs
        relevant: Set of relevant item IDs
        k: Cutoff rank

    Returns:
        Fraction of relevant items in top-k results.
    """
    if k == 0:
        return 0.0

    top_k = retrieved[:k]
    if not top_k:
        return 0.0

    relevant_in_top_k = sum(1 for item in top_k if item in relevant)
    return relevant_in_top_k / k


def recall_at_k(retrieved: list, relevant: set, k: int) -> float:
    """Recall@k: fraction of relevant items found in top-k.

    Args:
        retrieved: Ordered list of item IDs
        relevant: Set of relevant item IDs
        k: Cutoff rank

    Returns:
        Fraction of relevant items found in top-k.
    """
    if not relevant:
        return 0.0

    top_k = retrieved[:k]
    found = sum(1 for item in top_k if item in relevant)
    return found / len(relevant)


# ── Self-test ────────────────────────────────────────────────────────

def _run_tests():
    """Verify metrics against hand-calculated values."""
    passed = 0
    total = 0

    def check(name, actual, expected, tol=1e-6):
        nonlocal passed, total
        total += 1
        if abs(actual - expected) < tol:
            passed += 1
            print(f"  PASS: {name} = {actual:.6f} (expected {expected:.6f})")
        else:
            print(f"  FAIL: {name} = {actual:.6f} (expected {expected:.6f})")

    # Test case 1: Perfect ranking
    retrieved = ["a", "b", "c", "d", "e"]
    relevant = {"a", "b"}
    check("RR(perfect)", reciprocal_rank(retrieved, relevant), 1.0)
    check("NDCG@5(perfect)", ndcg_at_k(retrieved, relevant, 5), 1.0)
    check("P@5(perfect)", precision_at_k(retrieved, relevant, 5), 0.4)
    check("P@2(perfect)", precision_at_k(retrieved, relevant, 2), 1.0)

    # Test case 2: Imperfect ranking
    retrieved = ["x", "a", "y", "b", "z"]
    relevant = {"a", "b"}
    check("RR(imperfect)", reciprocal_rank(retrieved, relevant), 0.5)
    # DCG: rel at pos 2 (1/log2(3)) + rel at pos 4 (1/log2(5))
    # IDCG: rel at pos 1 (1/log2(2)) + rel at pos 2 (1/log2(3))
    actual_dcg = 1/math.log2(3) + 1/math.log2(5)
    ideal_dcg = 1/math.log2(2) + 1/math.log2(3)
    check("NDCG@5(imperfect)", ndcg_at_k(retrieved, relevant, 5), actual_dcg / ideal_dcg)
    check("P@5(imperfect)", precision_at_k(retrieved, relevant, 5), 0.4)
    check("P@3(imperfect)", precision_at_k(retrieved, relevant, 3), 1/3)

    # Test case 3: No relevant found
    retrieved = ["x", "y", "z"]
    relevant = {"a", "b"}
    check("RR(none)", reciprocal_rank(retrieved, relevant), 0.0)
    check("NDCG@3(none)", ndcg_at_k(retrieved, relevant, 3), 0.0)
    check("P@3(none)", precision_at_k(retrieved, relevant, 3), 0.0)

    # Test case 4: Empty input
    check("RR(empty)", reciprocal_rank([], {"a"}), 0.0)
    check("NDCG@5(empty)", ndcg_at_k([], {"a"}, 5), 0.0)
    check("P@5(empty)", precision_at_k([], {"a"}, 5), 0.0)
    check("NDCG(no_rel)", ndcg_at_k(["a", "b"], set(), 5), 0.0)

    # Test case 5: MRR across queries
    queries = [
        (["a", "b", "c"], {"a"}),      # RR = 1.0
        (["x", "a", "c"], {"a"}),      # RR = 0.5
        (["x", "y", "z"], {"a"}),      # RR = 0.0
    ]
    check("MRR(3queries)", mean_reciprocal_rank(queries), 0.5)

    # Test case 6: k > retrieved length
    retrieved = ["a", "b"]
    relevant = {"a", "b", "c"}
    check("P@10(short)", precision_at_k(retrieved, relevant, 10), 0.2)
    check("NDCG@10(short)", ndcg_at_k(retrieved, relevant, 10),
          (1/math.log2(2) + 1/math.log2(3)) /
          (1/math.log2(2) + 1/math.log2(3) + 1/math.log2(4)))

    print(f"\n  {passed}/{total} tests passed")
    return passed == total


if __name__ == "__main__":
    print("Running metric self-tests...")
    success = _run_tests()
    sys.exit(0 if success else 1)
