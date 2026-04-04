-- Migration 002: Cash buyer scoring and grading columns
-- Run in Supabase SQL Editor after migration 001

-- ── Score and grade columns ──────────────────────────────────────────────────
-- score:         1-100 investor activity score (DealMachine import → internal history)
-- score_source:  where the score came from ('dealsauce_import', 'internal')
-- grade:         A/B/C/D derived from deal_count + response_rate
-- response_rate: % of blasts replied to (positive or unclear)
-- grade_updated_at: last time grade was recalculated

ALTER TABLE cash_buyers
    ADD COLUMN IF NOT EXISTS score            INTEGER NOT NULL DEFAULT 50
                                              CHECK (score BETWEEN 1 AND 100),
    ADD COLUMN IF NOT EXISTS score_source     TEXT    NOT NULL DEFAULT 'dealsauce_import',
    ADD COLUMN IF NOT EXISTS grade            TEXT    NOT NULL DEFAULT 'D'
                                              CHECK (grade IN ('A', 'B', 'C', 'D')),
    ADD COLUMN IF NOT EXISTS response_rate    NUMERIC(5, 2) NOT NULL DEFAULT 0.00,
    ADD COLUMN IF NOT EXISTS grade_updated_at TIMESTAMPTZ;

COMMENT ON COLUMN cash_buyers.score IS
    '1-100 investor activity score. Initially set from DealMachine data on import; '
    'recalculated from internal deal history once deal_count >= 1.';

COMMENT ON COLUMN cash_buyers.grade IS
    'A: deal_count>=3 OR response_rate>=60%. '
    'B: deal_count>=1 OR response_rate>=30%. '
    'C: responded to at least one blast, no deal yet. '
    'D: never responded (default on import).';

-- ── UNIQUE constraints required by scraper upsert on_conflict ────────────────
-- Without these, supabase-py upsert ON CONFLICT has no unique key to resolve on.
-- Use NULLS NOT DISTINCT so multiple NULL values don't violate the constraint.

ALTER TABLE cash_buyers
    ADD CONSTRAINT uq_cash_buyers_email
        UNIQUE NULLS NOT DISTINCT (email);

ALTER TABLE cash_buyers
    ADD CONSTRAINT uq_cash_buyers_phone
        UNIQUE NULLS NOT DISTINCT (phone);

ALTER TABLE cash_buyers
    ADD CONSTRAINT uq_cash_buyers_mailing_address
        UNIQUE NULLS NOT DISTINCT (mailing_address);

-- ── Index for grade-based queries (blast targeting, buyer priority) ───────────
CREATE INDEX IF NOT EXISTS idx_cash_buyers_grade
    ON cash_buyers (grade)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_cash_buyers_score
    ON cash_buyers (score DESC)
    WHERE status = 'active';
