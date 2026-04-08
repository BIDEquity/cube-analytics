"""
cube_analytics - ARR Bridge and SaaS metrics analysis with Ibis.

A standalone module for generating and executing analytics queries
on cube data. Backend-agnostic (DuckDB, Polars, PostgreSQL, etc.)
with automatic SQL injection prevention via Ibis expressions.

Example:
    >>> import polars as pl
    >>> from cube_analytics import ARRBridgeQueries
    >>>
    >>> df = pl.read_parquet("cube_output.parquet")
    >>> queries = ARRBridgeQueries.from_polars(df)
    >>> result = queries.execute(queries.summary("2024-01", "2024-12"))

This module has NO dependencies on command_center and can be
extracted to a separate package.
"""

from cube_analytics.entity_matching import (
    DEFAULT_STOP_WORDS,
    DEFAULT_WEIGHTS,
    MatchResult,
    calculate_similarity,
    clean_name,
    match_entities,
    match_single,
)
from cube_analytics.queries.arr_bridge import ARRBridgeQueries
from cube_analytics.schema import ColumnMapping
from cube_analytics.revenue_recognition import PeriodAnchor, recognize_revs

__all__ = [
    "ARRBridgeQueries",
    "ColumnMapping",
    # Entity matching
    "match_entities",
    "match_single",
    "calculate_similarity",
    "clean_name",
    "MatchResult",
    "DEFAULT_STOP_WORDS",
    "DEFAULT_WEIGHTS",
    "recognize_revs",
    "PeriodAnchor",
]
__version__ = "1.3.0"
