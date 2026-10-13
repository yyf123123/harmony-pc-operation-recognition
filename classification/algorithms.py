"""Numerical core extracted from the verified Windows offline experiment."""

from collections import Counter, defaultdict

import numpy as np

SEED = 20261003

def norm_rows(v: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    if np.any(norms <= 0):
        raise ValueError("zero center")
    return v / norms

def spherical_kmeans(v: np.ndarray, k: int, seed: int, n_init: int = 8, max_iter: int = 100):
    best = None
    for init in range(n_init):
        rng = np.random.default_rng(seed + init * 1009)
        centers = [v[int(rng.integers(len(v)))].copy()]
        min_distance = 1.0 - v @ centers[0]
        for _ in range(1, k):
            probs = np.maximum(min_distance, 0) + 1e-12
            probs /= probs.sum()
            idx = int(rng.choice(len(v), p=probs))
            centers.append(v[idx].copy())
            min_distance = np.minimum(min_distance, 1.0 - v @ centers[-1])
        centers = norm_rows(np.asarray(centers))
        previous = None
        for _ in range(max_iter):
            assignments = np.argmax(v @ centers.T, axis=1)
            if previous is not None and np.array_equal(assignments, previous):
                break
            previous = assignments.copy()
            updated = np.asarray([v[assignments == j].sum(axis=0) if np.any(assignments == j)
                                  else v[int(rng.integers(len(v)))] for j in range(k)])
            centers = norm_rows(updated)
        assignments = np.argmax(v @ centers.T, axis=1)
        score = float(np.sum((v @ centers.T)[np.arange(len(v)), assignments]))
        if best is None or score > best[2]:
            best = centers.copy(), assignments.copy(), score
    return best

def hungarian_maximize(scores: np.ndarray) -> np.ndarray:
    # Exact maximum-weight bipartite matching; no test labels enter scores.
    n = len(scores)
    cost = -scores.astype(float)
    u, v, p, way = np.zeros(n + 1), np.zeros(n + 1), np.zeros(n + 1, dtype=int), np.zeros(n + 1, dtype=int)
    for row in range(1, n + 1):
        p[0] = row
        col0 = 0
        minimum = np.full(n + 1, np.inf)
        used = np.zeros(n + 1, dtype=bool)
        while True:
            used[col0] = True
            row0 = p[col0]
            delta, col1 = np.inf, 0
            for col in range(1, n + 1):
                if used[col]:
                    continue
                current = cost[row0 - 1, col - 1] - u[row0] - v[col]
                if current < minimum[col]:
                    minimum[col], way[col] = current, col0
                if minimum[col] < delta:
                    delta, col1 = minimum[col], col
            for col in range(n + 1):
                if used[col]:
                    u[p[col]] += delta
                    v[col] -= delta
                else:
                    minimum[col] -= delta
            col0 = col1
            if p[col0] == 0:
                break
        while True:
            col1 = way[col0]
            p[col0] = p[col1]
            col0 = col1
            if col0 == 0:
                break
    result = np.full(n, -1, dtype=int)
    for col in range(1, n + 1):
        result[p[col] - 1] = col - 1
    return result

def make_folds(records: list[dict], labels: list[str], fold_count: int = 5, seed: int = SEED) -> list[list[str]]:
    groups: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        groups[r["trial_id"]][r["label"]] += 1
    if len(groups) < fold_count:
        raise ValueError(f"only {len(groups)} trial groups; need more groups")
    rng = np.random.default_rng(seed)
    trial_ids = sorted(groups)
    rng.shuffle(trial_ids)
    # Larger multi-label groups first; balance sample counts and per-label counts.
    trial_ids.sort(key=lambda x: -sum(groups[x].values()))
    folds: list[list[str]] = [[] for _ in range(fold_count)]
    counts = np.zeros((fold_count, len(labels)), dtype=int)
    sizes = np.zeros(fold_count, dtype=int)
    for trial in trial_ids:
        vec = np.asarray([groups[trial][label] for label in labels])
        fold = min(range(fold_count), key=lambda f: (int(np.dot(counts[f], vec)), int(sizes[f]), len(folds[f]), f))
        folds[fold].append(trial)
        counts[fold] += vec
        sizes[fold] += int(vec.sum())
    if any(not fold for fold in folds):
        raise ValueError("group assignment produced an empty fold")
    return folds

def metrics(truth: np.ndarray, pred: np.ndarray, labels: list[str]) -> dict:
    cm = np.zeros((len(labels), len(labels)), dtype=int)
    np.add.at(cm, (truth, pred), 1)
    per = {}
    for j, label in enumerate(labels):
        tp = int(cm[j, j]); support = int(cm[j].sum()); predicted = int(cm[:, j].sum())
        precision = tp / predicted if predicted else 0.0
        recall = tp / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per[label] = {"support": support, "correct": tp, "accuracy": recall, "precision": precision, "recall": recall, "f1": f1}
    pairs = [{"true": labels[i], "predicted": labels[j], "count": int(cm[i, j])}
             for i in range(len(labels)) for j in range(len(labels)) if i != j and cm[i, j]]
    return {"accuracy": float(np.mean(truth == pred)), "macro_f1": float(np.mean([x["f1"] for x in per.values()])),
            "per_operation": per, "confusion_labels": labels, "confusion_matrix": cm.tolist(),
            "main_confusions": sorted(pairs, key=lambda x: (-x["count"], x["true"], x["predicted"]))[:10]}

def fit_model(v: np.ndarray, y: np.ndarray, labels: list[str], method: str, seed: int):
    if method == "class_centroid_cosine":
        centers = norm_rows(np.asarray([v[y == j].sum(axis=0) for j in range(len(labels))]))
        mapping = np.arange(len(labels))
    else:
        centers, clusters, _ = spherical_kmeans(v, len(labels), seed)
        contingency = np.zeros((len(labels), len(labels)), dtype=int)
        np.add.at(contingency, (clusters, y), 1)
        mapping = hungarian_maximize(contingency)
    return centers, mapping
