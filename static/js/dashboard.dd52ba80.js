// dashboard.js — Admin scheduling dashboard interactivity.
// Handles: tab switching, shift board, candidate slide-over, confirm flow,
// CRUD modals for shifts/staff/sites/availability.

'use strict';

// ── State ──────────────────────────────────────────────────────────────────
let state = {
  shifts: [],
  sites: [],
  staff: [],
  clients: [],
  requests: [],
  selectedShift: null,
  selectedCandidate: null,
  candidates: [],
  matchDiagnosis: null,
  confirmAssignmentId: null,
  boardDateFrom: '',
  boardDateTo: '',
  boardSiteId: '',
};

// ── Init ───────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const today = new Date();
  const todayStr = today.toISOString().split('T')[0];
  const twoWeeks = new Date(today);
  twoWeeks.setDate(twoWeeks.getDate() + 14);
  const twoWeeksStr = twoWeeks.toISOString().split('T')[0];
  document.getElementById('board-date-from').value = todayStr;
  document.getElementById('board-date-to').value = twoWeeksStr;
  state.boardDateFrom = todayStr;
  state.boardDateTo = twoWeeksStr;
  // Assignments date range defaults (same two-week window)
  document.getElementById('assign-date-from').value = todayStr;
  document.getElementById('assign-date-to').value = twoWeeksStr;
  // Auto-refresh board when date pickers change
  document.getElementById('board-date-from').addEventListener('change', loadBoard);
  document.getElementById('board-date-to').addEventListener('change', loadBoard);
  loadAll();
  // Restore panel from URL hash after data loads
  activatePanelFromHash();
  // Handle browser back/forward navigation
  window.addEventListener('hashchange', activatePanelFromHash);
});

// ── Tab Switching ───────────────────────────────────────────────────────────
function switchPanel(btn) {
  const panel = btn.dataset.panel;
  document.querySelectorAll('.dash-nav-item').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-' + panel).classList.add('active');
  // Persist active panel to URL hash so refresh preserves it
  history.replaceState(null, '', '#' + panel);
  // Keep metrics fresh whenever admin switches tab
  loadMetrics();
  if (panel === 'team') loadTeam();
  if (panel === 'billing') loadSeats();
}

// Activate the panel from URL hash (e.g. #settings → Use AI view)
function activatePanelFromHash() {
  const hash = location.hash.replace('#', '');
  if (!hash) return;
  const btn = document.querySelector(`.dash-nav-item[data-panel="${hash}"]`);
  if (btn) switchPanel(btn);
}

// ── Load All Data ───────────────────────────────────────────────────────────
async function loadAll() {
  await Promise.all([loadMetrics(), loadWorkSites(), loadShifts(), loadStaff(), loadClients(), loadRequests(), loadCerts(), loadTeam(), loadSeats(), loadAdminInfo()]);
  renderBoard();
  loadAssignments();
}

// ── Admin Info (sidebar) ─────────────────────────────────────────────────────
async function loadAdminInfo() {
  try {
    const res = await fetch('/api/admin/me');
    if (!res.ok) return;
    const data = await res.json();
    const nameEl = document.querySelector('.dash-sidebar-user-name');
    const emailEl = document.querySelector('.dash-sidebar-user-email');
    if (nameEl) nameEl.textContent = data.name || 'Admin';
    if (emailEl) emailEl.textContent = data.email || '';
  } catch (e) { /* sidebar stays as-is */ }
}

// ── Team Panel ───────────────────────────────────────────────────────────────
async function loadTeam() {
  try {
    const res = await fetch('/api/admin/team', {
      credentials: 'same-origin',
    });
    if (!res.ok) {
      toast('error', 'Failed to load team (' + res.status + ')');
      return;
    }
    const data = await res.json();
    state.team = Array.isArray(data) ? data : (data.team || []);
    renderTeamTable();
  } catch {
    toast('error', 'Failed to load team');
  }
}

function renderTeamTable() {
  document.getElementById('team-loading').style.display = 'none';
  const filter = document.getElementById('team-name-filter')?.value.trim().toLowerCase() || '';
  const members = (state.team || []).filter(m => {
    if (!filter) return true;
    return (m.name || '').toLowerCase().includes(filter) || (m.email || '').toLowerCase().includes(filter);
  });
  const empty = document.getElementById('team-empty');
  const wrap = document.getElementById('team-table-wrap');
  const tbody = document.getElementById('team-tbody');

  if (!members.length) {
    empty.style.display = 'block';
    wrap.style.display = 'none';
    return;
  }
  empty.style.display = 'none';
  wrap.style.display = 'block';

  tbody.innerHTML = members.map(m => {
    return `
      <tr>
        <td><span style="font-weight:600">${esc(m.name || '—')}</span></td>
        <td><span class="mono">${esc(m.email)}</span></td>
        <td><span class="tag">${esc(m.role || 'admin')}</span></td>
        <td>
          <div style="display:flex;gap:0.35rem;flex-wrap:wrap">
            <button class="btn btn-accent btn-sm" onclick="sendTeamMagicLink(${m.id}, '${esc(m.email || '')}', '${esc(m.name || '')}')">Send Link</button>
          </div>
        </td>
      </tr>`;
  }).join('');
}

function openAddTeamModal() {
  document.getElementById('form-team').reset();
  openModal('modal-team');
}

async function submitTeamInvite(e) {
  e.preventDefault();
  const payload = {
    name: document.getElementById('team-name').value,
    email: document.getElementById('team-email').value,
  };
  try {
    const res = await fetch('/api/admin/team', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(payload)
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.error || 'Failed to invite admin');
    }
    const data = await res.json();
    toast('success', 'Invite sent!');
    closeModal('modal-team');
    showMagicLinkModal(data.link, data.email_sent, data.email, 'admin');
    await loadTeam();
  } catch (err) {
    toast('error', err.message);
    closeModal('modal-team');
  }
}

async function sendTeamMagicLink(userId, email, name) {
  try {
    const res = await fetch(`/api/admin/team/${userId}/magic-link`, {
      method: 'POST',
      credentials: 'same-origin',
    });
    if (!res.ok) throw new Error();
    const data = await res.json();
    showMagicLinkModal(data.link, data.email_sent, data.email, 'admin');
  } catch {
    toast('error', 'Failed to generate magic link');
  }
}

// ── Open Shifts Count ─────────────────────────────────────────────────────────
function countOpenShifts() {
  return state.shifts.filter(s => (s.confirmed_count || 0) < (s.min_staff || 1)).length;
}

function renderOpenShiftsMetric() {
  const el = document.getElementById('val-open-shifts');
  if (el) el.textContent = countOpenShifts();
}

async function loadMetrics() {
  try {
    const res = await fetch('/api/admin/metrics', {
      credentials: 'same-origin',
    });
    if (!res.ok) return;
    const m = await res.json();
    // Use client-side count when shifts are already loaded (more accurate than
    // the backend metric which may not reflect underfilled shifts yet).
    if (state.shifts.length) {
      renderOpenShiftsMetric();
    } else {
      document.getElementById('val-open-shifts').textContent = m.open_shifts ?? 0;
    }
    document.getElementById('val-pending-requests').textContent = m.pending_requests ?? 0;
    document.getElementById('val-unassigned-staff').textContent = m.unassigned_staff ?? 0;
    document.getElementById('val-recent-confirmed').textContent = m.recent_confirmed ?? 0;
  } catch { /* non-critical */ }
}

async function loadWorkSites() {
  try {
    const res = await fetch('/api/work-sites');
    if (!res.ok) {
      toast('error', 'Failed to load work sites (' + res.status + ')');
      return;
    }
    const data = await res.json();
    state.sites = data.work_sites || [];
    renderSiteFilter();
    renderSiteSelect();
    renderWorkSites();
  } catch {
    toast('error', 'Failed to load work sites');
  }
}

async function loadShifts() {
  try {
    const fromDate = document.getElementById('board-date-from')?.value || state.boardDateFrom || '';
    const toDate = document.getElementById('board-date-to')?.value || state.boardDateTo || '';
    const params = new URLSearchParams();
    if (fromDate) params.set('from_date', fromDate);
    if (toDate) params.set('to_date', toDate);
    const url = '/api/shifts' + (params.toString() ? '?' + params.toString() : '');
    const res = await fetch(url, { credentials: 'same-origin' });
    if (!res.ok) {
      toast('error', 'Failed to load shifts (' + res.status + ')');
      return;
    }
    const data = await res.json();
    state.shifts = data.shifts || [];
    updatePendingBadge();
    renderOpenShiftsMetric();
    renderBoard();
  } catch {
    toast('error', 'Failed to load shifts');
  }
}

async function loadStaff() {
  try {
    const res = await fetch('/api/staff');
    if (!res.ok) {
      toast('error', 'Failed to load staff (' + res.status + ')');
      return;
    }
    const data = await res.json();
    state.staff = data.staff || [];
    renderStaffTable();
    renderStaffFilter();
  } catch {
    toast('error', 'Failed to load staff');
  }
}

async function loadAssignments() {
  try {
    const fromDate = document.getElementById('assign-date-from')?.value || '';
    const toDate = document.getElementById('assign-date-to')?.value || '';
    const staffId = document.getElementById('assign-staff-filter')?.value || '';
    const status = document.getElementById('assign-status-filter')?.value || '';
    const params = new URLSearchParams();
    if (fromDate) params.set('from_date', fromDate);
    if (toDate) params.set('to_date', toDate);
    if (staffId) params.set('staff_id', staffId);
    if (status) params.set('status', status);
    const url = '/api/assignments' + (params.toString() ? '?' + params.toString() : '');
    const res = await fetch(url);
    const data = await res.json();
    const allAssignments = data.assignments || [];

    renderAssignments(allAssignments);
  } catch {
    toast('error', 'Failed to load assignments');
  }
}

// ── Schedule Board ───────────────────────────────────────────────────────────
async function loadBoard() {
  state.boardDateFrom = document.getElementById('board-date-from')?.value || '';
  state.boardDateTo = document.getElementById('board-date-to')?.value || '';
  state.boardSiteId = document.getElementById('board-site-filter')?.value || '';
  // Re-fetch shifts from the API with the updated date range, then render the board
  await loadShifts();
  renderBoard();
}

