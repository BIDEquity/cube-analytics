# Cube Analytics

Shared analytics library and the canonical cube data contract. Consumed by
`cube-command-center` (the portal) and `cube-pipelines` (Dagster + dbt).

This package is the single source of truth for two things:

- **The cube data contract** — what `analysis.cube_output` must look like.
- **The KPI query engine** — ARR bridge, churn, concentration, cross-sell,
  non-recurring and upsell queries over cube data.

## Install

Pin to a release tag. Assets are attached to the GitHub Release.

```toml
# pyproject.toml
dependencies = [
  "cube-analytics @ https://github.com/BIDEquity/cube-analytics/releases/download/v1.4.0/cube_analytics-1.4.0-py3-none-any.whl",
]
```

## The cube data contract

The contract lives in `src/cube_analytics/contract/cube-contract.yaml` and ships
inside the wheel. It names the semantic roles a cube must carry, the column
names allowed to carry each role, and the types they may have.

It exists because the contract used to be written down in three places — prose
in the portal's steering docs, hand-copied constants in the portal's Python, and
nothing at all on the pipeline side. Those drifted. A producer and a consumer
holding different opinions about the contract is the failure this package
prevents.

### Producer side

Run the checks after building `analysis.cube_output`, before the file goes
anywhere:

```python
from cube_analytics import validate_columns

columns = {name: dtype for name, dtype in conn.execute('DESCRIBE cube_output').fetchall()}
rows = conn.execute('SELECT count(*) FROM cube_output').fetchone()[0]

result = validate_columns(columns, row_count=rows)

for message in result.soft:
    log.warning(message)
for message in result.hard:
    log.error(message)

result.raise_if_failed()   # omit during a warn-only rollout
```

`validate_columns` is pure. It takes a column-name-to-type mapping and a row
count, never a database connection, so the same function covers DuckDB, Polars,
Ibis and test fixtures. Adapt your source into those two arguments.

Hard failures stop a build: the core table missing, a required role with no
column carrying it, a non-date period, a non-numeric revenue, or zero rows.
Soft warnings do not: a missing recommended role, or a column the contract does
not recognise.

Reading `result.hard` without calling `raise_if_failed()` is the supported
warn-only mode. Roll out that way first, read one full cycle of warnings across
every tenant, then switch to raising.

### Consumer side

`ColumnMapping.detect` resolves a cube's actual column names to semantic roles
using the same lists:

```python
from cube_analytics import ColumnMapping

mapping = ColumnMapping.detect(column_names)
mapping.period, mapping.customer, mapping.revenue
```

A test in `tests/test_contract.py` asserts the producer never accepts a cube the
consumer would reject. If that test fails the two sides have drifted, and one of
them is wrong.

## Releasing

`bumpversion` owns the version. It updates `pyproject.toml` and
`src/cube_analytics/__init__.py` together, commits, and tags.

```sh
bumpversion patch    # 1.4.0 -> 1.4.1
bumpversion minor    # 1.4.0 -> 1.5.0
bumpversion major    # 1.4.0 -> 2.0.0
git push && git push --tags
```

Pushing the tag runs `.github/workflows/release.yml`, which tests, builds, and
attaches the wheel and sdist to a GitHub Release. The workflow refuses to
publish if the tag disagrees with the packaged version, or if the contract YAML
is missing from the wheel.

### What counts as which bump

The contract carries its own `contract_version` inside the YAML, separate from
the package version. Both matter.

| Change | Package bump | Contract version |
|---|---|---|
| New allowed name variant for an existing role | minor | minor |
| New optional or recommended role | minor | minor |
| New query class or helper | minor | unchanged |
| Bug fix, no API change | patch | unchanged |
| **New required column or role** | major | major |
| **Removing an allowed name, role, or public symbol** | major | major |

A new required column is breaking, because every existing cube fails validation
the moment a producer upgrades. Ship those behind a contract major and give
tenants a window on the old version.

## Backwards compatibility

Every public symbol exported at v1.3.0 is still exported. `PeriodAnchor` and
`recognize_revs` in particular — `cube-pipelines` imports them for crisalix's
end-of-month revenue recognition.

Do not remove a name from `__all__` in a minor release. Consumers pin to tags
and upgrade on their own schedule, so a removal surfaces as an ImportError in
someone else's CI weeks later.
