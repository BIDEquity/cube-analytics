"""Tests for validate_mirrored_columns.

Protects the boundary CCC and cube-pipelines both cross to reach the mirror:
a faithful schema — reported through either engine's spelling — must pass,
and each of the three hard-violation classes (missing column, mistyped
column, leaked secret column) must be caught without flagging a column CCC
added that the pin never asked for.
"""

from __future__ import annotations

from functools import partial

import pytest

from cube_analytics import ContractViolation, validate_mirrored_columns

# qa_corrections, reported the way Postgres' information_schema spells types.
PG_QA_CORRECTIONS = {
    'correction_id': 'integer',
    'tenant_id': 'integer',
    'rowid': 'character varying',
    'period': 'date',
    'customer_id': 'text',
    'product': 'character varying',
    'corrected_value': 'numeric',
    'is_inserted': 'boolean',
    'revenue_type': 'character varying',
    'corrected_at': 'timestamp without time zone',
    'voided_at': 'timestamp with time zone',
}

# The same table, reported the way DuckDB spells the same types.
DUCKDB_QA_CORRECTIONS = {
    'correction_id': 'BIGINT',
    'tenant_id': 'INTEGER',
    'rowid': 'VARCHAR',
    'period': 'DATE',
    'customer_id': 'VARCHAR',
    'product': 'VARCHAR',
    'corrected_value': 'DECIMAL(18,2)',
    'is_inserted': 'BOOLEAN',
    'revenue_type': 'VARCHAR',
    'corrected_at': 'TIMESTAMP',
    'voided_at': 'TIMESTAMP',
}

# churn_reasons: id integer, tenant_id integer, name text — small enough to
# use as the base fixture for the hard-violation tests.
CHURN_REASONS_BASE = {
    'id': 'integer',
    'tenant_id': 'integer',
    'name': 'text',
}


class TestFaithfulSchemaPasses:
    def test_postgres_spelled_qa_corrections_passes(self):
        r = validate_mirrored_columns('qa_corrections', PG_QA_CORRECTIONS)
        assert r.ok, r.hard

    def test_duckdb_spelled_qa_corrections_passes(self):
        r = validate_mirrored_columns('qa_corrections', DUCKDB_QA_CORRECTIONS)
        assert r.ok, r.hard

    def test_postgres_spelled_churn_reasons_passes(self):
        r = validate_mirrored_columns('churn_reasons', CHURN_REASONS_BASE)
        assert r.ok, r.hard

    def test_duckdb_spelled_churn_reasons_passes(self):
        duck = {'id': 'BIGINT', 'tenant_id': 'INTEGER', 'name': 'VARCHAR'}
        r = validate_mirrored_columns('churn_reasons', duck)
        assert r.ok, r.hard


class TestMissingColumn:
    def test_dropped_pinned_column_is_hard(self):
        cols = {k: v for k, v in CHURN_REASONS_BASE.items() if k != 'name'}
        r = validate_mirrored_columns('churn_reasons', cols)
        assert not r.ok
        assert any('name' in m and 'missing' in m for m in r.hard)


class TestMistypedColumn:
    def test_wrong_type_class_is_hard(self):
        cols = {**CHURN_REASONS_BASE, 'name': 'integer'}
        r = validate_mirrored_columns('churn_reasons', cols)
        assert not r.ok
        assert any('name' in m for m in r.hard)

    def test_unrecognised_spelling_on_a_non_text_column_is_hard(self):
        cols = {**CHURN_REASONS_BASE, 'id': 'not_a_real_type'}
        r = validate_mirrored_columns('churn_reasons', cols)
        assert not r.ok
        assert any('id' in m for m in r.hard)

    def test_enum_reported_as_user_defined_satisfies_a_text_column(self):
        # Postgres information_schema reports an enum column's data_type as
        # the literal string USER-DEFINED, not the enum's own type name.
        cols = {**CHURN_REASONS_BASE, 'name': 'USER-DEFINED'}
        r = validate_mirrored_columns('churn_reasons', cols)
        assert r.ok, r.hard

    def test_enum_reported_under_its_own_type_name_satisfies_a_text_column(self):
        # Some inspectors report the enum's own type name instead of
        # USER-DEFINED. That name is not enumerable here, so any spelling
        # this module cannot classify is accepted for a text-pinned column.
        cols = {**CHURN_REASONS_BASE, 'name': 'reason_kind_enum'}
        r = validate_mirrored_columns('churn_reasons', cols)
        assert r.ok, r.hard


class TestExtraColumnsIgnored:
    def test_extra_unpinned_column_is_not_a_violation(self):
        cols = {**CHURN_REASONS_BASE, 'extra_col': 'text'}
        r = validate_mirrored_columns('churn_reasons', cols)
        assert r.ok, r.hard


class TestSecretColumns:
    def test_leaked_secret_column_is_hard(self):
        cols = {
            'cube_version_id': 'integer',
            'tenant_id': 'integer',
            'period': 'timestamp',
            'status': 'text',
            'created_at': 'timestamp',
            'updated_at': 'timestamp',
            'connection_string': 'text',
        }
        r = validate_mirrored_columns('cube_versions', cols)
        assert not r.ok
        assert any('connection_string' in m for m in r.hard)

    def test_faithful_mirror_without_secrets_passes(self):
        cols = {
            'cube_version_id': 'integer',
            'tenant_id': 'integer',
            'period': 'timestamp',
            'status': 'text',
            'created_at': 'timestamp',
            'updated_at': 'timestamp',
        }
        r = validate_mirrored_columns('cube_versions', cols)
        assert r.ok, r.hard


class TestStrictMode:
    def test_strict_raises_contract_violation(self):
        cols = {k: v for k, v in CHURN_REASONS_BASE.items() if k != 'name'}
        with pytest.raises(ContractViolation):
            validate_mirrored_columns('churn_reasons', cols, strict=True)

    def test_strict_partial_application_raises(self):
        # Mirrors how the acceptance criteria call this through
        # functools.partial rather than a lambda.
        cols = {k: v for k, v in CHURN_REASONS_BASE.items() if k != 'name'}
        call = partial(
            validate_mirrored_columns, 'churn_reasons', cols, strict=True,
        )
        with pytest.raises(ContractViolation):
            call()

    def test_non_strict_never_raises(self):
        cols = {k: v for k, v in CHURN_REASONS_BASE.items() if k != 'name'}
        r = validate_mirrored_columns('churn_reasons', cols)
        assert not r.ok


class TestUnknownTable:
    def test_unknown_table_name_raises_value_error(self):
        with pytest.raises(ValueError, match='not a mirrored table'):
            validate_mirrored_columns('not_a_real_table', {})
