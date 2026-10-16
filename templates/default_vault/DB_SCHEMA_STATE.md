# Database Schema State

The current shape of the company's databases so that agents never guess table or column names. Empty until `sync_db_schema`, the database watcher or the repository analysis fills it.

## Engines

- (no engines documented yet)

## Tables

- (no tables documented yet)

## Migration Log

- (none yet)

## Agent Notes

Belongs here: one `###` section per table with its columns, types, nullability and defaults, plus indexes worth knowing, and a dated line per migration.
Does not belong here: row data, credentials, connection strings.
A section whose only line starts with "(no" is empty: it holds no facts, so never assume table or column names from it. Introspect the real database or ask engineering.
Prefer the `sync_db_schema` tool over manual edits; it rewrites only the tables that changed.
