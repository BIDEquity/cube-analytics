"""Upsell / Expansion analysis using Ibis expressions.

Generates type-safe, injection-proof queries for upsell dashboard.
Aggregates monthly data to yearly customer-product level for
frontend JS computation of cohorts, retention, and tenure analysis.
"""

from typing import TYPE_CHECKING, Any

import ibis
from ibis import _
from ibis.expr import types as ir

from cube_analytics.schema import ColumnMapping

if TYPE_CHECKING:
    import duckdb
    import polars as pl


class UpsellQueries:
    """Upsell / Expansion analysis using Ibis expressions.

    Generates base aggregation queries (yearly, per customer/product).
    Heavy computation (cohort bridge, retention, tenure) runs in
    frontend JavaScript using the base data returned by these queries.

    Example:
        >>> import polars as pl
        >>> from cube_analytics import UpsellQueries
        >>>
        >>> df = pl.read_parquet('cube.parquet')
        >>> queries = UpsellQueries.from_polars(df)
        >>> print(queries.to_sql(queries.available_years()))

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
    ) -> 'UpsellQueries':
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
    ) -> 'UpsellQueries':
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
    ) -> 'UpsellQueries':
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
    ) -> 'UpsellQueries':
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
            col.cast('string').substr(0, 4).cast('int32'),
        )

    def _base_table(
        self,
        countries: list[str] | None = None,
        products: list[str] | None = None,
    ) -> ir.Table:
        """Apply base filters (recurring, country, product).

        Args:
            countries: Optional list of countries to filter
            products: Optional list of products to filter

        Returns:
            Filtered Ibis table expression
        """
        t = self.table
        m = self.mapping

        # Filter to recurring revenue if column exists
        if m.is_recurring:
            t = t.filter(t[m.is_recurring].isin([True, 1]))

        # Country filter
        if countries and m.region:
            t = t.filter(t[m.region].isin(countries))

        # Product filter
        if products and m.product:
            t = t.filter(t[m.product].isin(products))

        return t

    def _product_col(self, t: ir.Table) -> ir.Value:
        """Get the product column expression, or literal '__all__' if no product column."""
        m = self.mapping
        if m.product:
            return t[m.product]
        return ibis.literal('__all__').cast('string')

    def _period_to_month(self, col: ir.Column) -> ir.Value:
        """Extract YYYY-MM from period column (for months_active counting)."""
        return ibis.coalesce(
            col.try_cast('date').strftime('%Y-%m'),
            col.cast('string').substr(0, 7),
        )

    # ══════════════════════════════════════════════════════════════
    # Query builders - return Ibis expressions (lazy)
    # ══════════════════════════════════════════════════════════════

    def available_years(self) -> ir.Table:
        """Expression for available years in the data.

        Returns:
            Ibis table with single 'year' column (integer)
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
            Ibis table with single 'product' column, or empty if no product column
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
            Ibis table with single 'country' column, or empty if no region column
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

    def customer_year_product(
        self,
        countries: list[str] | None = None,
        products: list[str] | None = None,
    ) -> ir.Table:
        """Base aggregation: monthly data → yearly at customer × product level.

        Two-step aggregation:
        1. Sum revenue per customer × product × year × month (handles multiple
           line items per month)
        2. Aggregate months → year: sum for annual_revenue, count for
           months_active, last month's sum for exit_mrr

        Output columns:
            customer_key, customer_name, country, product, year,
            annual_revenue, months_active, exit_mrr

        Args:
            countries: Optional list of countries to filter
            products: Optional list of products to filter

        Returns:
            Ibis table expression with yearly customer-product aggregation
        """
        m = self.mapping
        t = self._base_table(countries, products)

        group_col = m.customer_id or m.customer

        # Add derived columns before grouping so they're available as columns
        t = t.mutate(
            _year=self._extract_year(t[m.period]),
            _month=self._period_to_month(t[m.period]),
            _product=self._product_col(t),
        )

        # Step 1: aggregate to monthly level (sum line items within a month)
        monthly_agg_cols: dict[str, ir.Value] = {
            'monthly_revenue': t[m.revenue].sum().cast('float64'),
        }
        if m.customer_id:
            monthly_agg_cols['customer_name'] = t[m.customer].max()
        if m.region:
            monthly_agg_cols['country'] = t[m.region].max()

        monthly = t.group_by(
            customer_key=t[group_col],
            product=t._product,
            year=t._year,
            month=t._month,
        ).aggregate(**monthly_agg_cols)

        # Step 2: aggregate months → year
        yearly_agg_cols: dict[str, ir.Value] = {
            'annual_revenue': monthly.monthly_revenue.sum().cast('float64'),
            'months_active': monthly.month.nunique().cast('int32'),
            # exit_mrr = revenue in the last month of the year
            'exit_mrr': monthly.monthly_revenue.argmax(monthly.month).cast(
                'float64'
            ),
        }
        if m.customer_id:
            yearly_agg_cols['customer_name'] = monthly.customer_name.max()
        if m.region:
            yearly_agg_cols['country'] = monthly.country.max()
        else:
            yearly_agg_cols['country'] = ibis.literal('').cast('string')

        result = monthly.group_by(
            customer_key=monthly.customer_key,
            product=monthly.product,
            year=monthly.year,
        ).aggregate(**yearly_agg_cols)

        # If no separate customer_id, use customer_key as customer_name
        if not m.customer_id:
            result = result.mutate(customer_name=result.customer_key)

        return result.order_by(['customer_key', 'year', 'product'])

    def first_seen(
        self,
        countries: list[str] | None = None,
        products: list[str] | None = None,
    ) -> ir.Table:
        """Min year per customer (first_seen_year).

        Output columns:
            customer_key, first_seen_year

        Args:
            countries: Optional list of countries to filter
            products: Optional list of products to filter

        Returns:
            Ibis table with customer_key and first_seen_year
        """
        m = self.mapping
        t = self._base_table(countries, products)

        group_col = m.customer_id or m.customer
        t = t.mutate(_year=self._extract_year(t[m.period]))

        return (
            t.group_by(customer_key=t[group_col])
            .aggregate(first_seen_year=t._year.min().cast('int32'))
            .order_by('customer_key')
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

    def execute_to_pyarrow(self, expr: ir.Table):
        """Execute expression and return PyArrow Table."""
        return expr.to_pyarrow()
