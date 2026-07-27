"""Churn analytics using Ibis expressions.

Generates type-safe, injection-proof queries that work with
any Ibis backend (DuckDB, Polars, PostgreSQL, etc.)
"""

from typing import TYPE_CHECKING, Any

import ibis
from ibis import _
from ibis.expr import types as ir

from cube_analytics.schema import ColumnMapping

if TYPE_CHECKING:
    import duckdb
    import polars as pl


class ChurnQueries:
    """Churn analytics using Ibis expressions.

    Measures customer attrition (churn) and revenue contraction by comparing
    each month to its predecessor. Supports six registered query types for
    the churn analytics dashboard.

    Churn is defined as: a customer had MRR > 0 in month N-1 and MRR = 0
    in month N (they disappeared entirely).

    Contraction is defined as: a customer had higher MRR in month N-1
    than month N, but is still active (MRR > 0).

    Example:
        >>> import polars as pl
        >>> from cube_analytics import ChurnQueries
        >>>
        >>> df = pl.read_parquet('cube.parquet')
        >>> queries = ChurnQueries.from_polars(df)
        >>>
        >>> # Build and inspect query
        >>> expr = queries.churn_monthly_summary('2024-01', '2024-12')
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
    ) -> 'ChurnQueries':
        """Create from Polars DataFrame.

        Args:
            df: Polars DataFrame with cube data
            mapping: Optional column mapping (auto-detected if None)

        Returns:
            ChurnQueries instance
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
    ) -> 'ChurnQueries':
        """Create from DuckDB file path.

        Args:
            path: Path to DuckDB file
            schema: Schema name (default: 'analysis')
            table_name: Table name (default: 'cube_output')
            mapping: Optional column mapping (auto-detected if None)

        Returns:
            ChurnQueries instance
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
    ) -> 'ChurnQueries':
        """Create from existing DuckDB connection.

        Args:
            conn: DuckDB connection object
            schema: Schema name (default: 'analysis')
            table_name: Table name (default: 'cube_output')
            mapping: Optional column mapping (auto-detected if None)

        Returns:
            ChurnQueries instance
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
    ) -> 'ChurnQueries':
        """Create from existing Ibis connection (any backend).

        Args:
            con: Ibis backend connection
            table_name: Table name
            schema: Optional schema name
            mapping: Optional column mapping (auto-detected if None)

        Returns:
            ChurnQueries instance
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

    def _extract_year(self, month_col: ir.StringColumn) -> ir.StringValue:
        """Extract 4-digit year string from YYYY-MM column."""
        return month_col.substr(0, 4)

    def _next_month(self, month_col: ir.StringColumn) -> ir.StringValue:
        """Compute next month as YYYY-MM string from a YYYY-MM input.

        Converts to a full date (YYYY-MM-01), adds one calendar month,
        then formats back to YYYY-MM. Works with any Ibis DuckDB backend.
        """
        first_of_month = (month_col + ibis.literal('-01')).cast('date')
        return (first_of_month + ibis.interval(months=1)).strftime('%Y-%m')

    def _base_table(
        self,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
    ) -> ir.Table:
        """Apply base filters (recurring, region, industry, product).

        Args:
            countries: Optional list of countries to filter
            industries: Optional list of industries to filter
            products: Optional list of products to filter

        Returns:
            Filtered Ibis table expression
        """
        t = self.table
        m = self.mapping

        if m.is_recurring:
            t = t.filter(t[m.is_recurring].isin([True, 1]))
        if countries and m.region:
            t = t.filter(t[m.region].isin(countries))
        if industries and m.industry:
            t = t.filter(t[m.industry].isin(industries))
        if products and m.product:
            t = t.filter(t[m.product].isin(products))

        return t

    def _monthly_customer_mrr(
        self,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
    ) -> ir.Table:
        """Aggregate MRR per customer per month across all available months.

        Returns a table with customer_key, month, mrr, and optional
        customer_name / region / industry / product columns depending
        on ColumnMapping availability.
        """
        m = self.mapping
        t = self._base_table(countries, industries, products)
        group_col = m.customer_id or m.customer
        period_month = self._period_to_month(t[m.period])

        agg_cols: dict[str, Any] = {
            'mrr': t[m.revenue].sum().cast('float64'),
        }
        if m.customer_id:
            agg_cols['customer_name'] = t[m.customer].max()
        if m.region:
            agg_cols['region'] = t[m.region].max()
        if m.industry:
            agg_cols['industry'] = t[m.industry].max()
        if m.product:
            agg_cols['product'] = t[m.product].max()

        return t.group_by(
            customer_key=t[group_col],
            month=period_month,
        ).aggregate(**agg_cols)

    # ══════════════════════════════════════════════════════════════
    # Available dimension helpers
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
            return self.table.limit(0).select(
                country=ibis.literal(None).cast('string')
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
            return self.table.limit(0).select(
                industry=ibis.literal(None).cast('string')
            )
        return (
            self.table.select(industry=self.table[m.industry])
            .filter(_.industry.notnull() & (_.industry != ''))
            .distinct()
            .order_by('industry')
        )

    def available_products(self) -> ir.Table:
        """Expression for available products/segments.

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

    # ══════════════════════════════════════════════════════════════
    # Query builders - return Ibis expressions (lazy)
    # ══════════════════════════════════════════════════════════════

    def churn_base_data(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
    ) -> ir.Table:
        """Monthly customer revenue with churn/contraction classification.

        Detects negative revenue movements by comparing each month in the range
        to the previous month via a full outer join. Produces one row per
        customer per month where a churn or contraction event occurred.

        Movement types:
        - 'Churn': customer had MRR > 0 in month N-1, MRR = 0 in month N
        - 'Contraction': customer active in both months, MRR decreased

        The ``month`` column indicates when the movement occurred (the current
        month, not the prior month).

        Args:
            start_month: Start of period (YYYY-MM, inclusive)
            end_month: End of period (YYYY-MM, inclusive)
            countries: Optional country/region filter
            industries: Optional industry filter
            products: Optional product filter

        Returns:
            Ibis table with columns:
            - period (YYYY-MM) - observation month of the movement
            - customer_id - customer identifier
            - curr_mrr - revenue in this month (0.0 for churned customers)
            - prev_mrr - revenue in the prior month (always > 0)
            - movement_type - 'churn' | 'contraction'
            - customer (if customer_id column exists)
            - region, industry, product (if respective columns exist)
        """
        m = self.mapping
        monthly = self._monthly_customer_mrr(countries, industries, products)

        # Current period: rows in [start_month, end_month]
        curr = monthly.filter(
            (monthly.month >= start_month) & (monthly.month <= end_month)
        )

        # Previous period: all monthly data aliased with p_ prefix.
        # We add p_next_month so we can join: prev.p_next_month = curr.c_month
        prev_select: dict[str, Any] = {
            'p_customer_key': monthly.customer_key,
            'p_month': monthly.month,
            'p_mrr': monthly.mrr,
        }
        if m.customer_id:
            prev_select['p_customer_name'] = monthly.customer_name
        if m.region:
            prev_select['p_region'] = monthly.region
        if m.industry:
            prev_select['p_industry'] = monthly.industry
        if m.product:
            prev_select['p_product'] = monthly.product

        prev = monthly.select(**prev_select).mutate(
            p_next_month=self._next_month(_.p_month)
        )

        # Current period aliased with c_ prefix
        curr_select: dict[str, Any] = {
            'c_customer_key': curr.customer_key,
            'c_month': curr.month,
            'c_mrr': curr.mrr,
        }
        if m.customer_id:
            curr_select['c_customer_name'] = curr.customer_name
        if m.region:
            curr_select['c_region'] = curr.region
        if m.industry:
            curr_select['c_industry'] = curr.industry
        if m.product:
            curr_select['c_product'] = curr.product

        curr_aliased = curr.select(**curr_select)

        # Full outer join: prev (left) and curr (right).
        # - Rows only in prev → customer churned (disappeared in their next month)
        # - Rows in both → customer active in both; may be contracting
        joined = prev.outer_join(
            curr_aliased,
            (prev.p_customer_key == curr_aliased.c_customer_key)
            & (prev.p_next_month == curr_aliased.c_month),
        )

        # Build output columns, coalescing from both sides
        out_cols: dict[str, Any] = {
            'period': ibis.coalesce(curr_aliased.c_month, prev.p_next_month),
            'customer_id': ibis.coalesce(
                curr_aliased.c_customer_key, prev.p_customer_key
            ),
            'curr_mrr': ibis.coalesce(
                curr_aliased.c_mrr, ibis.literal(0.0).cast('float64')
            ),
            'prev_mrr': prev.p_mrr,
        }
        if m.customer_id:
            out_cols['customer'] = ibis.coalesce(
                curr_aliased.c_customer_name, prev.p_customer_name
            )
        if m.region:
            out_cols['region'] = ibis.coalesce(
                curr_aliased.c_region, prev.p_region
            )
        if m.industry:
            out_cols['industry'] = ibis.coalesce(
                curr_aliased.c_industry, prev.p_industry
            )
        if m.product:
            out_cols['product'] = ibis.coalesce(
                curr_aliased.c_product, prev.p_product
            )

        return (
            joined.select(**out_cols)
            # Only include: within range, had prior revenue, and experienced decline
            .filter(
                (_.period >= start_month)
                & (_.period <= end_month)
                & (_.prev_mrr > 0)
                & (_.curr_mrr < _.prev_mrr)
            )
            .mutate(
                movement_type=ibis.cases(
                    (_.curr_mrr == 0, 'churn'),
                    else_='contraction',
                )
            )
            .order_by(['period', _.prev_mrr.desc()])
        )

    def churn_monthly_summary(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
    ) -> ir.Table:
        """Aggregated churn and contraction MRR per month.

        For each month in the range, computes the total MRR lost to churn
        and contraction. Suitable for stacked bar charts.

        Args:
            start_month: Start period (YYYY-MM, inclusive)
            end_month: End period (YYYY-MM, inclusive)
            countries: Optional country/region filter
            industries: Optional industry filter
            products: Optional product filter

        Returns:
            Ibis table with columns:
            - period (YYYY-MM)
            - churn_mrr (positive value: MRR lost to full churn)
            - contraction_mrr (positive value: MRR reduction for contracted customers)
            - churn_count (number of churned logos)
            - contraction_count (number of contracted logos)
        """
        base = self.churn_base_data(
            start_month, end_month, countries, industries, products
        )

        return (
            base.group_by('period')
            .aggregate(
                churn_mrr=(
                    _.prev_mrr * (_.movement_type == 'churn').cast('int64')
                )
                .sum()
                .cast('float64'),
                contraction_mrr=(
                    (_.prev_mrr - _.curr_mrr)
                    * (_.movement_type == 'contraction').cast('int64')
                )
                .sum()
                .cast('float64'),
                churn_count=(_.movement_type == 'churn')
                .cast('int64')
                .sum()
                .cast('int64'),
                contraction_count=(_.movement_type == 'contraction')
                .cast('int64')
                .sum()
                .cast('int64'),
            )
            .order_by('period')
        )

    def churn_customer_details(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
    ) -> ir.Table:
        """Churned and contracted customers with prior MRR and last event month.

        Groups all churn/contraction events by customer across the full period.
        Each customer appears once, classified by their dominant movement type
        (churn takes precedence over contraction).

        Args:
            start_month: Start period (YYYY-MM, inclusive)
            end_month: End period (YYYY-MM, inclusive)
            countries: Optional country/region filter
            industries: Optional industry filter
            products: Optional product filter

        Returns:
            Ibis table with columns:
            - customer_id
            - last_period (YYYY-MM) - latest month a movement was detected
            - prev_mrr - highest MRR seen before a movement (proxy for peak revenue)
            - curr_mrr - lowest MRR seen during a movement
            - lost_mrr - total MRR lost across the period (churn + contraction delta)
            - movement_type - 'churn' | 'contraction' (dominant type)
            - customer (if customer_id column exists)
            - region, industry (if respective columns exist)
        """
        m = self.mapping
        base = self.churn_base_data(
            start_month, end_month, countries, industries, products
        )

        agg_cols: dict[str, Any] = {
            'last_period': _.period.max(),
            'prev_mrr': _.prev_mrr.max().cast('float64'),
            'curr_mrr': _.curr_mrr.min().cast('float64'),
            'lost_mrr': (
                _.prev_mrr * (_.movement_type == 'churn').cast('int64')
                + (_.prev_mrr - _.curr_mrr)
                * (_.movement_type == 'contraction').cast('int64')
            )
            .sum()
            .cast('float64'),
            '_churn_count': (_.movement_type == 'churn').cast('int64').sum(),
        }
        if m.customer_id:
            agg_cols['customer'] = _.customer.max()
        if m.region:
            agg_cols['region'] = _.region.max()
        if m.industry:
            agg_cols['industry'] = _.industry.max()

        return (
            base.group_by('customer_id')
            .aggregate(**agg_cols)
            .mutate(
                movement_type=ibis.cases(
                    (_._churn_count > 0, 'churn'),
                    else_='contraction',
                )
            )
            .drop('_churn_count')
            .order_by(_.lost_mrr.desc())
        )

    def revenue_at_risk(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
    ) -> ir.Table:
        """Customers with upcoming contract expirations.

        Identifies customers who were active in the period (had revenue between
        start_month and end_month) and whose contract_end_date has not yet
        passed start_month (i.e., contracts are still live or expiring soon).

        Returns an empty table with the same schema when ``contract_end_date``
        is absent from ColumnMapping, allowing callers to treat the missing
        column case uniformly.

        Args:
            start_month: Start of observation period (YYYY-MM, inclusive)
            end_month: End of observation period (YYYY-MM, inclusive)
            countries: Optional country/region filter
            industries: Optional industry filter
            products: Optional product filter

        Returns:
            Ibis table with columns:
            - customer - customer name
            - mrr - total MRR summed over the selected period
            - contract_end_date - contract expiry as YYYY-MM string
            - days_to_expiry - integer days from end_month to contract_end_date
            - region, industry (if respective columns exist)

            Empty table (0 rows, same schema) if contract_end_date is missing.
        """
        m = self.mapping

        if not m.contract_end_date:
            # Return empty result with the expected schema so callers need
            # no special-case branching.
            empty_cols: dict[str, Any] = {
                'customer': ibis.literal(None).cast('string'),
                'mrr': ibis.literal(None).cast('float64'),
                'contract_end_date': ibis.literal(None).cast('string'),
                'days_to_expiry': ibis.literal(None).cast('int64'),
            }
            return self.table.limit(0).select(**empty_cols)

        t = self._base_table(countries, industries, products)
        group_col = m.customer_id or m.customer
        period_month = self._period_to_month(t[m.period])

        # Aggregate per customer over the entire period; take latest contract end date
        agg_cols: dict[str, Any] = {
            'mrr': t[m.revenue].sum().cast('float64'),
            'contract_end_date': t[m.contract_end_date].max().cast('string'),
        }
        agg_cols['customer'] = t[m.customer].max()
        if m.region:
            agg_cols['region'] = t[m.region].max()
        if m.industry:
            agg_cols['industry'] = t[m.industry].max()

        # Restrict to the selected period for activity check
        active = t.filter(
            (period_month >= start_month) & (period_month <= end_month)
        )

        ref_date = ibis.literal(end_month + '-01').cast('date')

        return (
            active.group_by(customer_id=active[group_col])
            .aggregate(**agg_cols)
            # Only customers still active (positive MRR in the period)
            .filter(_.mrr > 0)
            # Only customers with a contract end date that hasn't expired before the period
            .filter(
                _.contract_end_date.notnull()
                & (_.contract_end_date >= start_month)
            )
            .mutate(
                days_to_expiry=(
                    _.contract_end_date.cast('date') - ref_date
                ).cast('int64'),
            )
            .drop('customer_id')
            .order_by('contract_end_date')
        )

    def cohort_base(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
    ) -> ir.Table:
        """Customer cohort assignment with monthly revenue for survival curves.

        Assigns each customer to a cohort based on their first observed month
        within the data (not restricted to the requested range). Returns monthly
        MRR per customer for the requested range, enriched with cohort metadata.

        The frontend uses this data to build:
        - Survival curves grouped by cohort_year
        - Retention heatmap (cohort_month x months_since_start)
        - Lifetime distribution histogram

        Args:
            start_month: Start of observation period (YYYY-MM, inclusive)
            end_month: End of observation period (YYYY-MM, inclusive)
            countries: Optional country/region filter
            industries: Optional industry filter
            products: Optional product filter

        Returns:
            Ibis table with columns:
            - customer_id
            - period (YYYY-MM) - observation month
            - mrr - revenue in this month
            - cohort_month (YYYY-MM) - first month customer appeared in data
            - cohort_year - 4-digit year string of cohort_month
            - months_since_start - integer offset from cohort_month to period
            - customer_name (if customer_id column exists)
            - region, industry (if respective columns exist)
        """
        m = self.mapping
        t = self._base_table(countries, industries, products)
        group_col = m.customer_id or m.customer
        period_month = self._period_to_month(t[m.period])

        # Monthly MRR aggregate for the requested range
        agg_cols: dict[str, Any] = {
            'mrr': t[m.revenue].sum().cast('float64'),
        }
        if m.customer_id:
            agg_cols['customer_name'] = t[m.customer].max()
        if m.region:
            agg_cols['region'] = t[m.region].max()
        if m.industry:
            agg_cols['industry'] = t[m.industry].max()
        if m.cohort:
            agg_cols['cohort'] = t[m.cohort].max()
        if m.pricing_segment:
            agg_cols['pricing_segment'] = t[m.pricing_segment].max()

        monthly_in_range = (
            t.group_by(
                customer_key=t[group_col],
                month=period_month,
            )
            .aggregate(**agg_cols)
            .filter((_.month >= start_month) & (_.month <= end_month))
        )

        # Cohort month = first ever month for each customer (across all data, not just range)
        all_months = (
            t.group_by(
                customer_key=t[group_col], month=period_month
            ).aggregate()  # just deduplicate
        )
        first_seen = all_months.group_by('customer_key').aggregate(
            cohort_month=_.month.min()
        )

        # Join cohort month back onto the ranged monthly data
        select_cols: dict[str, Any] = {
            'customer_id': monthly_in_range.customer_key,
            'period': monthly_in_range.month,
            'mrr': monthly_in_range.mrr,
            'cohort_month': first_seen.cohort_month,
        }
        if m.customer_id:
            select_cols['customer_name'] = monthly_in_range.customer_name
        if m.region:
            select_cols['region'] = monthly_in_range.region
        if m.industry:
            select_cols['industry'] = monthly_in_range.industry
        if m.cohort:
            select_cols['cohort'] = monthly_in_range.cohort
        if m.pricing_segment:
            select_cols['pricing_segment'] = monthly_in_range.pricing_segment

        joined = monthly_in_range.join(
            first_seen,
            monthly_in_range.customer_key == first_seen.customer_key,
        ).select(**select_cols)

        return joined.mutate(
            cohort_year=self._extract_year(_.cohort_month),
            months_since_start=(
                (
                    _.period.substr(0, 4).cast('int64')
                    - _.cohort_month.substr(0, 4).cast('int64')
                )
                * 12
                + (
                    _.period.substr(5, 2).cast('int64')
                    - _.cohort_month.substr(5, 2).cast('int64')
                )
            ).cast('int64'),
        ).order_by(['customer_id', 'period'])

    def churn_segment_matrix(
        self,
        start_month: str,
        end_month: str,
    ) -> ir.Table:
        """Churn percentage per cohort × pricing_segment (current-state snapshot).

        Computes what fraction of customers active at start_month had fully churned
        (MRR = 0 or absent) by end_month, broken down by acquisition cohort and
        pricing segment.

        Returns an empty table (0 rows, same schema) when either cohort or
        pricing_segment is absent from ColumnMapping, allowing callers to treat
        the missing-column case uniformly (e.g. hide the Segments tab).

        Args:
            start_month: Baseline month (YYYY-MM). Customers active here form the denominator.
            end_month: Evaluation month (YYYY-MM). Customers absent or at zero MRR are churned.

        Returns:
            Ibis table with columns:
            - cohort (str) — acquisition cohort value
            - pricing_segment (str) — pricing tier value
            - total_customers (int64) — customers active at start_month
            - churned_customers (int64) — subset with MRR=0 or absent by end_month
            - churn_pct (float64) — churned_customers * 100.0 / total_customers
        """
        m = self.mapping

        if not m.cohort or not m.pricing_segment:
            # Return empty result with expected schema so callers need no
            # special-case branching (mirrors revenue_at_risk pattern).
            empty_cols: dict[str, Any] = {
                'cohort': ibis.literal(None).cast('string'),
                'pricing_segment': ibis.literal(None).cast('string'),
                'total_customers': ibis.literal(None).cast('int64'),
                'churned_customers': ibis.literal(None).cast('int64'),
                'churn_pct': ibis.literal(None).cast('float64'),
            }
            return self.table.limit(0).select(**empty_cols)

        t = self._base_table()
        group_col = m.customer_id or m.customer
        period_month = self._period_to_month(t[m.period])

        # Monthly MRR per customer, carrying cohort + pricing_segment forward
        monthly = (
            t.group_by(
                customer_key=t[group_col],
                month=period_month,
            )
            .aggregate(
                mrr=t[m.revenue].sum().cast('float64'),
                cohort=t[m.cohort].max(),
                pricing_segment=t[m.pricing_segment].max(),
            )
        )

        # Denominator: customers who were active (mrr > 0) at start_month
        active_at_start = monthly.filter(
            (monthly.month == start_month) & (monthly.mrr > 0)
        ).select('customer_key', 'cohort', 'pricing_segment')

        # Lookup: each customer's MRR at end_month (absent → NULL after left join)
        at_end = monthly.filter(monthly.month == end_month).select(
            end_customer_key=monthly.customer_key,
            end_mrr=monthly.mrr,
        )

        a = active_at_start
        e = at_end

        # Left join retains all start-active customers; NULL end_mrr → churned
        joined = a.left_join(
            e,
            a.customer_key == e.end_customer_key,
        ).select(
            customer_key=a.customer_key,
            cohort=a.cohort,
            pricing_segment=a.pricing_segment,
            end_mrr=ibis.coalesce(e.end_mrr, ibis.literal(0.0).cast('float64')),
        )

        return (
            joined.group_by(['cohort', 'pricing_segment'])
            .aggregate(
                total_customers=_.customer_key.count().cast('int64'),
                churned_customers=(_.end_mrr == 0.0)
                .cast('int64')
                .sum()
                .cast('int64'),
            )
            .mutate(
                churn_pct=(
                    _.churned_customers.cast('float64')
                    * ibis.literal(100.0)
                    / _.total_customers.cast('float64')
                )
            )
            .order_by(['cohort', 'pricing_segment'])
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
