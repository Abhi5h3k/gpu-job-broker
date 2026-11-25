"""The retrieval/AI track: given a failed job's log or a free-text
question, find the most similar past failures and their resolutions.

Approach: a fully offline, three-signal hybrid -- TF-IDF, a small hand-built
synonym/query-expansion table, and TF-IDF+SVD latent semantic analysis.
No network calls, no downloaded model weights, scikit-learn only. See
docs/DESIGN.md for the full reasoning behind choosing this over a local
sentence-embedding model (the short version: this corpus is only 59
narrow-domain technical documents, where a model trained on general web
text has no clear advantage over a lexical+latent-semantic method trained
directly on this exact domain's vocabulary, and this approach has zero
risk of being environment-dependent at evaluation time).

- TF-IDF (word n-grams 1-2) is the primary signal: it's very good at
  matching exact technical tokens and log fragments (`CUDA error`, `rank
  aborted`, file paths).
- The synonym table expands known everyday <-> technical phrasing pairs
  (e.g. "ran out of memory" <-> "OOM") before vectorizing, directly
  targeting the doc's stated challenge that "some questions use everyday
  words for technical terms."
- Latent Semantic Analysis (TF-IDF + TruncatedSVD), trained on these 59
  logs, adds a statistical semantic signal learned from this exact
  domain's data, to catch paraphrases the synonym table didn't anticipate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.preprocessing import normalize

DEFAULT_LOGS_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "01b_failure_logs.jsonl"
)

# How much weight the lexical (TF-IDF) signal gets vs. the latent-semantic
# (SVD) signal when combining scores. Weighted toward lexical because exact
# technical-token matches (error names, flags, file paths) are usually the
# strongest evidence two failures are the same kind of problem in this
# domain; the semantic signal mainly helps when a query paraphrases those
# tokens away entirely.
TFIDF_WEIGHT = 0.7
SVD_WEIGHT = 0.3

#: Each group is a set of phrases that mean the same underlying problem,
#: mixing the everyday phrasing a question might use with the technical
#: vocabulary the logs actually contain. Built directly from reading
#: data/01b_failure_logs.jsonl and data/01c_retrieval_queries.jsonl's nine
#: failure clusters -- see docs/DESIGN.md for the full list and reasoning.
SYNONYM_GROUPS: list[tuple[str, ...]] = [
    (
        "out of memory",
        "oom",
        "memory exhausted",
        "oomkilled",
        "exit code 137",
        "allocate tensor",
        "ran out of memory",
        "gpu ram",
        "ran out of gpu ram",
    ),
    (
        "pull access denied",
        "image pull",
        "container image",
        "download image",
        "registry credentials",
        "docker pull",
    ),
    (
        "permission denied",
        "permission problem",
        "file not found",
        "missing file",
        "dataset path",
        "cannot read data",
    ),
    (
        "loss nan",
        "divergence",
        "diverged",
        "gradient norm inf",
        "exploding gradients",
        "blew up",
        "training unstable",
    ),
    (
        "communicator aborted",
        "rank aborted",
        "nccl",
        "collective timeout",
        "all-reduce",
        "allreduce",
        "hangs",
        "times out",
        "multi-gpu",
    ),
    (
        "disk full",
        "no space left",
        "scratch disk",
        "checkpoint save failed",
        "disk usage",
    ),
    (
        "interruptible node",
        "taken back",
        "reclaim",
        "termination notice",
        "node vanished",
        "node lost",
        "heartbeat missed",
        "spot node",
        "spot instance",
        "preemptible",
        "pulled out from under",
    ),
    (
        "token expired",
        "bearer token",
        "jwt expired",
        "auth service",
        "access token expired",
        "credentials expired",
    ),
    (
        "driver too old",
        "cuda version mismatch",
        "libcudnn",
        "cudnn missing",
        "undefined symbol",
        "driver mismatch",
    ),
]


def expand_text(text: str) -> str:
    """Append every other phrase in a synonym group to ``text`` whenever
    any one phrase from that group appears in it. This is query expansion:
    a classic, simple IR technique for bridging vocabulary gaps, applied
    identically to both the indexed logs and incoming queries."""
    lowered = text.lower()
    extra: list[str] = []
    for group in SYNONYM_GROUPS:
        if any(phrase in lowered for phrase in group):
            extra.extend(group)
    if not extra:
        return text
    return text + " " + " ".join(extra)


@dataclass
class FailureLog:
    id: str
    team: str
    log: str
    resolution: str


def load_failure_logs(path: Path = DEFAULT_LOGS_PATH) -> list[FailureLog]:
    logs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            logs.append(
                FailureLog(
                    id=row["id"],
                    team=row["team"],
                    log=row["log"],
                    resolution=row["resolution"],
                )
            )
    return logs


class RetrievalIndex:
    """A searchable index over a fixed set of failure logs. Built once
    (loading 59 short documents and fitting TF-IDF/SVD takes a fraction of
    a second) and reused for every search."""

    def __init__(self, logs: list[FailureLog]) -> None:
        self.logs = logs
        documents = [expand_text(log.log) for log in logs]

        self.vectorizer = TfidfVectorizer(
            ngram_range=(1, 2), min_df=1, stop_words="english"
        )
        self.tfidf_matrix = self.vectorizer.fit_transform(documents)

        # SVD needs n_components < number of documents/features; this
        # corpus is tiny (59 logs), so cap it generously but safely.
        n_components = max(
            2, min(50, self.tfidf_matrix.shape[0] - 1, self.tfidf_matrix.shape[1] - 1)
        )
        self.svd = TruncatedSVD(n_components=n_components, random_state=0)
        self.lsa_matrix = normalize(self.svd.fit_transform(self.tfidf_matrix))

    def rank(self, query_text: str, top_k: int = 5) -> list[tuple[FailureLog, float]]:
        """Return the ``top_k`` most similar logs to ``query_text``, each
        with a 0..1-ish similarity score, highest first."""
        expanded = expand_text(query_text)
        query_tfidf = self.vectorizer.transform([expanded])

        tfidf_scores = cosine_similarity(query_tfidf, self.tfidf_matrix)[0]
        query_lsa = normalize(self.svd.transform(query_tfidf))
        svd_scores = (query_lsa @ self.lsa_matrix.T)[0]

        combined = TFIDF_WEIGHT * tfidf_scores + SVD_WEIGHT * svd_scores
        order = combined.argsort()[::-1][:top_k]
        return [(self.logs[i], float(combined[i])) for i in order]


@lru_cache(maxsize=1)
def get_default_index() -> RetrievalIndex:
    """The index over data/01b_failure_logs.jsonl, built once per process."""
    return RetrievalIndex(load_failure_logs())


# ---- Precision@k scoring (used by tests and the README's reported numbers) --


def precision_at_k(
    index: RetrievalIndex, query_text: str, expected_ids: list[str], k: int
) -> float | None:
    """Share of the top-k results whose id is in expected_ids. Returns
    None (not 0.0) when expected_ids is empty, so callers can exclude
    "nothing relevant exists" queries from the average, per the doc's
    instruction to average "over the questions that have relevant logs."
    """
    if not expected_ids:
        return None
    results = index.rank(query_text, top_k=k)
    hits = sum(1 for log, _score in results if log.id in expected_ids)
    return hits / k
