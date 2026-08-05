/* ═══════════════════════════════════════════════════════════
   StreamScout Command Center — Client Application
   3-Tier Verification Dashboard
   ═══════════════════════════════════════════════════════════ */

const API = '';  // same origin

// ── State ────────────────────────────────────────────────
const state = {
    module: 'scanner',
    scanner: {
        page: 1,
        pageSize: 25,
        sortBy: 'avg_viewers',
        sortOrder: 'desc',
        filter: 'leads',  // 'leads' | 'active' | 'clippers' | 'all'
    },
    gig: { page: 1, pageSize: 25, platform: null, timeframe: 'month', sortOrder: 'desc' },
    logCount: 0,
};

// ── DOM refs ─────────────────────────────────────────────
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

// ── Init ─────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    initNavigation();
    initSidebarToggle();
    initScannerControls();
    initGigControls();
    initLogToggle();
    initIslandClose();
    loadApiStatus();
    loadStreamers();
});

// ═══════════════════════════════════════════════════════════
//  Navigation
// ═══════════════════════════════════════════════════════════

function initNavigation() {
    $$('.nav-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const mod = btn.dataset.module;
            if (mod === state.module) return;
            state.module = mod;
            $$('.nav-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            $$('.module').forEach(m => m.classList.remove('active'));
            $(`#module-${mod}`).classList.add('active');
            if (mod === 'scanner') loadStreamers();
            if (mod === 'gighunt') loadGigs();
            if (mod === 'settings') loadApiStatus();
        });
    });
}

function initSidebarToggle() {
    $('#sidebarToggle').addEventListener('click', () => {
        document.body.classList.toggle('sidebar-collapsed');
    });
}

// ═══════════════════════════════════════════════════════════
//  Streamer Scanner — 3-Tier Verification UI
// ═══════════════════════════════════════════════════════════

function initScannerControls() {
    // Filter chips
    $$('.chip').forEach(chip => {
        chip.addEventListener('click', () => {
            $$('.chip').forEach(c => c.classList.remove('active'));
            chip.classList.add('active');
            state.scanner.filter = chip.dataset.filter;
            state.scanner.page = 1;
            loadStreamers();
        });
    });

    $('#sortSelect').addEventListener('change', (e) => {
        state.scanner.sortBy = e.target.value;
        state.scanner.page = 1;
        loadStreamers();
    });

    $('#startScanBtn').addEventListener('click', startScan);
}

async function loadStreamers() {
    const s = state.scanner;
    const list = $('#streamerList');
    list.innerHTML = Array(5).fill('<div class="skeleton skeleton-row"></div>').join('');

    const params = new URLSearchParams({
        page: s.page,
        page_size: s.pageSize,
        sort_by: s.sortBy,
        sort_order: s.sortOrder,
    });

    // Apply filter based on selected chip
    switch (s.filter) {
        case 'leads':
            params.set('youtube_status', 'not_found');
            params.set('has_clippers', 'false');
            break;
        case 'active':
            params.set('youtube_status', 'active');
            break;
        case 'clippers':
            params.set('has_clippers', 'true');
            break;
        case 'all':
            // no filter
            break;
    }

    try {
        const res = await fetch(`${API}/api/streamers?${params}`);
        const data = await res.json();

        if (!data.items || data.items.length === 0) {
            const msgs = {
                leads: 'No verified leads yet. Run a scan to discover streamers with zero YouTube presence.',
                active: 'No streamers with active YouTube channels found.',
                clippers: 'No streamers with clipper channels found.',
                all: 'No streamers found. Click "Start Scan" to discover streamers.',
            };
            list.innerHTML = renderEmpty(
                s.filter === 'leads' ? '🎯' : '📡',
                s.filter === 'leads' ? 'No Verified Leads' : 'No Results',
                msgs[s.filter]
            );
            $('#streamerPagination').innerHTML = '';
            return;
        }

        list.innerHTML = data.items.map(row => renderStreamerRow(row, s.filter)).join('');
        renderPagination('streamerPagination', data.page, data.total_pages, (p) => {
            state.scanner.page = p;
            loadStreamers();
        });

        // Update summary stats
        updateScanSummary();
    } catch (err) {
        list.innerHTML = renderEmpty('⚠️', 'Failed to load', err.message);
        addLog(`[ERROR] Failed to load streamers: ${err.message}`, 'error');
    }
}

