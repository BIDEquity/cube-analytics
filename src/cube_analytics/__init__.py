"""cube_analytics — the cube data contract.

One thing, shared by two repositories: what `analysis.cube_output` must look
like. Which semantic roles must be present, which column names may carry each
role, and what types they may hold.

cube-pipelines produces cubes and validates them against this before export.
cube-command-center reads cubes and resolves their columns through the same
rules. Both install this package, so neither can hold a different opinion about
the contract than the other.

Two halves:

    schema     ColumnMapping — resolve a cube's columns to semantic roles
    contract   load_contract, validate_columns — check a cube against the spec

Example:
    >>> from cube_analytics import ColumnMapping, validate_columns
    >>>
    >>> validate_columns({'month': 'DATE', 'revenue': 'DOUBLE'}, row_count=1000)
    >>> ColumnMapping.detect(['month', 'group_level', 'revenue'])

Everything else this package used to carry — Ibis query builders, entity
matching, recurring normalization, revenue recognition — had exactly one
consumer each and now lives in that consumer. See the v2.0.0 section of the
README for where each one went.
"""

from cube_analytics.contract import (
    CONTRACT_VERSION,
    ContractViolation,
    CubeContract,
    load_contract,
    validate_columns,
)
from cube_analytics.schema import ColumnMapping

__all__ = [
    'CONTRACT_VERSION',
    'ColumnMapping',
    'ContractViolation',
    'CubeContract',
    'load_contract',
    'validate_columns',
]
__version__ = '2.0.0'