function updatePendingBadge() {
  const pending = state.shifts.filter(s => (s.confirmed_count || 0) < (s.min_staff || 1)).length;
  const el = document.getElementById('badge-pending');
  if (el) el.textContent = pending > 0 ? pending : '';
}

function renderBoard() {
  document.getElementById('board-loading').style.display = 'none';
  let shifts = state.shifts;
  if (state.boardSiteId) shifts = shifts.filter(s => String(s.site_id) === String(state.boardSiteId));
  const grid = document.getElementById('shift-grid');
  const empty = document.getElementById('board-empty');

  if (!shifts.length) {
    grid.innerHTML = '';
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';

  // Build a table of rows — same info as the old cards, laid out as columns.
  const rows = shifts.map(shift => {
    const start = fmtDT(shift.start_time);
    const end = fmtDT(shift.end_time);
    const skills = renderTags(Array.isArray(shift.required_skills) ? shift.required_skills : []);
    const minStaff = shift.min_staff || 1;
    const confirmedCount = shift.confirmed_count || 0;
    const isFilled = confirmedCount >= minStaff;
    const statusBadge = isFilled
      ? `<span class="badge badge-green">Filled (${minStaff})</span>`
      : confirmedCount > 0
        ? `<span class="badge badge-yellow">${confirmedCount}/${minStaff} Staffed</span>`
        : `<span class="badge badge-yellow">Open (${minStaff} needed)</span>`;
    const assigneeText = isFilled ? 'Fully staffed' : confirmedCount > 0 ? `${confirmedCount}/${minStaff} assigned` : 'Click to assign';

    return `
      <tr class="shift-row${state.selectedShift === shift.id ? ' selected' : ''}"
           onclick="openSlideOver(${shift.id}, '${esc(shift.site_name)}', '${esc(start)}')"
           id="tile-${shift.id}">
        <td class="shift-row-site">${esc(shift.site_name || 'Site #' + shift.site_id)}</td>
        <td class="shift-row-status">${statusBadge}</td>
        <td class="shift-row-time">${start} → ${end}</td>
        <td class="shift-row-skills">${skills}</td>
        <td class="shift-row-assignee">${assigneeText}</td>
        <td class="shift-row-actions" onclick="event.stopPropagation()">
          <div style="display:flex;gap:0.35rem">
            <button class="btn btn-secondary btn-sm" onclick="openSlideOver(${shift.id}, '${esc(shift.site_name)}', '${esc(start)}')">
              Candidates
            </button>
            <button class="btn btn-ghost btn-sm" onclick="openCreateShiftModal(${shift.id})">Edit</button>
            <button class="btn btn-danger btn-sm" onclick="deleteShift(${shift.id})">Del</button>
          </div>
        </td>
      </tr>
    `;
  }).join('');

  grid.innerHTML = `
    <table class="shift-board-table-inner">
      <thead>
        <tr>
          <th>Site</th>
          <th>Status</th>
          <th>Time</th>
          <th>Certifications</th>
          <th>Staffing</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function renderSiteFilter() {
  const sel = document.getElementById('board-site-filter');
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = '<option value="">All Sites</option>' +
    state.sites.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join('');
  sel.value = cur;
  sel.addEventListener('change', () => { state.boardSiteId = sel.value; renderBoard(); });
}

// ── Slide Over: Candidate Ranking ──────────────────────────────────────────
async function openSlideOver(shiftId, siteName, time) {
  state.selectedShift = shiftId;
  state.selectedCandidate = null;
  state.candidates = [];

  const shift = state.shifts.find(s => s.id === shiftId);
  const minStaff = shift ? (shift.min_staff || 1) : 1;
  const confirmedCount = shift ? (shift.confirmed_count || 0) : 0;
  const remaining = Math.max(0, minStaff - confirmedCount);

  const so = document.getElementById('slide-over');
  const backdrop = document.getElementById('so-backdrop');
  const body = document.getElementById('so-body');
  const foot = document.getElementById('so-foot');
  const title = document.getElementById('so-title');
  const sub = document.getElementById('so-sub');

  title.textContent = 'Candidates for Shift';
  const staffLabel = remaining > 0
    ? `${confirmedCount}/${minStaff} staffed · ${remaining} more needed`
    : `${minStaff}/${minStaff} staffed · Fully covered`;
  sub.textContent = `${siteName} · ${time} · ${staffLabel}`;
  body.innerHTML = '<div class="loading-row"><div class="spinner"></div> Finding candidates...</div>';
  foot.style.display = 'none';

  so.classList.add('open');
  backdrop.classList.add('open');

  try {
    const res = await fetch(`/api/shifts/${shiftId}/match`);
    const data = await res.json();
    state.candidates = data.candidates || [];
    state.matchDiagnosis = data.diagnosis || null;
    renderCandidates();
  } catch {
    body.innerHTML = '<div class="empty-state-title" style="color:#dc2626">Failed to load candidates</div>';
  }
}

function closeSlideOver() {
  document.getElementById('slide-over').classList.remove('open');
  document.getElementById('so-backdrop').classList.remove('open');
  document.getElementById('so-confirm-btn').disabled = true;
  state.selectedShift = null;
  state.selectedCandidate = null;
}

function toggleMatchDiagnosis() {
  const panel = document.getElementById('match-diagnosis-panel');
  if (!panel) return;
  const label = document.getElementById('why-no-matches-label');
  const isOpen = panel.style.display !== 'none';
  panel.style.display = isOpen ? 'none' : 'block';
  if (label) label.textContent = isOpen ? 'Why no matches?' : 'Hide reasons';
}

function renderCandidates() {
  const body = document.getElementById('so-body');
  const foot = document.getElementById('so-foot');
  const candidates = state.candidates;
  const diagnosis = state.matchDiagnosis;

  if (!candidates.length) {
    // Show diagnostic empty state with a toggle button to reveal gap reasons
    let diagHtml = '';
    const hasDiagnosis = diagnosis && diagnosis.summary && diagnosis.summary !== 'All staff matched successfully.';
    if (hasDiagnosis) {
      diagHtml = `
        <button class="btn btn-diagnosis-toggle" id="why-no-matches-btn" onclick="toggleMatchDiagnosis()">
          <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
          <span id="why-no-matches-label">Why no matches?</span>
        </button>
        <div class="match-diagnosis" id="match-diagnosis-panel" style="display:none">
          <div class="match-diagnosis-title">Why no matches?</div>
          <ul class="match-diagnosis-list">
            ${diagnosis.not_available > 0 ? `<li><strong>${diagnosis.not_available}</strong> staff member${diagnosis.not_available > 1 ? 's are' : ' is'} not available during this shift.</li>` : ''}
            ${diagnosis.available_but_conflict > 0 ? `<li><strong>${diagnosis.available_but_conflict}</strong> staff member${diagnosis.available_but_conflict > 1 ? 's are' : ' is'} available but already assigned to overlapping shifts.</li>` : ''}
            ${diagnosis.available_but_far > 0 ? `<li><strong>${diagnosis.available_but_far}</strong> available staff member${diagnosis.available_but_far > 1 ? 's are' : ' is'} too far from this site.</li>` : ''}
            ${diagnosis.missing_skills > 0 ? `<li><strong>${diagnosis.missing_skills}</strong> staff member${diagnosis.missing_skills > 1 ? 's are' : ' is'} missing the required skills.</li>` : ''}
            ${diagnosis.no_location > 0 ? `<li><strong>${diagnosis.no_location}</strong> staff member${diagnosis.no_location > 1 ? 's have' : ' has'} no location on file.</li>` : ''}
          </ul>
        </div>`;
    }
    body.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon"><svg width="40" height="40" fill="none" stroke="#eab308" stroke-width="1.5" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg></div>
        <div class="empty-state-title">No available staff match this shift</div>
        <div class="empty-state-sub">${diagnosis ? esc(diagnosis.summary) : 'Try adding staff with matching skills or checking availability.'}</div>
        ${diagHtml}
      </div>`;
    foot.style.display = 'none';
    return;
  }

  // Show diagnosis banner above candidates when there are gaps
  let diagBanner = '';
  if (diagnosis && diagnosis.summary && diagnosis.summary !== 'All staff matched successfully.') {
    // Find the best candidate's score to determine severity
    const topScore = candidates[0]?.score ?? 0;
    const severity = topScore < 0.5 ? 'warn' : 'info';
    diagBanner = `
      <div class="match-diagnosis-banner match-diagnosis-${severity}">
        <span class="match-diagnosis-icon">${severity === 'warn' ? '⚠' : 'ℹ'}</span>
        ${esc(diagnosis.summary)}
      </div>`;
  }

  body.innerHTML = `
    <div class="section-title" style="margin-bottom:0.75rem">
      Ranked Candidates
      <span class="count">${candidates.length}</span>
    </div>
    ${diagBanner}
    <div class="candidate-list">
      ${candidates.map((c, i) => {
        const bd = c.breakdown || {};
        const prox = (bd.proximity_score || 0);
        const skill = (bd.skills_score || 0);
        const avail = (bd.availability_score || 0);
        const selected = state.selectedCandidate && state.selectedCandidate.staff_id === c.staff_id;
        const lt = c.shift_local_time;
        const hasConflict = c.has_conflict;
        const gapReasons = c.gap_reasons || [];
        const primaryGap = gapReasons.length > 0 ? gapReasons[0] : null;
        return `
        <div class="candidate-card${i === 0 ? ' rank-1' : ''}${selected ? ' selected' : ''}${hasConflict ? ' has-conflict' : ''}${c.score === 0 ? ' no-match' : ''}"
             ${hasConflict ? '' : `onclick="selectCandidate(${c.staff_id})"`} id="cand-${c.staff_id}">
          <div class="candidate-rank">${i + 1}</div>
          <div class="candidate-info">
            <div class="candidate-name">${esc(c.name)}${hasConflict ? ' <span class="conflict-badge">Conflict</span>' : ''}</div>
            <div class="candidate-email">${esc(c.email || '')}</div>
            ${lt ? `<div class="candidate-breakdown" style="font-size:0.7rem;color:var(--text-dim)">${lt.day} ${lt.start}–${lt.end} <span style="color:var(--accent)">${lt.timezone}</span></div>` : ''}
            ${primaryGap ? `<div class="candidate-gap">${primaryGap.kind === 'conflict' ? '⚠' : primaryGap.kind === 'no_availability' ? '🕛' : primaryGap.kind === 'missing_skills' ? '📋' : primaryGap.kind === 'far_away' ? '📍' : '•'} ${esc(primaryGap.message)}</div>` : ''}
            <div class="candidate-breakdown">
              <span class="score-chip${prox > 0.5 ? ' good' : ''}">Prox: ${fmtScore(prox)}</span>
              <span class="score-chip${skill > 0.5 ? ' good' : ' ok'}">Skills: ${fmtScore(skill)}</span>
              <span class="score-chip${avail > 0.5 ? ' good' : ''}">Avail: ${fmtScore(avail)}</span>
            </div>
          </div>
          <div class="candidate-scores">
            <div class="candidate-score">${fmtScore(c.score)}</div>
            <div class="score-bar"><div class="score-bar-fill" style="width:${c.score * 100}%"></div></div>
          </div>
        </div>`;
      }).join('')}
    </div>
    <div style="margin-top:0.75rem;padding:0.65rem 0;background:var(--accent-dim);border:1px solid rgba(22,163,74,0.25);border-radius:7px">
      <div style="font-size:0.68rem;color:var(--accent);font-weight:700;letter-spacing:0.06em;text-transform:uppercase;text-align:center;padding:0 0.5rem">
        Select a candidate, then click Assign & Confirm · Repeat for multi-staff shifts
      </div>
    </div>`;

  foot.style.display = 'flex';
  updateSelectedInfo();
}

