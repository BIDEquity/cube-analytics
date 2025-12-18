"""Column mapping detection for cube schemas.

Provides flexible column detection that handles different naming conventions
across various cube outputs. Mirrors the logic from the TypeScript
qa_dashboard.py implementation.
"""

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class ColumnMapping:
    """Detected column mapping for cube schema.

    All column names are validated to exist in the source schema.
    Required columns (period, customer, revenue) raise errors if missing.
    Optional columns are set to None if not found.

    Attributes:
        period: Column containing the time period (month/date)
        customer: Column containing customer name/identifier
        revenue: Column containing revenue/MRR values
        customer_id: Optional unique customer ID for joining
        is_recurring: Optional flag for recurring vs one-time revenue
        region: Optional geographic region/country
        industry: Optional industry/sector classification
        price_increase_effect: Optional price change impact column
    """

    period: str
    customer: str
    revenue: str
    customer_id: str | None = None
    is_recurring: str | None = None
    region: str | None = None
    industry: str | None = None
    price_increase_effect: str | None = None

    @classmethod
    def detect(cls, columns: Sequence[str]) -> 'ColumnMapping':
        """Auto-detect column mapping from available column names.

        Uses case-insensitive matching with priority ordering.
        First matching candidate wins.

        Args:
            columns: List of available column names

        Returns:
            ColumnMapping with detected column names

        Raises:
            ValueError: If required columns (period, customer, revenue)
                       cannot be detected
        """

        def find_column(
            candidates: list[str],
            available: Sequence[str],
        ) -> str | None:
            """Find first matching column name (case-insensitive)."""
            lower_available = [c.lower() for c in available]
            for candidate in candidates:
                if candidate.lower() in lower_available:
                    idx = lower_available.index(candidate.lower())
                    return available[idx]  # Return original case
            return None

        # Detect required columns
        period = find_column(
            ['month', 'month_date', 'period', 'date'],
            columns,
        )
        customer = find_column(
            ['group_level', 'customer', 'customer_name', 'name'],
            columns,
        )
        revenue = find_column(
            ['revenue', 'amount', 'value', 'mrr'],
            columns,
        )

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
                f"Could not detect required columns: {', '.join(missing)}. "
                f"Available columns: {list(columns)}"
            )

        # Detect optional columns
        return cls(
            period=period,  # type: ignore (validated above)
            customer=customer,  # type: ignore
            revenue=revenue,  # type: ignore
            customer_id=find_column(
                ['group_level_id', 'customer_id', 'id'],
                columns,
            ),
            is_recurring=find_column(
                ['is_recurring', 'recurring'],
                columns,
            ),
            region=find_column(
                ['region', 'geography', 'location', 'country'],
                columns,
            ),
            industry=find_column(
                ['industry', 'sector', 'vertical'],
                columns,
            ),
            price_increase_effect=find_column(
                [
                    'price_increase_effect_absolute',
                    'price_increase_effect',
                    'price_effect',
                ],
                columns,
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
            'is_recurring',
            'region',
            'industry',
            'price_increase_effect',
        ]:
            col = getattr(self, field_name)
            if col is not None and col.lower() not in available_lower:
                missing.append(f"{field_name}='{col}'")

        if missing:
            raise ValueError(
                f"Mapped columns not found in schema: {', '.join(missing)}. "
                f"Available: {list(available)}"
            )
