"""Cross-sell analysis using Ibis expressions.

Generates type-safe, injection-proof queries for the cross-sell dashboard.
Returns monthly customer-product data so the frontend JavaScript layer can
compute initial products, cross-sell classifications, combination rankings,
and downsell risk metrics.

Cross-sell is defined as revenue growth through additional products between
two points in time t1 and t2. The initial product is the first acquired
product; if multiple products start simultaneously, the highest-revenue
one is considered the initial product. All classification logic runs in
the frontend compute layer.
"""

from typing import TYPE_CHECKING, Any

import ibis
from ibis import _
from ibis.expr import types as ir

from cube_analytics.recurring import is_recurring_predicate
from cube_analytics.schema import ColumnMapping

if TYPE_CHECKING:
    import duckdb
    import polars as pl
    import pyarrow as pa


class CrossSellQueries:
    """Cross-sell / product expansion analysis using Ibis expressions.

    Returns monthly customer-product base data. Heavy computation
    (initial product detection, cross-sell classification, combination
    ranking, downsell risk) runs in frontend JavaScript using the data
    returned by these queries.

    Example:
        >>> import polars as pl
        >>> from cube_analytics import CrossSellQueries
        >>>
        >>> df = pl.read_parquet('cube.parquet')
        >>> queries = CrossSellQueries.from_polars(df)
        >>> base = queries.cross_sell_base_data()
        >>> print(queries.to_sql(base))
        >>> result = queries.execute(base)

    Attributes:
        table: Ibis table expression
        mapping: Column mapping for the schema
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
        self.mapping = mapping or ColumnMapping.detect(table.columns)

    # ══════════════════════════════════════════════════════════════
    # Factory methods - create from various sources
    # ══════════════════════════════════════════════════════════════

    @classmethod
    def from_polars(
        cls,
        df: 'pl.DataFrame',
        mapping: ColumnMapping | None = None,
    ) -> 'CrossSellQueries':
        """Create from Polars DataFrame."""
        table = ibis.memtable(df)
        return cls(table, mapping)

    @classmethod
    def from_duckdb_path(
        cls,
        path: str,
        schema: str = 'analysis',
        table_name: str = 'cube_output',
        mapping: ColumnMapping | None = None,
    ) -> 'CrossSellQueries':
        """Create from DuckDB file path."""
        con = ibis.duckdb.connect(path, read_only=True)
        table = con.table(table_name, database=schema)
        return cls(table, mapping)

    @classmethod
    def from_duckdb_connection(
        cls,
        conn: 'duckdb.DuckDBPyConnection',
        schema: str = 'analysis',
        table_name: str = 'cube_output',
        mapping: ColumnMapping | None = None,
    ) -> 'CrossSellQueries':
        """Create from existing DuckDB connection."""
        con = ibis.duckdb.from_connection(conn)
        table = con.table(table_name, database=schema)
        return cls(table, mapping)

    @classmethod
    def from_ibis_connection(
        cls,
        con: Any,  # ibis.BaseBackend
        table_name: str,
        schema: str | None = None,
        mapping: ColumnMapping | None = None,
    ) -> 'CrossSellQueries':
        """Create from existing Ibis connection (any backend)."""
        table = con.table(table_name, database=schema)
        return cls(table, mapping)

    # ══════════════════════════════════════════════════════════════
    # Private helpers
    # ══════════════════════════════════════════════════════════════

    def _extract_year(self, col: ir.Column) -> ir.Value:
        """Extract year as integer from period column.

        Handles both date/timestamp columns and string columns.
        """
        return ibis.coalesce(
            col.try_cast('date').year(),
            col.cast('string').substr(0, 4).try_cast('int32'),
        )

    def _period_to_month(self, col: ir.Column) -> ir.Value:
        """Extract YYYY-MM string from period column."""
        return ibis.coalesce(
            col.try_cast('date').strftime('%Y-%m'),
            col.cast('string').substr(0, 7),
        )

    def _product_col(self, t: ir.Table) -> ir.Value:
        """Get the product column, or literal '__all__' if no product column."""
        m = self.mapping
        if m.product:
            return t[m.product]
        return ibis.literal('__all__').cast('string')

    def _base_table(self) -> ir.Table:
        """Apply base filter: recurring revenue only (if column present)."""
        t = self.table
        m = self.mapping
        if m.is_recurring:
            t = t.filter(is_recurring_predicate(t[m.is_recurring]))
        return t

    # ══════════════════════════════════════════════════════════════
    # Query builders - return Ibis expressions (lazy)
    # ══════════════════════════════════════════════════════════════

    def cross_sell_base_data(self) -> ir.Table:
        """Monthly customer-product revenue base data for cross-sell analysis.

        Returns one row per customer × product × month with summed revenue.
        Optional country and industry columns are included when present in
        the schema. The frontend JavaScript layer uses this data to:
        - detect each customer's initial product
        - classify subsequent products as cross-sell
        - compute cross-sell MRR timelines
        - rank product combination pairs
        - identify downsell risk

        Output columns (always present):
            customer_id     — canonical customer key
            customer_name   — display name (equals customer_id when no
                              separate name column exists)
            period          — YYYY-MM string (monthly granularity)
            product         — product name, or '__all__' if no product column
            revenue         — sum of revenue for that customer/product/month

        Output columns (present when detected in schema):
            country         — region / country label
            industry        — industry / segment label

        Returns:
            Ibis table expression ordered by customer_id, period, product
        """
        m = self.mapping
        t = self._base_table()

        group_col = m.customer_id or m.customer
        if not group_col:
            raise ValueError(
                'CrossSellQueries requires at least a customer column in the mapping'
            )

        # Derived columns used in grouping
        t = t.mutate(
            _month=self._period_to_month(t[m.period]),
            _product=self._product_col(t),
        )

        # Aggregation columns (always)
        agg_cols: dict[str, ir.Value] = {
            'revenue': t[m.revenue].sum().cast('float64'),
        }

        # Include display name only when a separate customer_id exists
        if m.customer_id:
            agg_cols['customer_name'] = t[m.customer].max()

        # Optional schema columns
        if m.region:
            agg_cols['country'] = t[m.region].max()
        if m.industry:
            agg_cols['industry'] = t[m.industry].max()

        result = t.group_by(
            customer_id=t[group_col],
            period=t._month,
            product=t._product,
        ).aggregate(**agg_cols)

        # When no separate customer_id, customer_name = customer_id
        if not m.customer_id:
            result = result.mutate(customer_name=result.customer_id)

        return result.order_by(['customer_id', 'period', 'product'])

    def available_years(self) -> ir.Table:
        """Expression for available years in the data.

        Returns:
            Ibis table with single 'year' column (integer), ordered ascending
        """
        m = self.mapping
        return (
            self.table.select(year=self._extract_year(self.table[m.period]))
            .distinct()
            .order_by('year')
        )

    def available_products(self) -> ir.Table:
        """Expression for available products.

        Returns:
            Ibis table with single 'product' column (string), ordered
            alphabetically; empty result with null column when no product
            column is detected in the schema.
        """
        m = self.mapping
        if not m.product:
            return self.table.limit(0).select(
                product=ibis.literal(None).cast('string')
            )

        return (
            self.table.select(product=self.table[m.product])
            .filter(_.product.notnull() & (_.product != ''))
            .distinct()
            .order_by('product')
        )

    def available_countries(self) -> ir.Table:
        """Expression for available countries/regions.

        Returns:
            Ibis table with single 'country' column (string), ordered
            alphabetically; empty result with null column when no region
            column is detected in the schema.
        """
        m = self.mapping
        if not m.region:
            return self.table.limit(0).select(
                country=ibis.literal(None).cast('string')
            )

        return (
            self.table.select(country=self.table[m.region])
            .filter(_.country.notnull() & (_.country != ''))
            .distinct()
            .order_by('country')
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

    def execute_to_pyarrow(self, expr: ir.Table) -> 'pa.Table':
        """Execute expression and return PyArrow Table."""
        return expr.to_pyarrow()
