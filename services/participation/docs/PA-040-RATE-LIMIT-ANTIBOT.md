# PA-040 rate limiting and bot protection

The API applies process-local sliding-window limits before selected public
write routes: registration, application creation, registration-access
requests, and invitation acceptance/declination. Requests are counted by the
ASGI peer IP, event where present, and a server-signed browser token where
present. `X-Forwarded-For` and arbitrary device headers are not used to derive
identity. The browser token is a rate-limit signal only, not an authentication
credential.

## Responses and challenge

Requests over a configured limit receive `429` and a `Retry-After` header.
Requests beyond a challenge threshold receive `428` with a signed, expiring,
one-use proof-of-work challenge. The client solves SHA-256 leading-zero work
and retries with `X-Participation-Challenge` and
`X-Participation-Proof`. Successful challenge proofs are bound to peer IP,
event, and the signed device token. Reuse, invalid proofs, and expired tokens
are rejected; business handlers are not called until the challenge passes.

This proof-of-work raises automated request cost; it is not a human CAPTCHA
and cannot stop a sufficiently resourced bot. A risk policy requiring human
verification needs a separately approved CAPTCHA/provider integration.

## Configuration

Settings are read at process startup:

| Environment variable | Default |
|---|---:|
| `PARTICIPATION_RATE_LIMIT_WINDOW_SECONDS` | `60` |
| `PARTICIPATION_RATE_LIMIT_IP_MAX` | `60` |
| `PARTICIPATION_RATE_LIMIT_EVENT_MAX` | `300` |
| `PARTICIPATION_RATE_LIMIT_DEVICE_MAX` | `60` |
| `PARTICIPATION_RATE_LIMIT_IP_CHALLENGE_AFTER` | `30` |
| `PARTICIPATION_RATE_LIMIT_EVENT_CHALLENGE_AFTER` | `100` |
| `PARTICIPATION_RATE_LIMIT_DEVICE_CHALLENGE_AFTER` | `30` |
| `PARTICIPATION_ANTIBOT_CHALLENGE_DIFFICULTY` | `16` bits |
| `PARTICIPATION_ANTIBOT_CHALLENGE_TTL_SECONDS` | `180` |
| `PARTICIPATION_ANTIBOT_SECRET` | random per process; if set, at least 32 bytes |
| `PARTICIPATION_RATE_LIMIT_ALLOWLIST` | empty; comma-separated IPs/CIDRs |

Challenge thresholds are counts within the configured sliding window. A
challenge is required after the count becomes greater than the corresponding
threshold; a hard limit still returns 429. Set each threshold below its
matching hard limit, or the hard limit will be reached before a challenge is
requested. Invalid values stop app startup rather than silently disabling
protection.

The allowlist is a deployment-controlled environment setting. It bypasses
only this rate-limit/challenge check; it does not grant application
permissions or bypass route authentication/authorization. No admin endpoint
is provided to change it.

## Operations, privacy, and limits

The limiter, challenge signing key, replay tracking, and aggregate counters
are in memory and local to one process. Restarting invalidates outstanding
challenges/device tokens. Multiple workers or replicas do not share counters;
per-process limits can multiply with replicas. For multi-worker deployments,
configure the same `PARTICIPATION_ANTIBOT_SECRET` on every worker so signed
tokens remain valid across workers. Challenge replay tracking remains local;
a shared atomic store would be required for cluster-wide limits and strict
cross-worker one-use enforcement, and is intentionally not added here.

The middleware uses the socket peer address. Behind a reverse proxy, configure
the ASGI server to trust only the known proxy chain before deriving a client
address. This application deliberately does not trust forwarding headers by
itself; without trusted proxy configuration, requests may be grouped under
the proxy address.

`app.state.abuse_protection.metrics_snapshot()` returns aggregate counters
with fixed outcome/dimension labels only. There is no public metrics endpoint,
and raw IPs, event identifiers, device tokens, and personal data are not
stored in the counters.
