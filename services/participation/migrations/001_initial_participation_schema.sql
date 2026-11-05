-- =============================================================================
-- EQUIPO 3: PARTICIPATION — MIGRACIÓN FORWARD-ONLY 001 (PostgreSQL 16+)
-- Cumple con:
--   * AD-3: Cupo atómico (confirmed_count <= capacity), waitlist monotónica y tickets JWS
--   * AD-5: Base lógica aislada sin FKs hacia otros microservicios
--   * AD-8: PII cifrada en reposo (AES-GCM) y búsqueda/deduplicación por blind index HMAC
--   * AD-12: Multi-tenancy con FORCE ROW LEVEL SECURITY (RLS) por organization_id
--   * STATE-MACHINES.md: Constraints explícitas para estados válidos
-- =============================================================================

BEGIN;

-- 1. Proyección local de releases publicados por Event Configuration (PA-010)
CREATE TABLE IF NOT EXISTS release_projections (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    site_slug text NOT NULL,
    event_id uuid NOT NULL,
    event_slug text NOT NULL,
    event_title text NOT NULL,
    publication_id uuid NOT NULL UNIQUE,
    release_version integer NOT NULL CHECK (release_version >= 1),
    state text NOT NULL CHECK (state IN ('pending', 'active', 'failed', 'superseded')),
    binding_purpose text NOT NULL CHECK (binding_purpose IN ('registration', 'application')),
    binding_state text NOT NULL CHECK (binding_state IN ('scheduled', 'open', 'closed', 'cancelled')),
    form_version_id uuid NOT NULL,
    consent_version text NOT NULL,
    schema_hash text NOT NULL,
    form_schema_json jsonb NOT NULL,
    ui_schema_json jsonb NOT NULL,
    consent_text text NOT NULL,
    requires_approval boolean NOT NULL DEFAULT false,
    requires_email_verification boolean NOT NULL DEFAULT false,
    revision bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_release_projections_tenant_event
    ON release_projections (organization_id, event_id, state);

-- 2. Contadores atómicos de capacidad por evento (PA-011)
CREATE TABLE IF NOT EXISTS capacity_counters (
    event_id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    publication_id uuid NOT NULL,
    capacity integer NOT NULL CHECK (capacity >= 0),
    confirmed_count integer NOT NULL DEFAULT 0 CHECK (confirmed_count >= 0 AND confirmed_count <= capacity),
    waitlist_enabled boolean NOT NULL DEFAULT true,
    waitlist_count integer NOT NULL DEFAULT 0 CHECK (waitlist_count >= 0),
    next_waitlist_seq integer NOT NULL DEFAULT 1 CHECK (next_waitlist_seq >= 1),
    revision bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_capacity_counters_tenant
    ON capacity_counters (organization_id, event_id);

-- 3. Registros de participantes con PII cifrada y blind index HMAC (PA-002, PA-004)
CREATE TABLE IF NOT EXISTS registrations (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    event_id uuid NOT NULL,
    publication_id uuid NOT NULL,
    form_version_id uuid NOT NULL,
    state text NOT NULL CHECK (
        state IN (
            'pending_verification',
            'pending_review',
            'waitlisted',
            'confirmed',
            'rejected',
            'cancelled'
        )
    ),
    email_hmac text NOT NULL,
    email_masked text NOT NULL,
    full_name_masked text NOT NULL,
    pii_ciphertext text NOT NULL,
    pii_key_id text NOT NULL,
    source text NOT NULL DEFAULT 'public_web',
    cancellation_reason text,
    rejection_reason text,
    revision bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    confirmed_at timestamptz,
    cancelled_at timestamptz
);

-- Deduplicación activa por organización + evento + email_hmac (permite re-registro si canceló antes)
CREATE UNIQUE INDEX IF NOT EXISTS uq_registrations_active_email_hmac
    ON registrations (organization_id, event_id, email_hmac)
    WHERE state NOT IN ('cancelled', 'rejected');

CREATE INDEX IF NOT EXISTS idx_registrations_tenant_event_state
    ON registrations (organization_id, event_id, state, created_at DESC);

-- 4. Respuestas de formulario cifradas (AD-8)
CREATE TABLE IF NOT EXISTS registration_answers (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    registration_id uuid NOT NULL UNIQUE REFERENCES registrations(id),
    form_version_id uuid NOT NULL,
    answers_ciphertext text NOT NULL,
    key_id text NOT NULL,
    schema_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- 5. Registro auditable de consentimientos (PA-002, PA-003)
CREATE TABLE IF NOT EXISTS consents (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    subject_type text NOT NULL CHECK (subject_type IN ('registration', 'application')),
    subject_id uuid NOT NULL,
    consent_version text NOT NULL,
    consent_hash text NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_consents_tenant_subject
    ON consents (organization_id, subject_type, subject_id);

-- 6. Lista de espera ordenada monotónicamente (PA-012, PA-013)
CREATE TABLE IF NOT EXISTS waitlist_entries (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    event_id uuid NOT NULL,
    registration_id uuid NOT NULL UNIQUE REFERENCES registrations(id),
    position integer NOT NULL CHECK (position >= 1),
    state text NOT NULL CHECK (state IN ('waiting', 'promoted', 'cancelled', 'expired')),
    promoted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, event_id, position)
);

CREATE INDEX IF NOT EXISTS idx_waitlist_fifo
    ON waitlist_entries (organization_id, event_id, state, position ASC);

-- 7. Tickets QR con firma JWS asimétrica sin PII (PA-008, PA-015, AD-11)
CREATE TABLE IF NOT EXISTS tickets (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    event_id uuid NOT NULL,
    registration_id uuid NOT NULL REFERENCES registrations(id),
    jti text NOT NULL UNIQUE,
    state text NOT NULL CHECK (state IN ('active', 'revoked', 'expired', 'replaced')),
    version integer NOT NULL DEFAULT 1,
    key_id text NOT NULL,
    audience text NOT NULL DEFAULT 'onsite',
    scope text NOT NULL DEFAULT 'general',
    ticket_jws text NOT NULL,
    issued_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    revision bigint NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_tickets_tenant_registration
    ON tickets (organization_id, registration_id, state);

-- 8. Lista de revocación de tickets para sincronización rápida con Onsite (PA-015)
CREATE TABLE IF NOT EXISTS ticket_revocations (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    event_id uuid NOT NULL,
    ticket_id uuid NOT NULL REFERENCES tickets(id),
    jti text NOT NULL UNIQUE,
    reason_code text NOT NULL,
    revoked_at timestamptz NOT NULL DEFAULT now()
);

-- 9. Emprendimientos, miembros y postulaciones (Épica PA-C)
CREATE TABLE IF NOT EXISTS ventures (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    event_id uuid NOT NULL,
    name text NOT NULL,
    track text NOT NULL,
    leader_email_hmac text NOT NULL,
    leader_pii_ciphertext text NOT NULL,
    revision bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS team_memberships (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    venture_id uuid NOT NULL REFERENCES ventures(id),
    email_hmac text NOT NULL,
    email_masked text NOT NULL,
    role text NOT NULL CHECK (role IN ('leader', 'co_founder', 'member')),
    state text NOT NULL CHECK (state IN ('active', 'left', 'removed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS team_invitations (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    venture_id uuid NOT NULL REFERENCES ventures(id),
    email_hmac text NOT NULL,
    email_masked text NOT NULL,
    role text NOT NULL,
    token_hash text NOT NULL UNIQUE,
    state text NOT NULL CHECK (state IN ('pending', 'accepted', 'declined', 'expired', 'revoked')),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS applications (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    event_id uuid NOT NULL,
    venture_id uuid NOT NULL REFERENCES ventures(id),
    form_version_id uuid NOT NULL,
    state text NOT NULL CHECK (
        state IN (
            'draft',
            'submitted',
            'needs_changes',
            'resubmitted',
            'admitted',
            'rejected',
            'withdrawn'
        )
    ),
    current_revision_number integer NOT NULL DEFAULT 1,
    draft_answers_ciphertext text NOT NULL,
    snapshot_hash text,
    revision bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    submitted_at timestamptz
);

CREATE TABLE IF NOT EXISTS application_revisions (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    application_id uuid NOT NULL REFERENCES applications(id),
    revision_number integer NOT NULL,
    snapshot_ciphertext text NOT NULL,
    snapshot_hash text NOT NULL,
    submitted_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (application_id, revision_number)
);

CREATE TABLE IF NOT EXISTS application_files (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    application_id uuid NOT NULL REFERENCES applications(id),
    field_name text NOT NULL,
    file_name text NOT NULL,
    mime_type text NOT NULL,
    size_bytes bigint NOT NULL,
    sha256_hash text NOT NULL,
    state text NOT NULL CHECK (state IN ('uploading', 'scanning', 'ready', 'quarantined', 'deleted')),
    created_at timestamptz NOT NULL DEFAULT now(),
    scanned_at timestamptz
);

-- 10. Claves de Idempotencia (API-CONTRACTS.md)
CREATE TABLE IF NOT EXISTS idempotency_keys (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    operation text NOT NULL,
    idempotency_key text NOT NULL,
    request_hash text NOT NULL,
    status_code integer NOT NULL,
    response_body_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    UNIQUE (organization_id, operation, idempotency_key)
);

-- 11. Consumer Inbox y Transactional Outbox (EVENT-CONTRACTS.md)
CREATE TABLE IF NOT EXISTS inbox (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    consumer_name text NOT NULL,
    domain_event_id uuid NOT NULL,
    topic text NOT NULL,
    aggregate_id uuid NOT NULL,
    aggregate_version bigint NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (consumer_name, domain_event_id)
);

CREATE TABLE IF NOT EXISTS outbox (
    id uuid PRIMARY KEY,
    message_id uuid NOT NULL UNIQUE,
    domain_event_id uuid NOT NULL UNIQUE,
    organization_id uuid NOT NULL,
    business_event_id uuid NOT NULL,
    topic text NOT NULL,
    schema_version integer NOT NULL DEFAULT 1,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    aggregate_version bigint NOT NULL,
    partition_key text NOT NULL,
    envelope_json jsonb NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    attempts integer NOT NULL DEFAULT 0
);

-- =============================================================================
-- ROW LEVEL SECURITY (RLS) OBLIGATORIO POR TENANT (AD-12)
-- =============================================================================
DO $$
DECLARE
    tbl text;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'release_projections',
        'capacity_counters',
        'registrations',
        'registration_answers',
        'consents',
        'waitlist_entries',
        'tickets',
        'ticket_revocations',
        'ventures',
        'team_memberships',
        'team_invitations',
        'applications',
        'application_revisions',
        'application_files',
        'idempotency_keys',
        'inbox',
        'outbox'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY;', tbl);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY;', tbl);
        EXECUTE format(
            'DROP POLICY IF EXISTS tenant_isolation_policy ON %I;
             CREATE POLICY tenant_isolation_policy ON %I
             USING (organization_id = current_setting(''app.current_organization_id'', true)::uuid)
             WITH CHECK (organization_id = current_setting(''app.current_organization_id'', true)::uuid);',
            tbl, tbl
        );
    END LOOP;
END
$$;

COMMIT;