function selectCandidate(staffId) {
  const cand = state.candidates.find(c => c.staff_id === staffId);
  if (!cand) return;
  if (cand.has_conflict) {
    toast('error', 'This staff member has a scheduling conflict and cannot be assigned');
    return;
  }
  state.selectedCandidate = cand;

  // Update visual selection
  document.querySelectorAll('.candidate-card').forEach(el => el.classList.remove('selected'));
  const el = document.getElementById('cand-' + staffId);
  if (el) el.classList.add('selected');

  updateSelectedInfo();
  document.getElementById('so-confirm-btn').disabled = false;
}

function updateSelectedInfo() {
  const c = state.selectedCandidate;
  document.getElementById('so-selected-name').textContent = c ? esc(c.name) : '—';
  document.getElementById('so-selected-score').textContent = c ? fmtScore(c.score) : '';
}

async function confirmFromSlideOver() {
  if (!state.selectedCandidate || !state.selectedShift) {
    toast('error', 'Select a candidate first');
    return;
  }
  const cand = state.selectedCandidate;

  // First create (or update) assignment, then confirm in one flow
  try {
    // Assign
    const assignRes = await fetch(`/api/shifts/${state.selectedShift}/assign`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ staff_id: cand.staff_id, score: cand.score })
    });
    if (!assignRes.ok) {
      const err = await assignRes.json();
      throw new Error(err.error || 'Assignment failed');
    }
    const assignment = await assignRes.json();

    // Confirm
    const confirmRes = await fetch(`/api/shifts/${state.selectedShift}/confirm/${assignment.id}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirmed_by: 'admin' })
    });
    if (!confirmRes.ok) {
      const err = await confirmRes.json();
      throw new Error(err.error || 'Confirmation failed');
    }

    toast('success', `${cand.name} assigned and confirmed!`);
    // Re-fetch shift data to update confirmed_count
    await loadShifts();
    await loadAssignments();
    await loadMetrics();
    updatePendingBadge();
    // Check if shift is now fully staffed — if so, close; otherwise stay open for more assignments
    const updatedShift = state.shifts.find(s => s.id === state.selectedShift);
    const minStaff = updatedShift ? (updatedShift.min_staff || 1) : 1;
    const newConfirmed = updatedShift ? (updatedShift.confirmed_count || 0) : 0;
    if (newConfirmed >= minStaff) {
      closeSlideOver();
    } else {
      // Stay open: re-fetch candidates with updated assignment state
      state.selectedCandidate = null;
      document.getElementById('so-confirm-btn').disabled = true;
      const shift = updatedShift;
      const remaining = Math.max(0, minStaff - newConfirmed);
      const siteName = shift ? (shift.site_name || 'Site #' + shift.site_id) : '';
      const time = shift ? fmtDT(shift.start_time) : '';
      const staffLabel = `${newConfirmed}/${minStaff} staffed · ${remaining} more needed`;
      document.getElementById('so-sub').textContent = `${siteName} · ${time} · ${staffLabel}`;
      try {
        const res = await fetch(`/api/shifts/${state.selectedShift}/match`);
        const data = await res.json();
        state.candidates = data.candidates || [];
        state.matchDiagnosis = data.diagnosis || null;
        renderCandidates();
      } catch {
        // Candidates refresh failed — keep panel open anyway
      }
    }
  } catch (err) {
    toast('error', err.message);
  }
}

// ── Assignments Panel ───────────────────────────────────────────────────────
function patchAssignmentsHeader() {
  const ths = document.querySelectorAll('#assignments-table thead th');
  if (ths.length !== 6) return;
  // Replace last <th> with a colspan=3 centered "Actions" header
  const last = ths[5];
  last.textContent = 'Actions';
  last.setAttribute('colspan', '3');
  last.classList.add('col-actions-group');
}

function renderAssignments(assignments) {
  document.getElementById('assignments-loading').style.display = 'none';
  const all = assignments.filter(a => !a._filtered);
  const empty = document.getElementById('assignments-empty');
  const tbody = document.getElementById('assignments-tbody');
  const wrap = document.querySelector('#panel-assignments .data-table-wrap');

  if (!all.length) {
    empty.style.display = 'block';
    wrap.style.display = 'none';
    return;
  }
  empty.style.display = 'none';
  wrap.style.display = 'block';

  patchAssignmentsHeader();

  tbody.innerHTML = all.map(a => {
    const statusClass = a.status === 'confirmed' ? 'badge-green' : a.status === 'rejected' ? 'badge-red' : 'badge-yellow';
    return `
      <tr>
        <td><span style="font-size:0.78rem;font-weight:600">${esc(a.site_name || 'Site #' + a.site_id)}</span><br><span class="mono">${fmtDT(a.start_time || '')}</span></td>
        <td>${esc(a.staff_name || a.staff_email || 'Staff #' + a.staff_id)}</td>
        <td><span class="badge badge-blue">${fmtScore(a.score)}</span></td>
        <td><span class="badge ${statusClass}">${a.status}</span></td>
        <td><span class="mono">${esc(a.confirmed_by || '—')}</span></td>
        <td class="col-confirm">
          ${a.status === 'pending' ? `<button class="btn btn-primary btn-sm" onclick="openConfirmModal(${a.id}, '${esc(a.staff_name)}', '${esc(a.site_name)}')">Confirm</button>` : ''}
          ${a.status === 'confirmed' ? `<button class="btn btn-ghost btn-sm" disabled>Confirmed ✓</button>` : ''}
          </td>
        <td class="col-action-btn">
          <button class="btn btn-secondary btn-sm" onclick="openReassignModal(${a.id}, ${a.staff_id}, '${esc(a.staff_name || '')}', '${esc(a.site_name || '')}')">Reassign</button>
          </td>
        <td class="col-action-btn">
          <button class="btn btn-danger btn-sm" onclick="openUnassignModal(${a.id}, '${esc(a.staff_name || '')}', '${esc(a.site_name || '')}')">Unassign</button>
          </td>
      </tr>`;
  }).join('');
}

function renderStaffFilter() {
  const sel = document.getElementById('assign-staff-filter');
  if (!sel) return;
  sel.innerHTML = '<option value="">All Staff</option>' +
    state.staff.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join('');
}

function renderSiteSelect() {
  const sel = document.getElementById('shift-site-id');
  if (!sel) return;
  sel.innerHTML = '<option value="">Select site...</option>' +
    state.sites.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join('');
}

// ── Staff Panel ──────────────────────────────────────────────────────────────
function fmtTime(t) {
  if (!t) return '—';
  const [h, m] = t.split(':');
  const hr = parseInt(h, 10);
  const suffix = hr >= 12 ? 'PM' : 'AM';
  const display = hr === 0 ? 12 : hr > 12 ? hr - 12 : hr;
  return `${display}:${m} ${suffix}`;
}

function renderStaffTable() {
  document.getElementById('staff-loading').style.display = 'none';
  const filter = document.getElementById('staff-name-filter')?.value.trim().toLowerCase() || '';
  const staff = state.staff.filter(s => !filter || (s.name || '').toLowerCase().includes(filter));
  const empty = document.getElementById('staff-empty');
  const wrap = document.getElementById('staff-table-wrap');
  const tbody = document.getElementById('staff-tbody');

  if (!staff.length) {
    empty.style.display = 'block';
    wrap.style.display = 'none';
    return;
  }
  empty.style.display = 'none';
  wrap.style.display = 'block';

  tbody.innerHTML = staff.map(s => {
    const allItems = (Array.isArray(s.certifications) ? s.certifications.map(c => c.name) : []);
    const skillsCertsCell = renderTags(allItems);
    const avail = Array.isArray(s.availability) ? s.availability : [];
    const DAYS = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    const availBadges = avail.length
      ? avail.map(a => `<span class="tag">${DAYS[a.day_of_week] || a.day_of_week} ${fmtTime(a.start_time)}–${fmtTime(a.end_time)}</span>`).join(' ')
      : '<span class="tag">—</span>';
    return `
      <tr>
        <td><span style="font-weight:600">${esc(s.name)}</span></td>
        <td><span class="mono">${esc(s.email)}</span></td>
        <td>${esc(s.phone || '—')}</td>
        <td>${esc(s.address || '—')}</td>
        <td>${skillsCertsCell}</td>
        <td>${availBadges}</td>
        <td><span class="tag">${esc(s.timezone || 'UTC')}</span></td>
        <td>
          <div style="display:flex;gap:0.35rem;flex-wrap:wrap">
            <button class="btn btn-accent btn-sm" onclick="sendMagicLink(${s.id})">Send Link</button>
            <button class="btn btn-secondary btn-sm" onclick="openAvailabilityModal(${s.id})">Avail</button>
            <button class="btn btn-secondary btn-sm" onclick="openAddCertModal(${s.id})">Add Cred</button>
            <button class="btn btn-ghost btn-sm" onclick="deleteStaff(${s.id})">Del</button>
          </div>
        </td>
      </tr>`;
  }).join('');
}

// ── Work Sites Panel ─────────────────────────────────────────────────────────
function renderWorkSites() {
  document.getElementById('sites-loading').style.display = 'none';
  const filter = document.getElementById('sites-search-filter')?.value.trim().toLowerCase() || '';
  const sites = state.sites.filter(s => {
    if (!filter) return true;
    return (s.name || '').toLowerCase().includes(filter) || (s.address || '').toLowerCase().includes(filter);
  });
  const empty = document.getElementById('sites-empty');
  const wrap = document.getElementById('sites-table-wrap');
  const tbody = document.getElementById('sites-tbody');

  if (!sites.length) {
    empty.style.display = 'block';
    wrap.style.display = 'none';
    return;
  }
  empty.style.display = 'none';
  wrap.style.display = 'block';

  tbody.innerHTML = sites.map(s => `
    <tr>
      <td><span style="font-weight:600">${esc(s.name)}</span></td>
      <td>${esc(s.address || '—')}</td>
      <td>${renderTags(Array.isArray(s.required_skills) ? s.required_skills : [])}</td>
      <td><span class="tag">${esc(s.timezone || 'UTC')}</span></td>
      <td>
        <div style="display:flex;gap:0.35rem;flex-wrap:wrap">
          <button class="btn btn-ghost btn-sm" onclick="openEditSiteModal(${s.id})">Edit</button>
          ${(s.shift_count || 0) + (s.request_count || 0) > 0
            ? `<button class="btn btn-danger btn-sm" disabled title="Cannot delete — has ${s.shift_count || 0} shift(s) and ${s.request_count || 0} request(s)">Del</button>`
            : `<button class="btn btn-danger btn-sm" onclick="deleteSite(${s.id})">Del</button>`}
        </div>
      </td>
    </tr>`).join('');
}

// ── Clients Panel ───────────────────────────────────────────────────────────
async function loadClients() {
  try {
    const res = await fetch('/api/admin/clients', {
      credentials: 'same-origin',
    });
    if (!res.ok) {
      toast('error', 'Failed to load clients (' + res.status + ')');
      return;
    }
    const data = await res.json();
    state.clients = Array.isArray(data) ? data : (data.clients || []);
    renderClients();
  } catch {
    toast('error', 'Failed to load clients');
  }
}

function renderClients() {
  document.getElementById('clients-loading').style.display = 'none';
  const nameFilter = document.getElementById('clients-name-filter')?.value.trim().toLowerCase() || '';
  const companyFilter = document.getElementById('clients-company-filter')?.value.trim().toLowerCase() || '';
  const clients = state.clients.filter(c => {
    if (nameFilter && !(c.name || '').toLowerCase().includes(nameFilter)) return false;
    if (companyFilter && !(c.company_name || '').toLowerCase().includes(companyFilter)) return false;
    return true;
  });
  const empty = document.getElementById('clients-empty');
  const wrap = document.getElementById('clients-table-wrap');
  const tbody = document.getElementById('clients-tbody');

  if (!clients.length) {
    empty.style.display = 'block';
    wrap.style.display = 'none';
    return;
  }
  empty.style.display = 'none';
  wrap.style.display = 'block';

  tbody.innerHTML = clients.map(c => {
    const reqCount = c.request_count || 0;
    const hasRequests = reqCount > 0;
    const delBtn = hasRequests
      ? `<button class="btn btn-ghost btn-sm" disabled title="Cannot delete — client has ${reqCount} request(s)">Del</button>`
      : `<button class="btn btn-ghost btn-sm" onclick="deleteClient(${c.id})">Del</button>`;
    return `
    <tr>
      <td><span style="font-weight:600">${esc(c.name)}</span></td>
      <td><span class="mono">${esc(c.email)}</span></td>
      <td>${esc(c.phone || '—')}</td>
      <td>${esc(c.company_name || '—')}</td>
      <td>${hasRequests ? `<span class="badge badge-yellow">${reqCount} request${reqCount > 1 ? 's' : ''}</span>` : '<span class="badge badge-green">No requests</span>'}</td>
      <td>
        <div style="display:flex;gap:0.35rem;flex-wrap:wrap">
          <button class="btn btn-accent btn-sm" onclick="sendClientMagicLink(${c.id})">Send Link</button>
          ${delBtn}
        </div>
      </td>
    </tr>`;
  }).join('');
}

// ── Requests Panel ───────────────────────────────────────────────────────────
async function loadRequests() {
  try {
    const status = document.getElementById('requests-status-filter')?.value || '';
    const url = '/api/admin/requests' + (status ? `?status=${status}` : '');
    const res = await fetch(url, {
      credentials: 'same-origin',
    });
    const data = await res.json();
    state.requests = Array.isArray(data) ? data : [];
    updateOpenRequestsBadge();
    renderRequests();
  } catch {
    toast('error', 'Failed to load staffing requests');
  }
}

function updateOpenRequestsBadge() {
  const open = state.requests.filter(r => r.status === 'open').length;
  const el = document.getElementById('badge-open-requests');
  if (el) el.textContent = open > 0 ? open : '';
}

function renderRequests() {
  document.getElementById('requests-loading').style.display = 'none';
  const requesterFilter = document.getElementById('requests-requester-filter')?.value.trim().toLowerCase() || '';
  const siteFilter = document.getElementById('requests-site-filter')?.value.trim().toLowerCase() || '';
  const requests = state.requests.filter(r => {
    if (requesterFilter && !(r.client_name || '').toLowerCase().includes(requesterFilter)) return false;
    if (siteFilter && !(r.site_name || '').toLowerCase().includes(siteFilter)) return false;
    return true;
  });
  const empty = document.getElementById('requests-empty');
  const list = document.getElementById('requests-list');

  if (!requests.length) {
    empty.style.display = 'block';
    list.innerHTML = '';
    return;
  }
  empty.style.display = 'none';

  list.innerHTML = requests.map(r => {
    const isOpen = r.status === 'open';
    const isAccepted = r.status === 'accepted';
    const canCancel = isOpen || isAccepted;
    const rawDate = r.shift_date || '';
    const dateStr = rawDate
      ? new Date(rawDate.includes('T') ? rawDate : rawDate + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
      : '—';
    const timeStr = r.start_time && r.end_time ? `${r.start_time} → ${r.end_time}` : '—';
    const statusBadge = isOpen
      ? '<span class="badge badge-yellow">Open</span>'
      : isAccepted
      ? '<span class="badge badge-blue">Accepted</span>'
      : r.status === 'filled'
      ? '<span class="badge badge-green">Filled</span>'
      : '<span class="badge badge-red">Cancelled</span>';
    return `
    <div class="request-card" id="req-${r.id}">
      <div class="request-card-head">
        <div>
          <div class="request-meta-label">Requested by</div>
          <div class="request-client">${esc(r.client_name || 'Client #' + r.client_id)}</div>
          <div class="request-company">${esc(r.client_email || '')}</div>
        </div>
        ${statusBadge}
      </div>
      <div class="request-card-body">
        <div class="request-meta-grid">
          <div class="request-meta-item">
            <span class="request-meta-label">Work Site</span>
            <span class="request-meta-value">${esc(r.site_name || (r.site_id ? 'Site #' + r.site_id : 'Not specified'))}</span>
          </div>
          <div class="request-meta-item">
            <span class="request-meta-label">Date</span>
            <span class="request-meta-value mono">${dateStr}</span>
          </div>
          <div class="request-meta-item">
            <span class="request-meta-label">Time</span>
            <span class="request-meta-value mono">${timeStr}</span>
          </div>
          <div class="request-meta-item">
            <span class="request-meta-label">Min Staff</span>
            <span class="request-meta-value">${r.min_staff || 1}</span>
          </div>
        </div>
        <div class="request-skills">${renderTags(Array.isArray(r.required_skills) ? r.required_skills : [])}</div>
        ${r.notes ? `<div class="request-notes"><span class="request-meta-label">Notes</span><div class="request-notes-text">${esc(r.notes)}</div></div>` : ''}
      </div>
      <div class="request-card-foot">
        <span class="request-created">${fmtDT(r.created_at)}</span>
        <div style="display:flex;gap:0.35rem">
          ${isOpen ? `
            <button class="btn btn-accent btn-sm" onclick="convertRequest(${r.id})">
              Convert to Shift
            </button>` : ''}
          ${canCancel ? `
            <button class="btn btn-ghost btn-sm" onclick="cancelRequest(${r.id})">
              Cancel
            </button>` : ''}
        </div>
      </div>
    </div>`;
  }).join('');
}

async function convertRequest(requestId) {
  if (!confirm('Convert this request to a shift? It will appear on the Schedule Board.')) return;
  const btn = document.querySelector(`#req-${requestId} .btn-accent`);
  if (btn) { btn.disabled = true; btn.textContent = 'Converting…'; }

  try {
    const res = await fetch(`/api/admin/requests/${requestId}/convert`, {
      method: 'POST',
      credentials: 'same-origin',
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.error || 'Conversion failed');
    }
    toast('success', 'Request converted to shift!');
    await loadRequests();
    await loadShifts();
    updatePendingBadge();
  } catch (err) {
    toast('error', err.message);
    if (btn) { btn.disabled = false; btn.textContent = 'Convert to Shift'; }
  }
}

