"""Tests for the shared cube data contract.

Two things are being protected here. That the contract file bundled in the wheel
parses into the shape callers expect, and that a producer running
`validate_columns` gets the same verdict a consumer would reach from
`ColumnMapping.detect`. Those two agreeing is the whole point of the package.
"""

from __future__ import annotations

import pytest

import cube_analytics
from cube_analytics import (
    CONTRACT_VERSION,
    ContractViolation,
    load_contract,
    validate_columns,
)
from cube_analytics.contract import is_date_like, is_numeric, is_varchar_like

# A minimal conforming cube, in column names a real tenant cube uses.
CONFORMING_CUBE = {
    'month': 'DATE',
    'group_level': 'VARCHAR',
    'group_level_id': 'VARCHAR',
    'product': 'VARCHAR',
    'revenue': 'DOUBLE',
    'is_recurring': 'BOOLEAN',
    'revenue_type': 'VARCHAR',
    'row_key': 'VARCHAR',
}

# The role -> allowed-names inventory contract 1.0.0 carried, pinned here so a
# future re-port against a newer contract version cannot silently drop
# something 1.0.0 promised. Contract 2.0.0 must remain a superset of this.
CONTRACT_V1_ROLE_ALLOWED_NAMES = {
    'period': {'month', 'month_date', 'period', 'date'},
    'customer': {'group_level', 'customer', 'customer_name', 'name'},
    'revenue': {'revenue', 'amount', 'value', 'mrr'},
    'customer_id': {'group_level_id', 'customer_id', 'id'},
    'product': {'product', 'product_name', 'segment'},
    'is_recurring': {'is_recurring', 'recurring', 'revenue_type'},
    'region': {'region', 'geography', 'location', 'country'},
    'industry': {'industry', 'sector', 'vertical'},
    'price_increase_effect': {
        'price_increase_effect_absolute',
        'price_increase_effect',
        'price_effect',
    },
}


class TestLoadContract:
    def test_bundled_contract_loads(self):
        c = load_contract()
        assert c.version
        assert c.qualified_table == 'analysis.cube_output'

    def test_required_roles_are_the_four_every_feature_needs(self):
        c = load_contract()
        assert set(c.required_roles) == {'period', 'customer', 'revenue', 'row_key'}

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

    @pytest.mark.parametrize('dtype', ['VARCHAR', 'TEXT', 'string', 'utf8'])
    def test_varchar_like_accepts(self, dtype):
        assert is_varchar_like(dtype)

    def test_varchar_like_rejects_a_number(self):
        assert not is_varchar_like('BIGINT')


