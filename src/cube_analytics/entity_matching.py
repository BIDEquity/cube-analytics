"""
Entity Matching - Fuzzy string matching for entity deduplication and linking.

A simple, standalone module for matching entities between datasets using
multiple similarity algorithms. No dependencies on command_center.

Example:
    >>> from cube_analytics.entity_matching import match_entities
    >>> source = [{"id": "1", "name": "Acme Inc."}]
    >>> target = [{"id": "a", "name": "ACME Corporation"}]
    >>> results = match_entities(source, target)
    >>> print(results[0].score)  # 0.85
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jaro import jaro_winkler_metric
from thefuzz import fuzz

if TYPE_CHECKING:
    import polars as pl

# Default stop words for company name matching
DEFAULT_STOP_WORDS: set[str] = {
    # German
    'gmbh', 'ag', 'kg', 'ev', 'mbh', 'ohg', 'gbr',
    # English
    'inc', 'llc', 'ltd', 'corp', 'company', 'co', 'llp', 'lp', 'plc',
    # French
    'sa', 'sarl', 'sas', 'snc', 'eurl',
    # Generic business terms
    'group', 'holdings', 'international', 'worldwide', 'global',
    'solutions', 'services', 'systems', 'technologies', 'technology',
    'consulting', 'consultancy', 'partners', 'partnership',
    'enterprises', 'industries', 'corporation',
}

# Default weights for similarity algorithms
DEFAULT_WEIGHTS: dict[str, float] = {
    'ratio': 0.15,
    'partial_ratio': 0.10,
    'token_sort_ratio': 0.25,
    'token_set_ratio': 0.25,
    'jaro_winkler': 0.25,
}


@dataclass
class MatchResult:
    """A single match result between source and target entity."""

    source_id: str
    source_name: str
    target_id: str | None
    target_name: str | None
    score: float
    is_exact: bool
    scores: dict[str, float]  # Individual algorithm scores


def clean_name(name: str, stop_words: set[str] | None = None) -> str:
    """
    Normalize a company/entity name for matching.

    - Converts to lowercase
    - Removes punctuation
    - Removes stop words (legal suffixes, generic terms)
    - Collapses whitespace

    Args:
        name: Raw entity name
        stop_words: Words to remove (defaults to DEFAULT_STOP_WORDS)

    Returns:
        Cleaned, normalized name
    """
    if not isinstance(name, str) or not name.strip():
        return ''

    stop_words = stop_words or DEFAULT_STOP_WORDS

    # Lowercase and remove punctuation
    cleaned = name.strip().lower()
    cleaned = re.sub(r'[^\w\s]', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()

    # Remove stop words
    words = cleaned.split()
    filtered = [w for w in words if w not in stop_words]

    return ' '.join(filtered)


def calculate_similarity(
    name1: str,
    name2: str,
    stop_words: set[str] | None = None,
    weights: dict[str, float] | None = None,
) -> tuple[float, dict[str, float], bool]:
    """
    Calculate similarity between two names using multiple algorithms.

    Uses a weighted combination of:
    - fuzz.ratio (Levenshtein distance)
    - fuzz.partial_ratio (best partial match)
    - fuzz.token_sort_ratio (sorted token comparison)
    - fuzz.token_set_ratio (set-based token comparison)
    - jaro_winkler_metric (Jaro-Winkler distance)

    Args:
        name1: First name
        name2: Second name
        stop_words: Words to remove before comparison
        weights: Algorithm weights (must sum to 1.0)

    Returns:
        Tuple of (composite_score, individual_scores, is_exact_match)
    """
    weights = weights or DEFAULT_WEIGHTS

    clean1 = clean_name(name1, stop_words)
    clean2 = clean_name(name2, stop_words)

    # Handle empty names
    if not clean1 or not clean2:
        return 0.0, {k: 0.0 for k in weights}, False

    # Check for exact match first
    if clean1 == clean2:
        perfect_scores = {k: 1.0 for k in weights}
        return 1.0, perfect_scores, True

    # Calculate individual scores
    scores = {
        'ratio': fuzz.ratio(clean1, clean2) / 100.0,
        'partial_ratio': fuzz.partial_ratio(clean1, clean2) / 100.0,
        'token_sort_ratio': fuzz.token_sort_ratio(clean1, clean2) / 100.0,
        'token_set_ratio': fuzz.token_set_ratio(clean1, clean2) / 100.0,
        'jaro_winkler': jaro_winkler_metric(clean1, clean2),
    }

    # Calculate weighted composite
    composite = sum(scores.get(k, 0.0) * w for k, w in weights.items())
    composite = min(composite, 1.0)

    return composite, scores, False


def match_entities(
    source: list[dict] | pl.DataFrame,
    target: list[dict] | pl.DataFrame | None = None,
    source_name_col: str = 'name',
    source_id_col: str = 'id',
    target_name_col: str | None = None,
    target_id_col: str | None = None,
    threshold: float = 0.7,
    max_matches: int = 5,
    stop_words: set[str] | None = None,
    weights: dict[str, float] | None = None,
    duplicate_mode: bool = False,
) -> list[MatchResult]:
    """
    Match entities between source and target datasets.

    Args:
        source: Source dataset (list of dicts or Polars DataFrame)
        target: Target dataset. If None and duplicate_mode=True, uses source
        source_name_col: Column with entity names in source
        source_id_col: Column with entity IDs in source
        target_name_col: Column with entity names in target (defaults to source_name_col)
        target_id_col: Column with entity IDs in target (defaults to source_id_col)
        threshold: Minimum similarity score to include (0.0-1.0)
        max_matches: Maximum matches to return per source entity
        stop_words: Custom stop words (or None for defaults)
        weights: Custom algorithm weights (or None for defaults)
        duplicate_mode: If True, find duplicates within source (ignores target)

    Returns:
        List of MatchResult objects, sorted by score descending per source
    """
    # Handle Polars DataFrames
    try:
        import polars as pl

        if isinstance(source, pl.DataFrame):
            source = source.to_dicts()
        if isinstance(target, pl.DataFrame):
            target = target.to_dicts()
    except ImportError:
        pass

    # Defaults for target columns
    target_name_col = target_name_col or source_name_col
    target_id_col = target_id_col or source_id_col

    # Handle duplicate mode
    if duplicate_mode:
        target = source
    elif target is None:
        msg = 'target is required when duplicate_mode=False'
        raise ValueError(msg)

    results: list[MatchResult] = []

    for src_record in source:
        src_name = src_record.get(source_name_col)
        src_id = str(src_record.get(source_id_col, ''))

        if not src_name or not isinstance(src_name, str):
            continue

        candidates: list[MatchResult] = []

        for tgt_record in target:
            tgt_name = tgt_record.get(target_name_col)
            tgt_id = str(tgt_record.get(target_id_col, ''))

            if not tgt_name or not isinstance(tgt_name, str):
                continue

            # Skip self-matches in duplicate mode
            if duplicate_mode and src_id == tgt_id:
                continue

            # Calculate similarity
            score, individual_scores, is_exact = calculate_similarity(
                src_name, tgt_name, stop_words, weights
            )

            # Apply threshold
            if score >= threshold:
                candidates.append(
                    MatchResult(
                        source_id=src_id,
                        source_name=src_name,
                        target_id=tgt_id,
                        target_name=tgt_name,
                        score=score,
                        is_exact=is_exact,
                        scores=individual_scores,
                    )
                )

        # Sort by score and take top N
        candidates.sort(key=lambda x: x.score, reverse=True)
        results.extend(candidates[:max_matches])

    return results


def match_single(
    name: str,
    candidates: list[str],
    threshold: float = 0.7,
    max_matches: int = 5,
    stop_words: set[str] | None = None,
    weights: dict[str, float] | None = None,
) -> list[tuple[str, float, bool]]:
    """
    Match a single name against a list of candidates.

    Convenience function for simple use cases without IDs.

    Args:
        name: Name to match
        candidates: List of candidate names
        threshold: Minimum similarity score
        max_matches: Maximum matches to return
        stop_words: Custom stop words
        weights: Custom algorithm weights

    Returns:
        List of (candidate_name, score, is_exact) tuples, sorted by score
    """
    matches = []

    for candidate in candidates:
        score, _, is_exact = calculate_similarity(
            name, candidate, stop_words, weights
        )
        if score >= threshold:
            matches.append((candidate, score, is_exact))

    matches.sort(key=lambda x: x[1], reverse=True)
    return matches[:max_matches]
