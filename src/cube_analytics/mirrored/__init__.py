"""The mirrored-CCC-table schema pin, loaded from one file both repositories share.

CCC (cube-command-center) mirrors five internal Postgres tables full-refresh
into MotherDuck, where cube-pipelines' tenant dbt models read them as the
`command_center` source. Until this module existed that boundary had no
column contract at all — a CCC migration renaming a consumed column was a
cross-repo break nothing flagged, and the pipelines-side change sensor
fingerprinted only a hand-picked subset of columns (a real prior miss: the
2026-07-08 backfill edited columns outside that subset and the sensor saw
nothing).

Everything here is derived from `mirrored-tables.yaml`, bundled inside the
`contract` package alongside `cube-contract.yaml` and shipped in the wheel, so
a consumer cannot hold a different opinion about the mirror than the producer
does. This module loads the pin; `validate_mirrored_columns` (re-exported
here from `cube_analytics.mirrored.validate`) checks a reported schema
against it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

__all__ = [
    'MIRRORED_TABLES_VERSION',
    'FingerprintSpec',
    'MirroredColumn',
    'MirroredTable',
    'MirroredValidationResult',
    'load_mirrored_tables',
    'validate_mirrored_columns',
]

_BUNDLED = Path(__file__).parent.parent / 'contract' / 'mirrored-tables.yaml'

# Read eagerly at import time, like CONTRACT_VERSION: the bundled YAML ships
# inside the wheel and never changes without a package release.
MIRRORED_TABLES_VERSION: str = str(
    (yaml.safe_load(_BUNDLED.read_text(encoding='utf-8')) or {}).get(
        'mirrored_tables_version', '0'
    )
)


@dataclass(frozen=True)
class MirroredColumn:
    """One column pinned from a mirrored CCC table."""

    name: str
    type_class: str
    nullable: bool


@dataclass(frozen=True)
class FingerprintSpec:
    """How the pipelines change-sensor should detect that a mirrored table changed.

    `columns` and `order_by` apply to `content_hash`; `sequence_column`
    applies to `append_only`; `none` uses neither.
    """

    strategy: str
    columns: tuple[str, ...] = ()
    order_by: str | None = None
    sequence_column: str | None = None


@dataclass(frozen=True)
class MirroredTable:
    """One CCC table mirrored into MotherDuck. Build it with :func:`load_mirrored_tables`."""

    name: str
    columns: Mapping[str, MirroredColumn]
    secret_columns: tuple[str, ...]
    fingerprint: FingerprintSpec


def load_mirrored_tables(path: str | Path | None = None) -> Mapping[str, MirroredTable]:
    """Load the pin. Defaults to the copy bundled in this package."""
    raw = yaml.safe_load(Path(path or _BUNDLED).read_text(encoding='utf-8'))

    tables: dict[str, MirroredTable] = {}
    for table_name, spec in (raw.get('tables') or {}).items():
        columns = {
            col['name']: MirroredColumn(
                name=col['name'],
                type_class=col['type_class'],
                nullable=bool(col.get('nullable', False)),
            )
            for col in spec.get('columns', ())
        }
        fp = spec.get('fingerprint') or {}
        fingerprint = FingerprintSpec(
            strategy=fp.get('strategy', 'none'),
            columns=tuple(fp.get('columns', ())),
            order_by=fp.get('order_by'),
            sequence_column=fp.get('sequence_column'),
        )
        tables[table_name] = MirroredTable(
            name=table_name,
            columns=columns,
            secret_columns=tuple(spec.get('secret_columns', ())),
            fingerprint=fingerprint,
        )
    return tables


# Imported after load_mirrored_tables is defined: validate.py imports it back
# from this package, so the function must already be bound before validate.py
# runs its own import.
from cube_analytics.mirrored.validate import (
    MirroredValidationResult,
    validate_mirrored_columns,
)
