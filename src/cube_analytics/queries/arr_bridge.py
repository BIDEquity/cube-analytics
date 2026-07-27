"""ARR Bridge analysis using Ibis expressions.

Generates type-safe, injection-proof queries that work with
any Ibis backend (DuckDB, Polars, PostgreSQL, etc.)
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import ibis
from ibis import _
from ibis.expr import types as ir
from loguru import logger

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
    crosssell_mrr: float
    upsell_mrr: float
    downsell_mrr: float
    churn_mrr: float
    new_business_count: int
    crosssell_count: int
    upsell_count: int
    downsell_count: int
    churn_count: int
    # Per-product expansion split (docs/steering/kpi-expansion.md §3). Display-only:
    # NRR/GRR stay net-based. intra reconciles gross up+cross back to net expansion.
    intra_expansion_downsell_mrr: float = 0.0
    both_count: int = 0


class ARRBridgeQueries:
    """ARR Bridge analysis using Ibis expressions.

    Generates type-safe, injection-proof queries that work with
    any Ibis backend (DuckDB, Polars, PostgreSQL, etc.)

    Example:
        >>> import polars as pl
        >>> from cube_analytics import ARRBridgeQueries
        >>>
        >>> # From Polars DataFrame
        >>> df = pl.read_parquet('cube.parquet')
        >>> queries = ARRBridgeQueries.from_polars(df)
        >>>
        >>> # Build and inspect query
        >>> expr = queries.summary('2024-01', '2024-12')
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
        products: list[str] | None = None,
        entities: list[str] | None = None,
        include_all_revenue: bool = False,
        extra_filters: dict[str, list[str]] | None = None,
    ) -> ir.Table:
        """Apply base filters (recurring, region, industry, product, entity).

        Args:
            countries: Optional list of countries to filter
            industries: Optional list of industries to filter
            products: Optional list of products to filter
            entities: Optional list of entities to filter
            include_all_revenue: If True, skip the is_recurring filter
            extra_filters: Mapping of {column_name: [allowed values]} (or None)
                for arbitrary cube columns not in the standard mapping. A column
                that is not present in the table schema is silently ignored
                (no-op), mirroring the standard filters' "column absent" behaviour.

        Returns:
            Filtered Ibis table expression
        """
        t = self.table
        m = self.mapping

        # Filter to recurring revenue if column exists (unless all revenue requested)
        if m.is_recurring and not include_all_revenue:
            t = t.filter(t[m.is_recurring].isin([True, 1]))

        # Country filter
        if countries and m.region:
            t = t.filter(t[m.region].isin(countries))

        # Industry filter
        if industries and m.industry:
            t = t.filter(t[m.industry].isin(industries))

        # Product filter
        if products and m.product:
            t = t.filter(t[m.product].isin(products))

        # Entity filter
        if entities and m.entity:
            t = t.filter(t[m.entity].isin(entities))

        # Extra-dimension filters: arbitrary cube columns keyed by name.
        # Unknown columns are a silent no-op (column-existence check against the
        # ibis table schema), and empty value lists are skipped.
        for col, values in (extra_filters or {}).items():
            if values and col in t.columns:
                t = t.filter(t[col].isin(values))

        return t

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

    def _extra_string_cols(self) -> list[str]:
        """Return column names that are string type and not in the standard mapping.

        Result is cached after the first access since the table schema is fixed
        for the lifetime of an ARRBridgeQueries instance.

        Note: Only ``dtype.is_string()`` columns are included. This covers
        ``StringType`` (and ``LargeStringType`` on some backends). DATE,
        TIMESTAMP, numeric, and CATEGORICAL/ENUM types are intentionally
        excluded — CATEGORICAL support can be added if needed.
        """
        m = self.mapping
        standard_cols = {
            col
            for col in [
                m.period,
                m.customer,
                m.revenue,
                m.customer_id,
                m.product,
                m.is_recurring,
                m.region,
                m.industry,
                m.entity,
                m.price_increase_effect,
                m.contract_end_date,
            ]
            if col is not None
        }
        schema = self.table.schema()
        return [
            col
            for col, dtype in schema.items()
            if col not in standard_cols and dtype.is_string()
        ]

    def available_extra_dimensions(self) -> ir.Table:
        """Expression for extra categorical dimensions not in the standard mapping.

        Inspects the table schema for string columns that are not part of the
        standard ColumnMapping fields and returns a unified list of
        (dim_name, value) pairs suitable for building dynamic filter dropdowns.

        Returns:
            Ibis table with columns: dim_name (str), value (str).
            Contains DISTINCT values across all extra string columns.
            Empty table with the same schema if no extra dimensions exist.
        """
        extra_cols = self._extra_string_cols()

        if not extra_cols:
            return self.table.limit(0).select(
                dim_name=ibis.literal(None).cast('string'),
                value=ibis.literal(None).cast('string'),
            )

        parts = [
            self.table.select(
                dim_name=ibis.literal(col),
                value=self.table[col].cast('string'),
            )
            .filter(_.value.notnull() & (_.value != ''))
            .distinct()
            for col in extra_cols
        ]

        result = parts[0]
        for part in parts[1:]:
            result = result.union(part)

        return result.order_by(['dim_name', 'value'])

    def _build_customer_bridge(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
        entities: list[str] | None = None,
        extra_filters: dict[str, list[str]] | None = None,
        fx_aware: bool = False,
    ) -> ir.Table:
        """Build the customer bridge expression (shared by summary and customers).

        With ``fx_aware=True`` the bridge additionally carries each
        customer's FX effect and real (constant-currency) change per
        docs/steering/kpi-arr-bridge.md §4, and the movement classification
        runs on the real change instead of the nominal one — the same
        rules and thresholds, a different input. Currency movement then
        never lands in Upsell/Downsell. The nominal path (``fx_aware=False``,
        the default) is untouched.

        Returns table with columns:
        - customer_key
        - customer_name (if customer_id exists)
        - start_mrr
        - end_mrr
        - mrr_change
        - fx_effect, real_change (only when fx_aware)
        - movement_type
        """
        m = self.mapping
        t = self._base_table(
            countries,
            industries,
            products,
            entities,
            extra_filters=extra_filters,
        )

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
            'start_mrr': ibis.coalesce(start_mrr.s_mrr, 0).cast('float64'),
            'end_mrr': ibis.coalesce(end_mrr.e_mrr, 0).cast('float64'),
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

        # Full outer join
        bridge = (
            start_mrr.outer_join(
                end_mrr,
                start_mrr.s_customer_key == end_mrr.e_customer_key,
            )
            .select(**select_cols)
            .mutate(mrr_change=_.end_mrr - _.start_mrr)
        )

        if fx_aware:
            cust_fx = self._customer_fx_effect(
                start_month,
                end_month,
                countries,
                industries,
                products,
                entities,
                extra_filters=extra_filters,
            )
            bridge = (
                bridge.left_join(
                    cust_fx,
                    bridge.customer_key == cust_fx.fx_customer_key,
                )
                .drop('fx_customer_key')
                # A customer with no computable pair (or a cube without the
                # FX columns) has a real FX effect of 0, not a missing one.
                .mutate(
                    fx_effect=ibis.coalesce(_.fx_effect, 0.0).cast('float64')
                )
                .mutate(
                    real_change=(_.mrr_change - _.fx_effect).cast('float64')
                )
            )

        # The delta the movement classification below thresholds on: nominal
        # by default, the §4 real (constant-currency) change when fx_aware.
        change = _.real_change if fx_aware else _.mrr_change

        # Cross-sell detection: a customer who has MORE products at end_month
        # than at start_month is a cross-sell, regardless of product filter.
        has_product_filter = products is not None and m.product is not None

        if has_product_filter:
            # Product-filtered view: check if "new-to-product" customers
            # already existed with other products at start_month
            t_all = self._base_table(
                countries,
                industries,
                products=None,
                entities=entities,
                extra_filters=extra_filters,
            )
            period_month_all = self._period_to_month(t_all[m.period])
            unfiltered_start = (
                t_all.filter(period_month_all == start_month)
                .group_by(u_customer_key=t_all[group_col])
                .aggregate(u_start_mrr=t_all[m.revenue].sum())
            )
            bridge = (
                bridge.left_join(
                    unfiltered_start,
                    bridge.customer_key == unfiltered_start.u_customer_key,
                )
                .drop('u_customer_key')
                .mutate(
                    movement_type=ibis.cases(
                        # Existing customer expanding to this product
                        (
                            (_.start_mrr == 0)
                            & (_.end_mrr != 0)
                            & (ibis.coalesce(_.u_start_mrr, 0) > 0),
                            'Cross-sell',
                        ),
                        (
                            (_.start_mrr == 0) & (_.end_mrr != 0),
                            'New Business',
                        ),
                        ((_.start_mrr != 0) & (_.end_mrr == 0), 'Churn'),
                        (
                            (_.start_mrr != 0)
                            & (_.end_mrr != 0)
                            & (change > 1),
                            'Upsell',
                        ),
                        (
                            (_.start_mrr != 0)
                            & (_.end_mrr != 0)
                            & (change < -1),
                            'Downsell',
                        ),
                        else_='Unchanged',
                    ),
                )
                .drop('u_start_mrr')
            )
        elif m.product is not None:
            # Unfiltered view WITH product column: detect cross-sell by
            # comparing distinct product counts per customer at start vs end
            t_unfiltered = self._base_table(
                countries,
                industries,
                products=None,
                entities=entities,
                extra_filters=extra_filters,
            )
            period_month_uf = self._period_to_month(t_unfiltered[m.period])

            start_products = (
                t_unfiltered.filter(period_month_uf == start_month)
                .group_by(sp_customer_key=t_unfiltered[group_col])
                .aggregate(
                    start_product_count=t_unfiltered[m.product].nunique()
                )
            )
            end_products = (
                t_unfiltered.filter(period_month_uf == end_month)
                .group_by(ep_customer_key=t_unfiltered[group_col])
                .aggregate(end_product_count=t_unfiltered[m.product].nunique())
            )

            bridge = (
                bridge.left_join(
                    start_products,
                    bridge.customer_key == start_products.sp_customer_key,
                )
                .drop('sp_customer_key')
                .left_join(
                    end_products,
                    _.customer_key == end_products.ep_customer_key,
                )
                .drop('ep_customer_key')
                .mutate(
                    movement_type=ibis.cases(
                        (
                            (_.start_mrr == 0) & (_.end_mrr != 0),
                            'New Business',
                        ),
                        ((_.start_mrr != 0) & (_.end_mrr == 0), 'Churn'),
                        # Cross-sell: existing customer gained new products
                        (
                            (_.start_mrr != 0)
                            & (_.end_mrr != 0)
                            & (change > 1)
                            & (
                                ibis.coalesce(_.end_product_count, 0)
                                > ibis.coalesce(_.start_product_count, 0)
                            ),
                            'Cross-sell',
                        ),
                        (
                            (_.start_mrr != 0)
                            & (_.end_mrr != 0)
                            & (change > 1),
                            'Upsell',
                        ),
                        (
                            (_.start_mrr != 0)
                            & (_.end_mrr != 0)
                            & (change < -1),
                            'Downsell',
                        ),
                        else_='Unchanged',
                    ),
                )
                .drop('start_product_count', 'end_product_count')
            )
        else:
            # No product column at all: cross-sell cannot be detected
            bridge = bridge.mutate(
                movement_type=ibis.cases(
                    ((_.start_mrr == 0) & (_.end_mrr != 0), 'New Business'),
                    ((_.start_mrr != 0) & (_.end_mrr == 0), 'Churn'),
                    (
                        (_.start_mrr != 0) & (_.end_mrr != 0) & (change > 1),
                        'Upsell',
                    ),
                    (
                        (_.start_mrr != 0) & (_.end_mrr != 0) & (change < -1),
                        'Downsell',
                    ),
                    else_='Unchanged',
                ),
            )

        return bridge

    def _customer_fx_effect(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
        entities: list[str] | None = None,
        extra_filters: dict[str, list[str]] | None = None,
    ) -> ir.Table:
        """Per-customer FX effect summed over the customer's currency pairs.

        Applies the FX-effect definition from docs/steering/kpi-arr-bridge.md
        §4 to every pair served by :meth:`fx_decomposition`. A pair with any
        NULL input is non-computable and contributes nothing — never a
        defaulted rate or amount (no-defaulting rule, kpi-arr-bridge.md §4).
        A currency equal to the cube's target currency carries a real rate
        that does not move, so its FX effect is arithmetically zero.

        Returns one row per customer: (fx_customer_key, fx_effect).
        """
        pairs = self.fx_decomposition(
            start_month,
            end_month,
            countries,
            industries,
            products,
            entities,
            extra_filters=extra_filters,
        )
        computable = (
            _.start_amount.notnull()
            & _.end_amount.notnull()
            & _.start_rate.notnull()
            & _.end_rate.notnull()
        )
        pair_fx = ibis.cases(
            (computable, _.start_amount * (_.end_rate - _.start_rate)),
            else_=0.0,
        )
        return (
            pairs.mutate(pair_fx=pair_fx)
            .group_by(fx_customer_key=_.customer_key)
            .aggregate(fx_effect=_.pair_fx.sum().cast('float64'))
        )

    def _build_product_movements(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
        entities: list[str] | None = None,
        extra_filters: dict[str, list[str]] | None = None,
        fx_aware: bool = False,
    ) -> ir.Table:
        """Augment the customer bridge with the canonical per-customer-PRODUCT
        expansion split (docs/steering/kpi-expansion.md §3).

        Within each net-expanding customer (the bridge's 'Upsell'/'Cross-sell'
        set) the product footprint is decomposed at start-vs-end month MRR:
        - product held at both endpoints, delta > 1  -> gross_upsell
        - product new at end (no start row), end > 1  -> gross_crosssell (full amount)
        - product shrunk / dropped / dead-zone        -> folded into intra_expansion_downsell

        ``intra_expansion_downsell`` is the residual
        ``gross_upsell + gross_crosssell - mrr_change``. This keeps the bridge
        reconciling (begin + new + crosssell + upsell - intra - downsell - churn
        = end) and keeps NRR/GRR net-based and numerically unchanged — the split
        is display-only, so churn_kpis() still uses the net customer bridge. The
        residual equals literal product shrinkage plus any per-product dead-zone
        remainder and can be marginally negative in pathological sub-threshold
        cases (kept as-is; clamping would break the identity).

        movement_type gains a 'Cross+Up-Sell' value for customers that have both.

        With ``fx_aware=True`` the underlying bridge classifies on the real
        (constant-currency) change per docs/steering/kpi-arr-bridge.md §4, and
        the residual reconciles the gross legs back to that real change — the
        per-product gross split itself stays nominal (§4 defines the FX effect
        at customer grain, not product grain), so the residual also absorbs
        the within-expander grain difference.
        """
        m = self.mapping
        bridge = self._build_customer_bridge(
            start_month,
            end_month,
            countries,
            industries,
            products,
            entities,
            extra_filters=extra_filters,
            fx_aware=fx_aware,
        )
        is_expander = _.movement_type.isin(['Upsell', 'Cross-sell'])
        # The per-customer movement the gross legs must reconcile to: nominal
        # by default, the §4 real change when fx_aware.
        change = _.real_change if fx_aware else _.mrr_change

        # No product column: cross-sell cannot be detected, so all net expansion
        # is upsell (mirrors the upsell dashboard's no-product path).
        if m.product is None:
            return bridge.mutate(
                gross_upsell=ibis.cases((is_expander, change), else_=0.0).cast(
                    'float64'
                ),
                gross_crosssell=ibis.literal(0.0),
                intra_expansion_downsell=ibis.literal(0.0),
            )

        # Per-(customer, product) start/end MRR on the SAME filtered base table
        # as the customer bridge (shared grain guarantees sum(delta_p) == mrr_change).
        t = self._base_table(
            countries,
            industries,
            products,
            entities,
            extra_filters=extra_filters,
        )
        group_col = m.customer_id or m.customer
        period_month = self._period_to_month(t[m.period])

        start_pp = (
            t.filter(period_month == start_month)
            .group_by(pm_s_key=t[group_col], pm_s_prod=t[m.product])
            .aggregate(pm_s_mrr=t[m.revenue].sum())
        )
        end_pp = (
            t.filter(period_month == end_month)
            .group_by(pm_e_key=t[group_col], pm_e_prod=t[m.product])
            .aggregate(pm_e_mrr=t[m.revenue].sum())
        )

        # Full outer join so new-only and dropped-only products both survive.
        pp = start_pp.outer_join(
            end_pp,
            (start_pp.pm_s_key == end_pp.pm_e_key)
            & (start_pp.pm_s_prod == end_pp.pm_e_prod),
        ).select(
            pm_customer_key=ibis.coalesce(start_pp.pm_s_key, end_pp.pm_e_key),
            sp_raw=start_pp.pm_s_mrr,
            ep_raw=end_pp.pm_e_mrr,
        )
        pp = pp.mutate(
            sp=ibis.coalesce(_.sp_raw, 0).cast('float64'),
            ep=ibis.coalesce(_.ep_raw, 0).cast('float64'),
        ).mutate(
            # Held product (present at both endpoints) that grew past the dead zone.
            p_upsell=ibis.cases(
                (
                    _.sp_raw.notnull()
                    & _.ep_raw.notnull()
                    & ((_.ep - _.sp) > 1),
                    _.ep - _.sp,
                ),
                else_=0.0,
            ),
            # New product (no start row) above the dead zone -> full run-rate.
            p_crosssell=ibis.cases(
                (_.sp_raw.isnull() & (_.ep > 1), _.ep), else_=0.0
            ),
        )
        prod_agg = pp.group_by(pm_customer_key=_.pm_customer_key).aggregate(
            g_upsell=_.p_upsell.sum().cast('float64'),
            g_crosssell=_.p_crosssell.sum().cast('float64'),
        )

        augmented = (
            bridge.left_join(
                prod_agg, bridge.customer_key == prod_agg.pm_customer_key
            )
            .drop('pm_customer_key')
            .mutate(
                gross_upsell=ibis.cases(
                    (is_expander, ibis.coalesce(_.g_upsell, 0.0)), else_=0.0
                ).cast('float64'),
                gross_crosssell=ibis.cases(
                    (is_expander, ibis.coalesce(_.g_crosssell, 0.0)), else_=0.0
                ).cast('float64'),
            )
            .drop('g_upsell', 'g_crosssell')
        )
        return augmented.mutate(
            intra_expansion_downsell=ibis.cases(
                (
                    is_expander,
                    _.gross_upsell + _.gross_crosssell - change,
                ),
                else_=0.0,
            ).cast('float64'),
            movement_type=ibis.cases(
                (
                    is_expander
                    & (_.gross_upsell > 1)
                    & (_.gross_crosssell > 1),
                    'Cross+Up-Sell',
                ),
                (is_expander & (_.gross_crosssell > 1), 'Cross-sell'),
                (is_expander & (_.gross_upsell > 1), 'Upsell'),
                else_=_.movement_type,
            ),
        )

    def _elapsed_months(self, start_month: str, end_month: str) -> int:
        """Elapsed months between two YYYY-MM point-in-time snapshots.

        The ARR bridge compares MRR at two points in time (it does NOT sum
        flows over a range), so Feb 2025 → Feb 2026 is 12 elapsed months,
        not 13 inclusive buckets. This is what annualization factors
        (12 / elapsed) need. Floored at 1 to avoid division by zero when
        start == end.
        """
        s_year, s_mon = int(start_month[:4]), int(start_month[5:7])
        e_year, e_mon = int(end_month[:4]), int(end_month[5:7])
        return max((e_year - s_year) * 12 + (e_mon - s_mon), 1)

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
            # Return empty result using the actual table (limit 0) to avoid memtable
            # This generates valid SQL that works in any backend
            return self.table.limit(0).select(
                industry=ibis.literal(None).cast('string')
            )

        return (
            self.table.select(industry=self.table[m.industry])
            .filter(_.industry.notnull() & (_.industry != ''))
            .distinct()
            .order_by('industry')
        )

    def available_entities(self) -> ir.Table:
        """Expression for available entities.

        Returns:
            Ibis table with single 'entity' column, or empty if no entity column
        """
        m = self.mapping
        if not m.entity:
            return self.table.limit(0).select(
                entity=ibis.literal(None).cast('string')
            )

        return (
            self.table.select(entity=self.table[m.entity])
            .filter(_.entity.notnull() & (_.entity != ''))
            .distinct()
            .order_by('entity')
        )

    def summary(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
        entities: list[str] | None = None,
        extra_filters: dict[str, list[str]] | None = None,
    ) -> ir.Table:
        """Expression for ARR Bridge summary.

        Args:
            start_month: Start period (YYYY-MM format)
            end_month: End period (YYYY-MM format)
            countries: Optional list of countries to filter
            industries: Optional list of industries to filter
            products: Optional list of products to filter
            entities: Optional list of entities to filter
            extra_filters: Optional {column: [allowed values]} for arbitrary
                cube columns not in the standard mapping (unknown column = no-op)

        Returns:
            Ibis table with summary metrics (single row):
            - beginning_mrr, ending_mrr
            - new_business_mrr, upsell_mrr, downsell_mrr, churn_mrr
            - new_business_count, upsell_count, downsell_count, churn_count
        """
        # Source the per-customer-product expansion split (kpi-expansion.md §3).
        # crosssell/upsell are GROSS product-level sums; intra reconciles them
        # back to net so NRR/GRR (computed by churn_kpis on the net bridge) are
        # unchanged. A customer may contribute to both upsell and cross-sell.
        bridge = self._build_product_movements(
            start_month,
            end_month,
            countries,
            industries,
            products,
            entities,
            extra_filters=extra_filters,
        )

        # Aggregate summary metrics
        return bridge.aggregate(**self._summary_metrics(_.mrr_change))

    def _summary_metrics(self, change: ibis.Deferred) -> dict[str, Any]:
        """Aggregate kwargs shared by summary() and fx_aware_summary().

        ``change`` is the per-customer movement the downsell dollars sum
        over — nominal ``mrr_change`` for summary(), the §4 real change
        (docs/steering/kpi-arr-bridge.md) for fx_aware_summary().
        """
        return {
            'beginning_mrr': _.start_mrr.sum().cast('float64'),
            'ending_mrr': _.end_mrr.sum().cast('float64'),
            'new_business_mrr': (
                _.end_mrr * (_.movement_type == 'New Business').cast('int64')
            )
            .sum()
            .cast('float64'),
            'crosssell_mrr': _.gross_crosssell.sum().cast('float64'),
            'upsell_mrr': _.gross_upsell.sum().cast('float64'),
            'intra_expansion_downsell_mrr': (
                _.intra_expansion_downsell.sum().cast('float64')
            ),
            'downsell_mrr': (
                change * (_.movement_type == 'Downsell').cast('int64')
            )
            .sum()
            .cast('float64'),
            'churn_mrr': (
                _.start_mrr * (_.movement_type == 'Churn').cast('int64')
            )
            .sum()
            .cast('float64'),
            'new_business_count': (_.movement_type == 'New Business')
            .cast('int64')
            .sum()
            .cast('int64'),
            # upsell_count and crosssell_count overlap (a 'Cross+Up-Sell'
            # customer increments both); both_count exposes the overlap.
            'crosssell_count': (_.gross_crosssell > 1)
            .cast('int64')
            .sum()
            .cast('int64'),
            'upsell_count': (_.gross_upsell > 1)
            .cast('int64')
            .sum()
            .cast('int64'),
            'both_count': (_.movement_type == 'Cross+Up-Sell')
            .cast('int64')
            .sum()
            .cast('int64'),
            'downsell_count': (_.movement_type == 'Downsell')
            .cast('int64')
            .sum()
            .cast('int64'),
            'churn_count': (_.movement_type == 'Churn')
            .cast('int64')
            .sum()
            .cast('int64'),
        }

    def fx_aware_summary(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
        entities: list[str] | None = None,
        extra_filters: dict[str, list[str]] | None = None,
    ) -> ir.Table:
        """FX-aware ARR bridge summary: the constant-currency view.

        Served ALONGSIDE the nominal summary(), never instead of it. Each
        customer's movement is decomposed per docs/steering/kpi-arr-bridge.md
        §4: the total FX effect is its own component (``fx_effect_mrr``) and
        the movement classification runs on the real (constant-currency)
        change — same rules and code path as the nominal bridge, different
        input. Currency movement therefore never lands in the Upsell /
        Downsell components here. Beginning and ending stay the nominal
        balances, so beginning + fx_effect + movements reconciles to ending
        up to the same dead-zone residue the nominal bridge carries.

        NRR / GRR / churn_kpis() stay computed on the nominal bridge (their
        kpi-retention.md §3/§4 definitions) and never read this view.

        A cube without the FX enrichment columns serves ``fx_effect_mrr = 0``
        and components identical to summary().

        Returns:
            Single-row table: every summary() column plus ``fx_effect_mrr``.
        """
        bridge = self._build_product_movements(
            start_month,
            end_month,
            countries,
            industries,
            products,
            entities,
            extra_filters=extra_filters,
            fx_aware=True,
        )
        metrics = self._summary_metrics(_.real_change)
        metrics['fx_effect_mrr'] = _.fx_effect.sum().cast('float64')
        return bridge.aggregate(**metrics)

    def customers(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
        entities: list[str] | None = None,
        exclude_unchanged: bool = True,
        extra_filters: dict[str, list[str]] | None = None,
    ) -> ir.Table:
        """Expression for customer-level bridge details.

        Args:
            start_month: Start period (YYYY-MM format)
            end_month: End period (YYYY-MM format)
            countries: Optional list of countries to filter
            industries: Optional list of industries to filter
            products: Optional list of products to filter
            entities: Optional list of entities to filter
            exclude_unchanged: If True, exclude customers with no movement
            extra_filters: Optional {column: [allowed values]} for arbitrary
                cube columns not in the standard mapping (unknown column = no-op)

        Returns:
            Ibis table with customer rows:
            - customer_key, customer_name (if available)
            - start_mrr, end_mrr, mrr_change
            - movement_type ('Cross+Up-Sell' when a customer has both)
            - region, industry (if available)
        """
        bridge = self._build_product_movements(
            start_month,
            end_month,
            countries,
            industries,
            products,
            entities,
            extra_filters=extra_filters,
        )

        if exclude_unchanged:
            bridge = bridge.filter(_.movement_type != 'Unchanged')

        return bridge.order_by(_.mrr_change.abs().desc())

    def revenue_evolution(
        self,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
        entities: list[str] | None = None,
        extra_filters: dict[str, list[str]] | None = None,
    ) -> ir.Table:
        """Expression for monthly ARR evolution (for charts).

        Args:
            countries: Optional list of countries to filter
            industries: Optional list of industries to filter
            products: Optional list of products to filter
            entities: Optional list of entities to filter
            extra_filters: Optional {column: [allowed values]} for arbitrary
                cube columns not in the standard mapping (unknown column = no-op)

        Returns:
            Ibis table with columns:
            - month (YYYY-MM)
            - arr (annualized recurring revenue = MRR * 12)
        """
        m = self.mapping
        t = self._base_table(
            countries,
            industries,
            products,
            entities,
            extra_filters=extra_filters,
        )

        return (
            t.group_by(month=self._period_to_month(t[m.period]))
            .aggregate(arr=(t[m.revenue].sum() * 12).cast('float64'))
            .order_by('month')
        )

    def total_revenue_evolution(
        self,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
        entities: list[str] | None = None,
        extra_filters: dict[str, list[str]] | None = None,
    ) -> ir.Table:
        """Monthly revenue evolution with ALL revenue types, split by is_recurring.

        Returns:
            Ibis table with columns: month, is_recurring, revenue
        """
        m = self.mapping
        t = self._base_table(
            countries,
            industries,
            products,
            entities,
            include_all_revenue=True,
            extra_filters=extra_filters,
        )

        group_cols: dict[str, Any] = {
            'month': self._period_to_month(t[m.period]),
        }
        if m.is_recurring:
            group_cols['is_recurring'] = t[m.is_recurring].cast('boolean')

        result = (
            t.group_by(**group_cols)
            .aggregate(revenue=t[m.revenue].sum().cast('float64'))
            .order_by('month')
        )

        # Add is_recurring=true if column doesn't exist (all data is recurring)
        if not m.is_recurring:
            result = result.mutate(is_recurring=ibis.literal(True))

        return result

    def total_customer_monthly(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
        entities: list[str] | None = None,
        extra_filters: dict[str, list[str]] | None = None,
    ) -> ir.Table:
        """Customer × month revenue matrix with ALL revenue types.

        Returns:
            Ibis table with columns: customer, customer_key, month, revenue,
            is_recurring, product (None if not available), and optional region,
            industry columns.
        """
        m = self.mapping
        t = self._base_table(
            countries,
            industries,
            products,
            entities,
            include_all_revenue=True,
            extra_filters=extra_filters,
        )
        group_col = m.customer_id or m.customer
        period_month = self._period_to_month(t[m.period])

        t_filtered = t.filter(
            (period_month >= start_month) & (period_month <= end_month)
        )

        agg_cols: dict[str, Any] = {
            'revenue': t_filtered[m.revenue].sum().cast('float64'),
        }
        if m.customer_id:
            agg_cols['customer'] = t_filtered[m.customer].max()
        if m.region:
            agg_cols['region'] = t_filtered[m.region].max()
        if m.industry:
            agg_cols['industry'] = t_filtered[m.industry].max()
        extra_cols = self._extra_string_cols()
        if extra_cols:
            # .max() picks the alphabetically last value when multiple distinct
            # values exist for the same customer+month combination.  This is
            # intentional (deterministic, cheap), but silently resolves
            # data-quality issues.  Log once so operators are aware.
            logger.warning(
                'total_customer_monthly: aggregating {} extra string column(s) '
                'with .max() — multiple values per customer/month will be '
                'silently collapsed to the alphabetically last value: {}',
                len(extra_cols),
                extra_cols,
            )
        for _extra_col in extra_cols:
            agg_cols[_extra_col] = t_filtered[_extra_col].max()

        group_cols: dict[str, Any] = {
            'customer_key': t_filtered[group_col],
            'month': period_month,
        }
        if m.is_recurring:
            group_cols['is_recurring'] = t_filtered[m.is_recurring].cast(
                'boolean'
            )
        if m.product:
            group_cols['product'] = t_filtered[m.product]

        result = t_filtered.group_by(**group_cols).aggregate(**agg_cols)

        if not m.customer_id:
            result = result.mutate(customer=result.customer_key)
        if not m.is_recurring:
            result = result.mutate(is_recurring=ibis.literal(True))
        if not m.product:
            result = result.mutate(product=ibis.literal(None).cast('string'))

        return result.order_by(['customer_key', 'month'])

    def customer_monthly(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
        entities: list[str] | None = None,
        extra_filters: dict[str, list[str]] | None = None,
    ) -> ir.Table:
        """Expression for customer-level monthly MRR data.

        Returns a flat table with one row per customer per month,
        suitable for pivoting into a customer × month matrix.

        Args:
            start_month: Start period (inclusive, YYYY-MM format)
            end_month: End period (inclusive, YYYY-MM format)
            countries: Optional list of countries to filter
            industries: Optional list of industries to filter
            products: Optional list of products to filter
            entities: Optional list of entities to filter
            extra_filters: Optional {column: [allowed values]} for arbitrary
                cube columns not in the standard mapping (unknown column = no-op)

        Returns:
            Ibis table with columns:
            - customer (customer name)
            - customer_key (customer identifier)
            - month (YYYY-MM)
            - mrr (monthly recurring revenue)
            - product (if available, None otherwise)
            - region (if available)
            - industry (if available)
            - and any extra string columns not in the standard mapping
        """
        m = self.mapping
        t = self._base_table(
            countries,
            industries,
            products,
            entities,
            extra_filters=extra_filters,
        )

        # Column to group customers by (prefer ID over name)
        group_col = m.customer_id or m.customer
        period_month = self._period_to_month(t[m.period])

        # Filter to the period range
        t_filtered = t.filter(
            (period_month >= start_month) & (period_month <= end_month)
        )

        # Build aggregation columns
        agg_cols: dict[str, Any] = {
            'mrr': t_filtered[m.revenue].sum().cast('float64'),
        }

        # Add customer name if we have a separate ID column
        if m.customer_id:
            agg_cols['customer'] = t_filtered[m.customer].max()

        # Add optional columns
        if m.region:
            agg_cols['region'] = t_filtered[m.region].max()
        if m.industry:
            agg_cols['industry'] = t_filtered[m.industry].max()

        # Extra string cols via .max() — same semantics as total_customer_monthly
        for _extra_col in self._extra_string_cols():
            agg_cols[_extra_col] = t_filtered[_extra_col].max()

        # Group by customer, month, and product (for parity with total_customer_monthly)
        group_by_kwargs: dict[str, Any] = {
            'customer_key': t_filtered[group_col],
            'month': period_month,
        }
        if m.product:
            group_by_kwargs['product'] = t_filtered[m.product]

        result = t_filtered.group_by(**group_by_kwargs).aggregate(**agg_cols)

        # If no separate customer_id, use customer_key as customer name too
        if not m.customer_id:
            result = result.mutate(customer=result.customer_key)
        if not m.product:
            result = result.mutate(product=ibis.literal(None).cast('string'))

        return result.order_by(['customer_key', 'month'])

    def price_increase_effect(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
        entities: list[str] | None = None,
        extra_filters: dict[str, list[str]] | None = None,
    ) -> ir.Table:
        """Expression for cumulative price increase effect.

        Sums the price_increase_effect column across all months
        between start (exclusive) and end (inclusive).

        Args:
            start_month: Start period (exclusive, YYYY-MM format)
            end_month: End period (inclusive, YYYY-MM format)
            countries: Optional list of countries to filter
            industries: Optional list of industries to filter
            products: Optional list of products to filter
            entities: Optional list of entities to filter
            extra_filters: Optional {column: [allowed values]} for arbitrary
                cube columns not in the standard mapping (unknown column = no-op)

        Returns:
            Ibis table with single column: price_increase_effect_total
            Returns 0 if no price_increase_effect column exists
        """
        m = self.mapping

        if not m.price_increase_effect:
            # Return 0 using the actual table to avoid memtable
            # Use aggregate with literal to generate valid SQL
            return self.table.aggregate(
                price_increase_effect_total=ibis.literal(0.0).cast('float64')
            )

        t = self._base_table(
            countries,
            industries,
            products,
            entities,
            extra_filters=extra_filters,
        )
        period_month = self._period_to_month(t[m.period])

        return t.filter(
            (period_month > start_month) & (period_month <= end_month)
        ).aggregate(
            price_increase_effect_total=ibis.coalesce(
                t[m.price_increase_effect].sum(), 0
            ).cast('float64')
        )

    def fx_decomposition(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
        entities: list[str] | None = None,
        extra_filters: dict[str, list[str]] | None = None,
    ) -> ir.Table:
        """Expression for per-customer, per-original-currency FX raw inputs.

        Serves the start-month and end-month original amount and applied
        exchange rate for every customer/original-currency pair — the raw
        inputs consumed by the FX-effect decomposition defined in
        docs/steering/kpi-arr-bridge.md §4. No effect is computed here.

        A pair present in only one of the two months keeps the other side's
        amount and rate NULL. A NULL original currency stays its own NULL
        group — it is never folded into the cube's target currency — and a
        month-side with any missing rate or amount serves NULL for that
        side, never a defaulted rate or a partial sum (no-defaulting rule,
        kpi-arr-bridge.md §4).

        Args:
            start_month: Start period (inclusive, YYYY-MM format)
            end_month: End period (inclusive, YYYY-MM format)
            countries: Optional list of countries to filter
            industries: Optional list of industries to filter
            products: Optional list of products to filter
            entities: Optional list of entities to filter
            extra_filters: Optional {column: [allowed values]} for arbitrary
                cube columns not in the standard mapping (unknown column = no-op)

        Returns:
            Ibis table with one row per customer per original currency:
            - customer (customer name)
            - customer_key (customer identifier)
            - currency (original currency of the pair; NULL stays NULL)
            - start_amount (start-month original amount, NULL when absent)
            - end_amount (end-month original amount, NULL when absent)
            - start_rate (start-month applied rate, NULL when absent)
            - end_rate (end-month applied rate, NULL when absent)
            Returns no rows when the cube lacks the FX enrichment columns
        """
        m = self.mapping

        if not (m.currency and m.exchange_rate and m.original_amount):
            # No FX enrichment on this cube: serve the full schema with zero
            # rows rather than inventing a currency or a rate.
            return self.table.select(
                customer=ibis.literal(None).cast('string'),
                customer_key=ibis.literal(None).cast('string'),
                currency=ibis.literal(None).cast('string'),
                start_amount=ibis.literal(None).cast('float64'),
                end_amount=ibis.literal(None).cast('float64'),
                start_rate=ibis.literal(None).cast('float64'),
                end_rate=ibis.literal(None).cast('float64'),
            ).limit(0)

        t = self._base_table(
            countries,
            industries,
            products,
            entities,
            extra_filters=extra_filters,
        )
        group_col = m.customer_id or m.customer
        period_month = self._period_to_month(t[m.period])
        t_f = t.filter(period_month.isin([start_month, end_month]))

        month = self._period_to_month(t_f[m.period])
        is_start = month == start_month
        is_end = month == end_month

        def month_side(
            in_month: ir.BooleanValue,
            col: ir.Column,
            reduced: ir.NumericScalar,
        ) -> ir.Value:
            # A month-side is NULL when the pair is absent that month OR any
            # of its rows is missing the value — a partial sum or a
            # NULL-skipping max would make an incompletely populated pair
            # look compliant (kpi-arr-bridge.md §4 refuses partial FX data).
            side_rows = t_f.count(where=in_month)
            return ibis.ifelse(
                (side_rows > 0) & (col.count(where=in_month) == side_rows),
                reduced,
                ibis.literal(None),
            ).cast('float64')

        amount = t_f[m.original_amount]
        rate = t_f[m.exchange_rate]
        agg_cols: dict[str, Any] = {
            'start_amount': month_side(
                is_start, amount, amount.sum(where=is_start)
            ),
            'end_amount': month_side(is_end, amount, amount.sum(where=is_end)),
            # The applied rate is uniform per currency-month by construction;
            # rate consistency is judged by the FX-readiness classifier, not
            # re-verified here, so max() only collapses identical values.
            'start_rate': month_side(is_start, rate, rate.max(where=is_start)),
            'end_rate': month_side(is_end, rate, rate.max(where=is_end)),
        }
        if m.customer_id:
            agg_cols['customer'] = t_f[m.customer].max()

        result = t_f.group_by(
            customer_key=t_f[group_col],
            currency=t_f[m.currency],
        ).aggregate(**agg_cols)

        if not m.customer_id:
            result = result.mutate(customer=result.customer_key)

        return result.order_by(['customer_key', 'currency'])

    def churn_kpis(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
        entities: list[str] | None = None,
        extra_filters: dict[str, list[str]] | None = None,
    ) -> ir.Table:
        """Aggregated churn KPIs from the ARR bridge.

        Returns a single-row table with churn metrics and retention rates.
        Uses CAGR for annualizing retention rates.

        Canonical definitions: docs/steering/kpi-retention.md §3 (NRR) and §4
        (GRR, capped at 100% via ibis.least to match UI page.tsx Math.min). This
        is the "Portfolio Point-in-Time" variant; do not synthesise the formula
        from general knowledge (see PIR 2026-04-14 NRR miscalculation).

        Args:
            start_month: Start period (YYYY-MM format)
            end_month: End period (YYYY-MM format)
            countries: Optional list of countries to filter
            industries: Optional list of industries to filter
            products: Optional list of products to filter
            entities: Optional list of entities to filter
            extra_filters: Optional {column: [allowed values]} for arbitrary
                cube columns not in the standard mapping (unknown column = no-op)

        Returns:
            Ibis table with single row:
            - beginning_arr, churn_arr, churn_count
            - churn_rate (annualized, linear)
            - gross_churn_rate (GRR via CAGR)
            - net_revenue_retention (NRR via CAGR)
        """
        bridge = self._build_customer_bridge(
            start_month,
            end_month,
            countries,
            industries,
            products,
            entities,
            extra_filters=extra_filters,
        )
        months = self._elapsed_months(start_month, end_month)
        ann_factor = ibis.literal(12.0 / months)

        raw = bridge.aggregate(
            beginning_arr=(_.start_mrr.sum() * 12).cast('float64'),
            churn_arr=(
                (
                    _.start_mrr * (_.movement_type == 'Churn').cast('int64')
                ).sum()
                * 12
            ).cast('float64'),
            churn_count=(
                (_.movement_type == 'Churn').cast('int64').sum()
            ).cast('int64'),
            upsell_arr=(
                (
                    _.mrr_change * (_.movement_type == 'Upsell').cast('int64')
                ).sum()
                * 12
            ).cast('float64'),
            crosssell_arr=(
                (
                    _.mrr_change
                    * (_.movement_type == 'Cross-sell').cast('int64')
                ).sum()
                * 12
            ).cast('float64'),
            contraction_arr=(
                (
                    _.mrr_change.abs()
                    * (_.movement_type == 'Downsell').cast('int64')
                ).sum()
                * 12
            ).cast('float64'),
        )

        return (
            raw.mutate(
                churn_rate=ibis.cases(
                    (
                        _.beginning_arr > 0,
                        _.churn_arr / _.beginning_arr * ann_factor * 100,
                    ),
                    else_=ibis.literal(0.0),
                ).cast('float64'),
                grr_ratio=ibis.cases(
                    (
                        _.beginning_arr > 0,
                        (_.beginning_arr - _.contraction_arr - _.churn_arr)
                        / _.beginning_arr,
                    ),
                    else_=ibis.literal(0.0),
                ),
                nrr_ratio=ibis.cases(
                    (
                        _.beginning_arr > 0,
                        (
                            _.beginning_arr
                            + _.upsell_arr
                            + _.crosssell_arr
                            - _.contraction_arr
                            - _.churn_arr
                        )
                        / _.beginning_arr,
                    ),
                    else_=ibis.literal(0.0),
                ),
            )
            .mutate(
                gross_churn_rate=ibis.cases(
                    (
                        _.grr_ratio > 0,
                        ibis.least(
                            ibis.literal(100.0),
                            (_.grr_ratio**ann_factor) * 100,
                        ),
                    ),
                    else_=ibis.literal(0.0),
                ).cast('float64'),
                net_revenue_retention=ibis.cases(
                    (_.nrr_ratio > 0, (_.nrr_ratio**ann_factor) * 100),
                    else_=ibis.literal(0.0),
                ).cast('float64'),
            )
            .drop(
                'upsell_arr',
                'crosssell_arr',
                'contraction_arr',
                'grr_ratio',
                'nrr_ratio',
            )
        )

    def churn_customer_table(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
        entities: list[str] | None = None,
        extra_filters: dict[str, list[str]] | None = None,
    ) -> ir.Table:
        """Per-customer churn detail from the ARR bridge.

        Filters the customer bridge to churned customers and enriches
        each row with tenure information from the raw data.

        Args:
            start_month: Start period (YYYY-MM format)
            end_month: End period (YYYY-MM format)
            countries: Optional list of countries to filter
            industries: Optional list of industries to filter
            products: Optional list of products to filter
            entities: Optional list of entities to filter
            extra_filters: Optional {column: [allowed values]} for arbitrary
                cube columns not in the standard mapping (unknown column = no-op)

        Returns:
            Ibis table with columns:
            - customer_name
            - start_arr, end_arr, arr_change
            - last_active_month (YYYY-MM)
            - tenure_months
        """
        m = self.mapping
        bridge = self._build_customer_bridge(
            start_month,
            end_month,
            countries,
            industries,
            products,
            entities,
            extra_filters=extra_filters,
        )
        churned = bridge.filter(_.movement_type == 'Churn')

        # Compute first/last active months per customer from raw data
        t = self._base_table(
            countries,
            industries,
            products,
            entities,
            extra_filters=extra_filters,
        )
        group_col = m.customer_id or m.customer
        period_month = self._period_to_month(t[m.period])

        activity = (
            t.filter(t[m.revenue] > 0)
            .group_by(a_customer_key=t[group_col])
            .aggregate(
                first_active_month=period_month.min(),
                last_active_month=period_month.max(),
            )
        )

        joined = churned.left_join(
            activity,
            churned.customer_key == activity.a_customer_key,
        ).drop('a_customer_key')

        return joined.select(
            customer_name=(
                joined.customer_name if m.customer_id else joined.customer_key
            ),
            start_arr=(joined.start_mrr * 12).cast('float64'),
            end_arr=(joined.end_mrr * 12).cast('float64'),
            arr_change=(joined.mrr_change * 12).cast('float64'),
            last_active_month=joined.last_active_month,
            tenure_months=(
                (
                    joined.last_active_month.substr(0, 4).cast('int64')
                    - joined.first_active_month.substr(0, 4).cast('int64')
                )
                * 12
                + (
                    joined.last_active_month.substr(5, 2).cast('int64')
                    - joined.first_active_month.substr(5, 2).cast('int64')
                )
            ).cast('int64'),
        ).order_by(_.arr_change)

    def waterfall_data(
        self,
        start_month: str,
        end_month: str,
        countries: list[str] | None = None,
        industries: list[str] | None = None,
        products: list[str] | None = None,
        entities: list[str] | None = None,
        display_mode: str = 'absolute',
        extra_filters: dict[str, list[str]] | None = None,
    ) -> ir.Table:
        """Waterfall chart segments for the ARR bridge.

        Returns a single-row table with ordered waterfall segments.
        Handles price increase/decrease decomposition when the
        price_increase_effect column exists.

        Args:
            start_month: Start period (YYYY-MM format)
            end_month: End period (YYYY-MM format)
            countries: Optional list of countries to filter
            industries: Optional list of industries to filter
            products: Optional list of products to filter
            display_mode: 'absolute' (ARR values), 'percent' (% of beginning),
                or 'percent_annualized' (annualized % with CAGR ending)
            extra_filters: Optional {column: [allowed values]} for arbitrary
                cube columns not in the standard mapping (unknown column = no-op)

        Returns:
            Ibis table with single row:
            - beginning_arr, new_business, crosssell
            - upsell_organic, upsell_price
            - downsell_organic, downsell_price
            - churn, ending_arr
        """
        bridge = self._build_customer_bridge(
            start_month,
            end_month,
            countries,
            industries,
            products,
            entities,
            extra_filters=extra_filters,
        )
        months = self._elapsed_months(start_month, end_month)
        ann_factor = ibis.literal(12.0 / months)

        summary = bridge.aggregate(
            beginning_mrr=_.start_mrr.sum().cast('float64'),
            ending_mrr=_.end_mrr.sum().cast('float64'),
            new_business_mrr=(
                _.end_mrr * (_.movement_type == 'New Business').cast('int64')
            )
            .sum()
            .cast('float64'),
            crosssell_mrr=(
                _.mrr_change * (_.movement_type == 'Cross-sell').cast('int64')
            )
            .sum()
            .cast('float64'),
            upsell_mrr=(
                _.mrr_change * (_.movement_type == 'Upsell').cast('int64')
            )
            .sum()
            .cast('float64'),
            downsell_mrr=(
                _.mrr_change * (_.movement_type == 'Downsell').cast('int64')
            )
            .sum()
            .cast('float64'),
            churn_mrr=(
                _.start_mrr * (_.movement_type == 'Churn').cast('int64')
            )
            .sum()
            .cast('float64'),
        )

        pie = self.price_increase_effect(
            start_month,
            end_month,
            countries,
            industries,
            products,
            entities,
            extra_filters=extra_filters,
        )
        combined = summary.cross_join(pie).mutate(
            price_up=ibis.cases(
                (
                    _.price_increase_effect_total > 0,
                    _.price_increase_effect_total,
                ),
                else_=ibis.literal(0.0),
            ).cast('float64'),
            price_down=ibis.cases(
                (
                    _.price_increase_effect_total < 0,
                    _.price_increase_effect_total,
                ),
                else_=ibis.literal(0.0),
            ).cast('float64'),
        )

        waterfall = combined.select(
            beginning_arr=(_.beginning_mrr * 12).cast('float64'),
            new_business=(_.new_business_mrr * 12).cast('float64'),
            crosssell=(_.crosssell_mrr * 12).cast('float64'),
            upsell_organic=((_.upsell_mrr - _.price_up) * 12).cast('float64'),
            upsell_price=(_.price_up * 12).cast('float64'),
            downsell_organic=((_.downsell_mrr - _.price_down) * 12).cast(
                'float64'
            ),
            downsell_price=(_.price_down * 12).cast('float64'),
            churn=(_.churn_mrr * -12).cast('float64'),
            ending_arr=(_.ending_mrr * 12).cast('float64'),
        )

        if display_mode == 'absolute':
            return waterfall

        # Percent modes: compute scaling factor and transform
        if display_mode == 'percent_annualized':
            factor_expr = ibis.literal(100.0) / _.beginning_arr * ann_factor
            ending_expr = ibis.cases(
                (
                    (_.beginning_arr > 0) & (_.ending_arr > 0),
                    (_.ending_arr / _.beginning_arr) ** ann_factor * 100,
                ),
                else_=ibis.literal(100.0),
            ).cast('float64')
        else:
            factor_expr = ibis.literal(100.0) / _.beginning_arr
            ending_expr = ibis.cases(
                (_.beginning_arr > 0, _.ending_arr / _.beginning_arr * 100),
                else_=ibis.literal(100.0),
            ).cast('float64')

        pct = waterfall.mutate(
            pct_factor=ibis.cases(
                (_.beginning_arr > 0, factor_expr),
                else_=ibis.literal(0.0),
            ).cast('float64'),
        )

        return pct.select(
            beginning_arr=ibis.literal(100.0).cast('float64'),
            new_business=(_.new_business * _.pct_factor).cast('float64'),
            crosssell=(_.crosssell * _.pct_factor).cast('float64'),
            upsell_organic=(_.upsell_organic * _.pct_factor).cast('float64'),
            upsell_price=(_.upsell_price * _.pct_factor).cast('float64'),
            downsell_organic=(_.downsell_organic * _.pct_factor).cast(
                'float64'
            ),
            downsell_price=(_.downsell_price * _.pct_factor).cast('float64'),
            churn=(_.churn * _.pct_factor).cast('float64'),
            ending_arr=ending_expr,
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
        products: list[str] | None = None,
        entities: list[str] | None = None,
    ) -> BridgeSummary:
        """Convenience method: execute summary and return typed result.

        Args:
            start_month: Start period (YYYY-MM format)
            end_month: End period (YYYY-MM format)
            countries: Optional list of countries to filter
            industries: Optional list of industries to filter
            products: Optional list of products to filter
            entities: Optional list of entities to filter

        Returns:
            BridgeSummary dataclass with results
        """
        expr = self.summary(
            start_month, end_month, countries, industries, products, entities
        )
        df = self.execute(expr)
        row = df.row(0, named=True)

        return BridgeSummary(
            beginning_mrr=float(row['beginning_mrr'] or 0),
            ending_mrr=float(row['ending_mrr'] or 0),
            new_business_mrr=float(row['new_business_mrr'] or 0),
            crosssell_mrr=float(row['crosssell_mrr'] or 0),
            upsell_mrr=float(row['upsell_mrr'] or 0),
            downsell_mrr=float(row['downsell_mrr'] or 0),
            churn_mrr=float(row['churn_mrr'] or 0),
            intra_expansion_downsell_mrr=float(
                row['intra_expansion_downsell_mrr'] or 0
            ),
            new_business_count=int(row['new_business_count'] or 0),
            crosssell_count=int(row['crosssell_count'] or 0),
            upsell_count=int(row['upsell_count'] or 0),
            both_count=int(row['both_count'] or 0),
            downsell_count=int(row['downsell_count'] or 0),
            churn_count=int(row['churn_count'] or 0),
        )
