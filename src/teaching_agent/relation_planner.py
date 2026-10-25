"""Bounded lexical candidates; selection suggests pairs, never asserts edges."""

import math
from collections import Counter, defaultdict

from .storage import dumps


def candidate_pairs(points, tokenize, limit=3):
    vectors, postings = [], defaultdict(list)
    for i, point in enumerate(points):
        counts = Counter(tokenize(point["title"] + " " + point["description"]))
        vectors.append(counts)
        for term in counts:
            postings[term].append(i)
    norms = []
    for counts in vectors:
        for term, count in counts.items():
            counts[term] = (1 + math.log(count)) * math.log(1 + len(points) / len(postings[term]))
        norms.append(math.sqrt(sum(v * v for v in counts.values())) or 1)
    pairs, previous = set(), {}
    for i, point in enumerate(points):
        section = point["section_id"]
        # Adjacent points give within-chapter coverage even without shared words.
        if section in previous:
            pairs.add((previous[section], i))
        previous[section] = i
        scores = defaultdict(float)
        for term, weight in vectors[i].items():
            for j in postings[term]:
                if i != j:
                    scores[j] += weight * vectors[j][term] / (norms[i] * norms[j])
        ranked = sorted(scores, key=lambda j: (-scores[j], j))
        for same_section in (True, False):
            chosen = [j for j in ranked if (points[j]["section_id"] == section) == same_section][
                :limit
            ]
            pairs.update(tuple(sorted((i, j))) for j in chosen)
    return sorted(pairs)


def relation_batches(points, pairs, budget=12000, pair_limit=32):
    """Deduplicate point descriptions within each request."""
    batches, selected, candidates = [], {}, []
    for i, j in pairs:
        pair = [points[i]["key"], points[j]["key"]]
        expanded = {**selected, i: points[i], j: points[j]}
        payload = {"points": list(expanded.values()), "candidate_pairs": candidates + [pair]}
        if candidates and (len(dumps(payload)) > budget or len(candidates) >= pair_limit):
            batches.append({"points": list(selected.values()), "candidate_pairs": candidates})
            expanded, candidates = {i: points[i], j: points[j]}, []
        selected = expanded
        candidates.append(pair)
    if candidates:
        batches.append({"points": list(selected.values()), "candidate_pairs": candidates})
    return batches
