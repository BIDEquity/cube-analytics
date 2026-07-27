"""
cube_analytics - ARR Bridge and SaaS metrics analysis with Ibis.

A standalone module for generating and executing analytics queries
on cube data. Backend-agnostic (DuckDB, Polars, PostgreSQL, etc.)
with automatic SQL injection prevention via Ibis expressions.

Example:
    >>> import polars as pl
    >>> from cube_analytics import ARRBridgeQueries
    >>>
    >>> df = pl.read_parquet('cube_output.parquet')
    >>> queries = ARRBridgeQueries.from_polars(df)
    >>> result = queries.execute(queries.summary('2024-01', '2024-12'))

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
from cube_analytics.queries.churn import ChurnQueries
from cube_analytics.queries.concentration import ConcentrationQueries
from cube_analytics.queries.cross_sell import CrossSellQueries
from cube_analytics.queries.non_recurring import NonRecurringQueries
from cube_analytics.queries.upsell import UpsellQueries
from cube_analytics.schema import ColumnMapping
from cube_analytics.revenue_recognition import PeriodAnchor, recognize_revs
from cube_analytics.contract import (
    CubeContract,
    ContractViolation,
    load_contract,
    validate_columns,
)

__all__ = [
    'DEFAULT_STOP_WORDS',
    'DEFAULT_WEIGHTS',
    'ARRBridgeQueries',
    'ChurnQueries',
    'ColumnMapping',
    'ConcentrationQueries',
    'CrossSellQueries',
    'MatchResult',
    'NonRecurringQueries',
    'UpsellQueries',
    'calculate_similarity',
    'clean_name',
    'match_entities',
    'match_single',
    # Revenue recognition — kept from v1.3.0, cube_pipelines imports these
    'PeriodAnchor',
    'recognize_revs',
    # Cube data contract
    'CubeContract',
    'ContractViolation',
    'load_contract',
    'validate_columns',
]
__version__ = '1.4.0'
