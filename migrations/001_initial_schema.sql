-- 001_initial_schema.sql
--
-- The checked-in schema for the Ventura COA platform. Plain PostgreSQL DDL,
-- applied in filename order; there is no ORM and no migration framework in
-- this deployment, so this file is the source of truth for table shape.
--
-- How it is applied: the one-shot `migrate` compose service runs it against
-- $P3_AUDIT_DSN once audit-db is healthy, and the `api` and `worker` services
-- declare `depends_on: migrate: condition: service_completed_successfully`, so
-- no process starts against an unmigrated database. `-v ON_ERROR_STOP=1` is
-- what makes that gate meaningful: without it psql reports SQL errors on stdout
-- and still exits 0, so a broken apply would look like a successful one.
--
-- It can also be applied by hand from a shell that can reach the database:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_initial_schema.sql
--
-- Every statement below is idempotent (IF NOT EXISTS), so re-applying the file
-- is safe and is a no-op against an already-migrated database.
--
-- Tables:
--   coa_metadata      - the COA header the completion gate and the report read
--   audit_events      - the append-only hash chain (FR12, 5-year retention FR13)
--   plant_mappings    - the sister-plant mapping (FR9)
--   admin_users       - the external-account registry (FR1)
--   schema_migrations - the applied-migration registry used by the runner

BEGIN;

-- ---------------------------------------------------------------------------
-- schema_migrations: what the runner has already applied to this database.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT        PRIMARY KEY,
    checksum   TEXT        NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- coa_metadata: one row per Certificate of Analysis.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS coa_metadata (
    id                BIGSERIAL   PRIMARY KEY,
    coa_id            TEXT        NOT NULL,
    plant_of_origin   TEXT,
    product_code      TEXT,
    lot_number        TEXT,
    completion_status TEXT        NOT NULL DEFAULT 'pending',
    issued_at         TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT coa_metadata_coa_id_key UNIQUE (coa_id),
    CONSTRAINT coa_metadata_completion_status_check
        CHECK (completion_status IN ('pending', 'completed', 'failed'))
);

-- Search and report access paths: by plant, by product, by open shipments.
CREATE INDEX IF NOT EXISTS coa_metadata_plant_idx
    ON coa_metadata (plant_of_origin);
CREATE INDEX IF NOT EXISTS coa_metadata_product_idx
    ON coa_metadata (product_code);
CREATE INDEX IF NOT EXISTS coa_metadata_status_issued_idx
    ON coa_metadata (completion_status, issued_at DESC);

-- ---------------------------------------------------------------------------
-- audit_events: the immutable, hash-chained audit trail (FR12).
--
-- sequence is an internal surrogate row id. The chain position that
-- src/audit/trail/chain.py verifies - dense, zero-based, monotonic - is
-- sequence_number below; it is written explicitly by
-- src.audit.trail.repository.PostgresAuditStorage rather than derived from the
-- surrogate, so a read-back matches what the in-memory backend produced.
--
-- prev_hash links to the previous row's entry_hash, and both sequence_number
-- and entry_hash are covered by unique indexes so the chain cannot be forked by
-- a bad writer.
--
-- Rows are never updated or deleted in place; the five-year retention sweep
-- (FR13) is the only remover of rows and reads a strictly ordered window
-- through audit_events_occurred_at_idx.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_events (
    sequence       BIGSERIAL   PRIMARY KEY,
    event_id       UUID        NOT NULL,
    event_type     TEXT        NOT NULL,
    actor_id       TEXT        NOT NULL,
    coa_id         TEXT,
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload_json   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    prev_hash      TEXT        NOT NULL,
    entry_hash     TEXT        NOT NULL,
    CONSTRAINT audit_events_event_id_key UNIQUE (event_id),
    CONSTRAINT audit_events_entry_hash_key UNIQUE (entry_hash)
);

