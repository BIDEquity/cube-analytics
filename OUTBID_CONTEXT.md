# OUTBID_CONTEXT.md

## Project Name
cube-analytics

## One-Liner
Python library for SaaS revenue analytics — ARR bridge movements, revenue recognition, fuzzy entity matching, and schema auto-detection for Cube data.

## The Problem
SaaS finance and analytics teams need to answer recurring questions: How much new revenue did we add? How much did we lose to churn? Which customers upgraded or downgraded? These calculations require precise period-over-period comparisons of subscription data, and they need to be consistent, auditable, and fast — even as data volumes grow.

Beyond ARR analysis, teams often deal with messy data: contracts stored in CRM don't always match customer names in the data warehouse, revenue from multi-year deals needs to be spread across accounting periods, and CSV exports from Cube look different depending on which report was run.

Without a shared library, every analyst reinvents the same logic — often with subtle differences that lead to conflicting numbers across dashboards.

## The Solution
cube-analytics provides a set of composable Python modules that solve the most common SaaS analytics data problems:

- **ARR Bridge**: Computes MRR/ARR movements (New Business, Churn, Upsell, Downsell, Reactivation) between any two periods, using a backend-agnostic query engine that works with DuckDB, Polars, PostgreSQL, and more.
- **Revenue Recognition**: Spreads contract revenue evenly across daily, weekly, monthly, quarterly, or yearly periods — matching standard accrual accounting logic.
- **Entity Matching**: Fuzzy-matches company names across datasets using five weighted algorithms, with built-in stop-word lists for German, English, and French legal entity suffixes (GmbH, Inc, SARL, etc.).
- **Schema Auto-Detection**: Automatically maps columns from different Cube report exports to a canonical internal format, so pipelines don't break when column names vary.

## Key Features

### ARR Bridge Analysis
- Categorizes MRR changes between two periods into standard SaaS movement types: New Business, Churn, Upsell, Downsell, Reactivation, Contraction
- Works with any tabular data source via Ibis (DuckDB, Polars, PostgreSQL, BigQuery, and others)
- SQL-injection-safe by design — uses Ibis expression trees, not string interpolation
- Supports multiple input formats: Polars DataFrame, DuckDB file path, DuckDB connection, Ibis connection, Parquet files

### Fuzzy Entity Matching
- Matches company names across two lists even when names differ slightly (e.g. "Acme GmbH" vs "ACME Germany")
- Uses five algorithms with tuned weights: Levenshtein (0.15), Partial Ratio (0.10), Token Sort (0.25), Token Set (0.25), Jaro-Winkler (0.25)
- Strips legal-form suffixes (GmbH, AG, Inc, LLC, SA, SARL, Ltd, SAS, GmbH & Co. KG, etc.) before comparing
- Configurable similarity threshold (default: 0.70)
- Supports both cross-list matching and duplicate detection within a single list
- Returns structured `MatchResult` objects with per-algorithm scores for auditability

### Revenue Recognition / Periodization
- Takes contract records with a start date, end date, and total revenue
- Distributes revenue evenly across all periods in the contract window
- Five intervals: daily, weekly, monthly, quarterly, yearly
- Outputs long format (one row per period) or wide format (pivot table with periods as columns)
- Validates data integrity — raises explicit errors on missing required fields

### Schema Auto-Detection
- Detects semantic column roles (period, customer, revenue, region, industry, etc.) from a list of actual column names
- Case-insensitive matching with priority-ordered candidate lists
- Raises clear errors when required columns are missing
- Handles variations in Cube export formats without manual configuration

## Target Audience

**Primary users:**
- **Finance analysts** at SaaS companies building ARR dashboards and monthly close reports
- **Data engineers** building revenue data pipelines that consume Cube exports
- **Analytics engineers** using dbt or similar tools who need reliable SaaS metric libraries

**Secondary users:**
- **RevOps teams** reconciling CRM data with data warehouse customer records
- **FP&A teams** spreading contract revenue across fiscal periods for budget vs. actuals
- **Data scientists** building models on top of SaaS subscription data

**Not required:** deep Python or SQL expertise — the library handles query generation and schema normalization automatically.

## Use Cases

### Use Case 1: Monthly ARR Bridge Report
**Workflow:**
1. Export subscription data from your data warehouse (Cube, Snowflake, BigQuery) as Parquet or load into DuckDB
2. Initialize `ARRBridgeQueries` with a Polars DataFrame or DuckDB path
3. Call `.arr_bridge(period_start, period_end)` to get a breakdown of MRR movements
4. Pipe results into your BI tool (Metabase, Looker, Superset) or send to finance as CSV
5. Numbers match exactly because the categorization logic is centralized and version-controlled

