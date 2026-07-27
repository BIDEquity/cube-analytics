# Cube Analytics

The canonical cube data contract. Consumed by `cube-command-center` (the portal)
and `cube-pipelines` (Dagster + dbt).

The package holds one thing: what `analysis.cube_output` must look like. Which
semantic roles a cube must carry, which column names may carry each role, and
what types they may hold.

It exists because that used to be written down in three places — prose in the
portal's steering docs, hand-copied constants in the portal's Python, and nothing
at all on the pipeline side. Those drifted. A producer and a consumer holding
different opinions about the contract is the failure this package prevents.

Nothing else belongs here. A module with one consumer lives in that consumer,
not in a package two repositories have to release against — see
[v2.0.0](#v200) below.

## Install

Pin to a release tag. Assets are attached to the GitHub Release.

```toml
# pyproject.toml
dependencies = [
  "cube-analytics @ https://github.com/BIDEquity/cube-analytics/releases/download/v2.0.0/cube_analytics-2.0.0-py3-none-any.whl",
]
```

The repository is public, so the pin needs no credentials in any environment.

## Producer side

Run the checks after building `analysis.cube_output`, before the file goes
anywhere:

```python
from cube_analytics import validate_columns

columns = {name: dtype for name, dtype, *_ in conn.execute('DESCRIBE cube_output').fetchall()}
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

## Consumer side

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

## v2.0.0

v2.0.0 removed everything that was not the contract. Each of these had exactly
one consumer, and a single-consumer module pays the full cost of being shared —
a PR here, a release, a version bump, and a second PR in the consumer — while
buying nothing. That toll is what made the portal keep a private copy of this
package, which then drifted from upstream for three and a half months.

| Removed | Now lives in |
|---|---|
| `queries/*` — `ARRBridgeQueries` and peers | `command_center.analytics.queries` |
| `entity_matching` | `command_center.analytics.entity_matching` |
| `recurring` | `command_center.analytics.recurring` |
| `revenue_recognition` — `PeriodAnchor`, `recognize_revs` | `cube_pipelines.utils.revenue_recognition` |

Nothing in the portfolio imported them from here at the time of release; both
consumers had already taken their own copy. Upgrading from v1.4.0 needs no code
change unless you import one of the four.

The dependency list shrank with it. v1.4.0 pulled in Ibis, polars, duckdb,
pyarrow, loguru and two fuzzy matchers. v2.0.0 needs PyYAML.

## Releasing

`bumpversion` owns the version. It updates `pyproject.toml` and
`src/cube_analytics/__init__.py` together, commits, and tags.

```sh
bumpversion patch    # 2.0.0 -> 2.0.1
bumpversion minor    # 2.0.0 -> 2.1.0
bumpversion major    # 2.0.0 -> 3.0.0
git push && git push --tags
```

It is not in the dev dependency group and not installed here. Whoever cuts a
release currently needs it on their own machine, which means releases depend on
what each person happens to have. Worth fixing: the original `bumpversion` is
unmaintained, `bump2version` reads the same `.bumpversion.cfg`, and
`bump-my-version` is the newer successor but prefers TOML config.

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
| Bug fix, no API change | patch | unchanged |
| **New required column or role** | major | major |
| **Removing an allowed name, role, or public symbol** | major | major |

A new required column is breaking, because every existing cube fails validation
the moment a producer upgrades. Ship those behind a contract major and give
tenants a window on the old version.

Do not remove a name from `__all__` in a minor release. Consumers pin to tags
and upgrade on their own schedule, so a removal surfaces as an ImportError in
someone else's CI weeks later.