async function updateScanSummary() {
    try {
        // Fetch total counts for summary bar
        const [allRes, leadsRes, activeRes] = await Promise.all([
            fetch(`${API}/api/streamers?page=1&page_size=1`),
            fetch(`${API}/api/streamers?page=1&page_size=1&youtube_status=not_found&has_clippers=false`),
            fetch(`${API}/api/streamers?page=1&page_size=1&youtube_status=active`),
        ]);
        const allData = await allRes.json();
        const leadsData = await leadsRes.json();
        const activeData = await activeRes.json();

        const total = allData.total || 0;
        const leads = leadsData.total || 0;
        const rejected = activeData.total || 0;

        if (total > 0) {
            $('#sumTotal').textContent = total;
            $('#sumLeads').textContent = leads;
            $('#sumRejected').textContent = rejected;
            $('#scanSummary').classList.remove('hidden');
        }
    } catch (err) {
        // Non-critical — just don't show summary
    }
}

function renderStreamerRow(s, filter) {
    const viewers = formatNum(s.avg_viewers);
    const followers = formatNum(s.follower_count);
    const twitchUrl = `https://twitch.tv/${escHtml(s.login)}`;
    const isLead = s.youtube_status === 'not_found' && !s.has_clippers;

    const avatar = s.profile_image_url
        ? `<img class="streamer-avatar" src="${escHtml(s.profile_image_url)}" alt="" loading="lazy">`
        : `<div class="streamer-avatar placeholder">👤</div>`;

    // Verification Status — replaces old badges
    let verificationHtml = '';
    if (isLead) {
        verificationHtml = `
            <div class="verification-status verified">
                <span class="verify-icon">✅</span>
                <div class="verify-details">
                    <span class="verify-label">Verified Lead</span>
                    <span class="verify-sub">No YouTube presence found</span>
                </div>
            </div>`;
    } else if (s.youtube_status === 'active') {
        verificationHtml = `
            <div class="verification-status rejected">
                <span class="verify-icon">❌</span>
                <div class="verify-details">
                    <span class="verify-label">Active YouTube</span>
                    <span class="verify-sub">Has active YouTube channel(s)</span>
                </div>
            </div>`;
    } else if (s.has_clippers) {
        verificationHtml = `
            <div class="verification-status clipped">
                <span class="verify-icon">⚠️</span>
                <div class="verify-details">
                    <span class="verify-label">Being Clipped</span>
                    <span class="verify-sub">YouTube highlight channels found</span>
                </div>
            </div>`;
    } else if (s.youtube_status === 'dormant') {
        verificationHtml = `
            <div class="verification-status dormant">
                <span class="verify-icon">💤</span>
                <div class="verify-details">
                    <span class="verify-label">Dormant Channel</span>
                    <span class="verify-sub">YouTube channel inactive</span>
                </div>
            </div>`;
    } else {
        verificationHtml = `
            <div class="verification-status review">
                <span class="verify-icon">🔍</span>
                <div class="verify-details">
                    <span class="verify-label">Manual Review</span>
                    <span class="verify-sub">Needs human verification</span>
                </div>
            </div>`;
    }

    // Tier check indicators (small dots)
    const t1Class = s.youtube_status === 'active' ? 'fail' : 'pass';
    const t2Class = s.youtube_status === 'active' ? 'fail' : 'pass';
    const t3Class = s.has_clippers ? 'fail' : 'pass';
    const tierDots = `
        <div class="tier-dots" title="T1: YT Link | T2: Name Search | T3: Clips">
            <span class="tier-dot ${t1Class}" title="Tier 1: Twitch YT Link"></span>
            <span class="tier-dot ${t2Class}" title="Tier 2: YouTube Search"></span>
            <span class="tier-dot ${t3Class}" title="Tier 3: Clipper Check"></span>
        </div>`;

    return `
    <div class="streamer-row ${isLead ? 'is-lead' : ''}" data-id="${s.id}">
      ${avatar}
      <div class="streamer-identity">
        <div class="streamer-name">${escHtml(s.display_name)}</div>
        <a class="streamer-link" href="${twitchUrl}" target="_blank" rel="noopener">
            <span class="twitch-icon">📺</span> twitch.tv/${escHtml(s.login)}
        </a>
        ${s.game_name ? `<div class="streamer-game">🎮 ${escHtml(s.game_name)}</div>` : ''}
      </div>
      <div class="metric">
        <div class="metric-value viewers">${viewers}</div>
        <div class="metric-label">Viewers</div>
      </div>
      <div class="metric">
        <div class="metric-value followers">${followers}</div>
        <div class="metric-label">Followers</div>
      </div>
      <div class="verification-col">
        ${verificationHtml}
        ${tierDots}
      </div>
      <div class="action-col">
        <a class="action-btn twitch-btn" href="${twitchUrl}" target="_blank" rel="noopener" title="Open Twitch">
            📺 Twitch
        </a>
        ${isLead ? `<button class="action-btn save-btn" onclick="saveLead(${s.id})" title="Save as prospect">⭐ Save</button>` : ''}
      </div>
    </div>`;
}

