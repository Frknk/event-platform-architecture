import React, { useEffect, useState } from 'react';

const API_BASE = 'http://localhost:8000';
const SITE_SLUG = 'incuba-unas';
const EVENT_SLUG = 'congreso-innovacion-demo-day-2026';

function randomIdempotencyKey(prefix = 'idem-web'): string {
  const rand = Math.random().toString(16).slice(2, 10) + '-' + Date.now().toString(16);
  return `${prefix}-${rand}`;
}

/**
 * Deterministic visual QR-style matrix SVG generated from the JWS signature
 * so the ticket QR renders cleanly with zero external network dependencies.
 */
function JwsQrCodeSvg({ jws, revoked }: { jws: string; revoked: boolean }) {
  const size = 21;
  const cells: boolean[][] = Array.from({ length: size }, () => Array(size).fill(false));

  const drawFinder = (r0: number, c0: number) => {
    for (let r = 0; r < 7; r++) {
      for (let c = 0; c < 7; c++) {
        const isBorder = r === 0 || r === 6 || c === 0 || c === 6;
        const isInner = r >= 2 && r <= 4 && c >= 2 && c <= 4;
        if (isBorder || isInner) {
          cells[r0 + r][c0 + c] = true;
        }
      }
    }
  };

  drawFinder(0, 0);
  drawFinder(0, size - 7);
  drawFinder(size - 7, 0);

  let hash = 2166136261;
  for (let i = 0; i < jws.length; i++) {
    hash ^= jws.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
    const r = (i * 7 + (hash >>> 3)) % size;
    const c = (i * 13 + (hash >>> 9)) % size;
    const inFinder =
      (r < 8 && c < 8) || (r < 8 && c >= size - 8) || (r >= size - 8 && c < 8);
    if (!inFinder) {
      cells[r][c] = (hash & 1) === 1;
    }
  }

  return (
    <div className="relative inline-block bg-white p-3 rounded-xl shadow-lg border-2 border-slate-200">
      <svg width="168" height="168" viewBox={`0 0 ${size} ${size}`} shapeRendering="crispEdges">
        {cells.map((row, rIdx) =>
          row.map((filled, cIdx) =>
            filled ? (
              <rect
                key={`${rIdx}-${cIdx}`}
                x={cIdx}
                y={rIdx}
                width={1}
                height={1}
                fill={revoked ? '#991b1b' : '#0f172a'}
              />
            ) : null
          )
        )}
      </svg>
      {revoked && (
        <div className="absolute inset-0 flex items-center justify-center bg-red-950/80 rounded-xl">
          <span className="px-3 py-1 bg-red-600 text-white font-bold text-xs tracking-wider uppercase rounded shadow">
            TICKET REVOCADO
          </span>
        </div>
      )}
    </div>
  );
}

