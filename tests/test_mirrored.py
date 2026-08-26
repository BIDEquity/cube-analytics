"""Tests for the mirrored-CCC-table schema pin loader.

Protects two things: that the bundled YAML parses into the shape callers
expect, and that the consumed-column inventory per table matches what the
spec pinned — so a later edit that silently drops a pinned column fails a
test here rather than a downstream dbt run.
"""

from __future__ import annotations

import pytest

import cube_analytics
from cube_analytics import (
    MIRRORED_TABLES_VERSION,
    MirroredTable,
    load_mirrored_tables,
)

EXPECTED_TABLE_NAMES = {
    'qa_corrections',
    'cube_versions',
    'churn_reason_assignments',
    'churn_reasons',
    'steward_decision_ledger',
}

# The full consumed-column inventory per table, pinned here so a later edit
# to mirrored-tables.yaml that silently drops a column fails this test
# instead of a downstream dbt run discovering it missing.
PINNED_COLUMN_INVENTORY = {
    'qa_corrections': {
        'correction_id',
        'tenant_id',
        'rowid',
        'period',
        'customer_id',
        'product',
        'corrected_value',
        'is_inserted',
        'revenue_type',
        'corrected_at',
        'voided_at',
    },
    'cube_versions': {
        'cube_version_id',
        'tenant_id',
        'period',
        'status',
        'created_at',
        'updated_at',
    },
    'churn_reason_assignments': {
        'id',
        'tenant_id',
        'customer_id',
        'product',
        'revenue_type',
        'reason_id',
        'set_by_name',
        'set_at',
    },
    'churn_reasons': {'id', 'tenant_id', 'name'},
    'steward_decision_ledger': {
        'written_seq',
        'tenant_id',
        'payload',
        'decision_id',
        'supersedes',
        'verb',
        'verdict',
    },
}


class TestLoadMirroredTables:
    def test_bundled_yaml_loads(self):
        tables = load_mirrored_tables()
        assert tables

    def test_all_five_tables_present(self):
        assert set(load_mirrored_tables()) == EXPECTED_TABLE_NAMES

    def test_every_table_is_a_mirrored_table(self):
        for table in load_mirrored_tables().values():
            assert isinstance(table, MirroredTable)

    def test_table_name_matches_its_key(self):
        for name, table in load_mirrored_tables().items():
            assert table.name == name


class TestVersion:
    def test_version_is_1_0_0(self):
        assert MIRRORED_TABLES_VERSION == '1.0.0'

    def test_reachable_from_the_top_level_package(self):
        assert cube_analytics.MIRRORED_TABLES_VERSION == '1.0.0'


class TestPinnedColumnInventory:
    def test_every_table_carries_exactly_its_pinned_columns(self):
        tables = load_mirrored_tables()
        for table_name, expected_columns in PINNED_COLUMN_INVENTORY.items():
            actual = set(tables[table_name].columns)
            assert actual == expected_columns, (
                f"{table_name}: pinned columns {expected_columns - actual} "
                f'missing, unexpected columns {actual - expected_columns}'
            )

    def test_nullable_generated_columns_on_steward_decision_ledger(self):
        columns = load_mirrored_tables()['steward_decision_ledger'].columns
        for name in ('decision_id', 'supersedes', 'verb', 'verdict'):
            assert columns[name].nullable, f'{name} must be nullable'

    def test_required_columns_are_not_nullable(self):
        columns = load_mirrored_tables()['qa_corrections'].columns
        assert not columns['correction_id'].nullable
        assert not columns['tenant_id'].nullable

    def test_type_classes_are_from_the_closed_set(self):
        allowed = {
            'integer', 'text', 'numeric', 'boolean', 'date', 'timestamp', 'json',
        }
        for table in load_mirrored_tables().values():
            for column in table.columns.values():
                assert column.type_class in allowed, (
                    f'{table.name}.{column.name} has unknown type_class '
                    f"'{column.type_class}'"
                )


class TestSecretColumns:
    def test_cube_versions_declares_its_secret_columns(self):
        table = load_mirrored_tables()['cube_versions']
        assert set(table.secret_columns) == {
            'connection_string',
            'analyst_connection_string',
        }

    def test_other_tables_declare_no_secret_columns(self):
        tables = load_mirrored_tables()
        for name in EXPECTED_TABLE_NAMES - {'cube_versions'}:
            assert tables[name].secret_columns == ()


class TestNullableRejectsNonBoolean:
    def test_quoted_string_nullable_raises(self, tmp_path):
        # PyYAML parses an unquoted `false` as a real bool, but a quoted
        # "false" parses as str — and bool("false") is True, which would
        # silently flip a not-null pin to nullable if left uncaught.
        pin = tmp_path / 'mirrored-tables.yaml'
        pin.write_text(
            'mirrored_tables_version: "1.0.0"\n'
            'tables:\n'
            '  churn_reasons:\n'
            '    columns:\n'
            '      - {name: id, type_class: integer, nullable: "false"}\n'
        )
        with pytest.raises(ValueError, match=r'churn_reasons\.id'):
            load_mirrored_tables(pin)

    def test_non_boolean_scalar_nullable_raises(self, tmp_path):
        pin = tmp_path / 'mirrored-tables.yaml'
        pin.write_text(
            'mirrored_tables_version: "1.0.0"\n'
            'tables:\n'
            '  churn_reasons:\n'
            '    columns:\n'
            '      - {name: id, type_class: integer, nullable: 1}\n'
        )
        with pytest.raises(ValueError, match=r'churn_reasons\.id'):
            load_mirrored_tables(pin)


class TestFingerprintSpecs:
    def test_qa_corrections_hashes_the_full_consumed_set_in_order(self):
        fp = load_mirrored_tables()['qa_corrections'].fingerprint
        assert fp.strategy == 'content_hash'
        assert fp.order_by == 'correction_id'
        assert set(fp.columns) == PINNED_COLUMN_INVENTORY['qa_corrections']

    def test_churn_reason_assignments_is_content_hash(self):
        fp = load_mirrored_tables()['churn_reason_assignments'].fingerprint
        assert fp.strategy == 'content_hash'
        assert fp.order_by == 'id'
        assert set(fp.columns) == {
            'id', 'tenant_id', 'customer_id', 'product',
            'revenue_type', 'reason_id', 'set_by_name', 'set_at',
        }

    def test_churn_reasons_is_content_hash(self):
        fp = load_mirrored_tables()['churn_reasons'].fingerprint
        assert fp.strategy == 'content_hash'
        assert fp.order_by == 'id'
        assert set(fp.columns) == {'id', 'tenant_id', 'name'}

    def test_steward_decision_ledger_is_append_only_on_written_seq(self):
        fp = load_mirrored_tables()['steward_decision_ledger'].fingerprint
        assert fp.strategy == 'append_only'
        assert fp.sequence_column == 'written_seq'

    def test_cube_versions_has_no_fingerprint_strategy(self):
        fp = load_mirrored_tables()['cube_versions'].fingerprint
        assert fp.strategy == 'none'
