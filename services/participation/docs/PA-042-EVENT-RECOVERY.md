# PA-042 event delivery and recovery runbook

## Scope and current integration status

Participation writes domain changes and outbox records in one database
transaction. Migration `004_event_reconciliation.sql` adds delivery state,
retry scheduling, dead-letter metadata, and detected release-version gaps.
Inbox effects and their inbox row are committed together.

The repository does not contain a production event-bus adapter, scheduler, or
authenticated operator integration. The worker is an explicit one-shot
processor; it does not run at application startup. Without a configured
transport it exits before opening a database or claiming rows. Do not treat
`published_at` values on pre-004 outbox records as proof of delivery: those
rows intentionally have no delivery state and are never automatically sent.

## Deployment prerequisites

1. Coordinate the transport adapter and acknowledgment semantics with its
   owner. The adapter must implement `publish(envelope, headers=...)` and
   return `DeliveryReceipt(message_id=<accepted message>, accepted=True)`
   only after the transport confirms acceptance. Convert transport failures to
   `TransportError` with one of the documented classifications; never include
   exception text or payload content in the error code.
2. Coordinate trusted tenant identity and operator authorization with Team 1.
   The local CLI requires an organization UUID, but it is not an authentication
   mechanism and must not be exposed as an HTTP operation.
3. Coordinate release manifest source, sequence semantics, and upstream replay
   with Team 2. Participation only detects a gap when it receives a valid,
   strictly increasing `aggregate.version` for the same release aggregate.
4. Apply the forward-only migration through the team's controlled deployment
   process. Application initialization also applies it, following the
   existing migration pattern. Take the normal database backup first; this
   runbook does not authorize applying migrations to an existing/shared
   database.

## One-shot outbox processing

Run from `services/participation` with the project's configured Python
environment. `PARTICIPATION_EVENT_TRANSPORT` must name a real adapter factory
available to the worker process. Set database and organization values from the
deployment's trusted configuration; do not copy production credentials into a
developer shell or repository file.

```powershell
$env:DATABASE_URL = '<approved Participation database URL>'
$env:PARTICIPATION_EVENT_TRANSPORT = 'deployment_adapter:build_transport'
python -m app.events.worker --organization-id '<trusted organization UUID>' --limit 50
```

For an explicitly selected local SQLite database, use
`PARTICIPATION_SQLITE_PATH` instead of `DATABASE_URL`. The file must already
exist and have the migrations applied. The worker opens the database without
initializing, seeding, or migrating it.

The command reports counts only. It claims a bounded batch with leases, sends
outside the database transaction, and records `published` only after a
matching positive transport receipt. Retries use capped exponential
full-jitter backoff and stop at the configured attempt limit. Repeated
invocations are safe; delivery is at-least-once, not exactly-once. Consumers
must deduplicate by `domain_event_id`.

If there is no transport adapter, the command prints a blocked message and
exits without claiming rows. A crash after remote acceptance but before the
database acknowledgment can cause redelivery after the lease expires.

## Failed delivery and DLQ

`event_dead_letters` stores only opaque IDs, topic, attempt count, a fixed
error classification, and timestamps; it does not copy the event payload or
exception text. The original outbox/delivery record remains available for
investigation. Do not manually edit status or delete queue rows to recover.

Inspect DLQ counts using the approved database access path with tenant context
set. Correlate by `delivery_id`, `message_id`, and `error_code`; do not export
envelopes or participant data to logs/tickets. Check adapter health and
contract/schema compatibility before requesting replay.

Replay is currently an internal service operation only. It requires an
idempotency key and a trusted, attributed `AuditContext`; it atomically creates
a new pending delivery and PA-043 audit record. It preserves
`domain_event_id` and `occurred_at`, generates a new `message_id`, and sends
the transport header `replay=true`. There is deliberately no public replay
endpoint or unauthenticated CLI command. Until the Team 1 authorization
integration exists, operators have no supported production replay entry point.

## Release gaps

When a release event arrives with `aggregate.version` greater than the next
expected version, Participation records the expected and observed versions in
`event_gaps`, leaves the event out of the processed inbox, and returns
`409 release_gap`. The producer must retain/replay the event; Participation
does not fabricate missing event contents. Request replay from Team 2, deliver
the missing version, then redeliver the rejected event. The gap is marked
resolved when the expected version is processed. No gap is inferred from
timestamps, UUID ordering, or `published_at`.

The demo release-ingest route is not an authenticated production consumer.
Production acknowledgment/retry behavior requires the agreed inbound transport
adapter and trusted tenant context.

## Legacy rows and observability

Rows already in `outbox` before migration 004 are preserved without inferred
delivery records, attempts, or DLQ entries. Reconcile them against the
authoritative transport/archive before taking any manual action. New rows start
as `pending`; no worker is scheduled automatically.

Track pending age, retry count, lease expiry, DLQ count, and open gaps as
aggregates keyed by tenant/topic. Never put full envelope JSON, PII, tokens,
or raw exception messages in logs or metrics.
