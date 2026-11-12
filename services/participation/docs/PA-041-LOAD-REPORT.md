# PA-041 load and concurrency report

## Result and scope

This report records an actual in-process FastAPI load run against a temporary
SQLite database. It is a reproducible functional/concurrency profile, not a
production-capacity forecast. No shared or development database was used.

The backlog does not define a numerical peak. For this test only, the base
peak is assumed to be **10 simultaneous registration requests**, matching the
test event capacity; this is a test-sizing assumption, not measured or
forecast demand. The 3x profile is **30 simultaneous registrations** against
capacity 10, followed by five simultaneous cancellations of confirmed
registrations to exercise waitlist promotion.

## Environment and measured results

| Measure | Observed |
|---|---:|
| Execution | Windows, Python 3.14.6 |
| Database | Temporary SQLite file, SQLite 3.50.4 |
| API client | FastAPI `TestClient`, in-process ASGI |
| Base peak assumption | 10 concurrent registrations (synthetic) |
| 3x registration load | 30 requests, 30 workers |
| Concurrent cancellations | 5 requests, 5 workers |
| Total business requests | 35 |
| Registration phase wall time | 0.370030 s |
| Registration throughput | 81.074 requests/s |
| Registration p95 latency | 350.738 ms |
| Cancellation phase wall time | 0.060886 s |
| Cancellation throughput | 82.121 requests/s |
| Cancellation p95 latency | 58.849 ms |
| Combined measured phase time | 0.430916 s |
| Combined throughput | 81.222 requests/s |
| Combined p95 latency | 350.738 ms |
| HTTP/application errors | 0 (30 × 201, 5 × 200) |

The figures above are the final captured run at `2026-11-11
14:38:44 UTC`; concurrent local runs can vary.

Latency is measured per request after a synchronization barrier releases all
workers. Phase throughput is successful request count divided by phase wall
time. Combined throughput uses the sum of registration and cancellation
phase wall times. p95 uses the **nearest-rank** method: sort the N request
latencies and select the one-based element `ceil(0.95 * N)`. Values are
rounded to three decimals for display.

## Concurrency invariants

| Invariant | Observed |
|---|---:|
| Capacity / confirmed counter after run | 10 / 10 |
| Confirmed registrations after run | 10 |
| Initially waitlisted | 20 |
| Promoted from FIFO waitlist | 5 unique registrations |
| Remaining on waitlist | 15 |
| Cancelled registrations | 5 |
| Ticket rows | 15 |
| Active tickets | 10 |
| Unique ticket JTIs | Yes (15 / 15) |
| Maximum active tickets per registration | 1 |
| Active tickets on non-confirmed registrations | 0 |

The 30 registrations produced exactly 10 confirmed and 20 waitlisted
participants. Five concurrent cancellations revoked their active tickets and
promoted five distinct registrations in waitlist order. Counter values,
registration states, waitlist entries, and ticket records were checked after
the operations.

## PA-040 interaction

The load app used finite, test-scoped PA-040 settings: 90 requests per
60-second window per IP/event/device and a challenge threshold of 60. Each
synthetic registration used a server-generated signed device token. All 30
registration requests were permitted and no challenge was issued. These
overrides exist only inside this pytest case and are restored by pytest; they
do not disable or change the service's configured defaults.

## Database connection pool

This run used SQLite, whose implementation has one connection protected by a
process-local reentrant lock. It does not measure PostgreSQL connection-pool
acquisition, wait time, utilization, or contention.

The PostgreSQL implementation in `app/db/database.py` configures
`psycopg_pool.ConnectionPool(min_size=1, max_size=20, timeout=10.0)`.
Transactions and read cursors acquire one connection from this pool for their
context and return it at context exit. No isolated PostgreSQL test instance
was configured for this run, so **no PostgreSQL pool usage or pool-related
latency is reported**. PostgreSQL pool validation remains outstanding.

## Limitations

- The assumed peak is a test fixture, not user traffic or an approved capacity
  target.
- The in-process TestClient and SQLite lock serialize database work and do not
  model network latency, PostgreSQL concurrency, multiple API processes, or
  production infrastructure.
- The measured p95 is real for this local SQLite profile only; it is not a
  PostgreSQL or production p95 and no service-level target was supplied.
- PostgreSQL integration tests were not run because no test URL was
  configured. Do not extrapolate these figures to a PostgreSQL deployment.
- The run checks a burst of 30 registrations (3x the assumed 10-request
  baseline) and five concurrent cancellation/promotion operations, not a
  sustained-duration load test.

## Repeat the profile

From `services/participation`, using the existing Participation virtual
environment:

```powershell
Remove-Item Env:DATABASE_URL,Env:TEST_DATABASE_URL,Env:PARTICIPATION_TEST_POSTGRES_URL,Env:PARTICIPATION_DB_URL -ErrorAction SilentlyContinue
& 'C:\Users\luisi\venvs\participation\Scripts\python.exe' -m pytest -q -s tests/test_pa041_load.py
```

The test creates and removes its own temporary SQLite directory. Its
`PA041_RESULT_JSON` line contains the measured values and invariant summary.
To repeat the PostgreSQL profile, first provide an approved, isolated test
database; do not point the suite at a development or production database.

## Validation status

- PA-041 profile plus the existing registration-concurrency and
  cancellation/promotion regression tests: **3 passed**.
- Full Participation suite: **60 passed, 8 skipped, 0 failed**. The eight
  skips require a PostgreSQL test URL; no PostgreSQL database was configured.
- The suite emits an existing Starlette/httpx deprecation warning.
- After authorization, frontend dependencies were installed from
  `package-lock.json` with `npm ci --no-audit --no-fund` (68 packages).
  `npx tsc --noEmit` and `npm run build` both passed with Node 24.12.0, npm
  11.7.0, TypeScript 5.9.3, and Vite 5.4.21.