async function saveLead(streamerId) {
    try {
        const res = await fetch(`${API}/api/leads?streamer_id=${streamerId}`, { method: 'POST' });
        if (!res.ok) throw new Error(await res.text());
        addLog(`[SUCCESS] Streamer #${streamerId} saved as lead`, 'success');
        const row = document.querySelector(`.streamer-row[data-id="${streamerId}"]`);
        if (row) {
            row.classList.add('saved');
            const saveBtn = row.querySelector('.save-btn');
            if (saveBtn) {
                saveBtn.textContent = '✅ Saved';
                saveBtn.disabled = true;
            }
        }
    } catch (err) {
        addLog(`[ERROR] Save lead failed: ${err.message}`, 'error');
    }
}

// ═══════════════════════════════════════════════════════════
//  Gig Finder
// ═══════════════════════════════════════════════════════════

function initGigControls() {
    $$('.platform-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            btn.classList.toggle('active');
            state.gig.page = 1;
            loadGigs();
        });
    });

    $$('.tf-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            $$('.tf-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            state.gig.timeframe = btn.dataset.tf;
            state.gig.page = 1;
        });
    });

    $('#searchGigsBtn').addEventListener('click', startGigSearch);
}

async function loadGigs() {
    const g = state.gig;
    const feed = $('#gigFeed');
    feed.innerHTML = Array(3).fill('<div class="skeleton skeleton-row"></div>').join('');

    const activePlatforms = [...$$('.platform-btn.active')].map(b => b.dataset.platform);
    const params = new URLSearchParams({
        page: g.page,
        page_size: g.pageSize,
        sort_order: g.sortOrder,
    });
    if (activePlatforms.length === 1) {
        params.set('platform', activePlatforms[0]);
    }

    try {
        const res = await fetch(`${API}/api/gigs?${params}`);
        const data = await res.json();

        if (!data.items || data.items.length === 0) {
            feed.innerHTML = renderEmpty('💼', 'No gigs found',
                'Click "Search Gigs" to scan Twitter & Reddit for hiring posts.');
            $('#gigPagination').innerHTML = '';
            return;
        }

        feed.innerHTML = data.items.map(renderGigCard).join('');
        renderPagination('gigPagination', data.page, data.total_pages, (p) => {
            state.gig.page = p;
            loadGigs();
        });
    } catch (err) {
        feed.innerHTML = renderEmpty('⚠️', 'Failed to load', err.message);
        addLog(`[ERROR] Failed to load gigs: ${err.message}`, 'error');
    }
}

function renderGigCard(g) {
    const freshness = getFreshnessLabel(g.posted_at);
    const platformLabel = g.platform === 'twitter' ? '✖️ Twitter' : '👽 Reddit';
    const highlighted = highlightKeywords(escHtml(g.text));
    const timeAgo = relativeTime(g.posted_at);

    return `
    <div class="gig-card">
      <div class="gig-header">
        ${freshness}
        <span class="platform-badge">${platformLabel}</span>
        <span class="gig-meta">Posted ${timeAgo}</span>
      </div>
      <div class="gig-author">@${escHtml(g.author)}</div>
      <div class="gig-text">${highlighted}</div>
      <div class="gig-footer">
        <div class="gig-stats">
          <span><span class="gig-stat-icon">❤️</span>${g.likes}</span>
          <span><span class="gig-stat-icon">💬</span>${g.replies}</span>
        </div>
        <div class="gig-actions">
          <a class="btn btn-ghost" href="${escHtml(g.url)}" target="_blank" rel="noopener">Go to Post ↗</a>
        </div>
      </div>
    </div>`;
}

function getFreshnessLabel(dateStr) {
    const diff = Date.now() - new Date(dateStr).getTime();
    const hours = diff / (1000 * 60 * 60);
    if (hours < 24) return '<span class="freshness-badge new">🔥 NEW</span>';
    if (hours < 168) return '<span class="freshness-badge recent">Recent</span>';
    return '<span class="freshness-badge old">Older</span>';
}

