-- Migration 001: Add missing columns to dispo_blasts and preferred_states to cash_buyers
-- Run in Supabase SQL Editor

-- dispo_blasts: add ghl_contact_id and ghl_opp_id used by blast_buyers() and reply routing
ALTER TABLE dispo_blasts
    ADD COLUMN IF NOT EXISTS ghl_contact_id TEXT,
    ADD COLUMN IF NOT EXISTS ghl_opp_id     TEXT;

CREATE INDEX IF NOT EXISTS idx_dispo_blasts_ghl_contact_id
    ON dispo_blasts (ghl_contact_id);

-- cash_buyers: add preferred_states used by match_buyers() and DealSauce scraper
ALTER TABLE cash_buyers
    ADD COLUMN IF NOT EXISTS preferred_states TEXT[] DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_cash_buyers_states
    ON cash_buyers USING GIN (preferred_states);

-- cash_buyers: add mailing address columns and source used by DealSauce scraper
-- (DealSauce has 0 skip-trace credits so phone/email are unavailable;
--  mailing_address is the fallback upsert key for records without contact info)
ALTER TABLE cash_buyers
    ADD COLUMN IF NOT EXISTS mailing_address TEXT,
    ADD COLUMN IF NOT EXISTS mailing_city    TEXT,
    ADD COLUMN IF NOT EXISTS mailing_state   TEXT,
    ADD COLUMN IF NOT EXISTS mailing_zip     TEXT,
    ADD COLUMN IF NOT EXISTS source          TEXT;

CREATE INDEX IF NOT EXISTS idx_cash_buyers_mailing_address
    ON cash_buyers (mailing_address)
    WHERE mailing_address IS NOT NULL;
