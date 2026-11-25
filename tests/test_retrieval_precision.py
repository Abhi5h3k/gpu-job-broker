"""Scores the retrieval index against the labelled query set
(data/01c_retrieval_queries.jsonl) the way the exercise asks for --
precision@3 and precision@5, averaged only over queries with a non-empty
``expected_ids`` -- plus a few hand-written paraphrases that don't appear
in that file at all, to check the ranker generalizes rather than having
memorized the 24 labelled queries.
"""

from __future__ import annotations

import json

import pytest

from app.retrieval import DEFAULT_LOGS_PATH, get_default_index, precision_at_k

QUERIES_PATH = DEFAULT_LOGS_PATH.parent / "01c_retrieval_queries.jsonl"


def load_queries():
    with open(QUERIES_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.fixture(scope="module")
def index():
    return get_default_index()


@pytest.fixture(scope="module")
def queries():
    return load_queries()


def test_mean_precision_at_3_and_5_meet_a_high_bar(index, queries):
    p3_scores = []
    p5_scores = []
    for q in queries:
        p3 = precision_at_k(index, q["query"], q["expected_ids"], 3)
        p5 = precision_at_k(index, q["query"], q["expected_ids"], 5)
        if p3 is not None:
            p3_scores.append(p3)
        if p5 is not None:
            p5_scores.append(p5)

    assert p3_scores, "there should be at least one query with non-empty expected_ids to score"
    mean_p3 = sum(p3_scores) / len(p3_scores)
    mean_p5 = sum(p5_scores) / len(p5_scores)
    # Observed on this data: mean P@3 = 1.0, mean P@5 ~= 0.93. Thresholds
    # below are set a bit under that so small, harmless ranking tweaks
    # don't make this test flaky.
    assert mean_p3 >= 0.9
    assert mean_p5 >= 0.8


def test_queries_with_no_expected_match_are_excluded_from_scoring(index, queries):
    unrelated = [q for q in queries if not q["expected_ids"]]
    assert unrelated, "the labelled set should include at least one genuinely unrelated query"
    for q in unrelated:
        assert precision_at_k(index, q["query"], q["expected_ids"], 3) is None


@pytest.mark.parametrize(
    "query_text,expected_cluster_prefix_any_of",
    [
        (
            "ran out of GPU RAM mid training",
            ["F001", "F002", "F003", "F004", "F005", "F006", "F910", "F911", "F912"],
        ),
        ("service says my access has expired", ["F043", "F044", "F045", "F046", "F047", "F048"]),
        (
            "can't find my dataset file, permission denied",
            ["F013", "F014", "F015", "F016", "F017", "F018"],
        ),
        (
            "two GPUs stopped talking to each other during all-reduce",
            ["F025", "F026", "F027", "F028", "F029", "F030"],
        ),
        (
            "ran out of disk space while writing a checkpoint",
            ["F031", "F032", "F033", "F034", "F035", "F036"],
        ),
        (
            "my spot node got pulled out from under my job",
            ["F037", "F038", "F039", "F040", "F041", "F042"],
        ),
    ],
)
def test_generalizes_to_paraphrases_not_in_the_labelled_set(
    index, query_text, expected_cluster_prefix_any_of
):
    """None of these exact phrasings appear in 01c_retrieval_queries.jsonl
    -- they're everyday paraphrases of the same 9 underlying failure
    clusters, checking the ranker isn't just memorizing the labelled
    queries' exact wording."""
    results = index.rank(query_text, top_k=3)
    top_ids = {log.id for log, _score in results}
    assert top_ids & set(expected_cluster_prefix_any_of), (
        f"expected at least one of {expected_cluster_prefix_any_of} in top-3 for {query_text!r}, got {top_ids}"
    )


def test_unrelated_query_scores_everything_at_zero(index):
    results = index.rank("what is the weather like today", top_k=5)
    assert all(score == 0.0 for _log, score in results)
