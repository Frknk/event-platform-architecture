# Contratos entre equipos

Este documento es el índice normativo de integración. El inventario HTTP completo está en [contracts/API-CONTRACTS.md](contracts/API-CONTRACTS.md), los mensajes en [contracts/EVENT-CONTRACTS.md](contracts/EVENT-CONTRACTS.md), las transiciones en [contracts/STATE-MACHINES.md](contracts/STATE-MACHINES.md) y la propiedad/retención de datos en [contracts/DATA-OWNERSHIP-AND-RETENTION.md](contracts/DATA-OWNERSHIP-AND-RETENTION.md).

## Reglas no negociables

- Cada agregado tiene un solo servicio escritor y una sola base lógica propietaria.
- Los consumidores materializan proyecciones; nunca escriben la base del productor.
- APIs privadas usan OIDC/OAuth2, audience exacta, scopes mínimos y tenant derivado de claims.
- Los comandos mutables usan `Idempotency-Key`; la edición usa ETag/`If-Match`.
- Los eventos se publican con transactional outbox y se consumen con inbox idempotente.
- PII no viaja en eventos salvo proyección cifrada con propósito, consumidor y retención explícitos.
- No hay transacciones distribuidas; los fallos parciales se reconcilian mediante estados y jobs observables.

## Dependencias autorizadas

| Productor/owner | Consumidor | Contrato permitido | Prohibido |
|---|---|---|---|
| Platform Foundation | equipos 2–4 | claims, memberships/capabilities, tenant/domain resolution, paquetes auth/telemetry | reenviar credenciales humanas entre servicios |
| Event Configuration | Content, Participation, Onsite | release completo versionado y eventos de cancelación | leer tablas de configuración |
| Participation | Content, Onsite, Operations | disponibilidad, ticket/revocación y proyecciones purpose-bound | respuestas completas de formulario en Content/Onsite |
| Onsite | Operations | hechos append-only de asistencia y correcciones | modificar registro o ticket |
| Operations | HukID | HTTPS a Integration API mediante adaptador outbound del Equipo 4 | acceso de HukID al bus/DB de Events |

## Flujos críticos

1. **Publicación:** Configuration valida y publica un manifiesto completo. Cada consumidor registra `pending`, valida hash/versión, materializa y activa atómicamente. Una release incompleta nunca es visible.
2. **Registro:** Participation valida binding/consentimiento/cupo en su propia transacción, persiste registro + outbox y devuelve receipt. Content sólo muestra disponibilidad orientativa.
3. **Check-in:** Onsite valida JWS, versión, revocación, asignación y alcance; crea un hecho append-only protegido por unicidad. Las correcciones son hechos compensatorios.
4. **Certificación:** Operations consume asistencia y una proyección cifrada autorizada, evalúa una regla versionada y emite PDF idempotente.
5. **HukID opcional:** el adaptador del Equipo 4 toma un certificado elegible y consentimiento vigente, crea una issuance request por HTTPS/OAuth2 e idempotencia, guarda el ID externo y reconcilia hasta estado terminal. El Equipo 5 no consume eventos de Events.

## Entrega de eventos (PA-042)

- El envelope nace con `published_at: null` y `attempts: 0`; sólo el worker de entrega lo completa cuando el transporte confirma `message_id` y `accepted=true`.
- La entrega es at-least-once con lease: un crash tras la aceptación remota puede reenviar. Los consumidores deduplican por `domain_event_id`.
- Los envelopes inválidos y los fallos permanentes terminan en `event_dead_letters`; los reintentos usan backoff con jitter y un límite de intentos.
- Los releases con `aggregate.version` menor al esperado se registran en `inbox` pero no se proyectan (`stale_ignored`); los huecos hacia adelante se registran en `event_gaps` y se rechazan con `409 release_gap`.

## Ticket y caché

- QR JWS asimétrico: `jti` opaco, `kid`, `aud=onsite`, organización, evento, scope, `iat`, `exp` y versión; sin PII.
- La lista de revocación es firmada/versionada. Online exige frescura menor a 15 s y falla cerrado si la copia venció.
- Unicidad efectiva: `(ticket_id, business_event_id, scope)`; repetición devuelve el resultado previo.
- Offline queda fuera del MVP; requiere protocolo separado de dispositivo, cola cifrada, TTL y reconciliación.
- Cache pública: `host/site/locale/event_slug/publication_id`; sin cookies privadas. Purge usa surrogate keys y es reintentable.

## Gate contractual

Antes de implementar una integración deben existir: schema OpenAPI/AsyncAPI validable, ejemplos válidos, owner, versión, auth/scopes, idempotencia, errores estables, límites, compatibilidad, contract tests y política de deprecación. Una tabla narrativa no sustituye esos artefactos; este paquete fija el inventario que cada equipo debe convertir a contratos como código durante Sprint 0.
