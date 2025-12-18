"""ARR Bridge analysis using Ibis expressions.

Generates type-safe, injection-proof queries that work with
any Ibis backend (DuckDB, Polars, PostgreSQL, etc.)
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import ibis
from ibis import _
from ibis.expr import types as ir

from cube_analytics.schema import ColumnMapping

if TYPE_CHECKING:
    import duckdb
    import polars as pl


@dataclass
class BridgeSummary:
    """Summary result from ARR Bridge calculation."""

    beginning_mrr: float
    ending_mrr: float
    new_business_mrr: float
    upsell_mrr: float
    downsell_mrr: float
    churn_mrr: float
    new_business_count: int
    upsell_count: int
    downsell_count: int
    churn_count: int


class ARRBridgeQueries:
    """ARR Bridge analysis using Ibis expressions.

    Generates type-safe, injection-proof queries that work with
    any Ibis backend (DuckDB, Polars, PostgreSQL, etc.)

    Example:
        >>> import polars as pl
        >>> from cube_analytics import ARRBridgeQueries
        >>>
        >>> # From Polars DataFrame
        >>> df = pl.read_parquet("cube.parquet")
        >>> queries = ARRBridgeQueries.from_polars(df)
        >>>
        >>> # Build and inspect query
        >>> expr = queries.summary("2024-01", "2024-12")
        >>> print(queries.to_sql(expr))
        >>>
        >>> # Execute
        >>> result = queries.execute(expr)

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
    ) -> 'ARRBridgeQueries':
        """Create from Polars DataFrame.

        Args:
            df: Polars DataFrame with cube data
            mapping: Optional column mapping (auto-detected if None)

        Returns:
            ARRBridgeQueries instance
        """
        table = ibis.memtable(df)
        return cls(table, mapping)

    @classmethod
    def from_duckdb_path(
        cls,
        path: str,
        schema: str = 'analysis',
        table_name: str = 'cube_output',
        mapping: ColumnMapping | None = None,
    ) -> 'ARRBridgeQueries':
        """Create from DuckDB file path.

        Args:
            path: Path to DuckDB file
            schema: Schema name (default: 'analysis')
            table_name: Table name (default: 'cube_output')
            mapping: Optional column mapping (auto-detected if None)

        Returns:
            ARRBridgeQueries instance
        """
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
    ) -> 'ARRBridgeQueries':
        """Create from existing DuckDB connection.

        Args:
            conn: DuckDB connection object
            schema: Schema name (default: 'analysis')
            table_name: Table name (default: 'cube_output')
            mapping: Optional column mapping (auto-detected if None)

        Returns:
            ARRBridgeQueries instance
        """
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
    ) -> 'ARRBridgeQueries':
        """Create from existing Ibis connection (any backend).

        Args:
            con: Ibis backend connection
            table_name: Table name
            schema: Optional schema name
            mapping: Optional column mapping (auto-detected if None)

        Returns:
            ARRBridgeQueries instance
        """
        table = con.table(table_name, database=schema)
        return cls(table, mapping)

    # ══════════════════════════════════════════════════════════════
    # Private helpers
    # ══════════════════════════════════════════════════════════════

    def _period_to_month(self, col: ir.Column) -> ir.StringValue:
        """Extract YYYY-MM from period column.

        Handles both date/timestamp columns and string columns.
        """
        return ibis.coalesce(
            col.try_cast('date').strftime('%Y-%m'),
            col.cast('string').substr(0, 7),
        )

    def _base_table(
        self,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
    ) -> ir.Table:
        """Apply base filters (recurring, region, industry).

        Args:
            countries: Optional list of countries to filter
            industries: Optional list of industries to filter

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

        # Industry filter
        if industries and m.industry:
            t = t.filter(t[m.industry].isin(industries))

        return t

    def _build_customer_bridge(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
    ) -> ir.Table:
        """Build the customer bridge expression (shared by summary and customers).

        Returns table with columns:
        - customer_key
        - customer_name (if customer_id exists)
        - start_mrr
        - end_mrr
        - mrr_change
        - movement_type
        """
        m = self.mapping
        t = self._base_table(countries, industries)

        # Column to group customers by (prefer ID over name)
        group_col = m.customer_id or m.customer
        period_month = self._period_to_month(t[m.period])

        # Build aggregation columns for start period
        start_agg = {'s_mrr': t[m.revenue].sum()}
        if m.customer_id:
            start_agg['s_customer_name'] = t[m.customer].max()
        if m.region:
            start_agg['s_region'] = t[m.region].max()
        if m.industry:
            start_agg['s_industry'] = t[m.industry].max()

        # Build aggregation columns for end period
        end_agg = {'e_mrr': t[m.revenue].sum()}
        if m.customer_id:
            end_agg['e_customer_name'] = t[m.customer].max()
        if m.region:
            end_agg['e_region'] = t[m.region].max()
        if m.industry:
            end_agg['e_industry'] = t[m.industry].max()

        # Start period MRR by customer
        start_mrr = (
            t.filter(period_month == start_month)
            .group_by(s_customer_key=t[group_col])
            .aggregate(**start_agg)
        )

        # End period MRR by customer
        end_mrr = (
            t.filter(period_month == end_month)
            .group_by(e_customer_key=t[group_col])
            .aggregate(**end_agg)
        )

        # Build select columns for the join
        select_cols = {
            'customer_key': ibis.coalesce(
                start_mrr.s_customer_key,
                end_mrr.e_customer_key,
            ),
            'start_mrr': ibis.coalesce(start_mrr.s_mrr, 0),
            'end_mrr': ibis.coalesce(end_mrr.e_mrr, 0),
        }

        # Add customer name if available
        if m.customer_id:
            select_cols['customer_name'] = ibis.coalesce(
                end_mrr.e_customer_name,
                start_mrr.s_customer_name,
            )

        # Add optional columns
        if m.region:
            select_cols['region'] = ibis.coalesce(
                end_mrr.e_region,
                start_mrr.s_region,
            )
        if m.industry:
            select_cols['industry'] = ibis.coalesce(
                end_mrr.e_industry,
                start_mrr.s_industry,
            )

        # Full outer join and classify movements
        bridge = (
            start_mrr.outer_join(
                end_mrr,
                start_mrr.s_customer_key == end_mrr.e_customer_key,
            )
            .select(**select_cols)
            .mutate(mrr_change=_.end_mrr - _.start_mrr)
            .mutate(
                movement_type=ibis.cases(
                    ((_.start_mrr == 0) & (_.end_mrr > 0), 'New Business'),
                    ((_.start_mrr > 0) & (_.end_mrr == 0), 'Churn'),
                    ((_.start_mrr > 0) & (_.end_mrr > _.start_mrr), 'Upsell'),
                    (
                        (_.start_mrr > 0)
                        & (_.end_mrr < _.start_mrr)
                        & (_.end_mrr > 0),
                        'Downsell',
                    ),
                    else_='Unchanged',
                ),
            )
        )

        return bridge

    # ══════════════════════════════════════════════════════════════
    # Query builders - return Ibis expressions (lazy)
    # ══════════════════════════════════════════════════════════════

    def available_months(self) -> ir.Table:
        """Expression for available months in the data.

        Returns:
            Ibis table with single 'month' column (YYYY-MM format)
        """
        m = self.mapping
        return (
            self.table.select(
                month=self._period_to_month(self.table[m.period])
            )
            .distinct()
            .order_by('month')
        )

    def available_countries(self) -> ir.Table:
        """Expression for available countries/regions.

        Returns:
            Ibis table with single 'country' column, or empty if no region column
        """
        m = self.mapping
        if not m.region:
            # Return empty result using the actual table (limit 0) to avoid memtable
            # This generates valid SQL that works in any backend
            return (
                self.table.limit(0)
                .select(country=ibis.literal(None).cast('string'))
            )

        return (
            self.table.select(country=self.table[m.region])
            .filter(_.country.notnull() & (_.country != ''))
            .distinct()
            .order_by('country')
        )

    def available_industries(self) -> ir.Table:
        """Expression for available industries.

        Returns:
            Ibis table with single 'industry' column, or empty if no industry column
        """
        m = self.mapping
        if not m.industry:
            # Return empty result using the actual table (limit 0) to avoid memtable
            # This generates valid SQL that works in any backend
            return (
                self.table.limit(0)
                .select(industry=ibis.literal(None).cast('string'))
            )

        return (
            self.table.select(industry=self.table[m.industry])
            .filter(_.industry.notnull() & (_.industry != ''))
            .distinct()
            .order_by('industry')
        )

    def summary(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
    ) -> ir.Table:
        """Expression for ARR Bridge summary.

        Args:
            start_month: Start period (YYYY-MM format)
            end_month: End period (YYYY-MM format)
            countries: Optional list of countries to filter
            industries: Optional list of industries to filter

        Returns:
            Ibis table with summary metrics (single row):
            - beginning_mrr, ending_mrr
            - new_business_mrr, upsell_mrr, downsell_mrr, churn_mrr
            - new_business_count, upsell_count, downsell_count, churn_count
        """
        bridge = self._build_customer_bridge(
            start_month, end_month, countries, industries
        )

        # Aggregate summary metrics
        return bridge.aggregate(
            beginning_mrr=_.start_mrr.sum(),
            ending_mrr=_.end_mrr.sum(),
            new_business_mrr=(
                _.end_mrr * (_.movement_type == 'New Business').cast('int64')
            ).sum(),
            upsell_mrr=(
                _.mrr_change * (_.movement_type == 'Upsell').cast('int64')
            ).sum(),
            downsell_mrr=(
                _.mrr_change * (_.movement_type == 'Downsell').cast('int64')
            ).sum(),
            churn_mrr=(
                _.start_mrr * (_.movement_type == 'Churn').cast('int64')
            ).sum(),
            new_business_count=(_.movement_type == 'New Business')
            .cast('int64')
            .sum(),
            upsell_count=(_.movement_type == 'Upsell').cast('int64').sum(),
            downsell_count=(_.movement_type == 'Downsell').cast('int64').sum(),
            churn_count=(_.movement_type == 'Churn').cast('int64').sum(),
        )

    def customers(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        exclude_unchanged: bool = True,
    ) -> ir.Table:
        """Expression for customer-level bridge details.

        Args:
            start_month: Start period (YYYY-MM format)
            end_month: End period (YYYY-MM format)
            countries: Optional list of countries to filter
            industries: Optional list of industries to filter
            exclude_unchanged: If True, exclude customers with no movement

        Returns:
            Ibis table with customer rows:
            - customer_key, customer_name (if available)
            - start_mrr, end_mrr, mrr_change
            - movement_type
            - region, industry (if available)
        """
        bridge = self._build_customer_bridge(
            start_month, end_month, countries, industries
        )

        if exclude_unchanged:
            bridge = bridge.filter(_.movement_type != 'Unchanged')

        return bridge.order_by(_.mrr_change.abs().desc())

    def revenue_evolution(
        self,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
    ) -> ir.Table:
        """Expression for monthly ARR evolution (for charts).

        Args:
            countries: Optional list of countries to filter
            industries: Optional list of industries to filter

        Returns:
            Ibis table with columns:
            - month (YYYY-MM)
            - arr (annualized recurring revenue = MRR * 12)
        """
        m = self.mapping
        t = self._base_table(countries, industries)

        return (
            t.group_by(month=self._period_to_month(t[m.period]))
            .aggregate(arr=t[m.revenue].sum() * 12)
            .order_by('month')
        )

    def customer_monthly(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
    ) -> ir.Table:
        """Expression for customer-level monthly MRR data.

        Returns a flat table with one row per customer per month,
        suitable for pivoting into a customer × month matrix.

        Args:
            start_month: Start period (inclusive, YYYY-MM format)
            end_month: End period (inclusive, YYYY-MM format)
            countries: Optional list of countries to filter
            industries: Optional list of industries to filter

        Returns:
            Ibis table with columns:
            - customer (customer name)
            - customer_key (customer identifier)
            - month (YYYY-MM)
            - mrr (monthly recurring revenue)
            - region (if available)
            - industry (if available)
        """
        m = self.mapping
        t = self._base_table(countries, industries)

        # Column to group customers by (prefer ID over name)
        group_col = m.customer_id or m.customer
        period_month = self._period_to_month(t[m.period])

        # Filter to the period range
        t_filtered = t.filter(
            (period_month >= start_month) & (period_month <= end_month)
        )

        # Build aggregation columns
        agg_cols = {
            'mrr': t_filtered[m.revenue].sum(),
        }

        # Add customer name if we have a separate ID column
        if m.customer_id:
            agg_cols['customer'] = t_filtered[m.customer].max()

        # Add optional columns
        if m.region:
            agg_cols['region'] = t_filtered[m.region].max()
        if m.industry:
            agg_cols['industry'] = t_filtered[m.industry].max()

        # Group by customer and month
        result = (
            t_filtered
            .group_by(
                customer_key=t_filtered[group_col],
                month=period_month,
            )
            .aggregate(**agg_cols)
        )

        # If no separate customer_id, use customer_key as customer name too
        if not m.customer_id:
            result = result.mutate(customer=result.customer_key)

        return result.order_by(['customer_key', 'month'])

    def price_increase_effect(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
    ) -> ir.Table:
        """Expression for cumulative price increase effect.

        Sums the price_increase_effect column across all months
        between start (exclusive) and end (inclusive).

        Args:
            start_month: Start period (exclusive, YYYY-MM format)
            end_month: End period (inclusive, YYYY-MM format)
            countries: Optional list of countries to filter
            industries: Optional list of industries to filter

        Returns:
            Ibis table with single column: price_increase_effect_total
            Returns 0 if no price_increase_effect column exists
        """
        m = self.mapping

        if not m.price_increase_effect:
            # Return 0 using the actual table to avoid memtable
            # Use aggregate with literal to generate valid SQL
            return self.table.aggregate(
                price_increase_effect_total=ibis.literal(0.0)
            )

        t = self._base_table(countries, industries)
        period_month = self._period_to_month(t[m.period])

        return (
            t.filter((period_month > start_month) & (period_month <= end_month))
            .aggregate(
                price_increase_effect_total=ibis.coalesce(
                    t[m.price_increase_effect].sum(), 0
                )
            )
        )

    # ══════════════════════════════════════════════════════════════
    # Execution helpers
    # ══════════════════════════════════════════════════════════════

    def to_sql(self, expr: ir.Table) -> str:
        """Get SQL string for debugging/inspection.

        Args:
            expr: Ibis table expression

        Returns:
            SQL string that would be executed
        """
        return ibis.to_sql(expr)

    def execute(self, expr: ir.Table) -> 'pl.DataFrame':
        """Execute expression and return Polars DataFrame.

        Args:
            expr: Ibis table expression to execute

        Returns:
            Polars DataFrame with results
        """
        return expr.to_polars()

    def execute_to_pyarrow(self, expr: ir.Table):
        """Execute expression and return PyArrow Table.

        Args:
            expr: Ibis table expression to execute

        Returns:
            PyArrow Table with results
        """
        return expr.to_pyarrow()

    def execute_summary(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
    ) -> BridgeSummary:
        """Convenience method: execute summary and return typed result.

        Args:
            start_month: Start period (YYYY-MM format)
            end_month: End period (YYYY-MM format)
            countries: Optional list of countries to filter
            industries: Optional list of industries to filter

        Returns:
            BridgeSummary dataclass with results
        """
        expr = self.summary(start_month, end_month, countries, industries)
        df = self.execute(expr)
        row = df.row(0, named=True)

        return BridgeSummary(
            beginning_mrr=float(row['beginning_mrr'] or 0),
            ending_mrr=float(row['ending_mrr'] or 0),
            new_business_mrr=float(row['new_business_mrr'] or 0),
            upsell_mrr=float(row['upsell_mrr'] or 0),
            downsell_mrr=float(row['downsell_mrr'] or 0),
            churn_mrr=float(row['churn_mrr'] or 0),
            new_business_count=int(row['new_business_count'] or 0),
            upsell_count=int(row['upsell_count'] or 0),
            downsell_count=int(row['downsell_count'] or 0),
            churn_count=int(row['churn_count'] or 0),
        )