async function cancelRequest(requestId) {
  if (!confirm('Cancel this staffing request? This cannot be undone.')) return;
  const btn = document.querySelector(`#req-${requestId} .btn-ghost`);
  if (btn) { btn.disabled = true; btn.textContent = 'Cancelling…'; }

  try {
    const res = await fetch(`/api/admin/requests/${requestId}/cancel`, {
      method: 'POST',
      credentials: 'same-origin',
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.error || 'Cancel failed');
    }
    toast('success', 'Request cancelled');
    await loadRequests();
    await loadShifts();
    renderBoard();
    updatePendingBadge();
  } catch (err) {
    toast('error', err.message);
    if (btn) { btn.disabled = false; btn.textContent = 'Cancel'; }
  }
}

function fmtRequestsDT(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true });
  } catch {
    return iso;
  }
}

// ── Copy Server URL ───────────────────────────────────────────────────────────
function copyServerUrl() {
  navigator.clipboard.writeText('https://senzii.com/mcp').then(() => {
    toast('success', 'Server URL copied to clipboard!');
  }).catch(() => {
    toast('error', 'Failed to copy');
  });
}

// ── Send Client Magic Link ────────────────────────────────────────────────────
async function sendClientMagicLink(clientId) {
  try {
    const res = await fetch(`/api/admin/clients/${clientId}/magic-link`, {
      method: 'POST',
      credentials: 'same-origin',
    });
    if (!res.ok) throw new Error();
    const data = await res.json();
    showMagicLinkModal(data.link, data.email_sent, data.email, 'client');
  } catch {
    toast('error', 'Failed to generate magic link');
  }
}