### Use Case 2: Reconciling CRM Customers with Data Warehouse
**Workflow:**
1. Export customer list from Salesforce (CRM) and from the data warehouse
2. Pass both lists to `match_entities(source=crm_list, target=dw_list)`
3. Review matched pairs with scores below 0.9 for manual confirmation
4. Use `duplicate_mode=True` to find duplicate entries within the CRM itself
5. Get a clean mapping table to join CRM attributes onto warehouse records

### Use Case 3: Multi-Year Contract Revenue Spreading
**Workflow:**
1. Load contract table with columns: contract_id, revenue, start_date, end_date
2. Call `recognize_revs(df, interval=RecognitionInterval.monthly, start_period=..., end_period=...)`
3. Get a monthly revenue schedule as a long DataFrame
4. Optionally pivot to wide format for spreadsheet-style reporting
5. Load results into the data warehouse as the recognized revenue fact table

### Use Case 4: Normalizing Inconsistent Cube Exports
**Workflow:**
1. Receive a Cube export where columns are named differently depending on the report (e.g. `month_date` vs `period` vs `date`)
2. Call `ColumnMapping.detect(df.columns)` to get a normalized mapping
3. Use the mapping to access `period`, `customer`, `revenue` fields consistently
4. Pipeline continues without manual column renaming for each export variant

## Integrations & Connections

| System | How it integrates |
|---|---|
| **DuckDB** | Native backend via `from_duckdb_path()` or `from_duckdb_connection()` — zero-copy, in-process OLAP |
| **Polars** | Primary DataFrame format; input and output; `from_polars()` for ARR Bridge |
| **Ibis** | Abstraction layer enabling any supported backend (PostgreSQL, BigQuery, Snowflake, SQLite, etc.) |
| **Parquet** | Readable via Polars or DuckDB without conversion |
| **Cube** | Schema auto-detection designed specifically for Cube report export column naming conventions |
| **Python ecosystem** | Standard Polars DataFrames as output — compatible with pandas, dbt Python models, Jupyter notebooks |

## Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| Language | Python ≥ 3.11 | Core runtime |
| DataFrames | Polars ≥ 1.34.0 | Fast columnar data processing |
| Query engine | Ibis ≥ 9.0.0 | Backend-agnostic SQL expression builder |
| OLAP database | DuckDB ≥ 1.4.1 | In-process analytical queries on local data |
| Fuzzy matching | thefuzz ≥ 0.22.1 | Levenshtein-based string similarity |
| Fuzzy matching | jaro-winkler ≥ 2.0.3 | Jaro-Winkler string distance metric |
| Build system | Hatch / pyproject.toml | Python packaging and dependency management |

## What This Project Does NOT Do

- **Not a BI tool**: Does not generate charts, dashboards, or visualizations — outputs DataFrames for downstream tools
- **Not a data warehouse**: Does not store data persistently — processes data in-memory or reads from existing sources
- **Not a Cube replacement**: Works with Cube output, does not replicate or replace Cube's query layer
- **Not an ETL pipeline**: Does not handle data ingestion, scheduling, or orchestration — use Airflow, Prefect, or dbt for that
- **Not an accounting system**: Revenue recognition output is for analytics purposes; consult your finance team for GAAP/IFRS compliance
- **Not a CRM**: Entity matching produces a mapping table; it does not write back to Salesforce or HubSpot
- **Not multi-tenant SaaS**: This is a Python library, not a hosted service — you run it in your own infrastructure

## Related Projects

- **Cube** — Semantic layer / metrics API that this library is designed to consume data from
- **Ibis** — The backend-agnostic query engine used internally; supports 20+ SQL backends
- **Polars** — The DataFrame library used for all data transformations and outputs
- **DuckDB** — The embedded OLAP engine recommended for local analytics workloads

## Status

**Current version:** 1.1.0
**Stability:** Active development — API may evolve; check changelog before upgrading
**Python support:** 3.11 and above
**Maintenance:** Actively maintained

## Keywords & Tags

`saas-analytics` `arr-bridge` `mrr` `arr` `churn` `revenue-recognition` `entity-matching` `fuzzy-matching` `company-name-matching` `cube` `duckdb` `polars` `ibis` `python` `finance-analytics` `subscription-analytics` `revenue-analytics` `periodization` `schema-detection` `data-pipeline` `revops` `fp-and-a` `saas-metrics` `b2b-saas` `customer-data` `data-engineering` `analytics-engineering` `dbt` `parquet` `olap`

## Contact & Ownership

**Organization:** BIDEquity
**Repository:** [https://github.com/BIDEquity/cube-analytics](https://github.com/BIDEquity/cube-analytics)
**Package:** cube-analytics (PyPI)
