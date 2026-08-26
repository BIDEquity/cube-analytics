"""The cube data contract, loaded from one file both repositories share.

The contract says what `analysis.cube_output` must look like: which semantic
roles must be present, which column names are allowed to carry each role, and
what types they may have.

Before this module the contract existed as prose in one repository and as
hand-copied constants in two others. They drifted. Everything here is derived
from `cube-contract.yaml`, which ships inside the wheel, so a consumer cannot
hold a different opinion about the contract than the producer does.

Two entry points:

    load_contract()    -> CubeContract, the parsed contract
    validate_columns() -> ValidationResult, the producer-side checks

`validate_columns` is deliberately pure. It takes a column-name-to-type mapping
and a row count, not a database connection, so it runs the same way against
DuckDB, Polars, an Ibis table or a test fixture. Callers adapt their own source
into those two arguments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

import yaml

__all__ = [
    'CONTRACT_VERSION',
    'ContractViolation',
    'CubeContract',
    'ValidationResult',
    'is_date_like',
    'is_numeric',
    'load_contract',
    'validate_columns',
]

_BUNDLED = Path(__file__).parent / 'cube-contract.yaml'

# Read eagerly at import time, not lazily behind a function call. The bundled
# YAML ships inside the wheel and never changes without a package release, so
# there's no staleness risk to guard against, and a plain module attribute
# lets a caller write `cube_analytics.CONTRACT_VERSION` without a call. This
# mirrors load_contract(), which already re-reads the same file per call.
CONTRACT_VERSION: str = str(
    (yaml.safe_load(_BUNDLED.read_text(encoding='utf-8')) or {}).get('contract_version', '0')
)

# Type-name fragments, matched case-insensitively against whatever the caller's
# engine reports. Kept as fragments because DuckDB says DECIMAL(18,2) where
# Polars says Decimal and Postgres says numeric.
_DATE_FRAGMENTS = ('date', 'timestamp')
_DATE_LIKE_TEXT = ('varchar', 'text', 'string', 'utf8')
_NUMERIC_FRAGMENTS = (
    'double', 'decimal', 'numeric', 'float', 'real',
    'int', 'bigint', 'smallint', 'hugeint',
)


class ContractViolation(Exception):
    """A cube breaks the contract in a way that must stop the build."""


@dataclass(frozen=True)
class CubeContract:
    """The parsed contract. Build it with :func:`load_contract`."""

    version: str
    table_schema: str
    table_name: str
    grain: tuple[str, ...]
    required_roles: tuple[str, ...]
    recommended_roles: tuple[str, ...]
    optional_roles: tuple[str, ...]
    allowed_names: Mapping[str, tuple[str, ...]]
    allowed_types: Mapping[str, tuple[str, ...]]

    @property
    def qualified_table(self) -> str:
        return f'{self.table_schema}.{self.table_name}'

    @property
    def every_known_name(self) -> frozenset[str]:
        """Every column name the contract recognises, in any role."""
        return frozenset(n for names in self.allowed_names.values() for n in names)

    def resolve(self, role: str, columns: Iterable[str]) -> str | None:
        """First allowed name for `role` present in `columns`, else None.

        Priority order comes from the contract, so a cube carrying both
        `customer_id` and `group_level_id` resolves the same way everywhere.
        """
        present = {c.lower() for c in columns}
        for name in self.allowed_names.get(role, ()):
            if name.lower() in present:
                return name
        return None


@dataclass
class ValidationResult:
    """Outcome of :func:`validate_columns`."""

    hard: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)
    resolved: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.hard

    def raise_if_failed(self) -> None:
        if self.hard:
            raise ContractViolation(
                f'cube_output breaks the data contract ({len(self.hard)} violation(s)):\n  - '
                + '\n  - '.join(self.hard)
            )


def load_contract(path: str | Path | None = None) -> CubeContract:
    """Load the contract. Defaults to the copy bundled in this package."""
    raw = yaml.safe_load(Path(path or _BUNDLED).read_text(encoding='utf-8'))

    def names(section: str) -> dict[str, tuple[str, ...]]:
        return {
            role: tuple(spec.get('allowed_names', ()))
            for role, spec in (raw.get(section) or {}).items()
        }

    def types(section: str) -> dict[str, tuple[str, ...]]:
        return {
            role: tuple(spec.get('allowed_types', ()))
            for role, spec in (raw.get(section) or {}).items()
        }

    allowed_names: dict[str, tuple[str, ...]] = {}
    allowed_types: dict[str, tuple[str, ...]] = {}
    for section in ('required_columns', 'recommended_columns', 'optional_columns'):
        allowed_names.update(names(section))
        allowed_types.update(types(section))

    core = raw.get('core_table') or {}
    return CubeContract(
        version=str(raw.get('contract_version', '0')),
        table_schema=core.get('schema', 'analysis'),
        table_name=core.get('name', 'cube_output'),
        grain=tuple(core.get('grain', ())),
        required_roles=tuple(raw.get('required_columns') or ()),
        recommended_roles=tuple(raw.get('recommended_columns') or ()),
        optional_roles=tuple(raw.get('optional_columns') or ()),
        allowed_names=allowed_names,
        allowed_types=allowed_types,
    )


def is_date_like(dtype: str) -> bool:
    """True for DATE/TIMESTAMP, and for text columns holding ISO-8601 dates.

    Text is accepted because the contract allows an ISO-8601 VARCHAR period.
    That is permissive by design — several tenants still export period as text.
    """
    d = dtype.lower()
    return any(f in d for f in _DATE_FRAGMENTS) or any(f in d for f in _DATE_LIKE_TEXT)


def is_numeric(dtype: str) -> bool:
    d = dtype.lower()
    return any(f in d for f in _NUMERIC_FRAGMENTS)


def validate_columns(
    columns: Mapping[str, str],
    row_count: int,
    *,
    contract: CubeContract | None = None,
    table_present: bool = True,
) -> ValidationResult:
    """Run the producer-side contract checks.

    Args:
        columns: column name -> engine type name, e.g. {'month': 'DATE'}.
        row_count: rows in the table.
        contract: defaults to the bundled contract.
        table_present: False when the core table is missing entirely. Every
            other check is meaningless then, so only that violation is reported.

    Returns:
        ValidationResult. Call `.raise_if_failed()` to turn hard violations into
        a ContractViolation, or inspect `.hard` / `.soft` to log and continue.
        Warn-first rollouts read the lists, strict callers raise.
    """
    c = contract or load_contract()
    result = ValidationResult()

    if not table_present:
        result.hard.append(f'core table {c.qualified_table} is missing')
        return result

    for role in c.required_roles:
        hit = c.resolve(role, columns)
        if hit is None:
            result.hard.append(
                f"no column carries the required role '{role}' — "
                f'allowed names are {list(c.allowed_names.get(role, ()))}, '
                f'the table has {sorted(columns)}'
            )
            continue
        result.resolved[role] = hit

    period = result.resolved.get('period')
    if period and not is_date_like(columns[period]):
        result.hard.append(
            f"period column '{period}' has type {columns[period]}, "
            'expected DATE, TIMESTAMP or an ISO-8601 text column'
        )

    revenue = result.resolved.get('revenue')
    if revenue and not is_numeric(columns[revenue]):
        result.hard.append(
            f"revenue column '{revenue}' has type {columns[revenue]}, expected a numeric type"
        )

    if row_count <= 0:
        result.hard.append(f'{c.qualified_table} has no rows')

    for role in c.recommended_roles:
        hit = c.resolve(role, columns)
        if hit is None:
            result.soft.append(
                f"no column carries the recommended role '{role}' — "
                'downstream features that need it will degrade'
            )
        else:
            result.resolved[role] = hit

    for role in c.optional_roles:
        hit = c.resolve(role, columns)
        if hit is not None:
            result.resolved[role] = hit

    known = {n.lower() for n in c.every_known_name}
    unknown = sorted(col for col in columns if col.lower() not in known)
    if unknown:
        result.soft.append(
            f'columns the contract does not recognise: {unknown} — '
            'either tenant-specific extensions or naming drift'
        )

    return result
