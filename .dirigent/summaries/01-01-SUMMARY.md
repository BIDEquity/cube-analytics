# Task 01-01: Read core source files

## Was wurde gemacht
Folgende Kerndateien gelesen und analysiert, um Projekt-Metadaten, Abhängigkeiten und Feature-Beschreibungen für die OUTBID_CONTEXT.md zu extrahieren:
- `pyproject.toml` – Projektname, Version (1.1.0), Beschreibung, Python-Abhängigkeiten
- `README.md` – Minimale README, nur Titel und einzeiliger Beschreibungstext
- `src/cube_analytics/__init__.py` – Modul-Docstring, öffentliche API-Exports
- `src/cube_analytics/queries/arr_bridge.py` – ARR Bridge Hauptklasse mit allen Query-Methoden
- `src/cube_analytics/entity_matching.py` – Fuzzy-Matching-Modul für Entitätsdeduplizierung
- `src/cube_analytics/revenue_recognition.py` – Revenue Recognition mit verschiedenen Intervallen
- `src/cube_analytics/schema.py` – ColumnMapping mit Auto-Detection

## Geänderte Dateien
Keine (reine Lese-Task)

## Extrahierte Schlüsselinformationen

**Projekt:** cube-analytics v1.1.0
**Beschreibung:** Helper libraries for analyzing cube data – ARR Bridge and SaaS metrics
**Python:** >=3.11

**Kernabhängigkeiten:**
- ibis-framework[duckdb,polars] >=9.0.0 (Backend-agnostic SQL)
- polars >=1.34.0 (DataFrames)
- duckdb >=1.4.1 (In-process OLAP)
- thefuzz >=0.22.1 + jaro-winkler >=2.0.3 (Fuzzy Matching)

**Hauptfeatures:**
1. **ARR Bridge** (`ARRBridgeQueries`): Analysiert MRR-Bewegungen (New Business, Churn, Upsell, Downsell) zwischen zwei Perioden. Backend-agnostisch über Ibis (DuckDB, Polars, PostgreSQL, etc.). SQL-Injection-sicher durch Ibis-Expressions.
2. **Entity Matching** (`match_entities`, `match_single`): Fuzzy-Matching von Firmennamen über mehrere Algorithmen (Jaro-Winkler, Levenshtein token-sort/-set). Stop-Words für DE/EN/FR Rechtformen.
3. **Revenue Recognition** (`recognize_revs`): Verteilt Vertragsrevenues über Zeiträume (daily/weekly/monthly/quarterly/yearly).
4. **Schema Auto-Detection** (`ColumnMapping`): Erkennt automatisch Spalten aus verschiedenen Cube-Outputs per case-insensitiver Namenssuche.

**Datenquellen (Input-Formate):**
- Polars DataFrame (from_polars)
- DuckDB Datei (from_duckdb_path)
- DuckDB Connection (from_duckdb_connection)
- Beliebiges Ibis-Backend (from_ibis_connection)
- Parquet-Dateien (via Polars/DuckDB)

## Deviations
Keine

## Nächste Schritte
Die extrahierten Informationen fließen direkt in Task 01-02 ein: Erstellung der OUTBID_CONTEXT.md mit allen Template-Abschnitten.