CREATE INDEX IF NOT EXISTS audit_events_coa_idx
    ON audit_events (coa_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS audit_events_actor_idx
    ON audit_events (actor_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS audit_events_type_time_idx
    ON audit_events (event_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS audit_events_occurred_at_idx
    ON audit_events (occurred_at);

-- ---------------------------------------------------------------------------
-- plant_mappings: sister-plant equivalences used by the FR9 print check.
--
-- At most one active mapping may exist per source/target pair, which the
-- partial unique index enforces; retired rows stay for history.
--
-- target_plant_id is the sister plant and active is the model's is_active: the
-- repository maps the two names onto these columns rather than duplicating a
-- column, so there is exactly one source of truth for each fact.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS plant_mappings (
    id               BIGSERIAL   PRIMARY KEY,
    source_plant_id  TEXT        NOT NULL,
    target_plant_id  TEXT        NOT NULL,
    active           BOOLEAN     NOT NULL DEFAULT TRUE,
    created_by       TEXT        NOT NULL,
    updated_by       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS plant_mappings_active_pair_key
    ON plant_mappings (source_plant_id, target_plant_id) WHERE active;
CREATE INDEX IF NOT EXISTS plant_mappings_source_idx
    ON plant_mappings (source_plant_id);
CREATE INDEX IF NOT EXISTS plant_mappings_target_idx
    ON plant_mappings (target_plant_id);

-- ---------------------------------------------------------------------------
-- admin_users: the external-account registry maintained by a Ventura admin
-- (FR1/FR2). Mirrors the UserDTO the portal's /admin/users endpoint serves:
-- one row per account, with the role restricted to the registry's
-- ALLOWED_ROLES set (internal, 3pl, customer).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admin_users (
    id            BIGSERIAL   PRIMARY KEY,
    user_id       TEXT        NOT NULL,
    username      TEXT        NOT NULL,
    email         TEXT,
    role          TEXT        NOT NULL,
    organization  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ,
    CONSTRAINT admin_users_user_id_key UNIQUE (user_id),
    CONSTRAINT admin_users_username_key UNIQUE (username),
    CONSTRAINT admin_users_role_check
        CHECK (role IN ('internal', '3pl', 'customer'))
);

CREATE INDEX IF NOT EXISTS admin_users_role_idx
    ON admin_users (role);
CREATE INDEX IF NOT EXISTS admin_users_organization_idx
    ON admin_users (organization);

-- ---------------------------------------------------------------------------
-- Runtime persistence columns.
--
-- The three durable repositories - the FR9 mapping store, the FR1/FR2 registry
-- and the FR12 trail - address rows by identifiers the tables above did not yet
-- carry. These statements are idempotent too, so 001 remains safe to re-apply.
-- ---------------------------------------------------------------------------

-- plant_mappings.mapping_uuid: the stable identifier every caller addresses a
-- mapping by. The bigint id stays an internal surrogate; the index is partial so
-- a row that predates this column is not forced to collide with itself.
ALTER TABLE plant_mappings ADD COLUMN IF NOT EXISTS mapping_uuid TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS plant_mappings_mapping_uuid_key
    ON plant_mappings (mapping_uuid) WHERE mapping_uuid IS NOT NULL;

-- audit_events.sequence_number: the dense, zero-based chain position the
-- verifier pins. Assigned under a transaction advisory lock by
-- PostgresAuditStorage, so two concurrent writers cannot mint the same position.
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS sequence_number INTEGER;

CREATE UNIQUE INDEX IF NOT EXISTS audit_events_sequence_number_key
    ON audit_events (sequence_number) WHERE sequence_number IS NOT NULL;

-- audit_events.external_event_id: the model's event_id verbatim. The event_id
-- column above is a UUID, but an entry may carry a caller-chosen idempotency key
-- instead, so the original string is kept here and read back in preference. The
-- event_id unique constraint then doubles as idempotency: a replayed key folds
-- to the same UUID and is rejected rather than appended twice.
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS external_event_id TEXT;

-- audit_events.details / ip_address: the two remaining AuditLogEntry fields.
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS details TEXT;
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS ip_address TEXT;

-- Reading the trail by chain position is the hot path (verify, export, and
-- every append's position lookup), so give it a dedicated ordered index.
CREATE INDEX IF NOT EXISTS audit_events_sequence_number_idx
    ON audit_events (sequence_number);

COMMIT;