// ── Add Client Modal ─────────────────────────────────────────────────────────
function openAddClientModal() {
  document.getElementById('form-client').reset();
  openModal('modal-client');
}

async function submitClient(e) {
  e.preventDefault();
  const payload = {
    name: document.getElementById('client-name').value,
    email: document.getElementById('client-email').value,
    phone: document.getElementById('client-phone').value || null,
    company_name: document.getElementById('client-company').value || null,
  };
  try {
    const res = await fetch('/api/admin/clients', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.error || 'Failed to add client');
    }
    toast('success', 'Client added!');
    closeModal('modal-client');
    // Retry load to handle Neon read replica lag
    const clientName = document.getElementById('client-name').value;
    await retryLoad(
      loadClients,
      () => state.clients.some(c => c.name === clientName),
    );
  } catch (err) {
    toast('error', err.message);
    closeModal('modal-client');
  }
}

async function deleteClient(id) {
  if (!confirm('Delete this client? This cannot be undone.')) return;
  try {
    const res = await fetch(`/api/admin/clients/${id}`, {
      method: 'DELETE',
      credentials: 'same-origin',
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.error || 'Failed to delete client');
    }
    toast('success', 'Client deleted');
    await loadClients();
  } catch (err) {
    toast('error', err.message);
  }
}

// ── Modals ───────────────────────────────────────────────────────────────────
function openModal(id) {
  document.getElementById(id).classList.add('open');
  // Prevent body scroll when modal is open
  document.body.style.overflow = 'hidden';
}
function closeModal(id) {
  document.getElementById(id).classList.remove('open');
  // Restore body scroll
  document.body.style.overflow = '';
}

// Close modal when clicking the backdrop (outside .modal content)
// Skip modals marked [data-require-confirm] — user must click Cancel
document.addEventListener('click', (e) => {
  const wrap = e.target.closest('.modal-wrap.open');
  if (wrap && !e.target.closest('.modal') && !wrap.hasAttribute('data-require-confirm')) {
    closeModal(wrap.id);
  }
});

// Close modal on Escape key (skip [data-require-confirm] modals)
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-wrap.open').forEach(m => {
      if (!m.hasAttribute('data-require-confirm')) closeModal(m.id);
    });
  }
});

function openCreateShiftModal(shiftId) {
  document.getElementById('modal-shift-title').textContent = shiftId ? 'Edit Shift' : 'New Shift';
  document.getElementById('form-shift').reset();
  document.getElementById('shift-id').value = shiftId || '';
  renderSiteSelect();
  if (shiftId) {
    const s = state.shifts.find(x => x.id === shiftId);
    if (s) {
      document.getElementById('shift-site-id').value = s.site_id || '';
      document.getElementById('shift-start').value = s.start_time?.replace('Z', '') || '';
      document.getElementById('shift-end').value = s.end_time?.replace('Z', '') || '';
      renderCertPicker('shift-cert-picker', 'shift-skills', s.required_skills || []);
      document.getElementById('shift-min-staff').value = s.min_staff || 1;
    }
  } else {
    renderCertPicker('shift-cert-picker', 'shift-skills', []);
  }
  openModal('modal-shift');
}

function openAddStaffModal() {
  document.getElementById('modal-staff-title').textContent = 'Add Staff';
  document.getElementById('form-staff').reset();
  document.getElementById('staff-edit-id').value = '';
  renderCertPicker('staff-cert-picker', 'staff-skills', []);
  openModal('modal-staff');
}

function openAddSiteModal() {
  document.getElementById('modal-site-title').textContent = 'Add Work Site';
  document.getElementById('form-site').reset();
  document.getElementById('site-edit-id').value = '';
  renderCertPicker('site-cert-picker', 'site-skills', []);
  openModal('modal-site');
}

function openAvailabilityModal(staffId) {
  document.getElementById('avail-staff-id').value = staffId;
  document.getElementById('form-avail').reset();
  openModal('modal-avail');
}

// Modal for confirming from assignments table
function openConfirmModal(assignmentId, staffName, siteName) {
  state.confirmAssignmentId = assignmentId;
  document.getElementById('confirm-details').innerHTML = `
    <div class="confirm-panel">
      <div class="confirm-panel-label">Assignment Details</div>
      <div class="confirm-selected">
        <div>
          <div class="confirm-selected-name">${esc(staffName)}</div>
          <div style="font-size:0.75rem;color:var(--text-muted)">${esc(siteName)}</div>
        </div>
      </div>
    </div>`;
  openModal('modal-confirm');
}

async function doConfirmAssignment() {
  const id = state.confirmAssignmentId;
  if (!id) return;
  try {
    const res = await fetch(`/api/assignments/${id}/confirm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirmed_by: 'admin' })
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.error || 'Confirmation failed');
    }
    toast('success', 'Assignment confirmed!');
    closeModal('modal-confirm');
    await loadAssignments();
  } catch (err) {
    toast('error', err.message || 'Failed to confirm assignment');
  }
}

// Reassign modal — change staff member on an assignment
function openReassignModal(assignmentId, currentStaffId, currentStaffName, siteName) {
  state.reassignAssignmentId = assignmentId;
  state.reassignCurrentStaffId = currentStaffId;
  const select = document.getElementById('reassign-staff-select');
  // Populate with staff from state, excluding the current staff member
  select.innerHTML = '<option value="">— Select new staff —</option>' +
    state.staff
      .filter(s => s.id !== currentStaffId)
      .map(s => `<option value="${s.id}">${esc(s.name)}</option>`)
      .join('');
  document.getElementById('reassign-details').innerHTML = `
    <div class="confirm-panel">
      <div class="confirm-panel-label">Current Assignment</div>
      <div class="confirm-selected">
        <div>
          <div class="confirm-selected-name">${esc(currentStaffName)}</div>
          <div style="font-size:0.75rem;color:var(--text-muted)">${esc(siteName)}</div>
        </div>
      </div>
    </div>`;
  openModal('modal-reassign');
}

async function doReassign() {
  const id = state.reassignAssignmentId;
  const newStaffId = parseInt(document.getElementById('reassign-staff-select').value, 10);
  if (!id || !newStaffId) {
    toast('error', 'Select a staff member to reassign to');
    return;
  }
  try {
    const res = await fetch(`/api/assignments/${id}/reassign`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ new_staff_id: newStaffId })
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.error || 'Reassignment failed');
    }
    toast('success', 'Assignment reassigned!');
    closeModal('modal-reassign');
    await loadAssignments();
  } catch (err) {
    toast('error', err.message || 'Failed to reassign');
  }
}

// Unassign modal — remove staff from a shift
function openUnassignModal(assignmentId, staffName, siteName) {
  state.unassignAssignmentId = assignmentId;
  document.getElementById('unassign-details').innerHTML = `
    <div class="confirm-panel">
      <div class="confirm-panel-label">Assignment</div>
      <div class="confirm-selected">
        <div>
          <strong>${esc(staffName)}</strong>
          <div style="font-size:0.78rem;color:var(--text-muted)">${esc(siteName)}</div>
        </div>
      </div>
    </div>`;
  openModal('modal-unassign');
}

async function doUnassign() {
  const id = state.unassignAssignmentId;
  if (!id) return;
  try {
    const res = await fetch(`/api/assignments/${id}`, { method: 'DELETE' });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.error || 'Failed to unassign');
    }
    toast('success', 'Assignment removed');
    closeModal('modal-unassign');
    await loadAssignments();
  } catch (err) {
    toast('error', err.message || 'Failed to unassign');
  }
}

// Add cert modal — uses org cert list as dropdown
let orgCerts = [];
async function loadCerts() {
  try {
    const res = await fetch('/api/org-certs');
    if (!res.ok) throw new Error();
    const data = await res.json();
    orgCerts = data.certifications || [];
    renderCerts();
    renderAllCertPickers();
  } catch {
    // Silently fail — certs panel may not be visible yet
  }
}

function renderCerts() {
  const loading = document.getElementById('certs-loading');
  const empty = document.getElementById('certs-empty');
  const content = document.getElementById('certs-content');
  const tableWrap = document.getElementById('certs-table-wrap');
  const tbody = document.getElementById('certs-tbody');
  if (!tbody) return;

  loading.style.display = 'none';
  content.style.display = '';
  if (!orgCerts.length) {
    empty.style.display = '';
    tableWrap.style.display = 'none';
    return;
  }
  empty.style.display = 'none';
  tableWrap.style.display = '';
  tbody.innerHTML = orgCerts.map(c => `
    <tr id="cert-row-${c.id}">
      <td id="cert-name-${c.id}"><strong>${esc(c.name)}</strong></td>
      <td>${c.created_at ? new Date(c.created_at).toLocaleDateString() : '—'}</td>
      <td>
        <button class="btn btn-ghost btn-sm" onclick="editCert(${c.id})">Edit</button>
        <button class="btn btn-ghost btn-sm" onclick="deleteCert(${c.id})">Delete</button>
      </td>
    </tr>
  `).join('');
}

async function addCert() {
  const input = document.getElementById('cert-new-name');
  const name = input.value.trim();
  if (!name) return;
  try {
    const res = await fetch('/api/org-certs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name })
    });
    if (res.status === 409) {
      toast('error', 'Certification already exists');
      return;
    }
    if (!res.ok) throw new Error();
    input.value = '';
    toast('success', 'Certification added');
    await loadCerts();
  } catch {
    toast('error', 'Failed to add certification');
  }
}

function editCert(id) {
  const cell = document.getElementById(`cert-name-${id}`);
  if (!cell) return;
  const cert = orgCerts.find(c => c.id === id);
  if (!cert) return;
  cell.innerHTML = `<input type="text" class="form-row-input" id="cert-edit-input-${id}" value="${esc(cert.name)}" style="width:100%">`;
  const input = document.getElementById(`cert-edit-input-${id}`);
  input.focus();
  input.select();
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') saveCertEdit(id);
    if (e.key === 'Escape') renderCerts();
  });
}

async function saveCertEdit(id) {
  const input = document.getElementById(`cert-edit-input-${id}`);
  if (!input) return;
  const name = input.value.trim();
  if (!name) { toast('error', 'Name cannot be empty'); return; }
  const cert = orgCerts.find(c => c.id === id);
  if (cert && cert.name === name) { renderCerts(); return; }
  try {
    const res = await fetch(`/api/org-certs/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name })
    });
    if (res.status === 409) { toast('error', 'Certification name already exists'); return; }
    if (!res.ok) throw new Error();
    toast('success', 'Certification updated');
    await loadCerts();
  } catch {
    toast('error', 'Failed to update certification');
    renderCerts();
  }
}

