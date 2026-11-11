CREATE TABLE IF NOT EXISTS outbox_deliveries (
    delivery_id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    outbox_id uuid NOT NULL REFERENCES outbox(id),
    message_id uuid NOT NULL UNIQUE,
    domain_event_id uuid NOT NULL,
    topic text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    aggregate_version bigint NOT NULL,
    partition_key text NOT NULL,
    envelope_json jsonb NOT NULL,
    occurred_at timestamptz NOT NULL,
    state text NOT NULL CHECK (
        state IN ('pending', 'in_flight', 'published', 'dead_letter')
    ),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz NOT NULL,
    lease_token text,
    lease_expires_at timestamptz,
    last_error_code text,
    replay_of_delivery_id uuid REFERENCES outbox_deliveries(delivery_id),
    replay_request_hash text,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    UNIQUE (organization_id, replay_request_hash)
);

CREATE INDEX IF NOT EXISTS idx_outbox_deliveries_due
    ON outbox_deliveries (
        organization_id, state, next_attempt_at, lease_expires_at
    );

CREATE INDEX IF NOT EXISTS idx_outbox_deliveries_domain_event
    ON outbox_deliveries (organization_id, domain_event_id);

ALTER TABLE outbox_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbox_deliveries FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS outbox_deliveries_tenant_isolation_policy
    ON outbox_deliveries;
CREATE POLICY outbox_deliveries_tenant_isolation_policy
    ON outbox_deliveries
    USING (
        organization_id = current_setting('app.current_organization_id', true)::uuid
    )
    WITH CHECK (
        organization_id = current_setting('app.current_organization_id', true)::uuid
    );

CREATE TABLE IF NOT EXISTS event_dead_letters (
    dead_letter_id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    delivery_id uuid NOT NULL UNIQUE
        REFERENCES outbox_deliveries(delivery_id),
    message_id uuid NOT NULL,
    domain_event_id uuid NOT NULL,
    topic text NOT NULL,
    attempts integer NOT NULL,
    error_code text NOT NULL CHECK (
        error_code IN (
            'transport_unavailable',
            'transport_timeout',
            'transport_rejected',
            'invalid_envelope',
            'rate_limited',
            'transport_error'
        )
    ),
    failed_at timestamptz NOT NULL DEFAULT now(),
    replayed_at timestamptz,
    replay_delivery_id uuid REFERENCES outbox_deliveries(delivery_id)
);

CREATE INDEX IF NOT EXISTS idx_event_dead_letters_tenant
    ON event_dead_letters (organization_id, failed_at DESC);

ALTER TABLE event_dead_letters ENABLE ROW LEVEL SECURITY;
ALTER TABLE event_dead_letters FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS event_dead_letters_tenant_isolation_policy
    ON event_dead_letters;
CREATE POLICY event_dead_letters_tenant_isolation_policy
    ON event_dead_letters
    USING (
        organization_id = current_setting('app.current_organization_id', true)::uuid
    )
    WITH CHECK (
        organization_id = current_setting('app.current_organization_id', true)::uuid
    );

CREATE TABLE IF NOT EXISTS event_gaps (
    gap_id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    consumer_name text NOT NULL,
    topic text NOT NULL,
    aggregate_id uuid NOT NULL,
    expected_version bigint NOT NULL CHECK (expected_version > 0),
    observed_version bigint NOT NULL CHECK (observed_version > expected_version),
    detected_by_domain_event_id uuid NOT NULL,
    state text NOT NULL CHECK (state IN ('open', 'resolved')),
    detected_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    UNIQUE (
        organization_id, consumer_name, aggregate_id, expected_version
    )
);

CREATE INDEX IF NOT EXISTS idx_event_gaps_open
    ON event_gaps (organization_id, state, aggregate_id);

ALTER TABLE event_gaps ENABLE ROW LEVEL SECURITY;
ALTER TABLE event_gaps FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS event_gaps_tenant_isolation_policy ON event_gaps;
CREATE POLICY event_gaps_tenant_isolation_policy ON event_gaps
    USING (
        organization_id = current_setting('app.current_organization_id', true)::uuid
    )
    WITH CHECK (
        organization_id = current_setting('app.current_organization_id', true)::uuid
    );