const GIG_KEYWORDS = /\b(hiring|editor|paid|budget|looking for|need an? editor|video editor|content creator|freelance|commission|gig)\b/gi;
function highlightKeywords(text) {
    return text.replace(GIG_KEYWORDS, '<span class="kw">$&</span>');
}

// ═══════════════════════════════════════════════════════════
//  SSE Scans — Activity Island
// ═══════════════════════════════════════════════════════════

function showIsland() {
    const el = $('#activityIsland');
    el.classList.remove('hidden', 'complete', 'error');
    $('#islandBar').style.width = '0%';
    $('#islandText').textContent = 'Initializing…';
}
function hideIsland() {
    $('#activityIsland').classList.add('hidden');
}
function initIslandClose() {
    $('#islandClose').addEventListener('click', hideIsland);
}

function startScan() {
    showIsland();
    addLog('[INFO] 3-Tier scan started — checking for zero YouTube presence…', 'info');
    const btn = $('#startScanBtn');
    btn.disabled = true;

    const es = new EventSource(`${API}/api/scan/stream?max_pages=10`);

    es.onopen = () => {
        addLog('[INFO] Connection established to scan engine.', 'info');
    };

    es.addEventListener('message', (e) => {
        const d = JSON.parse(e.data);
        if (d.type === 'status') {
            $('#islandBar').style.width = (d.percent || 0) + '%';
            $('#islandText').textContent = d.message;
            addLog(`[INFO] ${d.message}`, 'info');
        } else if (d.type === 'log') {
            addLog(d.message, d.message.includes('WARN') ? 'warn' : 'info');
        } else if (d.type === 'complete') {
            $('#islandBar').style.width = '100%';
            $('#islandText').textContent = d.message;
            $('#activityIsland').classList.add('complete');
            addLog(`[SUCCESS] ${d.message}`, 'success');
            es.close();
            btn.disabled = false;
            loadStreamers();
        } else if (d.type === 'error') {
            $('#islandBar').style.width = '100%';
            $('#islandText').textContent = d.message;
            $('#activityIsland').classList.add('error');
            addLog(`[ERROR] ${d.message}`, 'error');
            es.close();
            btn.disabled = false;
        }
    });
    es.addEventListener('error', (e) => {
        if (es.readyState === EventSource.CLOSED) return;
        $('#islandText').textContent = 'Connection lost';
        $('#activityIsland').classList.add('error');
        addLog('[ERROR] SSE connection lost or failed to start', 'error');
        es.close();
        btn.disabled = false;
    });
}

function startGigSearch() {
    showIsland();
    addLog('[INFO] Gig search started…', 'info');
    const btn = $('#searchGigsBtn');
    btn.disabled = true;

    const activePlatforms = [...$$('.platform-btn.active')].map(b => b.dataset.platform);
    const params = new URLSearchParams({
        platforms: activePlatforms.join(','),
        timeframe: state.gig.timeframe,
    });

    const es = new EventSource(`${API}/api/gigs/search/stream?${params}`);
    es.addEventListener('message', (e) => {
        const d = JSON.parse(e.data);
        if (d.type === 'status') {
            $('#islandBar').style.width = (d.percent || 0) + '%';
            $('#islandText').textContent = d.message;
            addLog(`[INFO] ${d.message}`, 'info');
        } else if (d.type === 'log') {
            addLog(d.message, d.message.includes('WARN') ? 'warn' : 'info');
        } else if (d.type === 'complete') {
            $('#islandBar').style.width = '100%';
            $('#islandText').textContent = d.message;
            $('#activityIsland').classList.add('complete');
            addLog(`[SUCCESS] ${d.message}`, 'success');
            es.close();
            btn.disabled = false;
            loadGigs();
        } else if (d.type === 'error') {
            $('#islandBar').style.width = '100%';
            $('#islandText').textContent = d.message;
            $('#activityIsland').classList.add('error');
            addLog(`[ERROR] ${d.message}`, 'error');
            es.close();
            btn.disabled = false;
        }
    });
    es.addEventListener('error', () => {
        es.close();
        btn.disabled = false;
        $('#activityIsland').classList.add('error');
        $('#islandText').textContent = 'Connection lost';
        addLog('[ERROR] SSE connection lost', 'error');
    });
}

// ═══════════════════════════════════════════════════════════
//  System Log
// ═══════════════════════════════════════════════════════════

