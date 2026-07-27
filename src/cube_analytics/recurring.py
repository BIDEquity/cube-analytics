"""Canonical recurring-revenue normalization (cube-data-contract §4.4).

The ``is_recurring`` column serves double duty across tenants: some use a
boolean (``true``/``false``), others a string enum (``Rec``/``Nonrec``,
``Recurring``/``Non-Recurring``). The canonical consumer-side normalization
(``docs/steering/cube-data-contract.md`` §4.4) treats a row as recurring when::

    is_recurring = TRUE
    OR LOWER(CAST(is_recurring AS VARCHAR)) IN ('true', 'rec', 'recurring', 'yes', '1')

This module exposes that normalization as a single Ibis predicate so every
query builder filters recurring revenue identically. The SQL reference
implementations live in ``command_center.domain.query_templates`` (budget /
anomaly detection); this is the Ibis-expression equivalent.

The earlier per-query form ``col.isin([True, 1])`` did NOT match this
normalization: on a string-enum column it crashed with a DuckDB INT cast error
(latent crash for any tenant whose only recurring indicator is a string enum
mapped via the ``revenue_type`` fallback). Casting to VARCHAR first makes the
predicate safe for both boolean and string-enum columns.
"""

from ibis.expr import types as ir

# Lower-cased string values that mark a row as recurring revenue (§4.4).
RECURRING_VALUES = ('true', 'rec', 'recurring', 'yes', '1')


def is_recurring_predicate(col: ir.Value) -> ir.BooleanValue:
    """Ibis predicate: ``True`` for recurring-revenue rows (§4.4 normalization).

    Casting to VARCHAR before comparison makes this safe for BOTH a boolean
    ``is_recurring`` column (``CAST(TRUE AS VARCHAR)`` → ``'true'``) and a
    string-enum one such as ``revenue_type`` (``'Rec'``/``'Nonrec'``). Negate
    the result (``~is_recurring_predicate(col)``) to select non-recurring rows.

    Args:
        col: Ibis column expression for the ``is_recurring`` column.

    Returns:
        Boolean Ibis expression, ``True`` for recurring rows.
    """
    return col.cast('string').lower().isin(RECURRING_VALUES)
