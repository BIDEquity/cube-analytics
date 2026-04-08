import datetime as dt
from enum import Enum

import polars as pl
import polars.selectors as cs


class RevenueRecognitionInvariantViolation(Exception):
    pass


class RecognitionInterval(Enum):
    daily = '1d'
    weekly = '1w'
    monthly = '1mo'
    quarterly = '3mo'
    yearly = '1y'


class PeriodAnchor(Enum):
    start_of_month = 'start'
    end_of_month = 'end'

def recognize_revs(
    df: pl.DataFrame,
    id_column: str,
    revenue_column: str,
    date_from_column: str,
    date_to_column: str,
    interval: RecognitionInterval = RecognitionInterval.monthly,
    period_anchor: PeriodAnchor = PeriodAnchor.start_of_month,
    start_period: dt.date | None = None,
    end_period: dt.date | None = None,
    wide_format: bool = False,
) -> pl.DataFrame:

    if df[id_column].has_nulls():
        raise RevenueRecognitionInvariantViolation(
            "There are empty IDs."
        )



    min_date = (
        df[date_from_column]
        .cast(pl.Date)
        .dt.truncate(every=interval.value)
        .min()
    )
    max_date = (
        df[date_to_column]
        .cast(pl.Date)
        .dt.truncate(every=interval.value)
        .max()
    )
    dts = pl.date_range(
        min_date, max_date, eager=True, interval=interval.value
    )

    if period_anchor == PeriodAnchor.end_of_month:
        period_check = pl.col('period').dt.month_end()
    else:
        period_check = pl.col('period')

    df_recognized = df.join_where(
        dts.to_frame('period'),
        period_check >= pl.col(date_from_column),
        period_check <= pl.col(date_to_column),
    ).with_columns(
        pl.col(id_column).len().over(id_column).alias('n_periods'),
        pl.col(revenue_column).alias('revenue_per_period')
        / pl.col(id_column).len().over(id_column).alias('n_periods'),
    )

    if start_period:
        df_recognized = df_recognized.filter(
            pl.col("period") >= start_period
        )

    if end_period:
        df_recognized = df_recognized.filter(
            pl.col("period") <= end_period
        )

    if wide_format:
        return df_recognized.pivot(
            on='period',
            index=cs.exclude('period'),
            values='revenue_per_period',
        ).sort(id_column)

    return df_recognized