function initLogToggle() {
    $('#logBar').addEventListener('click', () => {
        $('#logPanel').classList.toggle('hidden');
    });
}

function addLog(message, type = 'info') {
    state.logCount++;
    $('#logCount').textContent = state.logCount;
    const entry = document.createElement('div');
    entry.className = `log-entry ${type}`;
    const ts = new Date().toLocaleTimeString();
    entry.textContent = `[${ts}] ${message}`;
    const entries = $('#logEntries');
    entries.appendChild(entry);
    entries.scrollTop = entries.scrollHeight;
}

// ═══════════════════════════════════════════════════════════
//  Settings / API Status
// ═══════════════════════════════════════════════════════════

async function loadApiStatus() {
    try {
        const res = await fetch(`${API}/api/status`);
        const data = await res.json();

        const grid = $('#settingsGrid');
        const descriptions = {
            twitch: 'Create an app at dev.twitch.tv/console/apps and add TWITCH_CLIENT_ID & TWITCH_CLIENT_SECRET to your .env file.',
            youtube: 'Enable "YouTube Data API v3" in Google Cloud Console and add YOUTUBE_API_KEY to .env.',
            twitter: 'Log in to X.com, extract auth_token & ct0 cookies from DevTools, add to .env.',
            reddit: 'Create a "script" app at reddit.com/prefs/apps. Add REDDIT_CLIENT_ID & REDDIT_CLIENT_SECRET to .env.',
        };

        grid.innerHTML = Object.entries(data).map(([key, info]) => `
      <div class="setting-card">
        <div class="setting-card-header">
          <span class="setting-card-title">${escHtml(info.label)}</span>
          <span class="setting-status ${info.configured ? 'configured' : 'missing'}">
            ${info.configured ? '✓ Ready' : '✗ Missing'}
          </span>
        </div>
        <div class="setting-card-desc">${descriptions[key] || ''}</div>
      </div>
    `).join('');

        // Update sidebar health dot
        const configured = Object.values(data).filter(v => v.configured).length;
        const total = Object.values(data).length;
        const dot = $('#healthDot');
        const txt = $('#healthText');
        if (configured === total) {
            dot.className = 'health-dot online';
            txt.textContent = 'All APIs Online';
        } else if (configured > 0) {
            dot.className = 'health-dot partial';
            txt.textContent = `${configured}/${total} APIs Ready`;
        } else {
            dot.className = 'health-dot offline';
            txt.textContent = 'No APIs Configured';
        }
    } catch (err) {
        $('#healthDot').className = 'health-dot offline';
        $('#healthText').textContent = 'Server error';
        addLog(`[ERROR] Status check failed: ${err.message}`, 'error');
    }
}

// ═══════════════════════════════════════════════════════════
//  Utilities
// ═══════════════════════════════════════════════════════════

function formatNum(n) {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'k';
    return String(n);
}

function escHtml(str) {
    if (!str) return '';
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}

function relativeTime(dateStr) {
    const diff = Date.now() - new Date(dateStr).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.floor(hrs / 24);
    if (days < 30) return `${days}d ago`;
    return `${Math.floor(days / 30)}mo ago`;
}

function renderEmpty(icon, title, desc) {
    return `
    <div class="empty-state">
      <div class="empty-icon">${icon}</div>
      <div class="empty-title">${escHtml(title)}</div>
      <div class="empty-desc">${escHtml(desc)}</div>
    </div>`;
}

function renderPagination(containerId, currentPage, totalPages, onPageChange) {
    const container = document.getElementById(containerId);
    if (totalPages <= 1) { container.innerHTML = ''; return; }

    let html = `<button class="page-btn" ${currentPage <= 1 ? 'disabled' : ''} data-page="${currentPage - 1}">‹ Prev</button>`;
    const start = Math.max(1, currentPage - 2);
    const end = Math.min(totalPages, currentPage + 2);
    for (let i = start; i <= end; i++) {
        html += `<button class="page-btn ${i === currentPage ? 'active' : ''}" data-page="${i}">${i}</button>`;
    }
    html += `<span class="page-info">${currentPage} / ${totalPages}</span>`;
    html += `<button class="page-btn" ${currentPage >= totalPages ? 'disabled' : ''} data-page="${currentPage + 1}">Next ›</button>`;

    container.innerHTML = html;
    container.querySelectorAll('.page-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const p = parseInt(btn.dataset.page);
            if (p >= 1 && p <= totalPages) onPageChange(p);
        });
    });
}
