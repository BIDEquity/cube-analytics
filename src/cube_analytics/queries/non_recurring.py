"""Non-recurring revenue analysis using Ibis expressions.

Generates type-safe, injection-proof queries for one-off /
non-recurring revenue dashboard.
"""

from typing import TYPE_CHECKING

import ibis
from ibis.expr import types as ir

from cube_analytics.recurring import is_recurring_predicate
from cube_analytics.schema import ColumnMapping

if TYPE_CHECKING:
    import duckdb
    import polars as pl


class NonRecurringQueries:
    """Non-recurring revenue queries using Ibis expressions.

    Filters cube data to non-recurring rows and provides:
    - available_months: distinct periods for filter dropdowns
    - total: aggregated total non-recurring revenue in a date range
    - entries: line-level detail for the entries table

    Example:
        >>> import polars as pl
        >>> from cube_analytics.queries import NonRecurringQueries
        >>>
        >>> df = pl.read_parquet('cube.parquet')
        >>> queries = NonRecurringQueries.from_polars(df)
        >>> print(queries.to_sql(queries.entries('2024-01', '2024-12')))
    """

    def __init__(
        self,
        table: ir.Table,
        mapping: ColumnMapping | None = None,
    ):
        """Initialize with an Ibis table expression.

        Args:
            table: Ibis table expression (from any source)
            mapping: Column mapping (auto-detected if not provided)
        """
        self.table = table
        if mapping is None:
            mapping = ColumnMapping.detect(table.columns)
        self.mapping = mapping

    # ══════════════════════════════════════════════════════════════
    # Factory methods
    # ══════════════════════════════════════════════════════════════

    @classmethod
    def from_polars(
        cls,
        df: 'pl.DataFrame',
        mapping: ColumnMapping | None = None,
    ) -> 'NonRecurringQueries':
        """Create from Polars DataFrame."""
        table = ibis.memtable(df)
        return cls(table, mapping)

    @classmethod
    def from_duckdb_connection(
        cls,
        conn: 'duckdb.DuckDBPyConnection',
        schema: str = 'analysis',
        table_name: str = 'cube_output',
        mapping: ColumnMapping | None = None,
    ) -> 'NonRecurringQueries':
        """Create from existing DuckDB connection."""
        con = ibis.duckdb.from_connection(conn)
        table = con.table(table_name, database=schema)
        return cls(table, mapping)

    # ══════════════════════════════════════════════════════════════
    # Private helpers
    # ══════════════════════════════════════════════════════════════

    def _period_to_month(self, col: ir.Column) -> ir.Value:
        """Extract YYYY-MM from period column."""
        return ibis.coalesce(
            col.try_cast('date').strftime('%Y-%m'),
            col.cast('string').substr(0, 7),
        )

    def _base_table(
        self,
        countries: list[str] | None = None,
    ) -> ir.Table:
        """Apply base filters: non-recurring only + optional country filter.

        If there is no is_recurring column, returns the full table
        (the cube has no recurring/non-recurring distinction).
        """
        t = self.table
        m = self.mapping

        # Filter to NON-recurring revenue
        if m.is_recurring:
            t = t.filter(~is_recurring_predicate(t[m.is_recurring]))

        if countries and m.region:
            t = t.filter(t[m.region].isin(countries))

        return t

    # ══════════════════════════════════════════════════════════════
    # Query builders
    # ══════════════════════════════════════════════════════════════

    def available_months(self) -> ir.Table:
        """Distinct months in the non-recurring data.

        Returns:
            Ibis table with column ``month`` (string, YYYY-MM format)
        """
        t = self._base_table()
        m = self.mapping
        month = self._period_to_month(t[m.period]).name('month')
        return t.select(month).distinct().order_by('month')

    def total(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
    ) -> ir.Table:
        """Total non-recurring revenue in the given period.

        Returns:
            Single-row table with ``total_revenue`` and ``entry_count``.
        """
        t = self._base_table(countries=countries)
        m = self.mapping
        month = self._period_to_month(t[m.period])
        t = t.filter(month >= start_month, month <= end_month)
        return t.aggregate(
            total_revenue=t[m.revenue].sum(),
            # Count ROWS (one row = one entry), not non-null revenue values.
            # ``revenue.count()`` skips null-revenue rows and so disagreed with
            # the array length of ``entries()``; ``count()`` matches it.
            entry_count=t.count(),
        )

    def entries(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
    ) -> ir.Table:
        """Line-level detail of all non-recurring entries in the period.

        Returns:
            Table with month, customer, revenue, and optional columns
            (customer_id, product, region).
        """
        t = self._base_table(countries=countries)
        m = self.mapping
        month_col = self._period_to_month(t[m.period])
        t = t.filter(month_col >= start_month, month_col <= end_month)

        cols: list[ir.Value] = [
            month_col.name('month'),
            t[m.customer].name('customer'),
            t[m.revenue].name('revenue'),
        ]
        if m.customer_id:
            cols.append(t[m.customer_id].name('customer_id'))
        if m.product:
            cols.append(t[m.product].name('product'))
        if m.region:
            cols.append(t[m.region].name('country'))

        return t.select(cols).order_by(
            ibis.desc('month'), ibis.desc('revenue')
        )

    # ══════════════════════════════════════════════════════════════
    # Execution helpers
    # ══════════════════════════════════════════════════════════════

    def to_sql(self, expr: ir.Table) -> str:
        """Get SQL string for debugging/inspection."""
        return ibis.to_sql(expr)

    def execute(self, expr: ir.Table) -> 'pl.DataFrame':
        """Execute expression and return Polars DataFrame."""
        return expr.to_polars()
