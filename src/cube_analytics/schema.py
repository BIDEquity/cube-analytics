"""Column mapping detection for cube schemas.

Provides flexible column detection that handles different naming conventions
across various cube outputs. This is the single source of truth for column
name candidates across the entire codebase.
"""

import json
from dataclasses import asdict, dataclass
from typing import Any, Sequence

# Column name candidates - SINGLE SOURCE OF TRUTH
# Order matters: first match wins during detection
COLUMN_CANDIDATES = {
    'period': ['month', 'month_date', 'period', 'date'],
    'customer': ['group_level', 'customer', 'customer_name', 'name'],
    'revenue': ['revenue', 'amount', 'value', 'mrr'],
    'customer_id': ['group_level_id', 'customer_id', 'id'],
    # Second level of the customer hierarchy (the lowest billed entity).
    # JustRelate splits a group_level customer across many buying_centers;
    # detection/corrections happen at this sub-level when present.
    'sub_customer_id': [
        'sub_customer_id',
        'buying_center_id',
        'subcustomer_id',
        'sub_account_id',
    ],
    # Note: revenue_type is NOT a product - it's a recurring sub-type (see below)
    'product': ['product', 'product_name', 'segment'],
    # Recurring vs one-time flag. `revenue_type` is kept as a trailing fallback
    # for legacy cubes that encode the flag in that column (e.g. 'Rec'/'Nonrec')
    # with no dedicated boolean; when a real `is_recurring` boolean exists it
    # wins, and `revenue_type` is then a recurring sub-type (see below).
    'is_recurring': ['is_recurring', 'recurring', 'revenue_type'],
    # Recurring sub-type column (e.g. SaaS / Maintenance for JustRelate, or the
    # 'Rec'/'Nonrec' flag itself when a cube reuses one column for both). When
    # present it joins the detection/correction grain (resolved_grain) so distinct
    # recurring streams form SEPARATE series instead of being summed into one. The
    # `is_recurring` filter still runs first, so non-recurring sub-types
    # (Non-Recurring, Other) are excluded from detection and corrections.
    'revenue_type': ['revenue_type', 'rev_type', 'revenue_category'],
    'region': ['region', 'geography', 'location', 'country'],
    'industry': ['industry', 'sector', 'vertical'],
    'entity': ['entity'],
    'price_increase_effect': [
        'price_increase_effect_absolute',
        'price_increase_effect',
        'price_effect',
    ],
    'contract_end_date': ['contract_end_date', 'contract_end', 'end_date'],
    'cohort': ['cohort'],
    'pricing_segment': ['pricing_segment', 'price_segment'],
    # Canonical FX enrichment columns (cube-data-contract.md §4.5). Exactly
    # one candidate each, on purpose: the FX-readiness classifier
    # (command_center.domain.cube_fx_capability) and its probe look these up
    # by the same exact names, so an alias accepted here would let detection
    # claim a column the readiness verdict refuses. Never add name variants.
    'currency': ['currency'],
    'exchange_rate': ['exchange_rate'],
    'target_currency': ['target_currency'],
    'original_amount': ['original_amount'],
}


def find_column(
    candidates: list[str],
    available: Sequence[str],
) -> str | None:
    """Find first matching column name (case-insensitive).

    Args:
        candidates: List of possible column names to try (in priority order)
        available: List of available column names

    Returns:
        The matched column name (with original case), or None if no match
    """
    lower_available = [c.lower() for c in available]
    for candidate in candidates:
        if candidate.lower() in lower_available:
            idx = lower_available.index(candidate.lower())
            return available[idx]  # Return original case
    return None