class TestValidateColumns:
    def test_conforming_cube_passes(self):
        r = validate_columns(CONFORMING_CUBE, row_count=1000)
        assert r.ok
        assert r.hard == []

    def test_conforming_cube_resolves_every_role_it_carries(self):
        r = validate_columns(CONFORMING_CUBE, row_count=1)
        assert r.resolved['period'] == 'month'
        assert r.resolved['revenue'] == 'revenue'
        assert 'customer' in r.resolved

    def test_missing_required_role_is_hard(self):
        cols = {k: v for k, v in CONFORMING_CUBE.items() if k != 'month'}
        r = validate_columns(cols, row_count=1)
        assert not r.ok
        assert any("'period'" in m for m in r.hard)

    def test_missing_role_message_lists_what_was_tried(self):
        r = validate_columns({'revenue': 'DOUBLE'}, row_count=1)
        msg = ' '.join(r.hard)
        assert 'month' in msg  # an allowed name for period
        assert 'revenue' in msg  # the columns actually present

    def test_wrong_period_type_is_hard(self):
        r = validate_columns({**CONFORMING_CUBE, 'month': 'DOUBLE'}, row_count=1)
        assert any('period column' in m for m in r.hard)

    def test_text_period_is_allowed(self):
        # several tenants still export period as an ISO-8601 string
        r = validate_columns({**CONFORMING_CUBE, 'month': 'VARCHAR'}, row_count=1)
        assert r.ok

    def test_non_numeric_revenue_is_hard(self):
        r = validate_columns({**CONFORMING_CUBE, 'revenue': 'VARCHAR'}, row_count=1)
        assert any('revenue column' in m for m in r.hard)

    def test_empty_table_is_hard(self):
        r = validate_columns(CONFORMING_CUBE, row_count=0)
        assert any('no rows' in m for m in r.hard)

    def test_absent_table_reports_only_that(self):
        r = validate_columns({}, row_count=0, table_present=False)
        assert len(r.hard) == 1
        assert 'missing' in r.hard[0]

    def test_missing_recommended_role_is_soft_not_hard(self):
        cols = {k: v for k, v in CONFORMING_CUBE.items() if k != 'product'}
        r = validate_columns(cols, row_count=1)
        assert r.ok
        assert any("'product'" in m for m in r.soft)

    def test_unrecognised_column_is_soft(self):
        r = validate_columns({**CONFORMING_CUBE, 'wat': 'VARCHAR'}, row_count=1)
        assert r.ok
        assert any('wat' in m for m in r.soft)

    def test_tenant_extension_does_not_fail_the_build(self):
        # some tenants carry currency columns others do not
        r = validate_columns(
            {**CONFORMING_CUBE, 'currency': 'VARCHAR', 'exchange_rate': 'DOUBLE'},
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
        r = validate_columns({**CONFORMING_CUBE, 'wat': 'VARCHAR'}, row_count=1)
        r.raise_if_failed()  # must not raise

    def test_warn_only_callers_can_read_violations_without_raising(self):
        r = validate_columns({'nope': 'VARCHAR'}, row_count=0)
        assert len(r.hard) >= 2  # this is the rollout mode: inspect, log, continue


class TestStrictAndWarnModes:
    """warn is the default so no existing caller's behaviour changes; strict
    is an opt-in per call, not a package-level setting."""

    def test_strict_raises_on_a_missing_required_role(self):
        cols = {k: v for k, v in CONFORMING_CUBE.items() if k != 'row_key'}
        with pytest.raises(ContractViolation) as exc:
            validate_columns(cols, row_count=1, strict=True)
        assert "'row_key'" in str(exc.value)

    def test_warn_returns_the_same_violation_without_raising_on_the_same_input(self):
        cols = {k: v for k, v in CONFORMING_CUBE.items() if k != 'row_key'}
        r = validate_columns(cols, row_count=1)  # warn is the default
        assert not r.ok
        assert any("'row_key'" in m for m in r.hard)

    def test_strict_does_not_raise_on_a_conforming_cube(self):
        r = validate_columns(CONFORMING_CUBE, row_count=1, strict=True)
        assert r.ok


class TestRowKeyTypeCheck:
    """Opt-in: row_key is a required role in 2.0.0, but a tenant mid-migration
    may carry it before its type has settled."""

    def test_non_varchar_row_key_is_hard_when_opted_in(self):
        r = validate_columns(
            {**CONFORMING_CUBE, 'row_key': 'BIGINT'}, row_count=1, check_row_key_type=True
        )
        assert any('row_key column' in m for m in r.hard)

    def test_varchar_row_key_passes_when_opted_in(self):
        r = validate_columns(CONFORMING_CUBE, row_count=1, check_row_key_type=True)
        assert r.ok

    def test_row_key_type_is_not_checked_unless_opted_in(self):
        r = validate_columns({**CONFORMING_CUBE, 'row_key': 'BIGINT'}, row_count=1)
        assert not any('row_key column' in m for m in r.hard)


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

        assert validate_columns(CONFORMING_CUBE, row_count=1).ok
        mapping = ColumnMapping.detect(list(CONFORMING_CUBE))
        assert mapping.period and mapping.customer and mapping.revenue


class TestContractVersionTwoShape:
    """Coverage for what the 2.0.0 port added: row_key and cube_meta."""

    def test_contract_version_reads_2_0_0(self):
        assert load_contract().version == '2.0.0'

    def test_row_key_is_a_required_role_with_one_allowed_name(self):
        c = load_contract()
        assert 'row_key' in c.required_roles
        assert c.allowed_names['row_key'] == ('row_key',)

    def test_cube_meta_section_parses_with_its_build_metadata_columns(self):
        import pathlib

        import yaml

        from cube_analytics import contract as contract_module

        bundled = pathlib.Path(contract_module.__file__).parent / 'cube-contract.yaml'
        raw = yaml.safe_load(bundled.read_text(encoding='utf-8'))
        assert raw['cube_meta']['name'] == 'cube_meta'
        assert 'grain_columns' in raw['cube_meta']['columns']
        assert 'row_key' in raw['required_columns']


class TestContractVersionAccessor:
    """A consumer can read the contract version off the top-level package,
    without opening the YAML itself."""

    def test_reachable_from_the_top_level_package(self):
        assert cube_analytics.CONTRACT_VERSION == '2.0.0'

    def test_agrees_with_load_contract_version(self):
        assert CONTRACT_VERSION == load_contract().version


class TestCarriedForwardFromContractV1:
    """Contract 2.0.0 must be a strict superset of 1.0.0 - nothing the earlier
    contract promised may vanish silently in a re-port. See
    CONTRACT_V1_ROLE_ALLOWED_NAMES above for the pinned 1.0.0 inventory.
    """

    def test_every_v1_role_and_allowed_name_survives_carried_forward(self):
        c = load_contract()
        every_role = set(c.required_roles) | set(c.recommended_roles) | set(c.optional_roles)
        for role, names in CONTRACT_V1_ROLE_ALLOWED_NAMES.items():
            assert role in every_role, f"role '{role}' from contract 1.0.0 is missing in {c.version}"
            carried = set(c.allowed_names.get(role, ()))
            assert names <= carried, (
                f"contract {c.version} dropped allowed name(s) for '{role}': {names - carried}"
            )
