# Database migrations

These are the checked-in schema migrations, applied by ops in filename order
(`001_initial_schema.sql`, then `002_...`, and so on) with `psql`.

There is no alembic and no ORM in this deployment: each file is plain PostgreSQL
DDL wrapped in a transaction, so a failed apply rolls back completely.

Every file is idempotent (`CREATE TABLE IF NOT EXISTS`), so re-applying an
already-migrated database is a no-op rather than an error.

Schema changes ship as a new numbered file; existing files are never edited once
they have been applied to a shared environment.
