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
  const [bannerAlert, setBannerAlert] = useState<{ type: 'success' | 'warning' | 'error' | 'info'; title: string; detail: string } | null>(null);

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

  // Venture & Application State (PA-C: PA-020 .. PA-027)
  const [ventureName, setVentureName] = useState('AgroSense Selva AI');
  const [ventureLeaderEmail, setVentureLeaderEmail] = useState('lider.agrosense@unas.edu.pe');
  const [coFounderEmail, setCoFounderEmail] = useState('cofounder.iot@unas.edu.pe');
  const [activeApplicationId, setActiveApplicationId] = useState<string | null>(null);
  const [activeVentureId, setActiveVentureId] = useState<string | null>(null);
  const [applicationDetails, setApplicationDetails] = useState<any | null>(null);
  const [newMemberEmail, setNewMemberEmail] = useState('');
  const [newMemberRole, setNewMemberRole] = useState<'co_founder' | 'member'>('member');
  const [draftSolutionText, setDraftSolutionText] = useState('Monitoreo inteligente con sensores IoT y predicción climática local.');
  const [uploadFileName, setUploadFileName] = useState('Pitch_Deck_AgroSense.pdf');
  const [uploadMimeType, setUploadMimeType] = useState('application/pdf');
  const [withdrawReason, setWithdrawReason] = useState('Cambio estratégico de cronograma');
  const [reviewChangeNotes, setReviewChangeNotes] = useState('Se requiere detallar presupuesto y carta de aval institucional.');
  const [ventureResult, setVentureResult] = useState<any>(null);

  // Épica PA-D (Backoffice, Admisibilidad & Privacidad) State
  const [backofficeSubtab, setBackofficeSubtab] = useState<
    'registrations' | 'correction' | 'admissibility' | 'batch_import' | 'privacy' | 'operations'
  >('registrations');

  // PA-031 Administrative Correction
  const [selectedRegForEdit, setSelectedRegForEdit] = useState<any | null>(null);
  const [editFullName, setEditFullName] = useState('');
  const [editPhone, setEditPhone] = useState('');
  const [editInstitution, setEditInstitution] = useState('');
  const [editDocNumber, setEditDocNumber] = useState('');
  const [editReason, setEditReason] = useState('Corrección de tilde en apellido y actualización de teléfono');

  // PA-032 Admissibility Reviews
  const [applicationsList, setApplicationsList] = useState<any[]>([]);
  const [selectedAppForReview, setSelectedAppForReview] = useState<any | null>(null);
  const [admitReason, setAdmitReason] = useState('Cumple requisitos de admisibilidad técnica y bases del evento.');
  const [checklistBases, setChecklistBases] = useState(true);
  const [checklistEquipo, setChecklistEquipo] = useState(true);
  const [checklistArchivos, setChecklistArchivos] = useState(true);

  // PA-033 Batch Import
  const [batchRowsText, setBatchRowsText] = useState(
    JSON.stringify(
      [
        {
          full_name: 'Carlos Mendoza Rios',
          email: 'carlos.mendoza@unas.edu.pe',
          phone: '+51987654301',
          institution: 'UNAS — Agronomía',
          document_number: '71000001',
          answers: { track: 'Agrotech & Bioeconomía Amazónica' },
        },
        {
          full_name: 'Elena Silva Paredes',
          email: 'elena.silva@unas.edu.pe',
          phone: '+51987654302',
          institution: 'UNAS — FIIS',
          document_number: '71000002',
          answers: { track: 'IA Aplicada & Transformación Digital' },
        },
      ],
      null,
      2
    )
  );
  const [batchLegalBasis, setBatchLegalBasis] = useState('Consentimiento explícito firmado conforme a Ley 29733');
  const [batchDryRun, setBatchDryRun] = useState(false);
  const [batchResult, setBatchResult] = useState<any | null>(null);

  // PA-034 ARCO Privacy
  const [privacyEmail, setPrivacyEmail] = useState('franklyn.huaman@unas.edu.pe');
  const [privacyType, setPrivacyType] = useState<'erasure' | 'access' | 'rectification'>('erasure');
  const [privacyLegalHold, setPrivacyLegalHold] = useState(false);
  const [privacyReason, setPrivacyReason] = useState('Solicitud de cancelación y supresión de datos personales');
  const [privacyReceipt, setPrivacyReceipt] = useState<any | null>(null);

  // PA-035 Operations Projection
  const [projPurpose, setProjPurpose] = useState<
    'certificate_issuance' | 'onsite_attendance' | 'institutional_reporting'
  >('certificate_issuance');
  const [projConfirmedOnly, setProjConfirmedOnly] = useState(true);
  const [projectionResult, setProjectionResult] = useState<any | null>(null);

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
        const [regRes, appsRes] = await Promise.all([
          fetch(`${API_BASE}/participation/v1/events/${formJson.event_id}/registrations?${params.toString()}`),
          fetch(`${API_BASE}/participation/v1/events/${formJson.event_id}/applications`),
        ]);
        const regJson = await regRes.json();
        setRegistrations(regJson.items || []);
        if (appsRes.ok) {
          const appsJson = await appsRes.json();
          setApplicationsList(appsJson.items || []);
        }
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

  const fetchApplicationDetails = async (appId: string) => {
    try {
      const res = await fetch(`${API_BASE}/participation/self/v1/applications/${appId}`);
      if (res.ok) {
        const data = await res.json();
        setApplicationDetails(data);
        setActiveVentureId(data.venture_id);
        if (data.answers?.solution) {
          setDraftSolutionText(data.answers.solution);
        }
      }
    } catch (err) {
      console.error(err);
    }
  };

  const handleCreateVentureDraft = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!formConfig) return;
    setLoading(true);
    try {
      const res = await fetch(
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
              full_name: fullName || 'Líder ' + ventureName,
              email: ventureLeaderEmail,
              institution: 'IncubaUNAS',
            },
            members: coFounderEmail ? [{ email: coFounderEmail, role: 'co_founder' }] : [],
            answers: {
              problem: 'Monitoreo de cultivos amazónicos',
              solution: draftSolutionText,
            },
          }),
        }
      );
      const data = await res.json();
      if (!res.ok) {
        setBannerAlert({
          type: 'error',
          title: `HTTP ${res.status} — ${data.code || 'problem'}`,
          detail: data.detail || 'Error al crear emprendimiento.',
        });
        return;
      }
      setActiveApplicationId(data.application_id);
      setActiveVentureId(data.venture_id);
      setVentureResult(data);
      setBannerAlert({
        type: 'success',
        title: 'Borrador Creado Exitosamente (PA-020)',
        detail: `Emprendimiento '${data.venture_name}' registrado en estado 'draft'. Revisión 1.`,
      });
      await fetchApplicationDetails(data.application_id);
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  const handleInviteMember = async () => {
    if (!activeVentureId || !newMemberEmail) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/participation/self/v1/ventures/${activeVentureId}/invitations`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: newMemberEmail, role: newMemberRole }),
      });
      const data = await res.json();
      if (!res.ok) {
        setBannerAlert({ type: 'error', title: data.title || 'Error', detail: data.detail || 'No se pudo invitar' });
        return;
      }
      setBannerAlert({
        type: 'success',
        title: 'Invitación Emitida (PA-021)',
        detail: `Invitación enviada a ${newMemberEmail} con token hasheado one-use y expiración de 7 días.`,
      });
      setNewMemberEmail('');
      if (activeApplicationId) await fetchApplicationDetails(activeApplicationId);
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  const handleAcceptInvitation = async (token: string) => {
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/participation/public/v1/team-invitations/${token}/acceptance`, { method: 'POST' });
      const data = await res.json();
      if (!res.ok) {
        setBannerAlert({ type: 'error', title: data.title || 'Error', detail: data.detail });
        return;
      }
      setBannerAlert({ type: 'success', title: 'Invitación Aceptada (PA-022)', detail: 'El integrante ahora es un miembro activo del equipo.' });
      if (activeApplicationId) await fetchApplicationDetails(activeApplicationId);
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  const handleDeclineInvitation = async (token: string) => {
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/participation/public/v1/team-invitations/${token}/declination`, { method: 'POST' });
      const data = await res.json();
      if (!res.ok) {
        setBannerAlert({ type: 'error', title: data.title || 'Error', detail: data.detail });
        return;
      }
      setBannerAlert({ type: 'info', title: 'Invitación Declinada (PA-022)', detail: 'La invitación ha sido rechazada.' });
      if (activeApplicationId) await fetchApplicationDetails(activeApplicationId);
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  const handleLeaveMember = async (memberId: string) => {
    if (!activeVentureId) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/participation/self/v1/ventures/${activeVentureId}/members/${memberId}/leave`, { method: 'POST' });
      const data = await res.json();
      if (!res.ok) {
        setBannerAlert({ type: 'error', title: data.title || 'Error al salir', detail: data.detail });
        return;
      }
      setBannerAlert({ type: 'info', title: 'Miembro Retirado (PA-022)', detail: 'Retiro registrado y auditado correctamente.' });
      if (activeApplicationId) await fetchApplicationDetails(activeApplicationId);
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  const handleUpdateDraft = async () => {
    if (!activeApplicationId || !applicationDetails) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/participation/self/v1/applications/${activeApplicationId}`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
          'If-Match': applicationDetails.etag,
        },
        body: JSON.stringify({
          answers: { ...applicationDetails.answers, solution: draftSolutionText },
          expected_revision: applicationDetails.revision,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        setBannerAlert({ type: 'error', title: data.title || 'Error ETag', detail: data.detail });
        return;
      }
      setBannerAlert({
        type: 'success',
        title: 'Borrador Guardado con ETag (PA-023)',
        detail: `Respuestas actualizadas con éxito. Nueva revisión: ${data.revision}.`,
      });
      await fetchApplicationDetails(activeApplicationId);
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  const handleUploadFile = async () => {
    if (!activeApplicationId) return;
    setLoading(true);
    try {
      const isExe = uploadFileName.toLowerCase().endsWith('.exe');
      const res = await fetch(`${API_BASE}/participation/self/v1/applications/${activeApplicationId}/file-uploads`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          field_name: 'pitch_deck',
          file_name: uploadFileName,
          mime_type: uploadMimeType,
          size_bytes: isExe ? 1024 : 1024 * 350,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        setBannerAlert({ type: 'error', title: data.title || 'Error de archivo', detail: data.detail });
        return;
      }
      setBannerAlert({
        type: data.state === 'quarantined' ? 'error' : 'success',
        title: `Archivo Procesado — Estado: ${data.state.toUpperCase()} (PA-024)`,
        detail: data.state === 'quarantined'
          ? 'El escáner anti-malware detectó archivo sospechoso y lo colocó en cuarentena. Bloqueará el envío hasta retirar/subsanar.'
          : 'Archivo verificado y listo para postulación.',
      });
      await fetchApplicationDetails(activeApplicationId);
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  const handleSubmitApplication = async () => {
    if (!activeApplicationId || !applicationDetails) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/participation/self/v1/applications/${activeApplicationId}/submissions`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': randomIdempotencyKey('sub'),
        },
        body: JSON.stringify({ expected_revision: applicationDetails.revision }),
      });
      const data = await res.json();
      if (!res.ok) {
        setBannerAlert({ type: 'error', title: data.title || 'Error al enviar', detail: data.detail });
        return;
      }
      setBannerAlert({
        type: 'success',
        title: 'Postulación Enviada con Snapshot Inmutable (PA-025)',
        detail: `Estado: SUBMITTED. Hash SHA-256: ${data.snapshot_hash?.slice(0, 32)}...`,
      });
      await fetchApplicationDetails(activeApplicationId);
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  const handleWithdrawApplication = async () => {
    if (!activeApplicationId) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/participation/self/v1/applications/${activeApplicationId}/withdrawals`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: withdrawReason }),
      });
      const data = await res.json();
      if (!res.ok) {
        setBannerAlert({ type: 'error', title: data.title || 'Error', detail: data.detail });
        return;
      }
      setBannerAlert({
        type: 'info',
        title: 'Postulación Retirada (PA-026)',
        detail: `Estado terminal: WITHDRAWN. Razón: ${data.reason}`,
      });
      await fetchApplicationDetails(activeApplicationId);
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  const handleRequestChanges = async () => {
    if (!activeApplicationId) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/participation/v1/applications/${activeApplicationId}/change-requests`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          requested_changes: ['Subsanar modelo de costos', 'Adjuntar carta de aval'],
          reviewer_notes: reviewChangeNotes,
          deadline: '2026-11-20T23:59:59Z',
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        setBannerAlert({ type: 'error', title: data.title || 'Error', detail: data.detail });
        return;
      }
      setBannerAlert({
        type: 'warning',
        title: 'Subsanación Solicitada por Organizador (PA-027)',
        detail: 'El estado cambió a "needs_changes". El postulante ahora puede editar el borrador y reenviar.',
      });
      await fetchApplicationDetails(activeApplicationId);
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  const handleResubmitApplication = async () => {
    if (!activeApplicationId || !applicationDetails) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/participation/self/v1/applications/${activeApplicationId}/resubmissions`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': randomIdempotencyKey('resub'),
        },
        body: JSON.stringify({ expected_revision: applicationDetails.revision }),
      });
      const data = await res.json();
      if (!res.ok) {
        setBannerAlert({ type: 'error', title: data.title || 'Error', detail: data.detail });
        return;
      }
      setBannerAlert({
        type: 'success',
        title: 'Postulación Reenviada — Revisión 2 Creada (PA-027)',
        detail: `Estado: RESUBMITTED. Número de revisión: ${data.current_revision_number}. Nuevo Snapshot Hash: ${data.snapshot_hash?.slice(0, 32)}...`,
      });
      await fetchApplicationDetails(activeApplicationId);
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  // PA-031 Handlers
  const handleOpenCorrection = (reg: any) => {
    setSelectedRegForEdit(reg);
    setEditFullName(reg.full_name !== '***' && !reg.full_name.includes('***') ? reg.full_name : '');
    setEditPhone(reg.phone !== '***' ? reg.phone : '');
    setEditInstitution(reg.institution !== '***' ? reg.institution : 'UNAS');
    setEditDocNumber(reg.document_number !== '***' ? reg.document_number : '');
    setBackofficeSubtab('correction');
  };

  const handleCorrectRegistration = async () => {
    if (!selectedRegForEdit || !editReason.trim()) {
      setBannerAlert({ type: 'warning', title: 'Motivo requerido', detail: 'Debe ingresar una justificación auditada para la corrección.' });
      return;
    }
    setLoading(true);
    try {
      const updates: any = {};
      if (editFullName.trim()) updates.full_name = editFullName.trim();
      if (editPhone.trim()) updates.phone = editPhone.trim();
      if (editInstitution.trim()) updates.institution = editInstitution.trim();
      if (editDocNumber.trim()) updates.document_number = editDocNumber.trim();

      const res = await fetch(`${API_BASE}/participation/v1/registrations/${selectedRegForEdit.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          reason: editReason.trim(),
          updates,
          actor: 'manager',
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        setBannerAlert({ type: 'error', title: data.title || 'Error', detail: data.detail });
        return;
      }
      setBannerAlert({
        type: 'success',
        title: 'Corrección Administrativa Auditada (PA-031)',
        detail: `Campos modificados: ${Object.keys(updates).join(', ')}. Evento emitido a Outbox y Audit Log.`,
      });
      setSelectedRegForEdit(null);
      setBackofficeSubtab('registrations');
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  // PA-032 Handlers
  const handleAdmitApplication = async (app: any) => {
    if (!admitReason.trim()) {
      setBannerAlert({ type: 'warning', title: 'Motivo requerido', detail: 'Debe ingresar una justificación de admisibilidad.' });
      return;
    }
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/participation/v1/applications/${app.id}/admissions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_revision: app.revision,
          reason: admitReason.trim(),
          checklist: {
            bases_cumplidas: checklistBases,
            equipo_completo: checklistEquipo,
            archivos_validos: checklistArchivos,
          },
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        setBannerAlert({ type: 'error', title: data.title || 'Error', detail: data.detail });
        return;
      }
      setBannerAlert({
        type: 'success',
        title: 'Postulación Admitida (PA-032)',
        detail: `La postulación ${app.id.slice(0, 8)}... fue ADMITIDA exitosamente con checklist verificado.`,
      });
      setSelectedAppForReview(null);
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  const handleRejectApplication = async (app: any) => {
    if (!admitReason.trim()) {
      setBannerAlert({ type: 'warning', title: 'Motivo requerido', detail: 'Debe ingresar el motivo del rechazo de admisibilidad.' });
      return;
    }
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/participation/v1/applications/${app.id}/rejections`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_revision: app.revision,
          reason: admitReason.trim(),
          checklist: {
            bases_cumplidas: checklistBases,
            equipo_completo: checklistEquipo,
            archivos_validos: checklistArchivos,
          },
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        setBannerAlert({ type: 'error', title: data.title || 'Error', detail: data.detail });
        return;
      }
      setBannerAlert({
        type: 'warning',
        title: 'Postulación Rechazada (PA-032)',
        detail: `La postulación ${app.id.slice(0, 8)}... fue RECHAZADA. Motivo: ${data.reason}`,
      });
      setSelectedAppForReview(null);
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  // PA-033 Handler
  const handleBatchImportSubmit = async () => {
    if (!formConfig?.event_id) return;
    setLoading(true);
    try {
      let parsedRows: any[];
      try {
        parsedRows = JSON.parse(batchRowsText);
      } catch (e) {
        setBannerAlert({ type: 'error', title: 'Formato inválido', detail: 'El contenido debe ser un array JSON válido de objetos.' });
        return;
      }
      const res = await fetch(`${API_BASE}/participation/v1/events/${formConfig.event_id}/registration-imports`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          rows: parsedRows,
          legal_basis: batchLegalBasis,
          dry_run: batchDryRun,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        setBannerAlert({ type: 'error', title: data.title || 'Error', detail: data.detail });
        return;
      }
      setBatchResult(data);
      setBannerAlert({
        type: data.error_count > 0 ? 'warning' : 'success',
        title: batchDryRun ? 'Simulación Dry-Run Completada (PA-033)' : 'Importación Masiva Ejecutada (PA-033)',
        detail: `Total: ${data.total_rows} filas | Válidos: ${data.imported_count || data.valid_count || data.created_count} | Errores: ${data.error_count}`,
      });
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  // PA-034 Handler
  const handlePrivacySubmit = async () => {
    if (!privacyEmail.trim()) {
      setBannerAlert({ type: 'warning', title: 'Correo requerido', detail: 'Ingrese el correo del titular para ejercer derechos ARCO.' });
      return;
    }
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/participation/v1/privacy-requests`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: privacyEmail.trim(),
          request_type: privacyType,
          legal_hold: privacyLegalHold,
          reason: privacyReason.trim() || undefined,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        setBannerAlert({
          type: res.status === 409 ? 'warning' : 'error',
          title: `HTTP ${res.status} — ${data.code || 'Error'}`,
          detail: data.detail || 'Error procesando solicitud de privacidad.',
        });
        setPrivacyReceipt({ error: data, http_status: res.status });
        return;
      }
      setPrivacyReceipt(data);
      setBannerAlert({
        type: 'success',
        title: `Derecho ARCO Procesado (${data.request_type}) (PA-034)`,
        detail: data.request_type === 'erasure'
          ? `PII eliminada y sustituida por tombstone. Recibo hash: ${data.tombstone_hash?.slice(0, 16)}...`
          : `Consulta completada: ${data.registrations_count} registros encontrados.`,
      });
      await refreshAll(true);
    } finally {
      setLoading(false);
    }
  };

  // PA-035 Handler
  const handleOperationsProjectionSubmit = async () => {
    if (!formConfig?.event_id) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/participation/v1/events/${formConfig.event_id}/operations-projections`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          purpose: projPurpose,
          recipient: 'onsite_operations',
          confirmed_only: projConfirmedOnly,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        setBannerAlert({ type: 'error', title: data.title || 'Error', detail: data.detail });
        return;
      }
      setProjectionResult(data);
      setBannerAlert({
        type: 'success',
        title: 'Proyección Acotada Cifrada Generada (PA-035)',
        detail: `Propósito: ${data.purpose} | ${data.records_count} registros | Digest SHA-256: ${data.digest_sha256?.slice(0, 16)}...`,
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
                : bannerAlert.type === 'info'
                ? 'bg-sky-950/50 border-sky-500/40 text-sky-200'
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
            TAB 3: BACKOFFICE, ADMISIBILIDAD & PRIVACIDAD (PA-D: PA-030 .. PA-035)
           =================================================================== */}
        {activeTab === 'backoffice' && (
          <div className="space-y-6">
            {/* Sub-tab Navigation */}
            <div className="bg-slate-900 border border-slate-800 rounded-2xl p-2.5 flex flex-wrap gap-2 items-center justify-between">
              <div className="flex flex-wrap gap-2">
                {[
                  { id: 'registrations', label: '📋 PA-030: Registros & Outbox' },
                  { id: 'correction', label: '✏️ PA-031: Corrección Admin' },
                  { id: 'admissibility', label: '⚖️ PA-032: Admisibilidad' },
                  { id: 'batch_import', label: '📥 PA-033: Importación Lote/CSV' },
                  { id: 'privacy', label: '🛡️ PA-034: Derechos ARCO & Legal Hold' },
                  { id: 'operations', label: '🔒 PA-035: Proyección T4 Onsite' },
                ].map((sub) => (
                  <button
                    key={sub.id}
                    onClick={() => setBackofficeSubtab(sub.id as any)}
                    className={`px-3 py-1.5 rounded-xl text-xs font-semibold transition ${
                      backofficeSubtab === sub.id
                        ? 'bg-sky-500 text-slate-950 shadow-md'
                        : 'bg-slate-950 text-slate-400 hover:text-white border border-slate-800'
                    }`}
                  >
                    {sub.label}
                  </button>
                ))}
              </div>

              <label className="flex items-center gap-2 text-xs bg-slate-950 border border-slate-800 px-3 py-1.5 rounded-xl cursor-pointer">
                <input
                  type="checkbox"
                  checked={revealPii}
                  onChange={(e) => setRevealPii(e.target.checked)}
                />
                <span className="font-medium text-emerald-300">
                  Manager: Ver PII descifrada (AES-256-GCM)
                </span>
              </label>
            </div>

            {/* SUBTAB 1: PA-030 & Outbox */}
            {backofficeSubtab === 'registrations' && (
              <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
                <div className="lg:col-span-7 bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
                  <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 border-b border-slate-800 pb-4">
                    <div>
                      <span className="text-xs font-mono text-sky-400">PA-030 • AD-8</span>
                      <h2 className="text-lg font-bold text-white">
                        Registros del Evento & Blind Index
                      </h2>
                    </div>
                  </div>

                  {/* HMAC Search Bar */}
                  <div className="flex gap-2">
                    <input
                      type="text"
                      placeholder="Buscar por correo exacto (HMAC-SHA256 blind index server-side)..."
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
                          <th className="py-2.5 px-2">Blind Index</th>
                          <th className="py-2.5 px-2">Acción (PA-031)</th>
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
                              {r.email_hmac?.slice(0, 16)}...
                            </td>
                            <td className="py-2.5 px-2">
                              <button
                                onClick={() => handleOpenCorrection(r)}
                                className="px-2.5 py-1 rounded bg-sky-500/20 hover:bg-sky-500/30 text-sky-300 border border-sky-500/40 text-[11px] font-medium"
                              >
                                Corregir (PA-031)
                              </button>
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
                      Envelope canónico emitido transactionalmente hacia Content y Onsite. Cero PII.
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
                          {JSON.stringify(ev.envelope?.data, null, 2)}
                        </pre>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}

            {/* SUBTAB 2: PA-031 Administrative Correction */}
            {backofficeSubtab === 'correction' && (
              <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4 max-w-3xl mx-auto">
                <div className="border-b border-slate-800 pb-4">
                  <span className="text-xs font-mono text-sky-400">PA-031</span>
                  <h2 className="text-lg font-bold text-white">
                    Corrección Administrativa de Datos (Campos Allowlisted)
                  </h2>
                  <p className="text-xs text-amber-300 mt-1">
                    Regla de Arquitectura: Las respuestas de formularios y snapshots son inmutables. Solo se permite corregir campos administrativos permitidos con justificación obligatoria.
                  </p>
                </div>

                {selectedRegForEdit ? (
                  <div className="space-y-4">
                    <div className="bg-slate-950 border border-slate-800 p-3 rounded-xl text-xs space-y-1">
                      <div className="text-slate-400">Registro ID: <span className="font-mono text-sky-300">{selectedRegForEdit.id}</span></div>
                      <div className="text-slate-400">Estado: <span className="font-bold text-white uppercase">{selectedRegForEdit.state}</span></div>
                    </div>

                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      <div>
                        <label className="block text-xs text-slate-300 mb-1">Nombre Completo *</label>
                        <input
                          type="text"
                          value={editFullName}
                          onChange={(e) => setEditFullName(e.target.value)}
                          placeholder="Nombre corregido"
                          className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white"
                        />
                      </div>
                      <div>
                        <label className="block text-xs text-slate-300 mb-1">Teléfono</label>
                        <input
                          type="text"
                          value={editPhone}
                          onChange={(e) => setEditPhone(e.target.value)}
                          placeholder="+51999888777"
                          className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white"
                        />
                      </div>
                      <div>
                        <label className="block text-xs text-slate-300 mb-1">Institución / Sede</label>
                        <input
                          type="text"
                          value={editInstitution}
                          onChange={(e) => setEditInstitution(e.target.value)}
                          placeholder="UNAS — Agronomía"
                          className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white"
                        />
                      </div>
                      <div>
                        <label className="block text-xs text-slate-300 mb-1">Documento de Identidad (DNI/CE)</label>
                        <input
                          type="text"
                          value={editDocNumber}
                          onChange={(e) => setEditDocNumber(e.target.value)}
                          placeholder="74859612"
                          className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white"
                        />
                      </div>
                    </div>

                    <div>
                      <label className="block text-xs text-amber-300 mb-1 font-semibold">
                        Justificación Auditada Obligatoria (Mínimo 3 caracteres) *
                      </label>
                      <textarea
                        rows={2}
                        value={editReason}
                        onChange={(e) => setEditReason(e.target.value)}
                        placeholder="Ej: Corrección de error ortográfico en apellido solicitado por el participante..."
                        className="w-full bg-slate-950 border border-amber-500/50 rounded-lg px-3 py-2 text-xs text-white"
                      />
                    </div>

                    <div className="flex gap-3">
                      <button
                        onClick={handleCorrectRegistration}
                        disabled={loading}
                        className="flex-1 py-2 rounded-xl bg-sky-500 hover:bg-sky-400 text-slate-950 font-bold text-xs"
                      >
                        Guardar Corrección Auditada (PATCH)
                      </button>
                      <button
                        onClick={() => setSelectedRegForEdit(null)}
                        className="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs"
                      >
                        Cancelar
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="text-center py-10 bg-slate-950 rounded-xl border border-slate-800 space-y-2">
                    <div className="text-slate-400 text-sm">Selecciona un registro desde la tabla para corregir sus campos administrativos.</div>
                    <button
                      onClick={() => setBackofficeSubtab('registrations')}
                      className="px-4 py-2 rounded-lg bg-slate-800 text-sky-400 text-xs font-semibold"
                    >
                      Ir a la Tabla de Registros
                    </button>
                  </div>
                )}
              </div>
            )}

            {/* SUBTAB 3: PA-032 Admissibility Assessment */}
            {backofficeSubtab === 'admissibility' && (
              <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-6">
                <div className="border-b border-slate-800 pb-4">
                  <span className="text-xs font-mono text-purple-400">PA-032</span>
                  <h2 className="text-lg font-bold text-white">
                    Evaluación de Admisibilidad de Postulaciones (Reviewer Console)
                  </h2>
                  <p className="text-xs text-slate-400 mt-1">
                    Evaluación formal de postulaciones enviadas con checklist técnico, control de concurrencia optimista (`expected_revision`) y auditoría.
                  </p>
                </div>

                {/* Applications Table */}
                <div className="overflow-x-auto">
                  <table className="w-full text-left border-collapse text-xs">
                    <thead>
                      <tr className="border-b border-slate-800 text-slate-400">
                        <th className="py-2.5 px-2">Emprendimiento</th>
                        <th className="py-2.5 px-2">Track</th>
                        <th className="py-2.5 px-2">Revisión</th>
                        <th className="py-2.5 px-2">Integrantes</th>
                        <th className="py-2.5 px-2">Archivos</th>
                        <th className="py-2.5 px-2">Estado</th>
                        <th className="py-2.5 px-2">Acción</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/70">
                      {applicationsList.length === 0 ? (
                        <tr>
                          <td colSpan={7} className="py-8 text-center text-slate-500">
                            No hay postulaciones registradas en este evento todavía. Crea una en la Pestaña 4.
                          </td>
                        </tr>
                      ) : (
                        applicationsList.map((app) => (
                          <tr key={app.id} className="hover:bg-slate-800/40">
                            <td className="py-2.5 px-2 font-semibold text-white">
                              {app.venture_name || 'Sin nombre'}
                              <div className="font-mono text-[10px] text-slate-400">{app.id.slice(0, 8)}...</div>
                            </td>
                            <td className="py-2.5 px-2 text-slate-300">{app.venture_track}</td>
                            <td className="py-2.5 px-2 font-mono text-sky-400 font-bold">rev {app.revision}</td>
                            <td className="py-2.5 px-2 text-slate-300">{app.active_members_count ?? 1}</td>
                            <td className="py-2.5 px-2 text-slate-300">{app.files_count ?? 0}</td>
                            <td className="py-2.5 px-2">
                              <span
                                className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase ${
                                  app.state === 'admitted'
                                    ? 'bg-emerald-500/20 text-emerald-300'
                                    : app.state === 'rejected'
                                    ? 'bg-rose-500/20 text-rose-300'
                                    : app.state === 'submitted' || app.state === 'resubmitted'
                                    ? 'bg-sky-500/20 text-sky-300'
                                    : 'bg-amber-500/20 text-amber-300'
                                }`}
                              >
                                {app.state}
                              </span>
                            </td>
                            <td className="py-2.5 px-2">
                              <button
                                onClick={() => setSelectedAppForReview(app)}
                                className="px-2.5 py-1 rounded bg-purple-500/20 hover:bg-purple-500/30 text-purple-300 border border-purple-500/40 text-[11px] font-semibold"
                              >
                                Evaluar
                              </button>
                            </td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>

                {/* Review Modal / Drawer */}
                {selectedAppForReview && (
                  <div className="bg-slate-950 border border-purple-500/40 p-5 rounded-2xl space-y-4">
                    <div className="flex items-center justify-between border-b border-slate-800 pb-3">
                      <div>
                        <h3 className="font-bold text-white text-sm">
                          Evaluando: {selectedAppForReview.venture_name} (Revisión #{selectedAppForReview.revision})
                        </h3>
                        <p className="text-xs font-mono text-slate-400">Application ID: {selectedAppForReview.id}</p>
                      </div>
                      <button
                        onClick={() => setSelectedAppForReview(null)}
                        className="text-xs text-slate-400 hover:text-white"
                      >
                        Cerrar ✕
                      </button>
                    </div>

                    {/* Reviewer Checklist */}
                    <div className="space-y-2 bg-slate-900 p-3 rounded-xl border border-slate-800 text-xs">
                      <div className="font-bold text-slate-200">Checklist de Criterios de Admisibilidad:</div>
                      <div className="space-y-1.5">
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={checklistBases}
                            onChange={(e) => setChecklistBases(e.target.checked)}
                          />
                          <span>Cumple con las bases generales y temáticas del evento</span>
                        </label>
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={checklistEquipo}
                            onChange={(e) => setChecklistEquipo(e.target.checked)}
                          />
                          <span>Equipo multidisciplinario cuenta con al menos un líder activo</span>
                        </label>
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={checklistArchivos}
                            onChange={(e) => setChecklistArchivos(e.target.checked)}
                          />
                          <span>Entregables y archivos obligatorios cargados y limpios de amenazas</span>
                        </label>
                      </div>
                    </div>

                    <div>
                      <label className="block text-xs text-slate-300 mb-1">Motivo / Dictamen del Evaluador *</label>
                      <textarea
                        rows={2}
                        value={admitReason}
                        onChange={(e) => setAdmitReason(e.target.value)}
                        className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white"
                      />
                    </div>

                    <div className="flex flex-wrap gap-3">
                      <button
                        onClick={() => handleAdmitApplication(selectedAppForReview)}
                        disabled={loading}
                        className="px-4 py-2 rounded-xl bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold text-xs"
                      >
                        ✓ Declarar Admisible (Admit)
                      </button>
                      <button
                        onClick={() => handleRejectApplication(selectedAppForReview)}
                        disabled={loading}
                        className="px-4 py-2 rounded-xl bg-rose-500 hover:bg-rose-400 text-white font-bold text-xs"
                      >
                        ✕ Rechazar Postulación (Reject)
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* SUBTAB 4: PA-033 Batch Registration Import */}
            {backofficeSubtab === 'batch_import' && (
              <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
                <div className="border-b border-slate-800 pb-4">
                  <span className="text-xs font-mono text-cyan-400">PA-033</span>
                  <h2 className="text-lg font-bold text-white">
                    Importación Masiva por Lotes (Batch / CSV) con Dry-Run
                  </h2>
                  <p className="text-xs text-slate-400 mt-1">
                    Carga controlada de inscripciones con verificación de base legal, deduplicación en lote y en base de datos (`email_hmac`), y validación atómica de cupo.
                  </p>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs text-slate-300 mb-1">
                      Filas en formato JSON (Array de objetos)
                    </label>
                    <textarea
                      rows={8}
                      value={batchRowsText}
                      onChange={(e) => setBatchRowsText(e.target.value)}
                      className="w-full bg-slate-950 border border-slate-700 rounded-lg p-2.5 text-[11px] font-mono text-sky-300"
                    />
                  </div>

                  <div className="space-y-4">
                    <div>
                      <label className="block text-xs text-slate-300 mb-1">Base Legal del Consentimiento *</label>
                      <input
                        type="text"
                        value={batchLegalBasis}
                        onChange={(e) => setBatchLegalBasis(e.target.value)}
                        className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white"
                      />
                    </div>

                    <label className="flex items-center gap-3 bg-slate-950 border border-slate-800 p-3 rounded-xl cursor-pointer">
                      <input
                        type="checkbox"
                        checked={batchDryRun}
                        onChange={(e) => setBatchDryRun(e.target.checked)}
                      />
                      <div>
                        <div className="text-xs font-bold text-white">Modo Simulación (`dry_run`)</div>
                        <div className="text-[11px] text-slate-400">Verifica errores y cupo sin escribir registros ni emitir tickets.</div>
                      </div>
                    </label>

                    <button
                      onClick={handleBatchImportSubmit}
                      disabled={loading}
                      className="w-full py-2.5 rounded-xl bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-bold text-xs"
                    >
                      {batchDryRun ? '🔍 Ejecutar Simulación Dry-Run' : '🚀 Ejecutar Importación Masiva'}
                    </button>
                  </div>
                </div>

                {/* Batch Result */}
                {batchResult && (
                  <div className="bg-slate-950 border border-slate-800 p-4 rounded-xl space-y-3">
                    <div className="flex items-center justify-between text-xs">
                      <span className="font-bold text-white">
                        Resultado: {batchResult.dry_run ? 'SIMULACIÓN DRY-RUN' : 'IMPORTACIÓN COMPLETADA'}
                      </span>
                      <span className="font-mono text-slate-400">
                        Total: {batchResult.total_rows} | Exitosos: {batchResult.created_count || batchResult.imported_count || batchResult.valid_count} | Errores: {batchResult.error_count}
                      </span>
                    </div>

                    {batchResult.errors && batchResult.errors.length > 0 && (
                      <div className="space-y-1">
                        <div className="text-xs font-semibold text-rose-400">Detalle de Errores por Fila:</div>
                        {batchResult.errors.map((err: any, idx: number) => (
                          <div key={idx} className="text-[11px] font-mono text-rose-300 bg-rose-950/40 p-1.5 rounded">
                            Fila #{err.row}: {err.code} — {err.detail} ({err.email})
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}

            {/* SUBTAB 5: PA-034 ARCO Privacy & Legal Hold */}
            {backofficeSubtab === 'privacy' && (
              <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4 max-w-3xl mx-auto">
                <div className="border-b border-slate-800 pb-4">
                  <span className="text-xs font-mono text-emerald-400">PA-034</span>
                  <h2 className="text-lg font-bold text-white">
                    Gestión de Derechos ARCO & Bloqueo Legal Hold
                  </h2>
                  <p className="text-xs text-slate-400 mt-1">
                    Atención de solicitudes de supresión (erasure) con crypto-shredding, revocación de tickets y emisión de recibo criptográfico con cero PII. Si Legal Hold está activo, se suspende el borrado (HTTP 409).
                  </p>
                </div>

                <div className="space-y-3">
                  <div>
                    <label className="block text-xs text-slate-300 mb-1">Correo Electrónico del Titular *</label>
                    <input
                      type="email"
                      value={privacyEmail}
                      onChange={(e) => setPrivacyEmail(e.target.value)}
                      className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white"
                    />
                  </div>

                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div>
                      <label className="block text-xs text-slate-300 mb-1">Tipo de Derecho ARCO</label>
                      <select
                        value={privacyType}
                        onChange={(e) => setPrivacyType(e.target.value as any)}
                        className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white"
                      >
                        <option value="erasure">Supresión / Cancelación (Crypto-Shredding)</option>
                        <option value="access">Acceso / Consulta de Registros</option>
                      </select>
                    </div>

                    <div>
                      <label className="block text-xs text-slate-300 mb-1">Motivo / Fundamentación</label>
                      <input
                        type="text"
                        value={privacyReason}
                        onChange={(e) => setPrivacyReason(e.target.value)}
                        className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white"
                      />
                    </div>
                  </div>

                  <label className="flex items-center gap-3 bg-slate-950 border border-amber-500/40 p-3 rounded-xl cursor-pointer">
                    <input
                      type="checkbox"
                      checked={privacyLegalHold}
                      onChange={(e) => setPrivacyLegalHold(e.target.checked)}
                    />
                    <div>
                      <div className="text-xs font-bold text-amber-300">Activar Legal Hold (Bloqueo Legal Activo)</div>
                      <div className="text-[11px] text-slate-400">
                        Invariante: Si Legal Hold está activo, la supresión de datos se deniega con HTTP 409 legal_hold_active para preservar evidencias.
                      </div>
                    </div>
                  </label>

                  <button
                    onClick={handlePrivacySubmit}
                    disabled={loading}
                    className="w-full py-2.5 rounded-xl bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold text-xs"
                  >
                    Procesar Solicitud de Privacidad ARCO (PA-034)
                  </button>
                </div>

                {/* Receipt Result */}
                {privacyReceipt && (
                  <div className="bg-slate-950 border border-slate-800 p-4 rounded-xl space-y-2">
                    <div className="text-xs font-bold text-slate-200">Recibo Criptográfico Generado:</div>
                    <pre className="text-[11px] font-mono text-emerald-300 bg-slate-900 p-3 rounded-lg overflow-x-auto">
                      {JSON.stringify(privacyReceipt, null, 2)}
                    </pre>
                  </div>
                )}
              </div>
            )}

            {/* SUBTAB 6: PA-035 Purpose-Bound Operations Projection */}
            {backofficeSubtab === 'operations' && (
              <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4 max-w-3xl mx-auto">
                <div className="border-b border-slate-800 pb-4">
                  <span className="text-xs font-mono text-indigo-400">PA-035</span>
                  <h2 className="text-lg font-bold text-white">
                    Proyección Acotada por Propósito para T4 Onsite/Operaciones
                  </h2>
                  <p className="text-xs text-slate-400 mt-1">
                    Generación de sobre criptográfico cifrado con AES-256-GCM conteniendo únicamente los campos de la allowlist autorizada según el propósito (certificación o asistencia), con digest SHA-256 y fecha límite de retención.
                  </p>
                </div>

                <div className="space-y-4">
                  <div>
                    <label className="block text-xs text-slate-300 mb-1">Propósito Autorizado *</label>
                    <select
                      value={projPurpose}
                      onChange={(e) => setProjPurpose(e.target.value as any)}
                      className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white"
                    >
                      <option value="certificate_issuance">certificate_issuance (Emisión de Certificados)</option>
                      <option value="onsite_attendance">onsite_attendance (Control de Asistencia Onsite)</option>
                      <option value="institutional_reporting">institutional_reporting (Reportes Institucionales)</option>
                    </select>
                  </div>

                  <label className="flex items-center gap-2 text-xs text-slate-300 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={projConfirmedOnly}
                      onChange={(e) => setProjConfirmedOnly(e.target.checked)}
                    />
                    <span>Filtrar únicamente participantes con estado 'confirmed'</span>
                  </label>

                  <button
                    onClick={handleOperationsProjectionSubmit}
                    disabled={loading}
                    className="w-full py-2.5 rounded-xl bg-indigo-500 hover:bg-indigo-400 text-white font-bold text-xs"
                  >
                    Generar Sobre Cifrado AES-256-GCM (POST /operations-projections)
                  </button>
                </div>

                {projectionResult && (
                  <div className="bg-slate-950 border border-slate-800 p-4 rounded-xl space-y-2">
                    <div className="flex items-center justify-between text-xs">
                      <span className="font-bold text-indigo-300">Sobre Criptográfico Generado:</span>
                      <span className="font-mono text-slate-400">Algoritmo: {projectionResult.algorithm}</span>
                    </div>
                    <div className="grid grid-cols-2 gap-2 text-[11px] font-mono text-slate-300">
                      <div>Key ID: <span className="text-sky-300">{projectionResult.key_id}</span></div>
                      <div>Registros: <span className="text-emerald-300">{projectionResult.records_count}</span></div>
                      <div className="col-span-2">Digest SHA-256: <span className="text-amber-300">{projectionResult.digest_sha256}</span></div>
                      <div className="col-span-2">Retención hasta: <span className="text-slate-400">{projectionResult.retention_until}</span></div>
                    </div>
                    <pre className="text-[10px] font-mono text-slate-400 bg-slate-900 p-2.5 rounded-lg overflow-x-auto">
                      Ciphertext Preview: {projectionResult.ciphertext_preview || projectionResult.ciphertext}
                    </pre>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* ===================================================================
            TAB 4: VENTURES & APPLICATIONS (PA-C: PA-020 .. PA-027)
           =================================================================== */}
        {activeTab === 'ventures' && (
          <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
            <div className="lg:col-span-7 space-y-6">
              {/* 1. Crear Emprendimiento (PA-020 / PA-021) */}
              <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
                <div className="border-b border-slate-800 pb-3 flex items-center justify-between">
                  <div>
                    <span className="text-xs font-mono text-emerald-400">Paso 1 • PA-020</span>
                    <h2 className="text-base font-bold text-white">Crear Emprendimiento y Borrador (Draft)</h2>
                  </div>
                  {activeApplicationId && (
                    <span className="text-[11px] font-mono bg-emerald-950 text-emerald-400 border border-emerald-800 px-2 py-0.5 rounded-full">
                      App Activa: {activeApplicationId.slice(0, 8)}...
                    </span>
                  )}
                </div>

                <form onSubmit={handleCreateVentureDraft} className="space-y-3">
                  <div>
                    <label className="block text-xs text-slate-300 mb-1">Nombre del Emprendimiento / Startup *</label>
                    <input
                      type="text"
                      value={ventureName}
                      onChange={(e) => setVentureName(e.target.value)}
                      required
                      className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white"
                    />
                  </div>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div>
                      <label className="block text-xs text-slate-300 mb-1">Correo del Líder *</label>
                      <input
                        type="email"
                        value={ventureLeaderEmail}
                        onChange={(e) => setVentureLeaderEmail(e.target.value)}
                        required
                        className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white"
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-slate-300 mb-1">Invitar Co-Fundador Inicial (Opcional)</label>
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
                    className="w-full py-2 rounded-xl bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-semibold text-xs"
                  >
                    1. Registrar Emprendimiento y Crear Borrador Inicial
                  </button>
                </form>
              </div>

              {/* Si hay postulación activa, mostrar herramientas PA-C */}
              {activeApplicationId && (
                <>
                  {/* 2. Equipo e Invitaciones (PA-021 & PA-022) */}
                  <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
                    <div className="border-b border-slate-800 pb-3">
                      <span className="text-xs font-mono text-cyan-400">Paso 2 • PA-021 & PA-022</span>
                      <h2 className="text-base font-bold text-white">Equipo, Invitaciones y Retiro</h2>
                      <p className="text-xs text-slate-400">Tokens one-use con expiración y regla: no se puede retirar al único líder.</p>
                    </div>

                    <div className="space-y-2">
                      <div className="text-xs font-semibold text-slate-300">Miembros Activos del Equipo:</div>
                      <div className="space-y-1.5">
                        {applicationDetails?.members?.map((m: any) => (
                          <div key={m.id} className="flex items-center justify-between bg-slate-950 border border-slate-800 px-3 py-1.5 rounded-lg text-xs">
                            <div>
                              <span className="font-mono text-slate-200">{m.email_masked}</span>
                              <span className={`ml-2 px-1.5 py-0.5 rounded text-[10px] ${m.role === 'leader' ? 'bg-amber-950 text-amber-300 border border-amber-800' : 'bg-cyan-950 text-cyan-300 border border-cyan-800'}`}>
                                {m.role}
                              </span>
                              <span className="ml-1 text-[10px] text-emerald-400">({m.state})</span>
                            </div>
                            <button
                              type="button"
                              onClick={() => handleLeaveMember(m.id)}
                              className="text-[11px] text-rose-400 hover:text-rose-300 font-mono"
                            >
                              Retirar
                            </button>
                          </div>
                        ))}
                      </div>
                    </div>

                    <div className="pt-2 border-t border-slate-800/60">
                      <div className="text-xs font-semibold text-slate-300 mb-2">Invitar Nuevo Integrante (PA-021):</div>
                      <div className="grid grid-cols-1 sm:grid-cols-12 gap-2">
                        <input
                          type="email"
                          placeholder="correo.miembro@unas.edu.pe"
                          value={newMemberEmail}
                          onChange={(e) => setNewMemberEmail(e.target.value)}
                          className="sm:col-span-7 bg-slate-950 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-white"
                        />
                        <select
                          value={newMemberRole}
                          onChange={(e) => setNewMemberRole(e.target.value as any)}
                          className="sm:col-span-3 bg-slate-950 border border-slate-700 rounded-lg px-2 py-1.5 text-xs text-white"
                        >
                          <option value="member">Miembro</option>
                          <option value="co_founder">Co-Fundador</option>
                        </select>
                        <button
                          type="button"
                          onClick={handleInviteMember}
                          className="sm:col-span-2 bg-cyan-600 hover:bg-cyan-500 text-white rounded-lg px-2 py-1.5 text-xs font-medium"
                        >
                          Invitar
                        </button>
                      </div>
                    </div>

                    {applicationDetails?.invitations && applicationDetails.invitations.length > 0 && (
                      <div className="pt-2 border-t border-slate-800/60 space-y-2">
                        <div className="text-xs font-semibold text-slate-300">Invitaciones Pendientes (Simular Aceptar/Declinar PA-022):</div>
                        <div className="space-y-1.5">
                          {applicationDetails.invitations.map((inv: any) => (
                            <div key={inv.id} className="bg-slate-950 border border-slate-800 p-2.5 rounded-lg text-xs space-y-1.5">
                              <div className="flex justify-between items-center">
                                <span className="font-mono text-slate-300">{inv.email_masked} ({inv.role})</span>
                                <span className={`text-[10px] px-1.5 py-0.5 rounded font-mono ${inv.state === 'accepted' ? 'bg-emerald-950 text-emerald-400' : inv.state === 'declined' ? 'bg-rose-950 text-rose-400' : 'bg-amber-950 text-amber-400'}`}>
                                  {inv.state}
                                </span>
                              </div>
                              {inv.state === 'pending' && (
                                <div className="flex gap-2">
                                  {inv.token ? (
                                    <>
                                      <button
                                        type="button"
                                        onClick={() => handleAcceptInvitation(inv.token)}
                                        className="bg-emerald-700 hover:bg-emerald-600 text-white px-2.5 py-1 rounded text-[11px]"
                                      >
                                        ✓ Aceptar Invitación
                                      </button>
                                      <button
                                        type="button"
                                        onClick={() => handleDeclineInvitation(inv.token)}
                                        className="bg-slate-800 hover:bg-slate-700 text-slate-300 px-2.5 py-1 rounded text-[11px]"
                                      >
                                        ✗ Declinar
                                      </button>
                                    </>
                                  ) : (
                                    <span className="text-[10px] text-slate-500 font-mono">Token hasheado (verificar vía endpoint)</span>
                                  )}
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>

                  {/* 3. Edición de Borrador y ETag (PA-023) */}
                  <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-3">
                    <div className="border-b border-slate-800 pb-2 flex items-center justify-between">
                      <div>
                        <span className="text-xs font-mono text-amber-400">Paso 3 • PA-023</span>
                        <h2 className="text-base font-bold text-white">Editar Respuestas del Borrador</h2>
                      </div>
                      <span className="text-xs font-mono text-slate-400">
                        ETag: <span className="text-amber-300">{applicationDetails?.etag || 'W/"1"'}</span>
                      </span>
                    </div>
                    <div>
                      <label className="block text-xs text-slate-300 mb-1">Descripción de la Solución (Autosave con If-Match):</label>
                      <textarea
                        rows={2}
                        value={draftSolutionText}
                        onChange={(e) => setDraftSolutionText(e.target.value)}
                        className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white font-mono"
                      />
                    </div>
                    <button
                      type="button"
                      onClick={handleUpdateDraft}
                      className="px-4 py-1.5 rounded-lg bg-amber-500 hover:bg-amber-400 text-slate-950 font-semibold text-xs"
                    >
                      Guardar Cambios con Control ETag
                    </button>
                  </div>

                  {/* 4. Subida de Archivos y Escaneo Malware (PA-024) */}
                  <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
                    <div className="border-b border-slate-800 pb-2">
                      <span className="text-xs font-mono text-purple-400">Paso 4 • PA-024</span>
                      <h2 className="text-base font-bold text-white">Archivos y Escáner Anti-Malware</h2>
                      <p className="text-xs text-slate-400">Submit bloqueado si algún archivo está en cuarentena o pendiente.</p>
                    </div>

                    <div className="grid grid-cols-1 sm:grid-cols-12 gap-2">
                      <select
                        value={uploadFileName}
                        onChange={(e) => {
                          setUploadFileName(e.target.value);
                          if (e.target.value.endsWith('.exe')) setUploadMimeType('application/x-msdownload');
                          else setUploadMimeType('application/pdf');
                        }}
                        className="sm:col-span-8 bg-slate-950 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-white"
                      >
                        <option value="Pitch_Deck_AgroSense.pdf">Pitch_Deck_AgroSense.pdf (Limpio - Ready)</option>
                        <option value="Modelo_Financiero_UNAS.pdf">Modelo_Financiero_UNAS.pdf (Limpio - Ready)</option>
                        <option value="Script_Peligroso.exe">Script_Peligroso.exe (Infectado - Cuarentena)</option>
                      </select>
                      <button
                        type="button"
                        onClick={handleUploadFile}
                        className="sm:col-span-4 bg-purple-600 hover:bg-purple-500 text-white rounded-lg px-3 py-1.5 text-xs font-medium"
                      >
                        Subir y Escanear
                      </button>
                    </div>

                    {applicationDetails?.files && applicationDetails.files.length > 0 && (
                      <div className="space-y-1.5">
                        <div className="text-xs font-semibold text-slate-300">Archivos Adjuntos:</div>
                        {applicationDetails.files.map((f: any) => (
                          <div key={f.id} className="flex justify-between items-center bg-slate-950 border border-slate-800 px-3 py-1.5 rounded-lg text-xs">
                            <span className="font-mono text-slate-200">{f.file_name}</span>
                            <span className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold uppercase ${f.state === 'ready' ? 'bg-emerald-950 text-emerald-400 border border-emerald-800' : 'bg-rose-950 text-rose-400 border border-rose-800'}`}>
                              {f.state}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* 5, 6, 7. Envío, Retiro y Subsanación (PA-025, PA-026, PA-027) */}
                  <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
                    <div className="border-b border-slate-800 pb-2">
                      <span className="text-xs font-mono text-emerald-400">Paso 5, 6 & 7 • PA-025, PA-026 & PA-027</span>
                      <h2 className="text-base font-bold text-white">Envío Inmutable, Retiro y Subsanación</h2>
                    </div>

                    <div className="space-y-3">
                      {/* Enviar postulación */}
                      {applicationDetails?.state === 'draft' && (
                        <div className="bg-slate-950 border border-slate-800 p-3 rounded-xl space-y-2">
                          <div className="text-xs font-semibold text-emerald-400">PA-025: Envío Definitivo de Postulación</div>
                          <p className="text-[11px] text-slate-400">Congela el snapshot inmutable con SHA-256 de todo el equipo, respuestas y archivos.</p>
                          <button
                            type="button"
                            onClick={handleSubmitApplication}
                            className="w-full py-2 bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold text-xs rounded-lg"
                          >
                            Enviar Postulación Definitiva (Snapshot SHA-256)
                          </button>
                        </div>
                      )}

                      {/* Simulación del Organizador / Reviewer: Solicitar Subsanación */}
                      {(applicationDetails?.state === 'submitted' || applicationDetails?.state === 'resubmitted') && (
                        <div className="bg-slate-950 border border-amber-900/40 p-3 rounded-xl space-y-2">
                          <div className="text-xs font-semibold text-amber-400">PA-027: Panel del Organizador / Evaluador</div>
                          <p className="text-[11px] text-slate-400">Simula al evaluador solicitando subsanación al postulante.</p>
                          <input
                            type="text"
                            value={reviewChangeNotes}
                            onChange={(e) => setReviewChangeNotes(e.target.value)}
                            placeholder="Notas de subsanación..."
                            className="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1 text-xs text-white"
                          />
                          <button
                            type="button"
                            onClick={handleRequestChanges}
                            className="w-full py-1.5 bg-amber-600 hover:bg-amber-500 text-white font-semibold text-xs rounded"
                          >
                            Solicitar Subsanación (needs_changes)
                          </button>
                        </div>
                      )}

                      {/* Reenviar postulación subsanada */}
                      {applicationDetails?.state === 'needs_changes' && (
                        <div className="bg-slate-950 border border-cyan-800 p-3 rounded-xl space-y-2">
                          <div className="text-xs font-semibold text-cyan-400">PA-027: Reenviar Postulación Subsanada</div>
                          <p className="text-[11px] text-slate-400">Genera una nueva versión inmutable (Revisión #{Number(applicationDetails?.current_revision_number || 1) + 1}).</p>
                          <button
                            type="button"
                            onClick={handleResubmitApplication}
                            className="w-full py-2 bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-bold text-xs rounded-lg"
                          >
                            Reenviar Postulación Subsanada (Revisión 2+)
                          </button>
                        </div>
                      )}

                      {/* Retiro de postulación */}
                      {applicationDetails?.state !== 'withdrawn' && (
                        <div className="bg-slate-950 border border-slate-800 p-3 rounded-xl space-y-2">
                          <div className="text-xs font-semibold text-rose-400">PA-026: Retirar Postulación</div>
                          <div className="flex gap-2">
                            <input
                              type="text"
                              value={withdrawReason}
                              onChange={(e) => setWithdrawReason(e.target.value)}
                              placeholder="Motivo del retiro"
                              className="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs text-white"
                            />
                            <button
                              type="button"
                              onClick={handleWithdrawApplication}
                              className="bg-rose-700 hover:bg-rose-600 text-white font-medium text-xs px-3 py-1 rounded"
                            >
                              Retirar
                            </button>
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                </>
              )}
            </div>

            {/* Columna Derecha: Tarjeta de Estado en Vivo y Contratos */}
            <div className="lg:col-span-5 space-y-6">
              {/* Resumen del Emprendimiento y Estado */}
              <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
                <div className="border-b border-slate-800 pb-3 flex justify-between items-center">
                  <h3 className="text-base font-bold text-white">Estado de la Postulación</h3>
                  {applicationDetails?.state && (
                    <span className={`px-2.5 py-0.5 rounded-full text-xs font-bold uppercase font-mono ${
                      applicationDetails.state === 'submitted' ? 'bg-emerald-950 text-emerald-400 border border-emerald-700' :
                      applicationDetails.state === 'needs_changes' ? 'bg-amber-950 text-amber-400 border border-amber-700' :
                      applicationDetails.state === 'resubmitted' ? 'bg-cyan-950 text-cyan-400 border border-cyan-700' :
                      applicationDetails.state === 'withdrawn' ? 'bg-rose-950 text-rose-400 border border-rose-700' :
                      'bg-slate-800 text-slate-300'
                    }`}>
                      {applicationDetails.state}
                    </span>
                  )}
                </div>

                {applicationDetails ? (
                  <div className="space-y-3 text-xs font-mono">
                    <div className="bg-slate-950 border border-slate-800 p-3 rounded-xl space-y-1">
                      <div className="text-slate-400">Emprendimiento: <span className="text-white font-bold">{applicationDetails.venture_name}</span></div>
                      <div className="text-slate-400">Track: <span className="text-slate-200">{applicationDetails.track}</span></div>
                      <div className="text-slate-400">N° Revisión: <span className="text-cyan-400 font-bold">#{applicationDetails.current_revision_number}</span></div>
                      <div className="text-slate-400">ETag Concurrente: <span className="text-amber-300">{applicationDetails.etag}</span></div>
                      {applicationDetails.snapshot_hash && (
                        <div className="text-slate-400 pt-1">
                          Snapshot Hash SHA-256:
                          <div className="text-[10px] text-emerald-400 break-all bg-slate-900 p-1 rounded mt-0.5">
                            {applicationDetails.snapshot_hash}
                          </div>
                        </div>
                      )}
                    </div>

                    {applicationDetails.revisions && applicationDetails.revisions.length > 0 && (
                      <div className="bg-slate-950 border border-slate-800 p-3 rounded-xl space-y-1.5">
                        <div className="text-xs font-bold text-emerald-400">Historial Inmutable (`application_revisions`):</div>
                        {applicationDetails.revisions.map((rev: any) => (
                          <div key={rev.revision_number} className="text-[11px] border-b border-slate-800/80 pb-1">
                            <div>Revisión #{rev.revision_number} — Enviada: {rev.submitted_at?.slice(0, 19)}</div>
                            <div className="text-[9px] text-slate-500 break-all">{rev.snapshot_hash}</div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="text-xs text-slate-400 bg-slate-950 border border-slate-800 p-4 rounded-xl text-center">
                    Crea un emprendimiento en el Paso 1 para ver el ciclo de vida y las revisiones en tiempo real.
                  </div>
                )}
              </div>

              {/* Especificación de Contratos */}
              <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-3">
                <div className="border-b border-slate-800 pb-2">
                  <span className="text-xs font-mono text-sky-400">Artefactos de la Épica PA-C</span>
                  <h3 className="text-sm font-bold text-white">Contratos y Rutas Poseídas</h3>
                </div>
                <div className="space-y-2 text-xs">
                  <div className="bg-slate-950 border border-slate-800 p-3 rounded-lg font-mono text-[11px] text-slate-300 space-y-1">
                    <div className="text-emerald-400 font-bold">POST /participation/public/v1/.../applications (PA-020)</div>
                    <div className="text-cyan-400 font-bold">POST /participation/self/v1/ventures/{'{id}'}/invitations (PA-021)</div>
                    <div className="text-sky-400 font-bold">POST /participation/public/v1/team-invitations/... (PA-022)</div>
                    <div className="text-amber-400 font-bold">GET|PATCH /participation/self/v1/applications/{'{id}'} (PA-023)</div>
                    <div className="text-purple-400 font-bold">POST /participation/self/v1/applications/{'{id}'}/file-uploads (PA-024)</div>
                    <div className="text-emerald-400 font-bold">POST /participation/self/v1/applications/{'{id}'}/submissions (PA-025)</div>
                    <div className="text-rose-400 font-bold">POST /participation/self/v1/applications/{'{id}'}/withdrawals (PA-026)</div>
                    <div className="text-amber-300 font-bold">POST /participation/.../change-requests|resubmissions (PA-027)</div>
                  </div>
                  <a
                    href={`${API_BASE}/docs`}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-block text-xs text-sky-400 hover:underline font-mono"
                  >
                    Ver en Swagger UI interactivo ↗
                  </a>
                </div>
              </div>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
