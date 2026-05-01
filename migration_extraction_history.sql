-- Safe migration: sync extraction_history with the current SQLAlchemy model.
-- Handles: table missing, columns missing, VARCHAR → TEXT type mismatch.
-- SAFE: never drops rows. Run once against your PostgreSQL database.

BEGIN;

-- ── 1. Create table if it does not exist ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS extraction_history (
    id          VARCHAR(36)  PRIMARY KEY,
    user_id     VARCHAR(36)  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename    TEXT         NOT NULL DEFAULT '',
    spir_no     TEXT,
    tag_count   INTEGER      NOT NULL DEFAULT 0,
    spare_count INTEGER      NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── 2. Add any missing columns (idempotent) ───────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'extraction_history' AND column_name = 'filename'
    ) THEN
        ALTER TABLE extraction_history ADD COLUMN filename TEXT NOT NULL DEFAULT '';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'extraction_history' AND column_name = 'spir_no'
    ) THEN
        ALTER TABLE extraction_history ADD COLUMN spir_no TEXT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'extraction_history' AND column_name = 'tag_count'
    ) THEN
        ALTER TABLE extraction_history ADD COLUMN tag_count INTEGER NOT NULL DEFAULT 0;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'extraction_history' AND column_name = 'spare_count'
    ) THEN
        ALTER TABLE extraction_history ADD COLUMN spare_count INTEGER NOT NULL DEFAULT 0;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'extraction_history' AND column_name = 'created_at'
    ) THEN
        ALTER TABLE extraction_history ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
    END IF;
END $$;

-- ── 3. Fix column types: VARCHAR → TEXT (idempotent) ─────────────────────────
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'extraction_history'
          AND column_name = 'filename'
          AND data_type = 'character varying'
    ) THEN
        ALTER TABLE extraction_history ALTER COLUMN filename TYPE TEXT;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'extraction_history'
          AND column_name = 'spir_no'
          AND data_type = 'character varying'
    ) THEN
        ALTER TABLE extraction_history ALTER COLUMN spir_no TYPE TEXT;
    END IF;
END $$;

-- ── 4. Indexes (idempotent) ───────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS ix_extraction_history_user_id    ON extraction_history(user_id);
CREATE INDEX IF NOT EXISTS ix_extraction_history_created_at ON extraction_history(created_at);

COMMIT;