async function deleteCert(id) {
  if (!confirm('Delete this certification? This will also remove it from any staff who have it assigned.')) return;
  try {
    const res = await fetch(`/api/org-certs/${id}`, { method: 'DELETE' });
    if (!res.ok) throw new Error();
    toast('success', 'Certification removed');
    await loadCerts();
  } catch {
    toast('error', 'Failed to remove certification');
  }
}

function renderCertPicker(pickerId, hiddenId, selected) {
  const picker = document.getElementById(pickerId);
  const hidden = document.getElementById(hiddenId);
  if (!picker || !hidden) return;

  const optionsEl = picker.querySelector('.cert-picker-options');
  const emptyEl = picker.querySelector('.cert-picker-empty');

  if (!orgCerts.length) {
    emptyEl.style.display = '';
    optionsEl.innerHTML = '';
    hidden.value = '[]';
    return;
  }
  emptyEl.style.display = 'none';
  const sel = new Set(selected || []);
  optionsEl.innerHTML = orgCerts.map(c => {
    const checked = sel.has(c.name);
    return `<label class="cert-picker-option${checked ? ' selected' : ''}">
      <input type="checkbox" value="${esc(c.name)}" ${checked ? 'checked' : ''}>
      ${esc(c.name)}
    </label>`;
  }).join('');

  // Sync hidden input on toggle
  optionsEl.querySelectorAll('input[type="checkbox"]').forEach(cb => {
    cb.addEventListener('change', () => {
      cb.closest('.cert-picker-option').classList.toggle('selected', cb.checked);
      hidden.value = JSON.stringify(getPickerCerts(pickerId));
    });
  });
  hidden.value = JSON.stringify(getPickerCerts(pickerId));
}

function getPickerCerts(pickerId) {
  const picker = document.getElementById(pickerId);
  if (!picker) return [];
  return Array.from(picker.querySelectorAll('input[type="checkbox"]:checked')).map(cb => cb.value);
}

function renderAllCertPickers(selected) {
  renderCertPicker('shift-cert-picker', 'shift-skills', selected);
  renderCertPicker('site-cert-picker', 'site-skills', selected);
  renderCertPicker('staff-cert-picker', 'staff-skills', selected);
}

function openAddCertModal(staffId) {
  if (!orgCerts.length) {
    toast('error', 'No certifications defined. Add some in the Certs panel first.');
    return;
  }
  document.getElementById('add-cert-staff-id').value = staffId;
  document.getElementById('add-cert-expires').value = '';

  // Filter out certifications the staff member already has
  const staff = state.staff.find(s => s.id === staffId);
  const existing = new Set(Array.isArray(staff?.certifications) ? staff.certifications.map(c => c.name) : []);
  const available = orgCerts.filter(c => !existing.has(c.name));

  const listEl = document.getElementById('add-cert-list');
  const optionsEl = listEl.querySelector('.cert-picker-options');
  const emptyEl = listEl.querySelector('.cert-picker-empty');

  if (!available.length) {
    emptyEl.style.display = '';
    emptyEl.textContent = 'This staff member already has all available certifications.';
    optionsEl.innerHTML = '';
  } else {
    emptyEl.style.display = 'none';
    optionsEl.innerHTML = available.map(c => {
      return `<label class="cert-picker-option">
        <input type="radio" name="add-cert-choice" value="${esc(c.name)}">
        ${esc(c.name)}
      </label>`;
    }).join('');

    // Toggle .selected class on radio change
    optionsEl.querySelectorAll('input[name="add-cert-choice"]').forEach(radio => {
      radio.addEventListener('change', () => {
        optionsEl.querySelectorAll('.cert-picker-option').forEach(opt => opt.classList.remove('selected'));
        radio.closest('.cert-picker-option').classList.add('selected');
      });
    });
  }

  openModal('modal-add-cert');
}

async function submitAddCert() {
  const staffId = document.getElementById('add-cert-staff-id').value;
  const selected = document.querySelector('input[name="add-cert-choice"]:checked');
  if (!selected) {
    toast('error', 'Please select a certification.');
    return;
  }
  const name = selected.value;
  const expires = document.getElementById('add-cert-expires').value || null;
  try {
    const res = await fetch(`/api/staff/${staffId}/certifications`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, expires_at: expires })
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.error || 'Failed');
    }
    toast('success', 'Certification added');
    closeModal('modal-add-cert');
    const staffName = state.staff.find(s => s.id === parseInt(staffId))?.name;
    await retryLoad(
      loadStaff,
      () => state.staff.some(s => s.name === staffName && Array.isArray(s.certifications) && s.certifications.some(c => c.name === name)),
    );
  } catch (err) {
    toast('error', err.message || 'Failed to add certification');
  }
}

// ── Retry helper for Neon read replica lag ────────────────────────────────────
// After a write, the read replica may not yet have the new row.
// Retry the load function up to 5 times with a 500ms delay between attempts.
async function retryLoad(loadFn, checkFn, maxAttempts = 5, delay = 500) {
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    await loadFn();
    if (checkFn()) return;
    if (attempt < maxAttempts - 1) {
      await new Promise(r => setTimeout(r, delay));
    }
  }
}

// ── Form Submissions ─────────────────────────────────────────────────────────
async function submitShift(e) {
  e.preventDefault();
  const id = document.getElementById('shift-id').value;
  const payload = {
    site_id: parseInt(document.getElementById('shift-site-id').value),
    start_time: document.getElementById('shift-start').value,
    end_time: document.getElementById('shift-end').value,
    required_skills: getPickerCerts('shift-cert-picker'),
    min_staff: parseInt(document.getElementById('shift-min-staff').value) || 1,
  };
  try {
    if (id) {
      toast('info', 'Edit not implemented — create a new shift');
    } else {
      const res = await fetch('/api/shifts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (!res.ok) throw new Error('Failed to create shift');
      toast('success', 'Shift created!');
    }
    closeModal('modal-shift');
    await loadShifts();
  } catch {
    toast('error', 'Failed to save shift');
  }
}

async function submitStaff(e) {
  e.preventDefault();
  const payload = {
    name: document.getElementById('staff-name').value,
    email: document.getElementById('staff-email').value,
    phone: document.getElementById('staff-phone').value || null,
    address: document.getElementById('staff-address').value || null,
    timezone: document.getElementById('staff-tz').value || 'UTC',
  };
  const selectedCerts = getPickerCerts('staff-cert-picker');
  try {
    const res = await fetch('/api/staff', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!res.ok) {
      const err = await res.json();
      if (res.status === 402 && err.error === 'staff_limit_reached') {
        toast('error', err.message || 'Staff member limit reached. Add more seats in Settings → Billing.');
        closeModal('modal-staff');
        // Switch to billing panel so they can add staff members
        const billingNav = document.querySelector('.dash-nav-item[data-panel="billing"]');
        if (billingNav) switchPanel(billingNav);
        return;
      }
      throw new Error(err.error || 'Failed to add staff');
    }
    const newStaff = await res.json();

    // Assign selected certifications to the new staff member
    if (selectedCerts.length && newStaff.id) {
      for (const certName of selectedCerts) {
        await fetch(`/api/staff/${newStaff.id}/certifications`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: certName, expires_at: null })
        });
      }
    }

    toast('success', 'Staff member added!');
    closeModal('modal-staff');
    // Retry load to handle Neon read replica lag
    const staffName = document.getElementById('staff-name').value;
    await retryLoad(
      loadStaff,
      () => state.staff.some(s => s.name === staffName),
    );
  } catch (err) {
    toast('error', err.message);
    closeModal('modal-staff');
  }
}

