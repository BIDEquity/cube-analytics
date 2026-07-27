"""Customer concentration risk analytics using Ibis expressions.

Generates type-safe, injection-proof queries for the concentration risk dashboard.
All window-function math (HHI, Gini, cumulative share, Lorenz) is expressed in
Ibis so it executes entirely in the database — consistent with the ready-to-render
SQL architecture (see docs/steering/dashboard-architecture.md).

Exit run-rate = exit_mrr * 12, consistent with the upsell dashboard definition.
Zero-revenue customers (exit_mrr == 0) are excluded from all concentration metrics.
"""

from typing import TYPE_CHECKING, Any

import ibis
from ibis import _
from ibis.expr import types as ir

from cube_analytics.schema import ColumnMapping

if TYPE_CHECKING:
    import duckdb
    import polars as pl


class ConcentrationQueries:
    """Customer concentration risk analytics using Ibis expressions.

    Computes HHI, Gini coefficient, Top-N revenue shares, Lorenz curves,
    and segment breakdowns from monthly cube data. All computation happens
    in the database — no Python-side aggregation.

    Zero-revenue customers (exit_run_rate == 0) are excluded from all metrics.
    Exit run-rate is defined as exit_mrr * 12 (last non-null MRR in the year,
    annualized), consistent with the upsell dashboard.

    Example:
        >>> import polars as pl
        >>> from cube_analytics import ConcentrationQueries
        >>>
        >>> df = pl.read_parquet('cube.parquet')
        >>> queries = ConcentrationQueries.from_polars(df)
        >>>
        >>> expr = queries.hero_kpis(2024)
        >>> print(queries.to_sql(expr))
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
    # Factory methods
    # ══════════════════════════════════════════════════════════════

    @classmethod
    def from_polars(
        cls,
        df: 'pl.DataFrame',
        mapping: ColumnMapping | None = None,
    ) -> 'ConcentrationQueries':
        """Create from Polars DataFrame.

        Args:
            df: Polars DataFrame with cube data
            mapping: Optional column mapping (auto-detected if None)

        Returns:
            ConcentrationQueries instance
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
    ) -> 'ConcentrationQueries':
        """Create from DuckDB file path.

        Args:
            path: Path to DuckDB file
            schema: Schema name (default: 'analysis')
            table_name: Table name (default: 'cube_output')
            mapping: Optional column mapping (auto-detected if None)

        Returns:
            ConcentrationQueries instance
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
    ) -> 'ConcentrationQueries':
        """Create from existing DuckDB connection.

        Args:
            conn: DuckDB connection object
            schema: Schema name (default: 'analysis')
            table_name: Table name (default: 'cube_output')
            mapping: Optional column mapping (auto-detected if None)

        Returns:
            ConcentrationQueries instance
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
    ) -> 'ConcentrationQueries':
        """Create from existing Ibis connection (any backend).

        Args:
            con: Ibis backend connection
            table_name: Table name
            schema: Optional schema name
            mapping: Optional column mapping (auto-detected if None)

        Returns:
            ConcentrationQueries instance
        """
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

    def _period_to_month(self, col: ir.Column) -> ir.Value:
        """Extract YYYY-MM from period column."""
        return ibis.coalesce(
            col.try_cast('date').strftime('%Y-%m'),
            col.cast('string').substr(0, 7),
        )

    def _base_table(
        self,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
    ) -> ir.Table:
        """Apply base filters (recurring, country, industry, product)."""
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

    def _yearly_customer_exit_mrr(
        self,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
    ) -> ir.Table:
        """Per-customer exit_mrr aggregated to yearly level.

        Two-step aggregation (mirrors UpsellQueries.customer_year_product):
        1. Sum revenue per customer × year × month (collapse line items)
        2. Aggregate months → year: exit_mrr = last month's revenue

        Output columns: customer_key, customer_name, year, exit_mrr,
        country (if region mapped), industry (if industry mapped)
        """
        m = self.mapping
        t = self._base_table(countries, industries, products)
        group_col = m.customer_id or m.customer
        if not group_col:
            raise ValueError(
                'No customer or customer_id column found in mapping'
            )

        t = t.mutate(
            _year=self._extract_year(t[m.period]),
            _month=self._period_to_month(t[m.period]),
        )

        # Step 1: monthly totals per customer
        monthly_agg: dict[str, Any] = {
            'monthly_revenue': t[m.revenue].sum().cast('float64'),
        }
        if m.customer_id:
            monthly_agg['customer_name'] = t[m.customer].max()
        if m.region:
            monthly_agg['country'] = t[m.region].max()
        if m.industry:
            monthly_agg['industry'] = t[m.industry].max()

        monthly = t.group_by(
            customer_key=t[group_col],
            year=t._year,
            month=t._month,
        ).aggregate(**monthly_agg)

        # Step 2: collapse months → year; exit_mrr = revenue in the last month
        # with data (argmax on YYYY-MM string = lexicographic maximum =
        # chronologically last month that has a data point, not necessarily Dec).
        yearly_agg: dict[str, Any] = {
            'exit_mrr': monthly.monthly_revenue.argmax(monthly.month).cast(
                'float64'
            ),
        }
        if m.customer_id:
            yearly_agg['customer_name'] = monthly.customer_name.max()
        if m.region:
            yearly_agg['country'] = monthly.country.max()
        if m.industry:
            yearly_agg['industry'] = monthly.industry.max()

        result = monthly.group_by(
            customer_key=monthly.customer_key,
            year=monthly.year,
        ).aggregate(**yearly_agg)

        if not m.customer_id:
            result = result.mutate(customer_name=result.customer_key)

        return result

    def _kpi_scalars(self, t_run_rate: ir.Table) -> ir.Table:
        """Compute concentration KPIs from a customer exit_run_rate table.

        Input table must have columns: customer_key, exit_run_rate (> 0).
        Returns a single-row table with all concentration scalar KPIs.

        Uses two mutate passes (window functions) then a single aggregate:
        - Pass 1: _total, _n, _rnk (1-based desc), _rn_asc (0-based asc)
        - Pass 2: _share, _cum_desc (cumulative fraction descending)
        - Aggregate: top-N shares, HHI, Gini, 80/20 metric, customer count

        Gini formula (discrete, sorted ascending, 0-based rn_asc):
            G = (2 * SUM[(rn_asc + 1) * share] - (N + 1)) / N
        """
        t = t_run_rate

        all_win = ibis.window()
        cum_desc_win = ibis.window(
            order_by=[ibis.desc('exit_run_rate'), 'customer_key'], following=0
        )

        # Pass 1: totals + rank
        t = t.mutate(
            _total=t.exit_run_rate.sum().over(all_win).cast('float64'),
            _n=t.exit_run_rate.count().over(all_win).cast('float64'),
            _rnk=(
                ibis.row_number().over(
                    ibis.window(order_by=ibis.desc('exit_run_rate'))
                )
                + 1
            ).cast('int64'),
            _rn_asc=ibis.row_number()
            .over(ibis.window(order_by='exit_run_rate'))
            .cast('float64'),
        )

        # Pass 2: per-customer share + descending cumulative fraction
        t = t.mutate(
            _share=(t.exit_run_rate / t._total).cast('float64'),
            _cum_desc=(
                t.exit_run_rate.sum().over(cum_desc_win) / t._total
            ).cast('float64'),
        )

        n_expr = t._n.max()

        return t.aggregate(
            top_1_share=(
                (t.exit_run_rate * (t._rnk == 1).cast('float64')).sum()
                / t._total.max()
            ).cast('float64'),
            top_5_share=(
                (t.exit_run_rate * (t._rnk <= 5).cast('float64')).sum()
                / t._total.max()
            ).cast('float64'),
            top_10_share=(
                (t.exit_run_rate * (t._rnk <= 10).cast('float64')).sum()
                / t._total.max()
            ).cast('float64'),
            hhi=((t._share * t._share).sum() * ibis.literal(10000.0)).cast(
                'float64'
            ),
            # Discrete Gini: G = (2·Σ[(rn+1)·share] − (N+1)) / N
            # nullif guard prevents 0/0 → NaN when the table is empty (first year).
            gini=(
                (
                    ibis.literal(2.0)
                    * ((t._rn_asc + ibis.literal(1.0)) * t._share).sum()
                    - (n_expr + ibis.literal(1.0))
                )
                / n_expr.nullif(ibis.literal(0.0))
            ).cast('float64'),
            total_customers=t._n.max().cast('int64'),
            # Min rank where descending cumulative share first reaches 80%.
            # Multiply rank by 1 when threshold met, 0 otherwise, then nullif(0)
            # makes non-qualifying rows null so .min() picks the crossing rank.
            customers_for_80pct=(
                (t._rnk * (t._cum_desc >= ibis.literal(0.8)).cast('int64'))
                .nullif(0)
                .min()
            ),
        )

    # ══════════════════════════════════════════════════════════════
    # Query builders — return lazy Ibis expressions
    # ══════════════════════════════════════════════════════════════

    def available_years(self) -> ir.Table:
        """Distinct years present in the cube, sorted ascending.

        Returns:
            Table with single 'year' column (integer)
        """
        m = self.mapping
        return (
            self.table.select(year=self._extract_year(self.table[m.period]))
            .distinct()
            .order_by('year')
        )

    def hero_kpis(
        self,
        year: int,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
    ) -> ir.Table:
        """Single-row result with all hero KPIs for the selected year + prior year.

        Computes current and prior-year concentration metrics and cross-joins them
        into one row. Zero-revenue customers are excluded from both years.

        Args:
            year: The selected analysis year
            countries: Optional country/region filter
            industries: Optional industry filter
            products: Optional product filter

        Returns:
            Single-row table with columns:
            - top_1_share, top_5_share, top_10_share: fraction [0, 1]
            - hhi: HHI × 10,000 (range 0–10,000)
            - gini: Gini coefficient [0, 1]
            - total_customers: count of active customers in year
            - customers_for_80pct: min customers whose cumulative share ≥ 80%
            - prev_top_1_share, prev_top_5_share, prev_top_10_share
            - prev_hhi, prev_gini, prev_total_customers, prev_customers_for_80pct
        """
        yearly = self._yearly_customer_exit_mrr(
            countries, industries, products
        )

        def _run_rate_for_year(yr: int) -> ir.Table:
            return (
                yearly.filter(yearly.year == yr)
                .mutate(
                    exit_run_rate=(_.exit_mrr * ibis.literal(12.0)).cast(
                        'float64'
                    )
                )
                .filter(_.exit_run_rate > 0)
                .select('customer_key', 'exit_run_rate')
            )

        curr_kpis = self._kpi_scalars(_run_rate_for_year(year))
        # ibis .rename() takes {new_name: old_name}
        prev_kpis = self._kpi_scalars(_run_rate_for_year(year - 1)).rename(
            {
                'prev_top_1_share': 'top_1_share',
                'prev_top_5_share': 'top_5_share',
                'prev_top_10_share': 'top_10_share',
                'prev_hhi': 'hhi',
                'prev_gini': 'gini',
                'prev_total_customers': 'total_customers',
                'prev_customers_for_80pct': 'customers_for_80pct',
            }
        )

        return curr_kpis.cross_join(prev_kpis)

    def concentration_trend(
        self,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
    ) -> ir.Table:
        """Annual concentration metrics for all available years.

        Suitable for a multi-line trend chart (year on x-axis).
        Each row is one calendar year. Zero-revenue customers are excluded.

        Args:
            countries: Optional country/region filter
            industries: Optional industry filter
            products: Optional product filter

        Returns:
            Table ordered by year with columns:
            - year (int)
            - top_1_share, top_5_share, top_10_share: fraction [0, 1]
            - hhi: HHI × 10,000
        """
        yearly = self._yearly_customer_exit_mrr(
            countries, industries, products
        )
        t = yearly.mutate(
            exit_run_rate=(_.exit_mrr * ibis.literal(12.0)).cast('float64')
        ).filter(_.exit_run_rate > 0)

        # Per-year window specs
        yr_win = ibis.window(group_by='year')
        yr_desc_win = ibis.window(
            group_by='year',
            order_by=ibis.desc('exit_run_rate'),
        )

        t = t.mutate(
            _total_yr=t.exit_run_rate.sum().over(yr_win).cast('float64'),
            _rnk_yr=(ibis.row_number().over(yr_desc_win) + 1).cast('int64'),
        )
        t = t.mutate(
            _share_yr=(t.exit_run_rate / t._total_yr).cast('float64'),
        )

        return (
            t.group_by('year')
            .aggregate(
                top_1_share=(
                    (t.exit_run_rate * (t._rnk_yr == 1).cast('float64')).sum()
                    / t._total_yr.max()
                ).cast('float64'),
                top_5_share=(
                    (t.exit_run_rate * (t._rnk_yr <= 5).cast('float64')).sum()
                    / t._total_yr.max()
                ).cast('float64'),
                top_10_share=(
                    (t.exit_run_rate * (t._rnk_yr <= 10).cast('float64')).sum()
                    / t._total_yr.max()
                ).cast('float64'),
                hhi=(
                    (t._share_yr * t._share_yr).sum() * ibis.literal(10000.0)
                ).cast('float64'),
            )
            .order_by('year')
        )

    def customer_ranking(
        self,
        year: int,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
    ) -> ir.Table:
        """Ranked customer table with revenue shares and YoY growth.

        Sorted by exit run-rate descending (rank 1 = largest customer).
        Zero-revenue customers are excluded. YoY growth applies a ±1
        dead zone consistent with the upsell dashboard.

        Args:
            year: The selected analysis year
            countries: Optional country/region filter
            industries: Optional industry filter
            products: Optional product filter

        Returns:
            Table ordered by rank with columns:
            - rank (int, 1-based)
            - customer_id, customer (str)
            - exit_run_rate (float)
            - share_pct: percentage of total revenue [0, 100]
            - cumulative_share_pct: running sum descending [0, 100]
            - yoy_growth_pct: YoY change (null = no prior year, 0 = dead zone)
            - country (if region mapped), industry (if industry mapped)
        """
        m = self.mapping
        yearly = self._yearly_customer_exit_mrr(
            countries, industries, products
        )

        curr = (
            yearly.filter(yearly.year == year)
            .mutate(
                exit_run_rate=(_.exit_mrr * ibis.literal(12.0)).cast('float64')
            )
            .filter(_.exit_run_rate > 0)
        )

        prev = (
            yearly.filter(yearly.year == year - 1)
            .mutate(
                prev_run_rate=(_.exit_mrr * ibis.literal(12.0)).cast('float64')
            )
            .filter(_.prev_run_rate > 0)
            .select('customer_key', 'prev_run_rate')
        )

        # Left join: retain all current-year customers, prev_run_rate nullable
        joined = curr.left_join(prev, 'customer_key')

        all_win = ibis.window()
        cum_desc_win = ibis.window(
            order_by=[ibis.desc('exit_run_rate'), 'customer_key'], following=0
        )

        t = joined.mutate(
            _total=joined.exit_run_rate.sum().over(all_win).cast('float64'),
            _rnk=(
                ibis.row_number().over(
                    ibis.window(order_by=ibis.desc('exit_run_rate'))
                )
                + 1
            ).cast('int64'),
        )
        t = t.mutate(
            share_pct=(t.exit_run_rate / t._total * ibis.literal(100.0)).cast(
                'float64'
            ),
            _cum_run=t.exit_run_rate.sum().over(cum_desc_win).cast('float64'),
        )
        t = t.mutate(
            cumulative_share_pct=(
                t._cum_run / t._total * ibis.literal(100.0)
            ).cast('float64'),
        )

        # YoY growth with ±1 dead zone (same convention as upsell dashboard)
        _dead = ibis.literal(1.0)
        t = t.mutate(
            _delta=(
                t.exit_run_rate
                - ibis.coalesce(
                    t.prev_run_rate, ibis.literal(0.0).cast('float64')
                )
            ).cast('float64'),
        )
        t = t.mutate(
            yoy_growth_pct=ibis.cases(
                (t.prev_run_rate.isnull(), ibis.literal(None).cast('float64')),
                (t._delta.abs() <= _dead, ibis.literal(0.0).cast('float64')),
                else_=(t._delta / t.prev_run_rate * ibis.literal(100.0)).cast(
                    'float64'
                ),
            ),
        )

        out: dict[str, Any] = {
            'rank': t._rnk,
            'customer_id': t.customer_key,
            'customer': t.customer_name,
            'exit_run_rate': t.exit_run_rate,
            'share_pct': t.share_pct,
            'cumulative_share_pct': t.cumulative_share_pct,
            'yoy_growth_pct': t.yoy_growth_pct,
        }
        if m.region:
            out['country'] = t.country
        if m.industry:
            out['industry'] = t.industry

        return t.select(**out).order_by('rank')

    def lorenz_curve(
        self,
        year: int,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
    ) -> ir.Table:
        """Lorenz curve data: one row per customer, sorted ascending by revenue.

        x-axis = cumulative fraction of customers (sorted poorest-first)
        y-axis = cumulative fraction of revenue

        The diagonal line y=x represents perfect equality. The area between
        the diagonal and the curve equals half the Gini coefficient.

        Args:
            year: The selected analysis year
            countries: Optional country/region filter
            industries: Optional industry filter
            products: Optional product filter

        Returns:
            Table sorted ascending by exit_run_rate with columns:
            - customer_fraction: x [0, 1], fraction of customer base
            - cumulative_revenue_fraction: y [0, 1], cumulative revenue share
        """
        yearly = self._yearly_customer_exit_mrr(
            countries, industries, products
        )
        t = (
            yearly.filter(yearly.year == year)
            .mutate(
                exit_run_rate=(_.exit_mrr * ibis.literal(12.0)).cast('float64')
            )
            .filter(_.exit_run_rate > 0)
        )

        all_win = ibis.window()
        asc_cum_win = ibis.window(order_by='exit_run_rate', following=0)

        t = t.mutate(
            _n=t.exit_run_rate.count().over(all_win).cast('float64'),
            _total=t.exit_run_rate.sum().over(all_win).cast('float64'),
            # 1-based row number (ascending) for customer fraction
            _rn=(
                ibis.row_number().over(ibis.window(order_by='exit_run_rate'))
                + 1
            ).cast('float64'),
            _cum_rev=t.exit_run_rate.sum().over(asc_cum_win).cast('float64'),
        )

        return t.select(
            customer_fraction=(t._rn / t._n).cast('float64'),
            cumulative_revenue_fraction=(t._cum_rev / t._total).cast(
                'float64'
            ),
        ).order_by('customer_fraction')

    def segment_concentration(
        self,
        year: int,
        dimension: str,
        products: list[str] | None = None,
    ) -> ir.Table:
        """Concentration metrics grouped by a segment dimension.

        Answers: "Is revenue concentrated in a single country or vertical?"
        No dimension-level filter is applied (all segments are always shown).

        Args:
            year: The selected analysis year
            dimension: 'country' or 'industry'
            products: Optional product filter

        Returns:
            Table ordered by exit_run_rate descending with columns:
            - segment: dimension value (country name or industry)
            - exit_run_rate: total annualised revenue for segment
            - share_pct: percentage of total revenue [0, 100]
            - customer_count: distinct customers in segment
            - avg_revenue: exit_run_rate / customer_count

        Raises:
            ValueError: If dimension is not 'country'/'industry', or the
                       required schema column is not mapped.
        """
        m = self.mapping

        if dimension == 'country':
            dim_col = m.region
            col_name = 'country'
        elif dimension == 'industry':
            dim_col = m.industry
            col_name = 'industry'
        else:
            raise ValueError(
                f"dimension must be 'country' or 'industry', got {dimension!r}"
            )

        if not dim_col:
            mapped_field = 'region' if dimension == 'country' else 'industry'
            raise ValueError(
                f'Dimension {dimension!r} is not available: '
                f'ColumnMapping.{mapped_field} is not set for this cube.'
            )

        # No dimension filters — we want all segments visible
        yearly = self._yearly_customer_exit_mrr(products=products)
        t = (
            yearly.filter(yearly.year == year)
            .mutate(
                exit_run_rate=(_.exit_mrr * ibis.literal(12.0)).cast('float64')
            )
            .filter(_.exit_run_rate > 0)
        )

        seg_col = t[col_name]

        agg = t.group_by(segment=seg_col).aggregate(
            exit_run_rate=t.exit_run_rate.sum().cast('float64'),
            customer_count=t.customer_key.nunique().cast('int64'),
        )

        all_win = ibis.window()
        agg = agg.mutate(
            _total=agg.exit_run_rate.sum().over(all_win).cast('float64'),
        )

        return agg.select(
            segment=agg.segment,
            exit_run_rate=agg.exit_run_rate,
            share_pct=(
                agg.exit_run_rate / agg._total * ibis.literal(100.0)
            ).cast('float64'),
            customer_count=agg.customer_count,
            avg_revenue=(
                agg.exit_run_rate / agg.customer_count.cast('float64')
            ).cast('float64'),
        ).order_by(ibis.desc('exit_run_rate'))

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