@dataclass(frozen=True)
class ColumnMapping:
    """Detected column mapping for cube schema.

    All column names are validated to exist in the source schema.
    Required columns (period, customer, revenue) raise errors if missing.
    Optional columns are set to None if not found.

    This is the single source of truth for column name detection
    across the entire codebase (QA detection, ARR bridge, etc.).

    Attributes:
        period: Column containing the time period (month/date)
        customer: Column containing customer name/identifier
        revenue: Column containing revenue/MRR values
        customer_id: Optional unique customer ID for joining
        product: Optional product/segment column for multi-product cubes
        is_recurring: Optional flag for recurring vs one-time revenue
        region: Optional geographic region/country
        industry: Optional industry/sector classification
        entity: Optional legal entity / reporting entity column
        price_increase_effect: Optional price change impact column
        contract_end_date: Optional contract end date column (enables Revenue at Risk tab)
        cohort: Optional acquisition cohort column (Crisalix-specific, enables Segments tab)
        pricing_segment: Optional pricing segment column (Crisalix-specific, enables Segments tab)
        currency: Optional original (pre-conversion) currency of each row
        exchange_rate: Optional rate applied to convert the row into the target currency
        target_currency: Optional currency the revenue column is stated in
        original_amount: Optional pre-conversion amount in the row's original currency
    """

    period: str
    customer: str
    revenue: str
    customer_id: str | None = None
    # Second customer-hierarchy level (e.g. buying_center_id). When set, it is
    # the detection/correction grain; the chart drills customer -> sub_customer.
    sub_customer_id: str | None = None
    product: str | None = None
    is_recurring: str | None = None
    # Recurring sub-type column (e.g. SaaS / Maintenance). When present it joins
    # resolved_grain() so detection, rowids, and corrections key off it — keeping
    # distinct recurring streams separate instead of summed. May resolve to the
    # same column as `is_recurring` on cubes that encode the flag here.
    revenue_type: str | None = None
    region: str | None = None
    industry: str | None = None
    entity: str | None = None
    price_increase_effect: str | None = None
    contract_end_date: str | None = None
    cohort: str | None = None
    pricing_segment: str | None = None
    # Canonical FX enrichment columns (cube-data-contract.md §4.5). A cube
    # without them keeps all four None — a gap is never filled with a
    # plausible default, so a cube without FX facts is visibly without them.
    currency: str | None = None
    exchange_rate: str | None = None
    target_currency: str | None = None
    original_amount: str | None = None
    # Ordered list of column names forming the cube's unique row key (its
    # grain), period first. None → resolved_grain() falls back to the legacy
    # [period, customer, product] default, preserving rowids for cubes that
    # never declared a grain. Tenants whose cube is finer (e.g. JustRelate's
    # buying_center split) declare the full source grain here so detection,
    # issue keys, and correction writeback all key off the lowest grain.
    grain: list[str] | None = None

    def resolved_grain(self) -> list[str]:
        """Effective grain (ordered key columns), period first.

        Explicit `grain` wins. Otherwise default to the legacy natural key:
        [period, customer_id-or-customer, sub_customer?, product?]. This default
        reproduces the historical `period_customer_product` rowid, so cubes that
        never declared a grain keep byte-identical rowids.

        `revenue_type` (a recurring sub-type such as SaaS / Maintenance) is always
        appended last when the cube has the column — even on top of an explicit
        grain — so distinct recurring streams never collapse into one series.
        Cubes without the column are unaffected (rowids unchanged).
        """
        if self.grain:
            base = list(self.grain)
        else:
            base = [self.period, self.customer_id or self.customer]
            if self.sub_customer_id:
                base.append(self.sub_customer_id)
            if self.product:
                base.append(self.product)
        # period stays first (series_key = grain minus period); revenue_type last.
        if self.revenue_type and self.revenue_type not in base:
            base.append(self.revenue_type)
        return base

    def to_json(self) -> str:
        """Serialize column mapping to JSON string for database storage."""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, json_str: str) -> 'ColumnMapping':
        """Deserialize column mapping from JSON string.

        Args:
            json_str: JSON string from database storage

        Returns:
            ColumnMapping instance
        """
        data = json.loads(json_str)
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API responses."""
        return asdict(self)

    @classmethod
    def detect(cls, columns: Sequence[str]) -> 'ColumnMapping':
        """Auto-detect column mapping from available column names.

        Uses case-insensitive matching with priority ordering.
        First matching candidate wins. Column candidates are defined
        in COLUMN_CANDIDATES constant.

        Args:
            columns: List of available column names

        Returns:
            ColumnMapping with detected column names

        Raises:
            ValueError: If required columns (period, customer, revenue)
                       cannot be detected
        """
        # Detect required columns
        period = find_column(COLUMN_CANDIDATES['period'], columns)
        customer = find_column(COLUMN_CANDIDATES['customer'], columns)
        revenue = find_column(COLUMN_CANDIDATES['revenue'], columns)

        # Validate required columns exist
        missing = []
        if not period:
            missing.append('period (tried: month, month_date, period, date)')
        if not customer:
            missing.append(
                'customer (tried: group_level, customer, customer_name, name)'
            )
        if not revenue:
            missing.append('revenue (tried: revenue, amount, value, mrr)')

        if missing:
            raise ValueError(
                f'Could not detect required columns: {", ".join(missing)}. '
                f'Available columns: {list(columns)}'
            )

        # Detect optional columns using centralized candidate lists
        return cls(
            period=period,  # type: ignore (validated above)
            customer=customer,  # type: ignore
            revenue=revenue,  # type: ignore
            customer_id=find_column(COLUMN_CANDIDATES['customer_id'], columns),
            # sub_customer_id is OPT-IN, never auto-detected: a cube may carry
            # a buying_center_id column yet still be analysed at customer grain
            # (e.g. JustRelate). Set it explicitly in the stored mapping (or set
            # `grain`) to detect/correct at the finer level.
            product=find_column(COLUMN_CANDIDATES['product'], columns),
            is_recurring=find_column(
                COLUMN_CANDIDATES['is_recurring'], columns
            ),
            revenue_type=find_column(
                COLUMN_CANDIDATES['revenue_type'], columns
            ),
            region=find_column(COLUMN_CANDIDATES['region'], columns),
            industry=find_column(COLUMN_CANDIDATES['industry'], columns),
            entity=find_column(COLUMN_CANDIDATES['entity'], columns),
            price_increase_effect=find_column(
                COLUMN_CANDIDATES['price_increase_effect'], columns
            ),
            contract_end_date=find_column(
                COLUMN_CANDIDATES['contract_end_date'], columns
            ),
            cohort=find_column(COLUMN_CANDIDATES['cohort'], columns),
            pricing_segment=find_column(
                COLUMN_CANDIDATES['pricing_segment'], columns
            ),
            currency=find_column(COLUMN_CANDIDATES['currency'], columns),
            exchange_rate=find_column(
                COLUMN_CANDIDATES['exchange_rate'], columns
            ),
            target_currency=find_column(
                COLUMN_CANDIDATES['target_currency'], columns
            ),
            original_amount=find_column(
                COLUMN_CANDIDATES['original_amount'], columns
            ),
        )

    def validate_columns_exist(self, available: Sequence[str]) -> None:
        """Validate that all mapped columns exist in the available columns.

        Args:
            available: List of actually available column names

        Raises:
            ValueError: If any mapped column doesn't exist
        """
        available_lower = [c.lower() for c in available]
        missing = []

        for field_name in [
            'period',
            'customer',
            'revenue',
            'customer_id',
            'sub_customer_id',
            'product',
            'is_recurring',
            'revenue_type',
            'region',
            'industry',
            'entity',
            'price_increase_effect',
            'contract_end_date',
            'cohort',
            'pricing_segment',
            'currency',
            'exchange_rate',
            'target_currency',
            'original_amount',
        ]:
            col = getattr(self, field_name)
            if col is not None and col.lower() not in available_lower:
                missing.append(f"{field_name}='{col}'")

        # Validate declared grain columns exist (skip the default, which is
        # built from already-validated semantic fields).
        if self.grain:
            for col in self.grain:
                if col.lower() not in available_lower:
                    missing.append(f"grain='{col}'")

        if missing:
            raise ValueError(
                f'Mapped columns not found in schema: {", ".join(missing)}. '
                f'Available: {list(available)}'
            )
