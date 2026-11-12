CREATE TABLE IF NOT EXISTS audit_logs (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    actor text NOT NULL,
    action text NOT NULL,
    target_type text NOT NULL,
    target_id text NOT NULL,
    reason text NOT NULL,
    payload_json jsonb,
    created_at timestamptz NOT NULL
);

ALTER TABLE audit_logs
    ADD COLUMN IF NOT EXISTS actor_type text,
    ADD COLUMN IF NOT EXISTS actor_id text,
    ADD COLUMN IF NOT EXISTS result text,
    ADD COLUMN IF NOT EXISTS request_id text;

CREATE INDEX IF NOT EXISTS idx_audit_logs_tenant_target_created
    ON audit_logs (organization_id, target_type, target_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_logs_tenant_request
    ON audit_logs (organization_id, request_id);

ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_logs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS audit_logs_tenant_isolation_policy ON audit_logs;
CREATE POLICY audit_logs_tenant_isolation_policy ON audit_logs
    USING (organization_id = current_setting('app.current_organization_id', true)::uuid)
    WITH CHECK (organization_id = current_setting('app.current_organization_id', true)::uuid);
