CREATE TABLE IF NOT EXISTS audit_legal_hold_events (
    event_id uuid PRIMARY KEY,
    hold_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    target_type text NOT NULL,
    target_id text NOT NULL,
    event_type text NOT NULL CHECK (event_type IN ('placed', 'released')),
    authority_ref_digest text NOT NULL
        CHECK (authority_ref_digest ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (organization_id, hold_id, event_type)
);

CREATE INDEX IF NOT EXISTS idx_audit_legal_hold_target
    ON audit_legal_hold_events (
        organization_id, target_type, target_id, hold_id, event_type
    );

ALTER TABLE audit_legal_hold_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_legal_hold_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS audit_legal_hold_events_tenant_isolation_policy
    ON audit_legal_hold_events;
CREATE POLICY audit_legal_hold_events_tenant_isolation_policy
    ON audit_legal_hold_events
    USING (
        organization_id = current_setting('app.current_organization_id', true)::uuid
    )
    WITH CHECK (
        organization_id = current_setting('app.current_organization_id', true)::uuid
    );

CREATE OR REPLACE FUNCTION participation_validate_audit_legal_hold_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE' OR TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'audit legal hold events are append-only';
    END IF;

    IF NEW.event_type = 'released' THEN
        PERFORM 1
        FROM audit_legal_hold_events placed
        WHERE placed.organization_id = NEW.organization_id
          AND placed.hold_id = NEW.hold_id
          AND placed.target_type = NEW.target_type
          AND placed.target_id = NEW.target_id
          AND placed.event_type = 'placed'
          AND NOT EXISTS (
              SELECT 1
              FROM audit_legal_hold_events released
              WHERE released.organization_id = placed.organization_id
                AND released.hold_id = placed.hold_id
                AND released.event_type = 'released'
          )
        FOR UPDATE;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'cannot release an inactive audit legal hold';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS audit_legal_hold_events_validate_insert
    ON audit_legal_hold_events;
CREATE TRIGGER audit_legal_hold_events_validate_insert
    BEFORE INSERT ON audit_legal_hold_events
    FOR EACH ROW
    EXECUTE FUNCTION participation_validate_audit_legal_hold_event();

DROP TRIGGER IF EXISTS audit_legal_hold_events_no_update
    ON audit_legal_hold_events;
CREATE TRIGGER audit_legal_hold_events_no_update
    BEFORE UPDATE ON audit_legal_hold_events
    FOR EACH ROW
    EXECUTE FUNCTION participation_validate_audit_legal_hold_event();

DROP TRIGGER IF EXISTS audit_legal_hold_events_no_delete
    ON audit_legal_hold_events;
CREATE TRIGGER audit_legal_hold_events_no_delete
    BEFORE DELETE ON audit_legal_hold_events
    FOR EACH ROW
    EXECUTE FUNCTION participation_validate_audit_legal_hold_event();

CREATE OR REPLACE FUNCTION participation_reject_audit_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit history is append-only';
END;
$$;

DROP TRIGGER IF EXISTS audit_legal_hold_events_no_truncate
    ON audit_legal_hold_events;
CREATE TRIGGER audit_legal_hold_events_no_truncate
    BEFORE TRUNCATE ON audit_legal_hold_events
    FOR EACH STATEMENT
    EXECUTE FUNCTION participation_reject_audit_truncate();

CREATE OR REPLACE FUNCTION participation_guard_audit_log_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'audit_logs is append-only';
    END IF;

    IF OLD.created_at IS NULL
       OR OLD.created_at > statement_timestamp() - interval '2 years' THEN
        RAISE EXCEPTION 'audit retention period has not expired';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM audit_legal_hold_events placed
        WHERE placed.organization_id = OLD.organization_id
          AND placed.target_type = OLD.target_type
          AND placed.target_id = OLD.target_id::text
          AND placed.event_type = 'placed'
          AND NOT EXISTS (
              SELECT 1
              FROM audit_legal_hold_events released
              WHERE released.organization_id = placed.organization_id
                AND released.hold_id = placed.hold_id
                AND released.event_type = 'released'
          )
    ) THEN
        RAISE EXCEPTION 'audit record is protected by legal hold';
    END IF;

    RAISE EXCEPTION 'audit purge is disabled pending maintenance authorization';
END;
$$;

DROP TRIGGER IF EXISTS audit_logs_append_only
    ON audit_logs;
CREATE TRIGGER audit_logs_append_only
    BEFORE UPDATE OR DELETE ON audit_logs
    FOR EACH ROW
    EXECUTE FUNCTION participation_guard_audit_log_mutation();

DROP TRIGGER IF EXISTS audit_logs_no_truncate
    ON audit_logs;
CREATE TRIGGER audit_logs_no_truncate
    BEFORE TRUNCATE ON audit_logs
    FOR EACH STATEMENT
    EXECUTE FUNCTION participation_reject_audit_truncate();