async function submitSite(e) {
  e.preventDefault();
  const editId = document.getElementById('site-edit-id').value;
  const payload = {
    name: document.getElementById('site-name').value,
    address: document.getElementById('site-address').value || null,
    required_skills: getPickerCerts('site-cert-picker'),
    timezone: document.getElementById('site-tz').value || 'UTC',
  };
  try {
    let res;
    if (editId) {
      // Edit mode — PUT
      res = await fetch(`/api/work-sites/${editId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(payload),
      });
    } else {
      // Create mode — POST
      res = await fetch('/api/work-sites', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    }
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || (editId ? 'Failed to update site' : 'Failed to add site'));
    }
    toast('success', editId ? 'Work site updated!' : 'Work site added!');
    closeModal('modal-site');
    const siteName = document.getElementById('site-name').value;
    await retryLoad(
      loadWorkSites,
      () => state.sites.some(s => s.name === siteName),
    );
  } catch (err) {
    toast('error', err.message);
    closeModal('modal-site');
  }
}

async function submitAvailability(e) {
  e.preventDefault();
  const staffId = document.getElementById('avail-staff-id').value;
  const payload = {
    day_of_week: parseInt(document.getElementById('avail-day').value),
    start_time: document.getElementById('avail-start').value,
    end_time: document.getElementById('avail-end').value,
  };
  try {
    const res = await fetch(`/api/staff/${staffId}/availability`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!res.ok) throw new Error('Failed to set availability');
    toast('success', 'Availability set!');
    closeModal('modal-avail');
    loadStaff();
  } catch {
    toast('error', 'Failed to set availability');
  }
}

// ── Delete ───────────────────────────────────────────────────────────────────
async function deleteShift(id) {
  if (!confirm('Delete this shift? Assigned staff will be freed and the shift removed from the schedule board. This cannot be undone.')) return;
  try {
    const res = await fetch(`/api/shifts/${id}`, {
      method: 'DELETE',
      credentials: 'same-origin',
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      toast('error', data.error || 'Failed to delete shift');
      return;
    }
    toast('success', 'Shift deleted');
    loadBoard();
  } catch (err) {
    toast('error', 'Network error — could not delete shift');
  }
}

async function deleteStaff(id) {
  if (!confirm('Delete this staff member? They will be archived and removed from your active roster.')) return;
  try {
    const res = await fetch(`/api/staff/${id}`, {
      method: 'DELETE',
      credentials: 'same-origin',
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      toast('error', data.error || 'Failed to delete staff member');
      return;
    }
    toast('success', 'Staff member archived');
    loadStaff();
  } catch (err) {
    toast('error', 'Network error — could not delete staff member');
  }
}

async function deleteSite(id) {
  if (!confirm('Delete this work site? This cannot be undone.')) return;
  try {
    const res = await fetch(`/api/work-sites/${id}`, {
      method: 'DELETE',
      credentials: 'same-origin',
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.error || 'Failed to delete work site');
    }
    toast('success', 'Work site deleted');
    await loadWorkSites();
  } catch (err) {
    toast('error', err.message);
  }
}

function openEditSiteModal(id) {
  const s = state.sites.find(x => x.id === id);
  if (!s) return;
  document.getElementById('modal-site-title').textContent = 'Edit Work Site';
  document.getElementById('site-edit-id').value = id;
  document.getElementById('site-name').value = s.name || '';
  document.getElementById('site-address').value = s.address || '';
  document.getElementById('site-tz').value = s.timezone || 'UTC';
  renderCertPicker('site-cert-picker', 'site-skills', s.required_skills || []);
  document.getElementById('form-site').dataset.editMode = 'true';
  openModal('modal-site');
}

async function submitEditSite(e) {
  e.preventDefault();
  const id = document.getElementById('site-edit-id').value;
  const payload = {
    name: document.getElementById('site-name').value,
    address: document.getElementById('site-address').value || null,
    required_skills: getPickerCerts('site-cert-picker'),
    timezone: document.getElementById('site-tz').value || 'UTC',
  };
  try {
    const res = await fetch(`/api/work-sites/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || 'Failed to update work site');
    }
    toast('success', 'Work site updated!');
    closeModal('modal-site');
    const siteName = document.getElementById('site-name').value;
    await retryLoad(
      loadWorkSites,
      () => state.sites.some(s => s.name === siteName),
    );
  } catch (err) {
    toast('error', err.message || 'Failed to update work site');
    closeModal('modal-site');
  }
}

// ── Toast ─────────────────────────────────────────────────────────────────────
let toastTimer;
function toast(type, msg) {
  const el = document.getElementById('toast');
  const icon = document.getElementById('toast-icon');
  const msgEl = document.getElementById('toast-msg');
  clearTimeout(toastTimer);

  el.className = 'toast ' + type + ' show';
  icon.textContent = type === 'success' ? '✓' : type === 'error' ? '✗' : 'i';
  msgEl.textContent = msg;

  toastTimer = setTimeout(hideToast, 3500);
}

function hideToast() {
  document.getElementById('toast').classList.remove('show');
}

// ── Helpers ─────────────────────────────────────────────────────────────────
function renderTags(tags) {
  if (!tags.length) return '<span class="tag">—</span>';
  return tags.slice(0, 4).map(s => `<span class="tag tag-accent">${esc(s)}</span>`).join('') +
    (tags.length > 4 ? `<span class="tag">+${tags.length - 4}</span>` : '');
}

function fmtScore(v) {
  if (v == null || isNaN(v)) return '—';
  return (v * 100).toFixed(0) + '%';
}

function fmtDT(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    return d.toLocaleString('en-US', {
      month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit',
      hour12: true
    });
  } catch {
    return iso;
  }
}

function esc(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ── Send Magic Link ──────────────────────────────────────────────────────────
async function sendMagicLink(staffId) {
  try {
    const res = await fetch(`/api/admin/staff/${staffId}/magic-link`, {
      method: 'POST',
      credentials: 'same-origin',
    });
    if (!res.ok) throw new Error();
    const data = await res.json();
    // Show the link in a modal with email-sent status
    showMagicLinkModal(data.link, data.email_sent, data.email, 'staff');
  } catch {
    toast('error', 'Failed to generate magic link');
  }
}

function showMagicLinkModal(link, emailSent, email, type) {
  // Create overlay
  let overlay = document.getElementById('magic-link-overlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'magic-link-overlay';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.65);z-index:9999;display:flex;align-items:center;justify-content:center;padding:1rem';
    document.body.appendChild(overlay);
  }
  const typeLabel = type === 'client' ? 'Client' : 'Staff';
  const emailStatus = emailSent
    ? `<div style="background:rgba(22,163,74,0.1);border:1px solid rgba(22,163,74,0.3);border-radius:8px;padding:0.5rem 0.75rem;margin-bottom:0.85rem;font-size:0.8rem;color:#16a34a">
        <strong>✓ Email sent</strong> to ${esc(email || '')}
       </div>`
    : `<div style="background:rgba(234,179,8,0.1);border:1px solid rgba(234,179,8,0.3);border-radius:8px;padding:0.5rem 0.75rem;margin-bottom:0.85rem;font-size:0.8rem;color:#ca8a04">
        <strong>⚠ Email not sent</strong> (RESEND_API_KEY not configured). Copy the link below and send it manually.
       </div>`;
  overlay.innerHTML = `
    <div style="background:var(--surface2);border:1px solid var(--border);border-radius:14px;padding:1.5rem;max-width:440px;width:100%;font-family:var(--font-body)">
      <div style="font-family:var(--font-head);font-size:1.1rem;font-weight:700;margin-bottom:0.35rem;color:var(--text)">${typeLabel} Magic Link</div>
      <div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:1rem">Share this link — it expires in 7 days.</div>
      ${emailStatus}
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:0.6rem 0.75rem;word-break:break-all;font-size:0.75rem;color:var(--accent);margin-bottom:0.85rem;max-height:120px;overflow:auto" id="magic-link-text">${link}</div>
      <div style="display:flex;gap:0.5rem">
        <button onclick="copyMagicLink()" style="flex:1;background:var(--accent);color:var(--brand);border:none;border-radius:8px;font-family:var(--font-body);font-size:0.85rem;font-weight:600;padding:0.65rem;cursor:pointer">Copy Link</button>
        <button onclick="closeMagicLinkModal()" style="flex:1;background:var(--surface3);color:var(--text-muted);border:1px solid var(--border);border-radius:8px;font-family:var(--font-body);font-size:0.85rem;padding:0.65rem;cursor:pointer">Close</button>
      </div>
    </div>`;
  overlay.style.display = 'flex';
  window._currentMagicLink = link;
}

function copyMagicLink() {
  navigator.clipboard.writeText(window._currentMagicLink).then(() => {
    toast('success', 'Link copied to clipboard!');
  }).catch(() => {
    // Fallback
    const ta = document.createElement('textarea');
    ta.value = window._currentMagicLink;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    toast('success', 'Link copied!');
  });
}

function closeMagicLinkModal() {
  const overlay = document.getElementById('magic-link-overlay');
  if (overlay) overlay.style.display = 'none';
}

// ── Docs Sidebar ──────────────────────────────────────────────────────────
function openDocs() {
  // Determine which docs group to show based on the active panel
  const activePanel = document.querySelector('.panel.active')?.id || '';

  const boardNav = document.getElementById('docs-nav-board');
  const assignmentsNav = document.getElementById('docs-nav-assignments');
  const staffNav = document.getElementById('docs-nav-staff');
  const sitesNav = document.getElementById('docs-nav-sites');
  const clientsNav = document.getElementById('docs-nav-clients');
  const certsNav = document.getElementById('docs-nav-certs');
  const requestsNav = document.getElementById('docs-nav-requests');
  const billingNav = document.getElementById('docs-nav-billing');
  const teamNav = document.getElementById('docs-nav-team');
  const mcpNav = document.getElementById('docs-nav-mcp');

  // Hide all groups first
  boardNav.style.display = 'none';
  assignmentsNav.style.display = 'none';
  staffNav.style.display = 'none';
  sitesNav.style.display = 'none';
  clientsNav.style.display = 'none';
  certsNav.style.display = 'none';
  requestsNav.style.display = 'none';
  billingNav.style.display = 'none';
  teamNav.style.display = 'none';
  mcpNav.style.display = 'none';

  if (activePanel === 'panel-board') {
    boardNav.style.display = '';
    activateDocsGroup('board');
  } else if (activePanel === 'panel-assignments') {
    assignmentsNav.style.display = '';
    activateDocsGroup('assignments');
  } else if (activePanel === 'panel-staff') {
    staffNav.style.display = '';
    activateDocsGroup('staff');
  } else if (activePanel === 'panel-sites') {
    sitesNav.style.display = '';
    activateDocsGroup('sites');
  } else if (activePanel === 'panel-clients') {
    clientsNav.style.display = '';
    activateDocsGroup('clients');
  } else if (activePanel === 'panel-certs') {
    certsNav.style.display = '';
    activateDocsGroup('certs');
  } else if (activePanel === 'panel-requests') {
    requestsNav.style.display = '';
    activateDocsGroup('requests');
  } else if (activePanel === 'panel-billing') {
    billingNav.style.display = '';
    activateDocsGroup('billing');
  } else if (activePanel === 'panel-team') {
    teamNav.style.display = '';
    activateDocsGroup('team');
  } else {
    mcpNav.style.display = '';
    activateDocsGroup('mcp');
  }

  document.getElementById('docs-sidebar').classList.add('open');
  document.getElementById('docs-backdrop').classList.add('open');
  document.body.style.overflow = 'hidden';
}

// Activate the first page in a docs group (board, assignments, or mcp)
function activateDocsGroup(group) {
  // Deactivate all pages
  document.querySelectorAll('.docs-page').forEach(p => p.classList.remove('active'));
  // Deactivate all nav buttons
  document.querySelectorAll('.docs-nav-btn').forEach(b => b.classList.remove('active'));
  // Activate the first page+button in the group
  const navId = group === 'board' ? 'docs-nav-board'
    : group === 'assignments' ? 'docs-nav-assignments'
    : group === 'staff' ? 'docs-nav-staff'
    : group === 'sites' ? 'docs-nav-sites'
    : group === 'clients' ? 'docs-nav-clients'
    : group === 'certs' ? 'docs-nav-certs'
    : group === 'requests' ? 'docs-nav-requests'
    : group === 'billing' ? 'docs-nav-billing'
    : group === 'team' ? 'docs-nav-team'
    : 'docs-nav-mcp';
  const firstBtn = document.querySelector(`#${navId} .docs-nav-btn`);
  if (firstBtn) {
    firstBtn.classList.add('active');
    const pageId = 'docs-' + firstBtn.dataset.docsPage;
    const target = document.getElementById(pageId);
    if (target) target.classList.add('active');
  }
}

function closeDocs() {
  document.getElementById('docs-sidebar').classList.remove('open');
  document.getElementById('docs-backdrop').classList.remove('open');
  document.body.style.overflow = '';
}

// ── Support Sidebar ────────────────────────────────────────────────────────
function openSupport() {
  document.getElementById('support-sidebar').classList.add('open');
  document.getElementById('support-backdrop').classList.add('open');
  document.body.style.overflow = 'hidden';
}

function closeSupport() {
  document.getElementById('support-sidebar').classList.remove('open');
  document.getElementById('support-backdrop').classList.remove('open');
  document.body.style.overflow = '';
}

function switchDocsPage(btn) {
  const pageId = 'docs-' + btn.dataset.docsPage;
  document.querySelectorAll('.docs-nav-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.docs-page').forEach(p => p.classList.remove('active'));
  const target = document.getElementById(pageId);
  if (target) target.classList.add('active');
}

// Close docs or support on Escape key
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    closeDocs();
    closeSupport();
  }
});

