"""Tests for the shared cube data contract.

Two things are being protected here. That the contract file bundled in the wheel
parses into the shape callers expect, and that a producer running
`validate_columns` gets the same verdict a consumer would reach from
`ColumnMapping.detect`. Those two agreeing is the whole point of the package.
"""

from __future__ import annotations

import pytest

from cube_analytics import ContractViolation, load_contract, validate_columns
from cube_analytics.contract import is_date_like, is_numeric

# A minimal conforming cube, in the column names justrelate actually uses.
JUSTRELATE_LIKE = {
    'month': 'DATE',
    'group_level': 'VARCHAR',
    'group_level_id': 'VARCHAR',
    'product': 'VARCHAR',
    'revenue': 'DOUBLE',
    'is_recurring': 'BOOLEAN',
    'revenue_type': 'VARCHAR',
}


class TestLoadContract:
    def test_bundled_contract_loads(self):
        c = load_contract()
        assert c.version
        assert c.qualified_table == 'analysis.cube_output'

    def test_required_roles_are_the_three_every_feature_needs(self):
        c = load_contract()
        assert set(c.required_roles) == {'period', 'customer', 'revenue'}

    def test_resolve_follows_contract_priority_not_dict_order(self):
        c = load_contract()
        # both names present — the contract's priority order decides
        both = ['customer_name', 'group_level']
        assert c.resolve('customer', both) == c.allowed_names['customer'][0]

    def test_resolve_returns_none_when_no_variant_present(self):
        assert load_contract().resolve('period', ['unrelated']) is None

    def test_resolve_is_case_insensitive(self):
        assert load_contract().resolve('period', ['MONTH']) == 'month'


class TestTypePredicates:
    @pytest.mark.parametrize('dtype', ['DATE', 'TIMESTAMP', 'VARCHAR', 'timestamp with time zone'])
    def test_date_like_accepts(self, dtype):
        assert is_date_like(dtype)

    @pytest.mark.parametrize('dtype', ['DOUBLE', 'DECIMAL(18,2)', 'BIGINT', 'float64'])
    def test_numeric_accepts(self, dtype):
        assert is_numeric(dtype)

    def test_numeric_rejects_text(self):
        assert not is_numeric('VARCHAR')

    def test_date_like_rejects_a_number(self):
        assert not is_date_like('DOUBLE')


class TestValidateColumns:
    def test_conforming_cube_passes(self):
        r = validate_columns(JUSTRELATE_LIKE, row_count=1000)
        assert r.ok
        assert r.hard == []

    def test_conforming_cube_resolves_every_role_it_carries(self):
        r = validate_columns(JUSTRELATE_LIKE, row_count=1)
        assert r.resolved['period'] == 'month'
        assert r.resolved['revenue'] == 'revenue'
        assert 'customer' in r.resolved

    def test_missing_required_role_is_hard(self):
        cols = {k: v for k, v in JUSTRELATE_LIKE.items() if k != 'month'}
        r = validate_columns(cols, row_count=1)
        assert not r.ok
        assert any("'period'" in m for m in r.hard)

    def test_missing_role_message_lists_what_was_tried(self):
        r = validate_columns({'revenue': 'DOUBLE'}, row_count=1)
        msg = ' '.join(r.hard)
        assert 'month' in msg  # an allowed name for period
        assert 'revenue' in msg  # the columns actually present

    def test_wrong_period_type_is_hard(self):
        r = validate_columns({**JUSTRELATE_LIKE, 'month': 'DOUBLE'}, row_count=1)
        assert any('period column' in m for m in r.hard)

    def test_text_period_is_allowed(self):
        # several tenants still export period as an ISO-8601 string
        r = validate_columns({**JUSTRELATE_LIKE, 'month': 'VARCHAR'}, row_count=1)
        assert r.ok

    def test_non_numeric_revenue_is_hard(self):
        r = validate_columns({**JUSTRELATE_LIKE, 'revenue': 'VARCHAR'}, row_count=1)
        assert any('revenue column' in m for m in r.hard)

    def test_empty_table_is_hard(self):
        r = validate_columns(JUSTRELATE_LIKE, row_count=0)
        assert any('no rows' in m for m in r.hard)

    def test_absent_table_reports_only_that(self):
        r = validate_columns({}, row_count=0, table_present=False)
        assert len(r.hard) == 1
        assert 'missing' in r.hard[0]

    def test_missing_recommended_role_is_soft_not_hard(self):
        cols = {k: v for k, v in JUSTRELATE_LIKE.items() if k != 'product'}
        r = validate_columns(cols, row_count=1)
        assert r.ok
        assert any("'product'" in m for m in r.soft)

    def test_unrecognised_column_is_soft(self):
        r = validate_columns({**JUSTRELATE_LIKE, 'wat': 'VARCHAR'}, row_count=1)
        assert r.ok
        assert any('wat' in m for m in r.soft)

    def test_tenant_extension_does_not_fail_the_build(self):
        # crisalix carries currency columns others do not
        r = validate_columns(
            {**JUSTRELATE_LIKE, 'currency': 'VARCHAR', 'exchange_rate': 'DOUBLE'},
            row_count=1,
        )
        assert r.ok


class TestRaiseIfFailed:
    def test_raises_on_hard_violation(self):
        r = validate_columns({'nope': 'VARCHAR'}, row_count=1)
        with pytest.raises(ContractViolation) as exc:
            r.raise_if_failed()
        assert 'data contract' in str(exc.value)

    def test_silent_when_only_soft(self):
        r = validate_columns({**JUSTRELATE_LIKE, 'wat': 'VARCHAR'}, row_count=1)
        r.raise_if_failed()  # must not raise

    def test_warn_only_callers_can_read_violations_without_raising(self):
        r = validate_columns({'nope': 'VARCHAR'}, row_count=0)
        assert len(r.hard) >= 2  # this is the rollout mode: inspect, log, continue


class TestConsumerProducerAgreement:
    """The producer must not accept a cube the consumer will reject."""

    def test_contract_roles_match_the_consumer_candidate_lists(self):
        from cube_analytics.schema import COLUMN_CANDIDATES

        c = load_contract()
        for role in c.required_roles:
            assert role in COLUMN_CANDIDATES, f'{role} required by contract, unknown to ColumnMapping'
            assert set(c.allowed_names[role]) <= set(COLUMN_CANDIDATES[role]), (
                f"contract allows names for '{role}' that ColumnMapping would not detect: "
                f'{set(c.allowed_names[role]) - set(COLUMN_CANDIDATES[role])}'
            )

    def test_a_cube_the_producer_accepts_is_detectable_by_the_consumer(self):
        from cube_analytics.schema import ColumnMapping

        assert validate_columns(JUSTRELATE_LIKE, row_count=1).ok
        mapping = ColumnMapping.detect(list(JUSTRELATE_LIKE))
        assert mapping.period and mapping.customer and mapping.revenue
