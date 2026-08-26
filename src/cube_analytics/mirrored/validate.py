"""Check a reported mirror schema against the pinned mirrored-tables contract.

`validate_mirrored_columns` is the pure check: a column-name-to-engine-type
mapping in, a result out, no database connection. It runs the same way
whether the caller queried Postgres `information_schema`, a SQLAlchemy
inspector, or a DuckDB `DESCRIBE`, because CCC and cube-pipelines each get
there through a different engine and must still agree on the verdict.

The engine-type spelling map lives in this module rather than in
`cube_analytics.mirrored`'s `__init__.py` because it is bulky enough to bury
the loader it would otherwise sit next to.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from cube_analytics.contract import ContractViolation
from cube_analytics.mirrored import load_mirrored_tables

__all__ = [
    'MirroredValidationResult',
    'validate_mirrored_columns',
]

_TRAILING_PRECISION = re.compile(r'\s*\([^)]*\)\s*$')

# Closed spelling sets per type_class, matched against the normalized engine
# type name (lower-cased, trailing precision suffix stripped). `text` also
# accepts USER-DEFINED, the literal `information_schema.columns.data_type`
# value Postgres reports for an enum column — the enum's own type name is
# arbitrary and not enumerable here, so an unrecognized spelling falls back
# to `text` too (see `_classify`).
_INTEGER_SPELLINGS = {
    'smallint', 'integer', 'bigint', 'int', 'int4', 'int8',
    'serial', 'bigserial', 'hugeint',
    'tinyint', 'utinyint', 'usmallint', 'uinteger', 'ubigint',
}
_TEXT_SPELLINGS = {
    'character varying', 'varchar', 'text', 'char', 'character', 'bpchar',
    'user-defined',
}
_NUMERIC_SPELLINGS = {
    'numeric', 'decimal', 'double precision', 'double',
    'real', 'float', 'float4', 'float8',
}
_BOOLEAN_SPELLINGS = {'boolean', 'bool'}
_DATE_SPELLINGS = {'date'}
_JSON_SPELLINGS = {'json', 'jsonb'}
_TIMESTAMP_EXACT = {'datetime'}


def _normalize(engine_type: str) -> str:
    return _TRAILING_PRECISION.sub('', engine_type.strip().lower()).strip()


def _classify(engine_type: str) -> str | None:
    """Map a reported engine type to one of the pin's type_class values.

    Returns None when the spelling is not recognised at all — the caller
    decides what that means, since it is only harmless when the pinned
    column expects `text` (an unenumerable enum type name).
    """
    normalized = _normalize(engine_type)
    if normalized in _INTEGER_SPELLINGS:
        return 'integer'
    if normalized in _NUMERIC_SPELLINGS:
        return 'numeric'
    if normalized in _BOOLEAN_SPELLINGS:
        return 'boolean'
    if normalized in _DATE_SPELLINGS:
        return 'date'
    if normalized in _JSON_SPELLINGS:
        return 'json'
    if normalized in _TIMESTAMP_EXACT or normalized.startswith('timestamp'):
        return 'timestamp'
    if normalized in _TEXT_SPELLINGS:
        return 'text'
    return None


@dataclass
class MirroredValidationResult:
    """Outcome of :func:`validate_mirrored_columns`."""

    hard: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.hard

    def raise_if_failed(self) -> None:
        if self.hard:
            raise ContractViolation(
                'mirrored table breaks the schema pin '
                f'({len(self.hard)} violation(s)):\n  - '
                + '\n  - '.join(self.hard)
            )


def validate_mirrored_columns(
    table_name: str,
    columns: Mapping[str, str],
    *,
    strict: bool = False,
) -> MirroredValidationResult:
    """Check a reported mirror schema against the pin for `table_name`.

    Args:
        table_name: one of the tables `load_mirrored_tables()` returns.
        columns: column name -> engine type name, as reported by an
            information_schema query, a SQLAlchemy inspector, or a DuckDB
            DESCRIBE. Columns the pin does not list are ignored — CCC stays
            free to add columns without breaking this check.
        strict: raise ContractViolation on a hard violation instead of
            returning it for the caller to inspect. Reuses the contract
            module's exception rather than a second type, and stays a
            per-call switch like `validate_columns`' so a caller can move
            from warn to raise without a release of this package.

    Returns:
        MirroredValidationResult. In warn mode (the default) call
        `.raise_if_failed()` yourself, or inspect `.hard`. In strict mode
        this function has already raised by the time it would return.
    """
    tables = load_mirrored_tables()
    if table_name not in tables:
        raise ValueError(
            f"'{table_name}' is not a mirrored table — "
            f'known tables are {sorted(tables)}'
        )
    table = tables[table_name]
    result = MirroredValidationResult()

    for name, pinned in table.columns.items():
        if name not in columns:
            result.hard.append(
                f"pinned column '{name}' is missing from {table_name}"
            )
            continue
        reported = columns[name]
        classified = _classify(reported)
        if classified is None:
            # Unrecognised spelling. Harmless only when the pin expects
            # text, since that is where an enum's own type name lands.
            if pinned.type_class != 'text':
                result.hard.append(
                    f"pinned column '{name}' on {table_name} has "
                    f"unrecognised type '{reported}', "
                    f"expected {pinned.type_class}"
                )
        elif classified != pinned.type_class:
            result.hard.append(
                f"pinned column '{name}' on {table_name} has type "
                f"'{reported}' ({classified}), expected {pinned.type_class}"
            )

    for name in table.secret_columns:
        if name in columns:
            result.hard.append(
                f"secret column '{name}' is present in the mirror of "
                f'{table_name} — it must be dropped before extraction'
            )

    if strict:
        result.raise_if_failed()
    return result
