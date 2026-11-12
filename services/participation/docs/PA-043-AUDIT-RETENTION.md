# PA-043 audit trail, legal hold, and retention

This runbook describes the guarantees implemented by
`migrations/002_audit_logs_enrichment.sql`,
`migrations/003_audit_retention.sql`, and the SQLite equivalent created by
`ParticipationDatabase.init_schema()`.

## What is enforced

| Guarantee | SQLite | PostgreSQL |
|---|---|---|
| `audit_logs` is append-only (no `UPDATE`/`DELETE`) | trigger | trigger (migration 003) |
| `TRUNCATE` is rejected | n/a | trigger (migration 003) |
| Legal holds are append-only | triggers | triggers (migration 003) |
| Releasing an inactive hold is rejected | trigger | trigger (migration 003) |
| Tenant isolation for hold events | application scope | RLS `FORCE` (migration 003) |
| Demo reset preserves audit history | yes | yes |

Every migration after `001` is applied in order on startup by
`ParticipationDatabase.init_schema()`. If a migration is missing from the
deployment image the application fails closed with `FileNotFoundError` instead
of starting with partial guarantees.

## Audit record fields

`AuditService.record()` writes:

- `request_id`: server-generated UUIDv7. Incoming `X-Request-Id` is ignored on
  purpose so clients cannot forge audit correlation.
- `actor` / `actor_type` / `actor_id`: `actor` is `unattributed` until a
  verified principal exists. Only `request.state.verified_audit_principal`
  (typed `VerifiedAuditPrincipal`, set by an authentication adapter) populates
  the actor. Client-supplied actor fields are never trusted.
- `result`: `success`, `rejected`, `failed`, or `indeterminate`.
- `reason`: operator-supplied justification, whitespace-normalized and capped
  at 500 characters. **Operators must not paste personal data here**; it is
  stored verbatim.
- `payload_json`: non-PII metadata only (for example `updated_fields`,
  `checklist`, counts, or `problem_code`). Never store names, emails, document
  numbers, tokens, or free-text answers.

Sensitive operations (correction, admission, rejection, import, privacy
request, DLQ replay) require an `AuditContext`. The audit row is written in the
same transaction as the business change, so a failed audit insert rolls the
change back. Rejections and unexpected failures are recorded in a separate
transaction; if that write fails it is logged and the original response is
returned unchanged.

## Retention and purge

The retention window is **2 years**. The database currently rejects every
`DELETE` on `audit_logs` with `audit purge is disabled pending maintenance
authorization`, even for rows older than two years and without a legal hold.
This is intentional: there is no authorized purge procedure, role, or job in
this repository. Do not disable the trigger or delete rows manually.

If a purge is authorized later, it must:

1. Run with a dedicated maintenance role (never the application role).
2. Delete only rows older than the window with no active legal hold.
3. Emit its own audit evidence before deleting.
4. Be reviewed as a migration, not as an ad-hoc script.

`/participation/v1/demo/reset` preserves audit history; it does not truncate
`audit_logs`.

## Legal holds

`audit_legal_hold_events` stores append-only `placed`/`released` events keyed by
`organization_id`, `hold_id`, and a hex `authority_ref_digest` (never the
authority reference itself). A `released` event is only accepted when the
matching `placed` event exists and has not been released. While a hold is
active, deletion of a matching audit record is rejected.

Operational checks (run with tenant context set):

```sql
-- Active holds
SELECT organization_id, hold_id, target_type, target_id, MAX(created_at)
FROM audit_legal_hold_events
GROUP BY organization_id, hold_id, target_type, target_id
HAVING COUNT(*) FILTER (WHERE event_type = 'released') = 0;

-- Audit volume by action/result
SELECT action, result, COUNT(*)
FROM audit_logs
GROUP BY action, result
ORDER BY action, result;
```

## Deployment caveat: superusers bypass RLS

Row-level security does not apply to superusers or roles with `BYPASSRLS`. The
provided `docker-compose.yml` creates `participation_app` as the image's
superuser, so tenant isolation (including legal-hold RLS) is **not enforced**
in that local setup. The PostgreSQL integration test that checks cross-tenant
isolation skips itself when it detects a superuser and explains why. Production
must connect with a non-superuser role without `BYPASSRLS` that owns nothing
beyond the required grants.