// ── Billing Panel ─────────────────────────────────────────────────────────
async function loadSeats() {
  const loading = document.getElementById('billing-loading');
  const summary = document.getElementById('billing-summary');
  const notConfigured = document.getElementById('billing-not-configured');
  const errorDiv = document.getElementById('billing-error');

  if (loading) loading.style.display = '';
  if (summary) summary.style.display = 'none';
  if (notConfigured) notConfigured.style.display = 'none';
  if (errorDiv) errorDiv.style.display = 'none';

  try {
    const res = await fetch('/api/seats', { credentials: 'same-origin' });
    if (!res.ok) {
      throw new Error('Failed to load billing info');
    }
    const data = await res.json();

    if (loading) loading.style.display = 'none';

    const rawStatus = (data.subscription_status || '').toLowerCase();

    // If seats=0 and no Stripe customer at all, show "not configured"
    if (data.seats === 0 && !data.stripe_customer_id) {
      if (notConfigured) notConfigured.style.display = '';
      return;
    }

    if (summary) summary.style.display = '';
    document.getElementById('billing-seats').textContent = data.seats;
    document.getElementById('billing-staff-count').textContent = data.staff_count;
    document.getElementById('billing-available').textContent = data.available;
    document.getElementById('billing-monthly').textContent = data.monthly_total_display;

    // Next billing date
    if (data.current_period_end) {
      const d = new Date(data.current_period_end);
      document.getElementById('billing-next-date').textContent = d.toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' });
    } else {
      document.getElementById('billing-next-date').textContent = '—';
    }

    // Trial badge
    const trialBadge = document.getElementById('billing-trial-badge');
    if (data.is_trial && data.trial_end) {
      const d = new Date(data.trial_end);
      document.getElementById('billing-trial-end').textContent = d.toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' });
      if (trialBadge) trialBadge.style.display = 'flex';
    } else {
      if (trialBadge) trialBadge.style.display = 'none';
    }

    // Subscription status
    const subStatusEl = document.getElementById('billing-sub-status');
    const cancelBtn = document.getElementById('billing-cancel-btn');
    const reactivateBtn = document.getElementById('billing-reactivate-btn');
    const statusLabels = {
      'trialing': 'Trial',
      'active': 'Active',
      'past_due': 'Past Due',
      'canceled': 'Canceled',
      'incomplete': 'Incomplete',
      'incomplete_expired': 'Expired',
      'unpaid': 'Unpaid',
    };
    if (subStatusEl) subStatusEl.textContent = statusLabels[rawStatus] || rawStatus.charAt(0).toUpperCase() + rawStatus.slice(1);
    // Show sales agent info instead of cancel/reactivate buttons
    var agentName = data.sales_agent_name;
    var agentEmail = data.sales_agent_email;
    var agentDiv = document.getElementById('billing-sales-agent');
    var noAgentDiv = document.getElementById('billing-no-agent');
    if (agentName) {
      document.getElementById('billing-agent-name').textContent = agentName;
      document.getElementById('billing-agent-email').textContent = agentEmail || '';
      if (agentDiv) agentDiv.style.display = 'block';
      if (noAgentDiv) noAgentDiv.style.display = 'none';
    } else {
      if (agentDiv) agentDiv.style.display = 'none';
      if (noAgentDiv) noAgentDiv.style.display = 'block';
    }
    // Self-serve cancel/reactivate disabled — hide old buttons
    // if (cancelBtn) {
    //   cancelBtn.style.display = (rawStatus === 'active' || rawStatus === 'trialing') ? '' : 'none';
    // }
    // if (reactivateBtn) {
    //   reactivateBtn.style.display = (rawStatus === 'canceled' && data.stripe_customer_id) ? '' : 'none';
    // }

    // Update remove input max to available removable (seats - staff_count)
    const removeInput = document.getElementById('billing-remove-qty');
    if (removeInput) {
      const maxRemovable = data.seats - data.staff_count;
      removeInput.max = Math.max(0, maxRemovable);
    }
  } catch (err) {
    if (loading) loading.style.display = 'none';
    if (errorDiv) {
      errorDiv.textContent = err.message;
      errorDiv.style.display = 'block';
    }
  }
}

async function addSeats() {
  const qty = parseInt(document.getElementById('billing-add-qty').value) || 1;
  const errorDiv = document.getElementById('billing-error');
  if (errorDiv) errorDiv.style.display = 'none';

  if (qty < 1) {
    if (errorDiv) { errorDiv.textContent = 'Enter at least 1 staff member.'; errorDiv.style.display = 'block'; }
    return;
  }

  try {
    const res = await fetch('/api/seats/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ seats: qty })
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.message || data.error || 'Failed to add staff members');
    }
    toast('success', data.message || `Added ${qty} staff member(s).`);
    document.getElementById('billing-add-qty').value = 1;
    await loadSeats();
    await loadStaff();
  } catch (err) {
    if (errorDiv) { errorDiv.textContent = err.message; errorDiv.style.display = 'block'; }
    toast('error', err.message);
  }
}

async function removeSeats() {
  const qty = parseInt(document.getElementById('billing-remove-qty').value) || 1;
  const errorDiv = document.getElementById('billing-error');
  if (errorDiv) errorDiv.style.display = 'none';

  if (qty < 1) {
    if (errorDiv) { errorDiv.textContent = 'Enter at least 1 staff member.'; errorDiv.style.display = 'block'; }
    return;
  }

  try {
    const res = await fetch('/api/seats/remove', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ seats: qty })
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.message || data.error || 'Failed to remove staff members');
    }
    toast('success', data.message || `Removed ${qty} staff member(s).`);
    document.getElementById('billing-remove-qty').value = 1;
    await loadSeats();
  } catch (err) {
    if (errorDiv) { errorDiv.textContent = err.message; errorDiv.style.display = 'block'; }
    toast('error', err.message);
  }
}

// Self-serve cancel/reactivate disabled — customers contact sales agent.
// async function cancelSubscription() {
//   openModal('modal-cancel-sub');
// }
//
// async function doCancelSubscription() {
//   const errorDiv = document.getElementById('billing-error');
//   if (errorDiv) errorDiv.style.display = 'none';
//   const cancelBtn = document.getElementById('do-cancel-sub-btn');
//   if (cancelBtn) cancelBtn.disabled = true;
//
//   try {
//     const res = await fetch('/api/seats/cancel', {
//       method: 'POST',
//       credentials: 'same-origin'
//     });
//     const data = await res.json();
//     if (!res.ok) {
//       throw new Error(data.message || data.error || 'Failed to cancel subscription');
//     }
//     closeModal('modal-cancel-sub');
//     toast('success', data.message || 'Subscription canceled.');
//     await loadSeats();
//   } catch (err) {
//     if (errorDiv) { errorDiv.textContent = err.message; errorDiv.style.display = 'block'; }
//     toast('error', err.message);
//   } finally {
//     if (cancelBtn) cancelBtn.disabled = false;
//   }
// }
//
// async function reactivateSubscription() {
//   const errorDiv = document.getElementById('billing-error');
//   if (errorDiv) errorDiv.style.display = 'none';
//   const reactivateBtn = document.getElementById('billing-reactivate-btn');
//   if (reactivateBtn) reactivateBtn.disabled = true;
//
//   try {
//     const res = await fetch('/api/seats/reactivate', {
//       method: 'POST',
//       credentials: 'same-origin'
//     });
//     const data = await res.json();
//     if (!res.ok) {
//       throw new Error(data.message || data.error || 'Failed to reactivate subscription');
//     }
//     toast('success', data.message || 'Subscription reactivated.');
//     await loadSeats();
//   } catch (err) {
//     if (errorDiv) { errorDiv.textContent = err.message; errorDiv.style.display = 'block'; }
//     toast('error', err.message);
//   } finally {
//     if (reactivateBtn) reactivateBtn.disabled = false;
//   }
// }