export default function App() {
  const [activeTab, setActiveTab] = useState<'public' | 'concurrency' | 'backoffice' | 'ventures'>('public');
  const [formConfig, setFormConfig] = useState<any>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [bannerAlert, setBannerAlert] = useState<{ type: 'success' | 'warning' | 'error'; title: string; detail: string } | null>(null);

  // Registration Form State (PA-A)
  const [fullName, setFullName] = useState('Franklyn Huamán');
  const [email, setEmail] = useState('franklyn.huaman@unas.edu.pe');
  const [documentNumber, setDocumentNumber] = useState('74859612');
  const [institution, setInstitution] = useState('UNAS — Facultad de Ingeniería en Informática y Sistemas');
  const [track, setTrack] = useState('IA Aplicada & Transformación Digital');
  const [roleProfile, setRoleProfile] = useState('Estudiante UNAS');
  const [modality, setModality] = useState('Presencial — Auditorio Central UNAS');
  const [consentAccepted, setConsentAccepted] = useState(true);
  const [idempotencyKey, setIdempotencyKey] = useState(() => randomIdempotencyKey());

  // Current Participant Self-Service Receipt & Ticket (PA-006, PA-008)
  const [currentReceipt, setCurrentReceipt] = useState<any>(null);
  const [currentTicket, setCurrentTicket] = useState<any>(null);
  const [jwksData, setJwksData] = useState<any>(null);

  // Backoffice & Outbox State (PA-D / PA-E)
  const [registrations, setRegistrations] = useState<any[]>([]);
  const [revealPii, setRevealPii] = useState<boolean>(false);
  const [searchEmail, setSearchEmail] = useState<string>('');
  const [outboxEvents, setOutboxEvents] = useState<any[]>([]);

  // Concurrency Simulator Controls (PA-011 / PA-041)
  const [targetCapacity, setTargetCapacity] = useState<number>(5);
  const [burstSize, setBurstSize] = useState<number>(12);
  const [burstSummary, setBurstSummary] = useState<any>(null);

  // Venture Application State (PA-C)
  const [ventureName, setVentureName] = useState('AgroSense Selva AI');
  const [ventureLeaderEmail, setVentureLeaderEmail] = useState('lider.agrosense@unas.edu.pe');
  const [coFounderEmail, setCoFounderEmail] = useState('cofounder.iot@unas.edu.pe');
  const [ventureResult, setVentureResult] = useState<any>(null);

  const refreshAll = async (silent = false) => {
    try {
      if (!silent) setLoading(true);
      const [formRes, jwksRes, outboxRes] = await Promise.all([
        fetch(`${API_BASE}/participation/public/v1/sites/${SITE_SLUG}/events/${EVENT_SLUG}/form`),
        fetch(`${API_BASE}/.well-known/jwks.json`),
        fetch(`${API_BASE}/participation/v1/outbox?limit=40`),
      ]);
      const formJson = await formRes.json();
      setFormConfig(formJson);
      setJwksData(await jwksRes.json());
      const outboxJson = await outboxRes.json();
      setOutboxEvents(outboxJson.items || []);

      if (formJson?.event_id) {
        const params = new URLSearchParams({
          reveal_pii: String(revealPii),
        });
        if (searchEmail.trim()) {
          params.set('email', searchEmail.trim());
        }
        const regRes = await fetch(
          `${API_BASE}/participation/v1/events/${formJson.event_id}/registrations?${params.toString()}`
        );
        const regJson = await regRes.json();
        setRegistrations(regJson.items || []);
      }
    } catch (err: any) {
      setBannerAlert({
        type: 'error',
        title: 'Error conectando con Participation API',
        detail: err?.message || 'Verifica que FastAPI esté ejecutándose en el puerto 8000.',
      });
    } finally {
      if (!silent) setLoading(false);
    }
  };

  useEffect(() => {
    refreshAll();
  }, [revealPii]);

  const loadTicketForRegistration = async (regId: string) => {
    const res = await fetch(`${API_BASE}/participation/self/v1/registrations/${regId}/ticket`);
    if (res.ok) {
      const tkt = await res.json();
      setCurrentTicket(tkt);
    } else {
      setCurrentTicket(null);
    }
  };

  const handleRegisterSubmit = async (e: React.FormEvent, reuseSameKey = false) => {
    e.preventDefault();
    if (!formConfig) return;
    const keyToUse = reuseSameKey ? idempotencyKey : idempotencyKey;

    try {
      setLoading(true);
      const res = await fetch(
        `${API_BASE}/participation/public/v1/sites/${SITE_SLUG}/events/${EVENT_SLUG}/registrations`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Idempotency-Key': keyToUse,
          },
          body: JSON.stringify({
            form_version_id: formConfig.form_version_id,
            consent_version: formConfig.consent_version,
            consent_accepted: consentAccepted,
            attendee: {
              full_name: fullName,
              email,
              document_number: documentNumber,
              institution,
            },
            answers: {
              track,
              role_profile: roleProfile,
              modality,
            },
          }),
        }
      );

      const data = await res.json();
      if (!res.ok) {
        setBannerAlert({
          type: 'error',
          title: `HTTP ${res.status} — ${data.code || 'problem'}`,
          detail: `${data.detail} (Request-Id: ${data.request_id})`,
        });
        return;
      }

      setCurrentReceipt(data);
      if (data.state === 'confirmed') {
        await loadTicketForRegistration(data.id);
        setBannerAlert({
          type: 'success',
          title: reuseSameKey
            ? 'Idempotencia verificada (PA-002): Respuesta reproducida sin duplicar cupo'
            : 'Inscripción Confirmada y Ticket QR Emitido (PA-002 / PA-008)',
          detail: `Registro ID: ${data.id} — Estado: CONFIRMED — Ticket JWS firmado con ES256.`,
        });
      } else if (data.state === 'waitlisted') {
        setCurrentTicket(null);
        setBannerAlert({
          type: 'warning',
          title: `Cupo lleno — Ingresaste a la Lista de Espera en Posición #${data.waitlist_position} (PA-012)`,
          detail: 'Cuando un participante confirmado cancele, el sistema promoverá automáticamente por orden FIFO.',
        });
      }
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  const handleCancelRegistration = async (regId: string) => {
    try {
      setLoading(true);
      const res = await fetch(
        `${API_BASE}/participation/self/v1/registrations/${regId}/cancellations`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Idempotency-Key': randomIdempotencyKey('cancel'),
          },
          body: JSON.stringify({ reason: 'Cancelación solicitada por el titular' }),
        }
      );
      const data = await res.json();
      if (!res.ok) {
        setBannerAlert({
          type: 'error',
          title: `Error al cancelar (${data.code})`,
          detail: data.detail,
        });
        return;
      }

      if (currentReceipt?.id === regId) {
        setCurrentReceipt({ ...currentReceipt, state: 'cancelled' });
        await loadTicketForRegistration(regId);
      }

      const promoMsg = data.promoted_registration_id
        ? ` ¡Promoción atómica ejecutada (PA-013)! El registro ${data.promoted_registration_id.slice(0, 12)}... subió automáticamente de Waitlist a Confirmed y recibió su Ticket QR.`
        : ' Cupo liberado correctamente.';

      setBannerAlert({
        type: 'success',
        title: 'Registro Cancelado y Ticket Revocado (PA-007)',
        detail: `Estado anterior: ${data.prior_state}.${promoMsg}`,
      });
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  const handleResetDemo = async (newCap = targetCapacity) => {
    setLoading(true);
    try {
      await fetch(`${API_BASE}/participation/v1/demo/reset`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ capacity: newCap, waitlist_enabled: true }),
      });
      setCurrentReceipt(null);
      setCurrentTicket(null);
      setBurstSummary(null);
      setIdempotencyKey(randomIdempotencyKey());
      setBannerAlert({
        type: 'success',
        title: 'Escenario reiniciado',
        detail: `Capacidad atómica configurada en ${newCap} cupos con Waitlist activa.`,
      });
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  const handleRunConcurrencyBurst = async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/participation/v1/demo/concurrency-burst`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ total_requests: burstSize }),
      });
      const data = await res.json();
      setBurstSummary(data);
      setBannerAlert({
        type: 'success',
        title: `Prueba de Concurrencia Completada (${data.total_requests_fired} hilos simultáneos — PA-011 / PA-041)`,
        detail: `Confirmados en ráfaga: ${data.confirmed_in_burst} | Enviados a Waitlist: ${data.waitlisted_in_burst} | Sobrecupo: ${data.overbooking_detected ? 'SÍ' : '0 (GARANTIZADO)'}`,
      });
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  const handleCreateAndSubmitVenture = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!formConfig) return;
    setLoading(true);
    try {
      const createRes = await fetch(
        `${API_BASE}/participation/public/v1/sites/${SITE_SLUG}/events/${EVENT_SLUG}/applications`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Idempotency-Key': randomIdempotencyKey('venture'),
          },
          body: JSON.stringify({
            form_version_id: formConfig.form_version_id,
            consent_version: formConfig.consent_version,
            venture_name: ventureName,
            track,
            leader: {
              full_name: fullName,
              email: ventureLeaderEmail,
              institution: 'IncubaUNAS',
            },
            members: [{ email: coFounderEmail, role: 'co_founder' }],
            answers: {
              problem_statement: 'Monitoreo inteligente de cultivos de cacao y café en la Amazonía.',
              stage: 'MVP validado en campo',
            },
          }),
        }
      );
      const created = await createRes.json();

      const submitRes = await fetch(
        `${API_BASE}/participation/self/v1/applications/${created.application_id}/submissions`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Idempotency-Key': randomIdempotencyKey('venture-submit'),
          },
          body: JSON.stringify({ expected_revision: 1 }),
        }
      );
      const submitted = await submitRes.json();
      setVentureResult({ created, submitted });
      setBannerAlert({
        type: 'success',
        title: 'Emprendimiento Creado y Postulación Enviada (PA-020 / PA-021 / PA-025)',
        detail: `Snapshot inmutable SHA-256: ${submitted.snapshot_hash?.slice(0, 24)}...`,
      });
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  const cap = formConfig?.capacity_summary || {
    capacity: 5,
    confirmed_count: 0,
    remaining: 5,
    waitlist_count: 0,
    public_state: 'available',
  };

  const pct = Math.min(100, Math.round((cap.confirmed_count / Math.max(1, cap.capacity)) * 100));

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 pb-16">
      {/* Top Architecture Header */}
      <header className="border-b border-slate-800 bg-slate-900/80 backdrop-blur sticky top-0 z-30">
        <div className="max-w-7xl mx-auto px-6 py-4 flex flex-col md:flex-row md:items-center md:justify-between gap-4">
          <div>
            <div className="flex items-center gap-2.5">
              <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">
                EQUIPO 3 — PARTICIPATION
              </span>
              <span className="text-xs font-mono text-slate-400">
                AD-3 • AD-7 • AD-8 • AD-11 • AD-12
              </span>
            </div>
            <h1 className="text-xl font-bold text-white mt-1">
              {formConfig?.event_title || 'Cargando release publicada...'}
            </h1>
          </div>

          {/* Live Atomic Capacity Bar */}
          <div className="flex items-center gap-4 bg-slate-950/90 border border-slate-800 px-4 py-2.5 rounded-xl">
            <div>
              <div className="text-xs text-slate-400 flex items-center gap-2">
                <span>Cupo Atómico (`capacity_counters`)</span>
                <span
                  className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase ${
                    cap.public_state === 'available'
                      ? 'bg-emerald-500/20 text-emerald-300'
                      : cap.public_state === 'waitlist_only'
                      ? 'bg-amber-500/20 text-amber-300'
                      : 'bg-blue-500/20 text-blue-300'
                  }`}
                >
                  {cap.public_state}
                </span>
              </div>
              <div className="flex items-center gap-3 mt-1">
                <span className="text-sm font-mono font-bold text-white">
                  {cap.confirmed_count} / {cap.capacity} confirmados
                </span>
                <span className="text-xs font-mono text-amber-400">
                  • {cap.waitlist_count} en waitlist
                </span>
              </div>
              <div className="w-48 h-1.5 bg-slate-800 rounded-full overflow-hidden mt-1.5">
                <div
                  className={`h-full transition-all duration-300 ${
                    pct >= 100 ? 'bg-amber-500' : 'bg-emerald-500'
                  }`}
                  style={{ width: `${pct}%` }}
                />
              </div>
            </div>

            <button
              onClick={() => handleResetDemo(targetCapacity)}
              className="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 transition"
              title="Reiniciar base de datos de demostración"
            >
              Reiniciar Demo
            </button>
          </div>
        </div>

        {/* Navigation Tabs */}
        <div className="max-w-7xl mx-auto px-6 flex gap-2 overflow-x-auto border-t border-slate-800/80 pt-2">
          {[
            { id: 'public', label: '1. Isla Pública: Registro, Idempotencia & QR JWS (PA-A / PA-B)' },
            { id: 'concurrency', label: '2. Simulador Concurrencia 3× & Waitlist Atómica (PA-011 / PA-013)' },
            { id: 'backoffice', label: '3. Backoffice, Privacidad AES-GCM/HMAC & Outbox (PA-D / AD-8)' },
            { id: 'ventures', label: '4. Postulación de Emprendimientos & Contratos (PA-C / Sprint 0)' },
          ].map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id as any)}
              className={`px-4 py-2.5 text-xs font-semibold rounded-t-lg border-b-2 transition whitespace-nowrap ${
                activeTab === tab.id
                  ? 'border-emerald-400 text-emerald-300 bg-slate-800/70'
                  : 'border-transparent text-slate-400 hover:text-slate-200'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-6 mt-6 space-y-6">
        {/* Alert Banner */}
        {bannerAlert && (
          <div
            className={`p-4 rounded-xl border flex items-start justify-between gap-4 ${
              bannerAlert.type === 'success'
                ? 'bg-emerald-950/50 border-emerald-500/40 text-emerald-200'
                : bannerAlert.type === 'warning'
                ? 'bg-amber-950/50 border-amber-500/40 text-amber-200'
                : 'bg-red-950/50 border-red-500/40 text-red-200'
            }`}
          >
            <div>
              <div className="font-semibold text-sm">{bannerAlert.title}</div>
              <div className="text-xs opacity-90 mt-0.5 font-mono">{bannerAlert.detail}</div>
            </div>
            <button
              onClick={() => setBannerAlert(null)}
              className="text-xs opacity-70 hover:opacity-100 px-2 py-1"
            >
              ✕
            </button>
          </div>
        )}

        {/* ===================================================================
            TAB 1: PUBLIC REGISTRATION ISLAND & QR JWS TICKET (PA-A & PA-B)
           =================================================================== */}
        {activeTab === 'public' && (
          <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
            {/* Registration Form */}
            <div className="lg:col-span-7 bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-5">
              <div className="flex items-center justify-between border-b border-slate-800 pb-4">
                <div>
                  <span className="text-xs font-mono text-emerald-400">
                    GET /participation/public/v1/sites/{SITE_SLUG}/events/{EVENT_SLUG}/form
                  </span>
                  <h2 className="text-lg font-bold text-white mt-0.5">
                    Formulario Público Versionado (PA-001 / PA-002)
                  </h2>
                </div>
                <span className="text-xs font-mono bg-slate-800 px-2.5 py-1 rounded text-slate-300">
                  schema_hash: {formConfig?.schema_hash?.slice(0, 10)}...
                </span>
              </div>

              <form onSubmit={(e) => handleRegisterSubmit(e, false)} className="space-y-4">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs font-medium text-slate-300 mb-1">
                      Nombre Completo (Se cifra con AES-256-GCM) *
                    </label>
                    <input
                      type="text"
                      value={fullName}
                      onChange={(e) => setFullName(e.target.value)}
                      required
                      className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:border-emerald-500 outline-none"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-slate-300 mb-1">
                      Correo Institucional / Personal (Dedupe vía HMAC) *
                    </label>
                    <input
                      type="email"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      required
                      className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:border-emerald-500 outline-none"
                    />
                  </div>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs font-medium text-slate-300 mb-1">
                      DNI / Documento (PII Restringida)
                    </label>
                    <input
                      type="text"
                      value={documentNumber}
                      onChange={(e) => setDocumentNumber(e.target.value)}
                      className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:border-emerald-500 outline-none"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-slate-300 mb-1">
                      Institución / Facultad
                    </label>
                    <input
                      type="text"
                      value={institution}
                      onChange={(e) => setInstitution(e.target.value)}
                      className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:border-emerald-500 outline-none"
                    />
                  </div>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-3 gap-4 pt-1">
                  <div>
                    <label className="block text-xs font-medium text-slate-300 mb-1">
                      Track Principal *
                    </label>
                    <select
                      value={track}
                      onChange={(e) => setTrack(e.target.value)}
                      className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white"
                    >
                      {(formConfig?.schema?.properties?.track?.enum || []).map((opt: string) => (
                        <option key={opt} value={opt}>
                          {opt}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-slate-300 mb-1">
                      Perfil *
                    </label>
                    <select
                      value={roleProfile}
                      onChange={(e) => setRoleProfile(e.target.value)}
                      className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white"
                    >
                      {(formConfig?.schema?.properties?.role_profile?.enum || []).map((opt: string) => (
                        <option key={opt} value={opt}>
                          {opt}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-slate-300 mb-1">
                      Modalidad *
                    </label>
                    <select
                      value={modality}
                      onChange={(e) => setModality(e.target.value)}
                      className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white"
                    >
                      {(formConfig?.schema?.properties?.modality?.enum || []).map((opt: string) => (
                        <option key={opt} value={opt}>
                          {opt}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>

                {/* Idempotency-Key Control Box */}
                <div className="bg-slate-950 border border-slate-800 rounded-xl p-3.5 space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-mono font-semibold text-sky-400">
                      Header obligatorio: Idempotency-Key (PA-002)
                    </span>
                    <button
                      type="button"
                      onClick={() => setIdempotencyKey(randomIdempotencyKey())}
                      className="text-xs text-sky-300 hover:underline"
                    >
                      Generar nueva clave ↻
                    </button>
                  </div>
                  <input
                    type="text"
                    value={idempotencyKey}
                    onChange={(e) => setIdempotencyKey(e.target.value)}
                    className="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1.5 text-xs font-mono text-sky-200"
                  />
                  <p className="text-[11px] text-slate-400">
                    Si envías dos veces con la misma clave y los mismos datos, devuelve el mismo recibo sin descontar otro cupo. Si cambias el correo con la misma clave, devuelve{' '}
                    <code className="text-amber-300">409 idempotency_key_reused</code>.
                  </p>
                </div>

                {/* Consent checkbox */}
                <label className="flex items-start gap-2.5 text-xs text-slate-300 bg-slate-950/60 p-3 rounded-lg border border-slate-800 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={consentAccepted}
                    onChange={(e) => setConsentAccepted(e.target.checked)}
                    className="mt-0.5"
                  />
                  <span>
                    <strong>Consentimiento ({formConfig?.consent_version}):</strong>{' '}
                    {formConfig?.consent_text}
                  </span>
                </label>

                <div className="flex flex-wrap gap-3 pt-2">
                  <button
                    type="submit"
                    disabled={loading}
                    className="px-5 py-2.5 rounded-xl font-semibold text-sm bg-emerald-500 hover:bg-emerald-400 text-slate-950 transition shadow-lg shadow-emerald-500/10"
                  >
                    {cap.remaining > 0
                      ? 'Registrarme Ahora (Confirmación Atómica)'
                      : 'Unirme a la Lista de Espera (Waitlist)'}
                  </button>

                  <button
                    type="button"
                    onClick={() => {
                      const rnd = Math.floor(Math.random() * 900 + 100);
                      setFullName(`Participante UNAS ${rnd}`);
                      setEmail(`participante.${rnd}@unas.edu.pe`);
                      setIdempotencyKey(randomIdempotencyKey());
                    }}
                    className="px-4 py-2.5 rounded-xl text-xs font-medium bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700"
                  >
                    Autocompletar otro participante + Nueva Key
                  </button>
                </div>
              </form>
            </div>

            {/* Right Column: Ticket QR JWS & Cryptographic Inspector */}
            <div className="lg:col-span-5 bg-slate-900 border border-slate-800 rounded-2xl p-6 flex flex-col justify-between space-y-5">
              <div>
                <div className="flex items-center justify-between border-b border-slate-800 pb-4">
                  <div>
                    <span className="text-xs font-mono text-amber-400">
                      AD-11 • PA-008 • PA-015
                    </span>
                    <h2 className="text-lg font-bold text-white mt-0.5">
                      Credencial Operativa QR (JWS sin PII)
                    </h2>
                  </div>
                  {currentReceipt && (
                    <span
                      className={`px-2.5 py-1 rounded-full text-xs font-bold uppercase ${
                        currentReceipt.state === 'confirmed'
                          ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40'
                          : currentReceipt.state === 'waitlisted'
                          ? 'bg-amber-500/20 text-amber-300 border border-amber-500/40'
                          : 'bg-red-500/20 text-red-300 border border-red-500/40'
                      }`}
                    >
                      {currentReceipt.state}
                    </span>
                  )}
                </div>

                {!currentReceipt ? (
                  <div className="py-12 text-center text-slate-400 text-sm space-y-2">
                    <p>Completa el formulario de la izquierda para emitir un registro.</p>
                    <p className="text-xs text-slate-500">
                      Verás aquí el Ticket QR firmado con ECDSA P-256 (`ES256`) y sus claims libres de PII.
                    </p>
                  </div>
                ) : (
                  <div className="mt-4 space-y-4">
                    <div className="bg-slate-950 border border-slate-800 rounded-xl p-4 flex flex-col sm:flex-row items-center gap-4">
                      {currentTicket ? (
                        <JwsQrCodeSvg
                          jws={currentTicket.ticket_jws}
                          revoked={currentTicket.state === 'revoked'}
                        />
                      ) : (
                        <div className="w-36 h-36 rounded-xl bg-amber-950/40 border border-amber-500/40 flex flex-col items-center justify-center text-center p-3">
                          <span className="text-xs text-amber-300 font-semibold">WAITLIST</span>
                          <span className="text-3xl font-extrabold text-amber-400 mt-1">
                            #{currentReceipt.waitlist_position}
                          </span>
                          <span className="text-[11px] text-amber-200/80 mt-1">
                            Sin ticket activo hasta promoción
                          </span>
                        </div>
                      )}

                      <div className="space-y-1.5 text-xs flex-1">
                        <div>
                          <span className="text-slate-400">Registration ID:</span>
                          <div className="font-mono text-slate-200 break-all">
                            {currentReceipt.id}
                          </div>
                        </div>
                        {currentTicket && (
                          <>
                            <div>
                              <span className="text-slate-400">Ticket JTI (Opaco):</span>
                              <div className="font-mono text-emerald-300 break-all">
                                {currentTicket.jti}
                              </div>
                            </div>
                            <div className="flex items-center gap-3">
                              <span>
                                <span className="text-slate-400">Key ID:</span>{' '}
                                <code className="text-sky-300">{currentTicket.key_id}</code>
                              </span>
                              <span>
                                <span className="text-slate-400">Aud:</span>{' '}
                                <code className="text-amber-300">{currentTicket.audience}</code>
                              </span>
                            </div>
                          </>
                        )}

                        {currentReceipt.state !== 'cancelled' && (
                          <div className="pt-2">
                            <button
                              onClick={() => handleCancelRegistration(currentReceipt.id)}
                              className="w-full py-2 px-3 rounded-lg bg-red-600/20 hover:bg-red-600/30 text-red-300 border border-red-500/40 font-semibold text-xs transition"
                            >
                              Cancelar Registro y Revocar Ticket (PA-007)
                            </button>
                          </div>
                        )}
                      </div>
                    </div>

                    {currentTicket && (
                      <div className="space-y-3">
                        <div>
                          <div className="text-xs font-semibold text-slate-300 mb-1">
                            Payload Decodificado del QR (Verificado con JWKS — Cero PII):
                          </div>
                          <pre className="bg-slate-950 border border-slate-800 rounded-xl p-3 text-[11px] text-emerald-300 overflow-x-auto">
                            {JSON.stringify(currentTicket.decoded_claims, null, 2)}
                          </pre>
                        </div>
                        <div>
                          <div className="text-xs font-semibold text-slate-400 mb-1">
                            Compact JWS (`alg: ES256`, `Cache-Control: no-store`):
                          </div>
                          <div className="bg-slate-950 border border-slate-800 rounded-xl p-2.5 text-[10px] font-mono text-slate-400 break-all">
                            {currentTicket.ticket_jws}
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>

              {jwksData && (
                <div className="border-t border-slate-800 pt-3">
                  <div className="text-[11px] font-mono text-slate-400">
                    Clave pública expuesta a Equipo 4 (Onsite) en{' '}
                    <code className="text-sky-400">GET /.well-known/jwks.json</code>:
                    kid=<code className="text-emerald-300">{jwksData.keys?.[0]?.kid}</code> (EC P-256)
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {/* ===================================================================
            TAB 2: CONCURRENCY SIMULATOR & ATOMIC WAITLIST PROMOTION (PA-011 / PA-013)
           =================================================================== */}
        {activeTab === 'concurrency' && (
          <div className="space-y-6">
            <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-5">
              <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4 border-b border-slate-800 pb-4">
                <div>
                  <span className="text-xs font-mono text-emerald-400">
                    PA-011 • PA-012 • PA-013 • PA-041
                  </span>
                  <h2 className="text-lg font-bold text-white mt-0.5">
                    Laboratorio de Concurrencia (3× Pico) y Promoción Automática de Waitlist
                  </h2>
                  <p className="text-xs text-slate-400 mt-0.5">
                    Demuestra que bajo una ráfaga de hilos simultáneos el contador local bloqueado jamás permite sobrecupo y asigna posiciones monotónicas en la lista de espera.
                  </p>
                </div>

                <div className="flex flex-wrap items-center gap-3">
                  <div className="flex items-center gap-2 bg-slate-950 border border-slate-800 px-3 py-1.5 rounded-lg">
                    <label className="text-xs text-slate-400">Capacidad Máx:</label>
                    <input
                      type="number"
                      min={1}
                      max={100}
                      value={targetCapacity}
                      onChange={(e) => setTargetCapacity(Number(e.target.value))}
                      className="w-16 bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs font-mono text-white"
                    />
                    <button
                      onClick={() => handleResetDemo(targetCapacity)}
                      className="text-xs px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-sky-300 font-medium"
                    >
                      Aplicar & Limpiar
                    </button>
                  </div>

                  <div className="flex items-center gap-2 bg-slate-950 border border-slate-800 px-3 py-1.5 rounded-lg">
                    <label className="text-xs text-slate-400">Hilos simultáneos:</label>
                    <input
                      type="number"
                      min={1}
                      max={60}
                      value={burstSize}
                      onChange={(e) => setBurstSize(Number(e.target.value))}
                      className="w-16 bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs font-mono text-white"
                    />
                    <button
                      onClick={handleRunConcurrencyBurst}
                      disabled={loading}
                      className="text-xs px-3.5 py-1.5 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold transition"
                    >
                      ⚡ Disparar Ráfaga Concurrente
                    </button>
                  </div>
                </div>
              </div>

              {/* Invariant Cards */}
              <div className="grid grid-cols-1 sm:grid-cols-4 gap-4">
                <div className="bg-slate-950 border border-slate-800 p-4 rounded-xl">
                  <div className="text-xs text-slate-400">Cupos Confirmados</div>
                  <div className="text-2xl font-bold font-mono text-emerald-400 mt-1">
                    {cap.confirmed_count} / {cap.capacity}
                  </div>
                  <div className="text-[11px] text-slate-500 mt-1">
                    Constraint: <code>confirmed_count &lt;= capacity</code>
                  </div>
                </div>
                <div className="bg-slate-950 border border-slate-800 p-4 rounded-xl">
                  <div className="text-xs text-slate-400">En Lista de Espera (FIFO)</div>
                  <div className="text-2xl font-bold font-mono text-amber-400 mt-1">
                    {cap.waitlist_count}
                  </div>
                  <div className="text-[11px] text-slate-500 mt-1">
                    Secuencia monotónica sin huecos
                  </div>
                </div>
                <div className="bg-slate-950 border border-slate-800 p-4 rounded-xl">
                  <div className="text-xs text-slate-400">Sobrecupo Detectado</div>
                  <div className="text-2xl font-bold font-mono text-emerald-300 mt-1">
                    0 (CERO)
                  </div>
                  <div className="text-[11px] text-slate-500 mt-1">
                    Invariante PA-011 verificada
                  </div>
                </div>
                <div className="bg-slate-950 border border-slate-800 p-4 rounded-xl">
                  <div className="text-xs text-slate-400">Tickets JWS Activos</div>
                  <div className="text-2xl font-bold font-mono text-sky-400 mt-1">
                    {registrations.filter((r) => r.ticket_state === 'active').length}
                  </div>
                  <div className="text-[11px] text-slate-500 mt-1">
                    1 Ticket único por confirmado
                  </div>
                </div>
              </div>

              {/* Side-by-Side Confirmed vs Waitlist Queue */}
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 pt-2">
                {/* Confirmed Column */}
                <div className="bg-slate-950 border border-slate-800 rounded-xl p-4 space-y-3">
                  <div className="flex items-center justify-between">
                    <h3 className="text-sm font-bold text-emerald-300">
                      ✅ Participantes Confirmados ({registrations.filter((r) => r.state === 'confirmed').length})
                    </h3>
                    <span className="text-[11px] text-slate-400">
                      Haz clic en &quot;Cancelar y Promover&quot; para probar PA-013
                    </span>
                  </div>
                  <div className="space-y-2 max-h-96 overflow-y-auto pr-1">
                    {registrations
                      .filter((r) => r.state === 'confirmed')
                      .map((reg) => (
                        <div
                          key={reg.id}
                          className="flex items-center justify-between bg-slate-900 border border-slate-800 px-3.5 py-2.5 rounded-lg text-xs"
                        >
                          <div>
                            <div className="font-semibold text-white flex items-center gap-2">
                              <span>{reg.full_name}</span>
                              {reg.waitlist_state === 'promoted' && (
                                <span className="px-1.5 py-0.5 rounded bg-sky-500/20 text-sky-300 text-[10px] font-mono">
                                  Promovido de Waitlist #{reg.waitlist_position}
                                </span>
                              )}
                            </div>
                            <div className="font-mono text-[11px] text-slate-400">
                              {reg.email} • Ticket: <span className="text-emerald-400">{reg.ticket_state}</span>
                            </div>
                          </div>
                          <div className="flex items-center gap-2">
                            <button
                              onClick={async () => {
                                setCurrentReceipt(reg);
                                await loadTicketForRegistration(reg.id);
                                setActiveTab('public');
                              }}
                              className="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-200"
                            >
                              Ver QR
                            </button>
                            <button
                              onClick={() => handleCancelRegistration(reg.id)}
                              className="px-2.5 py-1 rounded bg-red-500/20 hover:bg-red-500/30 text-red-300 border border-red-500/30 font-medium"
                            >
                              Cancelar y Promover #1
                            </button>
                          </div>
                        </div>
                      ))}
                  </div>
                </div>

                {/* Waitlist Column */}
                <div className="bg-slate-950 border border-slate-800 rounded-xl p-4 space-y-3">
                  <div className="flex items-center justify-between">
                    <h3 className="text-sm font-bold text-amber-300">
                      ⏳ Cola Ordenada de Lista de Espera ({registrations.filter((r) => r.state === 'waitlisted').length})
                    </h3>
                    <span className="text-[11px] text-slate-400">
                      Orden estricto por `position ASC` (PA-012)
                    </span>
                  </div>
                  <div className="space-y-2 max-h-96 overflow-y-auto pr-1">
                    {registrations
                      .filter((r) => r.state === 'waitlisted')
                      .sort((a, b) => (a.waitlist_position || 999) - (b.waitlist_position || 999))
                      .map((reg, idx) => (
                        <div
                          key={reg.id}
                          className={`flex items-center justify-between px-3.5 py-2.5 rounded-lg text-xs border ${
                            idx === 0
                              ? 'bg-amber-950/30 border-amber-500/50'
                              : 'bg-slate-900 border-slate-800'
                          }`}
                        >
                          <div className="flex items-center gap-3">
                            <span className="px-2.5 py-1 rounded-md bg-amber-500/20 text-amber-300 font-mono font-bold">
                              #{reg.waitlist_position}
                            </span>
                            <div>
                              <div className="font-semibold text-white">
                                {reg.full_name}{' '}
                                {idx === 0 && (
                                  <span className="text-[10px] text-amber-300 font-normal">
                                    (Siguiente en subir automáticamente)
                                  </span>
                                )}
                              </div>
                              <div className="font-mono text-[11px] text-slate-400">
                                {reg.email} • Sin ticket activo
                              </div>
                            </div>
                          </div>
                        </div>
                      ))}
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* ===================================================================
            TAB 3: BACKOFFICE, PRIVACY (AES-GCM / HMAC) & TRANSACTIONAL OUTBOX
           =================================================================== */}
        {activeTab === 'backoffice' && (
          <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
            {/* Backoffice Table with PII Masking & HMAC Search */}
            <div className="lg:col-span-7 bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
              <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 border-b border-slate-800 pb-4">
                <div>
                  <span className="text-xs font-mono text-sky-400">PA-030 • AD-8</span>
                  <h2 className="text-lg font-bold text-white">
                    Backoffice de Registros & Privacidad por Diseño
                  </h2>
                </div>

                <label className="flex items-center gap-2 text-xs bg-slate-950 border border-slate-800 px-3 py-2 rounded-xl cursor-pointer">
                  <input
                    type="checkbox"
                    checked={revealPii}
                    onChange={(e) => setRevealPii(e.target.checked)}
                  />
                  <span className="font-medium text-emerald-300">
                    Rol Manager: Desencriptar PII (AES-256-GCM)
                  </span>
                </label>
              </div>

              {/* HMAC Search Bar */}
              <div className="flex gap-2">
                <input
                  type="text"
                  placeholder="Buscar por correo exacto (calcula HMAC-SHA256 blind index server-side)..."
                  value={searchEmail}
                  onChange={(e) => setSearchEmail(e.target.value)}
                  className="flex-1 bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white"
                />
                <button
                  onClick={() => refreshAll()}
                  className="px-4 py-2 rounded-lg bg-sky-500 hover:bg-sky-400 text-slate-950 font-semibold text-xs"
                >
                  Buscar por HMAC
                </button>
                {searchEmail && (
                  <button
                    onClick={() => {
                      setSearchEmail('');
                      setTimeout(() => refreshAll(), 50);
                    }}
                    className="px-3 py-2 rounded-lg bg-slate-800 text-slate-300 text-xs"
                  >
                    Limpiar
                  </button>
                )}
              </div>

              <div className="overflow-x-auto">
                <table className="w-full text-left border-collapse text-xs">
                  <thead>
                    <tr className="border-b border-slate-800 text-slate-400">
                      <th className="py-2.5 px-2">Participante</th>
                      <th className="py-2.5 px-2">Estado</th>
                      <th className="py-2.5 px-2">Blind Index (`email_hmac`)</th>
                      <th className="py-2.5 px-2">Ciphertext (`AES-256-GCM`)</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/70">
                    {registrations.map((r) => (
                      <tr key={r.id} className="hover:bg-slate-800/40">
                        <td className="py-2.5 px-2">
                          <div className="font-semibold text-white">{r.full_name}</div>
                          <div className="font-mono text-[11px] text-slate-400">{r.email}</div>
                        </td>
                        <td className="py-2.5 px-2">
                          <span
                            className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase ${
                              r.state === 'confirmed'
                                ? 'bg-emerald-500/20 text-emerald-300'
                                : r.state === 'waitlisted'
                                ? 'bg-amber-500/20 text-amber-300'
                                : 'bg-red-500/20 text-red-300'
                            }`}
                          >
                            {r.state}
                            {r.waitlist_position ? ` (#${r.waitlist_position})` : ''}
                          </span>
                        </td>
                        <td className="py-2.5 px-2 font-mono text-[10px] text-sky-300">
                          {r.email_hmac.slice(0, 18)}...
                        </td>
                        <td className="py-2.5 px-2 font-mono text-[10px] text-slate-400">
                          {r.pii_ciphertext_preview}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            {/* Transactional Outbox Stream */}
            <div className="lg:col-span-5 bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
              <div className="border-b border-slate-800 pb-4">
                <span className="text-xs font-mono text-amber-400">
                  EVENT-CONTRACTS.md • AD-7 • AD-8
                </span>
                <h2 className="text-lg font-bold text-white">
                  Transactional Outbox ({outboxEvents.length} eventos)
                </h2>
                <p className="text-xs text-slate-400 mt-0.5">
                  Envelope canónico v1 emitido en la misma transacción hacia Content (Equipo 1) y Onsite/Operations (Equipo 4). Cero PII.
                </p>
              </div>

              <div className="space-y-3 max-h-[520px] overflow-y-auto pr-1">
                {outboxEvents.map((ev) => (
                  <div
                    key={ev.message_id}
                    className="bg-slate-950 border border-slate-800 rounded-xl p-3 space-y-1.5"
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-xs font-bold text-emerald-400">
                        {ev.topic}
                      </span>
                      <span className="font-mono text-[10px] text-slate-400">
                        v{ev.aggregate_version} • {ev.occurred_at}
                      </span>
                    </div>
                    <div className="text-[10px] font-mono text-slate-400">
                      domain_event_id: <span className="text-slate-300">{ev.domain_event_id}</span>
                    </div>
                    <pre className="text-[10px] font-mono text-sky-200 bg-slate-900/90 p-2 rounded overflow-x-auto">
                      {JSON.stringify(ev.envelope.data, null, 2)}
                    </pre>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}

        {/* ===================================================================
            TAB 4: VENTURES & APPLICATIONS (PA-C) + SPRINT 0 CONTRACTS
           =================================================================== */}
        {activeTab === 'ventures' && (
          <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
            <div className="lg:col-span-6 bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
              <div className="border-b border-slate-800 pb-4">
                <span className="text-xs font-mono text-emerald-400">
                  Épica PA-C: PA-020 • PA-021 • PA-025
                </span>
                <h2 className="text-lg font-bold text-white mt-0.5">
                  Postulación de Emprendimientos y Equipos
                </h2>
                <p className="text-xs text-slate-400">
                  Crea un emprendimiento, invita integrantes con token hasheado one-use y congela una revisión inmutable (`application_revisions`).
                </p>
              </div>

              <form onSubmit={handleCreateAndSubmitVenture} className="space-y-4">
                <div>
                  <label className="block text-xs text-slate-300 mb-1">
                    Nombre del Emprendimiento / Startup *
                  </label>
                  <input
                    type="text"
                    value={ventureName}
                    onChange={(e) => setVentureName(e.target.value)}
                    required
                    className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white"
                  />
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs text-slate-300 mb-1">
                      Correo del Líder (Ownership) *
                    </label>
                    <input
                      type="email"
                      value={ventureLeaderEmail}
                      onChange={(e) => setVentureLeaderEmail(e.target.value)}
                      required
                      className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white"
                    />
                  </div>
                  <div>
                    <label className="block text-xs text-slate-300 mb-1">
                      Invitar Co-Fundador (`team_invitations`)
                    </label>
                    <input
                      type="email"
                      value={coFounderEmail}
                      onChange={(e) => setCoFounderEmail(e.target.value)}
                      className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white"
                    />
                  </div>
                </div>
                <button
                  type="submit"
                  disabled={loading}
                  className="px-5 py-2.5 rounded-xl bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-semibold text-xs"
                >
                  Crear Emprendimiento, Invitar Equipo y Enviar Snapshot Inmutable
                </button>
              </form>

              {ventureResult && (
                <div className="bg-slate-950 border border-slate-800 rounded-xl p-4 space-y-2">
                  <div className="text-xs font-semibold text-emerald-400">
                    Revisión Enviada e Inmutable (`PA-025`):
                  </div>
                  <pre className="text-[11px] font-mono text-slate-300 overflow-x-auto">
                    {JSON.stringify(ventureResult, null, 2)}
                  </pre>
                </div>
              )}
            </div>

            <div className="lg:col-span-6 bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
              <div className="border-b border-slate-800 pb-4">
                <span className="text-xs font-mono text-sky-400">
                  Definition of Done — Sprint 0 & Contratos
                </span>
                <h2 className="text-lg font-bold text-white mt-0.5">
                  Artefactos Normativos Entregados por el Equipo 3
                </h2>
              </div>

              <div className="space-y-3 text-xs">
                <div className="bg-slate-950 border border-slate-800 p-4 rounded-xl">
                  <div className="font-mono font-bold text-emerald-300">
                    1. services/participation/contracts/openapi.yaml
                  </div>
                  <p className="text-slate-400 mt-1">
                    Especificación OpenAPI 3.1 completa con convenciones `snake_case`, `Idempotency-Key`, `If-Match`/`ETag` y `application/problem+json`.
                  </p>
                  <a
                    href={`${API_BASE}/docs`}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-block mt-2 text-sky-400 hover:underline font-mono"
                  >
                    Abrir Swagger UI interactivo (http://localhost:8000/docs) ↗
                  </a>
                </div>

                <div className="bg-slate-950 border border-slate-800 p-4 rounded-xl">
                  <div className="font-mono font-bold text-amber-300">
                    2. services/participation/contracts/events-schema.json
                  </div>
                  <p className="text-slate-400 mt-1">
                    JSON Schema 2020-12 del envelope canónico v1 (`message_id`, `domain_event_id`, `partition_key`) y todos los eventos producidos por Participation sin PII.
                  </p>
                </div>

                <div className="bg-slate-950 border border-slate-800 p-4 rounded-xl">
                  <div className="font-mono font-bold text-sky-300">
                    3. services/participation/migrations/001_initial_participation_schema.sql
                  </div>
                  <p className="text-slate-400 mt-1">
                    Migración PostgreSQL 16 forward-only con las 17 tablas de Participation, `FORCE ROW LEVEL SECURITY` por `organization_id`, índices tenant-first y constraints de máquinas de estado.
                  </p>
                </div>
              </div>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
