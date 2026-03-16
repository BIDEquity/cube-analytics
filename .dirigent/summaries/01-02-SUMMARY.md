# Task 01-02: Read supporting modules

## Was wurde gemacht
Die drei unterstützenden Module gelesen und ihre Feature-Details, Algorithmen und Input/Output-Contracts extrahiert.

### entity_matching.py
**Zweck:** Fuzzy-Matching von Firmennamen für Entitätsdeduplizierung und -verknüpfung zwischen Datensätzen.

**Algorithmen (gewichtet):**
- `fuzz.ratio` (Levenshtein, Gewicht 0.15)
- `fuzz.partial_ratio` (bestes Partial-Match, 0.10)
- `fuzz.token_sort_ratio` (Sorted-Token-Vergleich, 0.25)
- `fuzz.token_set_ratio` (Set-basierter Token-Vergleich, 0.25)
- `jaro_winkler_metric` (Jaro-Winkler-Distanz, 0.25)

**Stop-Words:** DE/EN/FR Rechtsformen (GmbH, AG, Inc, LLC, SA, SARL, etc.) + generische Business-Begriffe (Group, Holdings, Solutions, etc.)

**Öffentliche API:**
- `match_entities(source, target, ...)` → `list[MatchResult]`
  - Input: `list[dict]` oder `pl.DataFrame`
  - Threshold: 0.0–1.0 (default 0.7)
  - `duplicate_mode=True`: Duplikate innerhalb einer Liste finden
- `match_single(name, candidates, ...)` → `list[tuple[str, float, bool]]`
  - Einfache Convenience-Funktion ohne IDs
- `MatchResult` dataclass: source_id, source_name, target_id, target_name, score, is_exact, scores (individual)

### revenue_recognition.py
**Zweck:** Verteilt Vertragsrevenues gleichmäßig über Zeiträume (Revenue Recognition / Periodisierung).

**Algorithmus:** Für jeden Contract-Record wird per `join_where` eine Zeitreihe mit allen Perioden zwischen `date_from` und `date_to` erstellt. Revenue wird durch Anzahl Perioden geteilt (`revenue_per_period = revenue / n_periods`).

**Intervalle (`RecognitionInterval` Enum):**
- `daily` (1d), `weekly` (1w), `monthly` (1mo), `quarterly` (3mo), `yearly` (1y)

**Öffentliche API:**
- `recognize_revs(df, id_column, revenue_column, date_from_column, date_to_column, interval, start_period, end_period, wide_format)` → `pl.DataFrame`
  - Input: `pl.DataFrame` mit Contract-Daten
  - Output: Long-Format (Zeile pro Period) oder Wide-Format (Pivot mit Perioden als Spalten)
  - Validierung: Raises `RevenueRecognitionInvariantViolation` bei leeren IDs

### schema.py
**Zweck:** Automatische Erkennung von Spaltenmappings aus verschiedenen Cube-Outputs (case-insensitive, prioritätsbasiert).

**Erkennungslogik:** Für jedes semantische Feld wird eine priorisierte Kandidatenliste durchsucht (first match wins, case-insensitive).

**Spaltenmappings (Kandidaten-Reihenfolge):**
- `period`: month, month_date, period, date (**required**)
- `customer`: group_level, customer, customer_name, name (**required**)
- `revenue`: revenue, amount, value, mrr (**required**)
- `customer_id`: group_level_id, customer_id, id (optional)
- `is_recurring`: is_recurring, recurring (optional)
- `region`: region, geography, location, country (optional)
- `industry`: industry, sector, vertical (optional)
- `price_increase_effect`: price_increase_effect_absolute, price_increase_effect, price_effect (optional)

**Öffentliche API:**
- `ColumnMapping.detect(columns)` → `ColumnMapping` (frozen dataclass)
  - Raises `ValueError` wenn required columns fehlen
- `ColumnMapping.validate_columns_exist(available)` → validates post-construction

## Geänderte Dateien
Keine (reine Lese-Task)

## Deviations
Keine

## Nächste Schritte
Alle Modul-Details sind jetzt vollständig extrahiert. Die OUTBID_CONTEXT.md kann erstellt werden mit:
- Entity Matching als Key Feature mit 5 Algorithmen, Stop-Words für DE/EN/FR
- Revenue Recognition mit 5 Intervallen (daily bis yearly)
- Schema Auto-Detection für verschiedene Cube-Output-Formate
- Klare Input/Output-Contracts für alle öffentlichen APIs
