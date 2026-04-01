-- =============================================================================
-- Supabase/PostgreSQL Schema: Real Estate Investment Business
-- Helpful Homebuyers USA
-- Generated: 2026-03-21
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Utility: auto-update updated_at trigger function
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- ---------------------------------------------------------------------------
-- 1. leads — motivated seller leads from county records, MLS, skip tracing
-- ---------------------------------------------------------------------------
COMMENT ON FUNCTION set_updated_at IS
    'Trigger function that sets updated_at to now() on every UPDATE.';

CREATE TABLE leads (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source          TEXT CHECK (source IN (
                        'county_recorder', 'mls', 'referral',
                        'direct_mail', 'social'
                    )),
    lead_type       TEXT CHECK (lead_type IN (
                        'probate', 'bankruptcy', 'pre_foreclosure',
                        'tax_delinquent', 'expired_listing', 'general'
                    )),
    first_name      TEXT,
    last_name       TEXT,
    phone           TEXT,
    email           TEXT,
    property_address TEXT,
    property_city   TEXT,
    property_state  TEXT,
    property_zip    TEXT,
    status          TEXT NOT NULL DEFAULT 'new'
                        CHECK (status IN ('new', 'contacted', 'qualified', 'dead')),
    ghl_contact_id  TEXT,
    skip_traced_at  TIMESTAMPTZ,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE leads IS
    'Motivated seller leads sourced from county records, MLS, skip tracing, '
    'direct mail, social media, and referrals. Central table linking to calls, '
    'properties, and deals.';

CREATE TRIGGER leads_set_updated_at
    BEFORE UPDATE ON leads
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ---------------------------------------------------------------------------
-- 2. calls — all Retell AI call records
-- ---------------------------------------------------------------------------
CREATE TABLE calls (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    retell_call_id      TEXT UNIQUE,
    agent_id            TEXT,
    agent_name          TEXT CHECK (agent_name IN (
                            'shelby', 'alex', 'cole', 'jordan'
                        )),
    ghl_contact_id      TEXT,
    lead_id             UUID REFERENCES leads(id) ON DELETE SET NULL,
    call_outcome        TEXT,
    duration_seconds    INTEGER,
    sentiment           TEXT,
    summary             TEXT,
    transcript          TEXT,
    appointment_set     BOOLEAN NOT NULL DEFAULT false,
    motivation_level    INTEGER CHECK (motivation_level BETWEEN 1 AND 10),
    urgency_level       TEXT,
    flags               TEXT,
    prop_address        TEXT,
    offer_range         TEXT,
    email_captured      TEXT,
    followup_sms_sent   BOOLEAN NOT NULL DEFAULT false,
    followup_tag_applied TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE calls IS
    'Retell AI call records including transcripts, sentiment analysis, '
    'motivation scoring, and follow-up tracking. Links to leads via lead_id '
    'and to GoHighLevel via ghl_contact_id.';


-- ---------------------------------------------------------------------------
-- 3. properties — property details and valuation
-- ---------------------------------------------------------------------------
CREATE TABLE properties (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id         UUID REFERENCES leads(id) ON DELETE SET NULL,
    address         TEXT,
    city            TEXT,
    state           TEXT,
    zip             TEXT,
    county          TEXT,
    beds            INTEGER,
    baths           NUMERIC(3, 1),
    sqft            INTEGER,
    year_built      INTEGER,
    lot_size_sqft   INTEGER,
    condition       TEXT CHECK (condition IN (
                        'excellent', 'good', 'fair', 'poor', 'teardown'
                    )),
    arv             NUMERIC(12, 2),
    repair_estimate NUMERIC(12, 2),
    estimated_offer NUMERIC(12, 2),
    status          TEXT NOT NULL DEFAULT 'prospect'
                        CHECK (status IN (
                            'prospect', 'active', 'under_contract',
                            'closed', 'dead'
                        )),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE properties IS
    'Property details including physical attributes, condition assessment, '
    'ARV, repair estimates, and disposition status.';

CREATE TRIGGER properties_set_updated_at
    BEFORE UPDATE ON properties
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ---------------------------------------------------------------------------
-- 4. deals — active deals and pipeline tracking
-- ---------------------------------------------------------------------------
CREATE TABLE deals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id     UUID REFERENCES properties(id) ON DELETE SET NULL,
    lead_id         UUID REFERENCES leads(id) ON DELETE SET NULL,
    status          TEXT NOT NULL DEFAULT 'prospecting'
                        CHECK (status IN (
                            'prospecting', 'negotiating', 'under_contract',
                            'closed', 'dead'
                        )),
    offer_amount    NUMERIC(12, 2),
    arv             NUMERIC(12, 2),
    purchase_price  NUMERIC(12, 2),
    repair_estimate NUMERIC(12, 2),
    profit_estimate NUMERIC(12, 2),
    exit_strategy   TEXT CHECK (exit_strategy IN (
                        'flip', 'rental', 'wholesale', 'novation'
                    )),
    contract_date   DATE,
    close_date      DATE,
    earnest_money   NUMERIC(10, 2),
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE deals IS
    'Active deals tracking offer amounts, ARV, repair costs, profit estimates, '
    'exit strategy, and contract milestones from prospecting through close.';

CREATE TRIGGER deals_set_updated_at
    BEFORE UPDATE ON deals
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ---------------------------------------------------------------------------
-- 5. cash_buyers — buyer network for wholesales and dispositions
-- ---------------------------------------------------------------------------
CREATE TABLE cash_buyers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    first_name      TEXT,
    last_name       TEXT,
    phone           TEXT,
    email           TEXT,
    company         TEXT,
    buy_criteria    JSONB,
    price_range_min NUMERIC(12, 2),
    price_range_max NUMERIC(12, 2),
    preferred_cities TEXT[],
    preferred_states TEXT[],
    deal_count      INTEGER NOT NULL DEFAULT 0,
    last_deal_at    TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'inactive')),
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE cash_buyers IS
    'Cash buyer network for wholesale dispositions. Stores buy criteria, '
    'preferred markets, price ranges, and deal history.';

CREATE TRIGGER cash_buyers_set_updated_at
    BEFORE UPDATE ON cash_buyers
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ---------------------------------------------------------------------------
-- 6. comps — comparable sales for ARV analysis
-- ---------------------------------------------------------------------------
CREATE TABLE comps (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id     UUID REFERENCES properties(id) ON DELETE CASCADE,
    address         TEXT,
    city            TEXT,
    state           TEXT,
    zip             TEXT,
    sale_price      NUMERIC(12, 2),
    sqft            INTEGER,
    beds            INTEGER,
    baths           NUMERIC(3, 1),
    year_built      INTEGER,
    sale_date       DATE,
    distance_miles  NUMERIC(5, 2),
    price_per_sqft  NUMERIC(10, 2),
    source          TEXT CHECK (source IN ('zillow', 'redfin', 'mls')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE comps IS
    'Comparable sales used to calculate ARV for subject properties. '
    'Sourced from Zillow, Redfin, and MLS data.';


-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

-- leads
CREATE INDEX idx_leads_ghl_contact_id ON leads (ghl_contact_id);
CREATE INDEX idx_leads_phone           ON leads (phone);
CREATE INDEX idx_leads_type_status     ON leads (lead_type, status);

-- calls
CREATE INDEX idx_calls_ghl_contact_id  ON calls (ghl_contact_id);
CREATE INDEX idx_calls_retell_call_id  ON calls (retell_call_id);
CREATE INDEX idx_calls_created_at      ON calls (created_at);

-- deals
CREATE INDEX idx_deals_status          ON deals (status);

-- cash_buyers
CREATE INDEX idx_cash_buyers_status    ON cash_buyers (status);


-- ---------------------------------------------------------------------------
-- 7. dispo_blasts — idempotent record of buyer blast events
-- ---------------------------------------------------------------------------
CREATE TABLE dispo_blasts (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_opportunity_id   TEXT NOT NULL,
    buyer_id              UUID NOT NULL REFERENCES cash_buyers(id),
    ghl_contact_id        TEXT,
    ghl_opp_id            TEXT,
    blasted_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    response              TEXT,
    response_at           TIMESTAMPTZ,
    outcome               TEXT,
    UNIQUE(deal_opportunity_id, buyer_id)
);

COMMENT ON TABLE dispo_blasts IS
    'One row per (deal, buyer) blast attempt. UNIQUE constraint prevents double-blasting. '
    'ghl_contact_id enables reply routing back to this record.';

CREATE INDEX idx_dispo_blasts_ghl_contact ON dispo_blasts (ghl_contact_id);
CREATE INDEX idx_dispo_blasts_deal        ON dispo_blasts (deal_opportunity_id);
