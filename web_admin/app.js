// ── PolyServer Admin Dashboard ──────────────────────────────────────────────

let currentTab = 'overview';
let adminKey = localStorage.getItem('admin_key') || '';

document.addEventListener('DOMContentLoaded', () => {
    if (!adminKey) {
        showLogin();
    } else {
        switchTab('overview');
    }
});

function showLogin() {
    const main = document.querySelector('main');
    main.innerHTML = `
        <div style="max-width:360px;margin:80px auto;text-align:center">
            <div class="logo-icon" style="width:48px;height:48px;font-size:22px;margin:0 auto 24px">S</div>
            <h3 style="margin-bottom:24px;font-size:18px">PolyServer Admin</h3>
            <input id="login-key" type="password" class="setting-select"
                style="width:100%;padding:10px 14px;margin-bottom:12px;font-size:14px"
                placeholder="Admin API Key" onkeydown="if(event.key==='Enter')doLogin()">
            <button class="btn-primary" style="width:100%;padding:10px" onclick="doLogin()">Sign In</button>
            <p id="login-error" style="color:var(--red);margin-top:12px;font-size:13px;display:none"></p>
            <p style="color:var(--gray-8);margin-top:16px;font-size:12px">
                Key shown in server startup output
            </p>
        </div>`;
    document.getElementById('login-key').focus();
}

async function doLogin() {
    const key = document.getElementById('login-key').value.trim();
    try {
        const resp = await fetch('/api/auth/login', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({key}),
        });
        if (resp.ok) {
            adminKey = key;
            localStorage.setItem('admin_key', key);
            switchTab('overview');
        } else {
            const err = document.getElementById('login-error');
            err.textContent = 'Invalid admin key';
            err.style.display = 'block';
        }
    } catch (e) {
        const err = document.getElementById('login-error');
        err.textContent = 'Could not connect to server';
        err.style.display = 'block';
    }
}

function authHeaders() {
    return adminKey ? {'Authorization': `Bearer ${adminKey}`, 'Content-Type': 'application/json'} : {'Content-Type': 'application/json'};
}

function switchTab(tab) {
    currentTab = tab;
    document.querySelectorAll('nav button').forEach(b => {
        b.classList.toggle('active', b.dataset.tab === tab);
    });
    const main = document.querySelector('main');
    main.innerHTML = '<div class="loading"><div class="spinner"></div> Loading...</div>';

    const loaders = {
        overview: loadOverview,
        devices: loadDevices,
        compliance: loadCompliance,
        policies: loadPolicies,
        commands: loadCommands,
        audit: loadAudit,
    };
    (loaders[tab] || loadOverview)();
}

function esc(s) {
    if (s == null) return '';
    const div = document.createElement('div');
    div.textContent = String(s);
    return div.innerHTML;
}

// ── Overview ────────────────────────────────────────────────────────────────

async function loadOverview() {
    const main = document.querySelector('main');
    try {
        const resp = await fetch('/api/stats');
        const stats = await resp.json();
        renderOverview(stats);
    } catch (e) {
        main.innerHTML = '<div class="empty"><h3>Could not load stats</h3><p>Is the server running?</p></div>';
    }
}

function renderOverview(stats) {
    const main = document.querySelector('main');
    let html = `
        <div class="section-header"><div class="section-title">Fleet Overview</div></div>
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-value">${stats.total_devices}</div>
                <div class="stat-label">Total Devices</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">${stats.total_agents}</div>
                <div class="stat-label">Agents</div>
            </div>
            <div class="stat-card">
                <div class="stat-value accent">${stats.online_agents}</div>
                <div class="stat-label">Online Now</div>
            </div>
        </div>`;

    if (stats.by_category && stats.by_category.length > 0) {
        html += `<div class="section-header" style="margin-top:32px"><div class="section-title">By Category</div></div>
            <div class="stats-grid">`;
        for (const c of stats.by_category) {
            html += `<div class="stat-card"><div class="stat-value">${c.count}</div>
                <div class="stat-label">${esc(c.category || 'Unknown')}</div></div>`;
        }
        html += '</div>';
    }

    if (stats.by_firmware && stats.by_firmware.length > 0) {
        html += `<div class="section-header" style="margin-top:32px"><div class="section-title">Firmware Versions</div></div>
            <table class="catalog-table"><thead><tr>
                <th>PID</th><th>Device</th><th>Firmware</th><th>Count</th>
            </tr></thead><tbody>`;
        for (const f of stats.by_firmware) {
            html += `<tr><td>${esc(f.pid_hex)}</td><td>${esc(f.friendly_name)}</td>
                <td style="color:var(--green)">${esc(f.firmware)}</td>
                <td>${f.count}</td></tr>`;
        }
        html += '</tbody></table>';
    }

    if (stats.recent_activity && stats.recent_activity.length > 0) {
        html += `<div class="section-header" style="margin-top:32px"><div class="section-title">Recent Activity</div></div>
            <div class="log-list">`;
        for (const a of stats.recent_activity.slice(0, 10)) {
            html += `<div class="log-entry">
                <span class="log-time">${esc(a.timestamp)}</span>
                <span class="log-action">${esc(a.action)}</span>
                <span class="log-detail">${esc(a.detail)}</span>
            </div>`;
        }
        html += '</div>';
    }

    main.innerHTML = html;
}

// ── Devices ─────────────────────────────────────────────────────────────────

async function loadDevices() {
    const main = document.querySelector('main');
    try {
        const resp = await fetch('/api/fleet');
        const data = await resp.json();
        renderFleetDevices(data.devices);
    } catch (e) {
        main.innerHTML = '<div class="empty"><h3>Failed to load devices</h3></div>';
    }
}

function renderFleetDevices(devices) {
    const main = document.querySelector('main');
    if (!devices || devices.length === 0) {
        main.innerHTML = '<div class="empty"><div class="empty-icon">&#x1F4E1;</div><h3>No devices reported</h3><p>Start an agent on a workstation to begin reporting devices</p></div>';
        return;
    }

    let html = `<div class="section-header"><div class="section-title">${devices.length} Devices</div></div>
        <table class="catalog-table"><thead><tr>
            <th>Status</th><th>Device</th><th>Firmware</th><th>Serial</th><th>Host</th><th>User</th><th>Last Seen</th>
        </tr></thead><tbody>`;

    for (const d of devices) {
        const status = d.online
            ? '<span class="dot online"></span>'
            : '<span class="dot offline"></span>';
        html += `<tr>
            <td>${status}</td>
            <td><strong>${esc(d.friendly_name || d.product_name)}</strong><br><span style="color:var(--gray-8);font-size:11px">${esc(d.pid_hex)}</span></td>
            <td style="font-family:'JetBrains Mono',monospace;font-size:12px">${esc(d.firmware)}</td>
            <td style="font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--gray-8)" title="${esc(d.serial)}">${esc((d.serial||'').substring(0,12))}</td>
            <td>${esc(d.hostname)}</td>
            <td>${esc(d.username)}</td>
            <td style="color:var(--gray-8);font-size:11px">${esc(d.last_seen)}</td>
        </tr>`;
    }
    html += '</tbody></table>';
    main.innerHTML = html;
}

// ── Compliance ──────────────────────────────────────────────────────────────

async function loadCompliance() {
    const main = document.querySelector('main');
    try {
        const resp = await fetch('/api/fleet/compliance');
        const data = await resp.json();
        renderCompliance(data);
    } catch (e) {
        main.innerHTML = '<div class="empty"><h3>Failed to load compliance</h3></div>';
    }
}

function renderCompliance(data) {
    const main = document.querySelector('main');
    const pct = data.compliance_pct || 100;
    const color = pct >= 90 ? 'var(--green)' : pct >= 70 ? 'var(--yellow)' : 'var(--red)';

    let html = `
        <div class="section-header"><div class="section-title">Compliance</div></div>
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-value" style="color:${color}">${pct}%</div>
                <div class="stat-label">Compliant</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">${data.compliant}</div>
                <div class="stat-label">In Policy</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" style="color:var(--red)">${data.non_compliant}</div>
                <div class="stat-label">Violations</div>
            </div>
        </div>`;

    if (data.devices) {
        const violations = data.devices.filter(d => !d.compliant);
        if (violations.length > 0) {
            html += `<div class="section-header" style="margin-top:32px"><div class="section-title">Violations</div></div>
                <div class="update-list">`;
            for (const d of violations) {
                html += `<div class="update-card available">
                    <div class="update-header">
                        <div class="device-name">${esc(d.friendly_name || d.product_name)}</div>
                        <div class="update-status available">${d.violations.length} violation(s)</div>
                    </div>`;
                for (const v of d.violations) {
                    html += `<div class="version-info">${esc(v.policy)}: expected <strong>${esc(v.expected)}</strong>, got <strong>${esc(v.actual)}</strong></div>`;
                }
                html += '</div>';
            }
            html += '</div>';
        }
    }

    main.innerHTML = html;
}

// ── Policies ────────────────────────────────────────────────────────────────

async function loadPolicies() {
    const main = document.querySelector('main');
    try {
        const resp = await fetch('/api/policies');
        const data = await resp.json();
        renderPolicies(data.policies);
    } catch (e) {
        main.innerHTML = '<div class="empty"><h3>Failed to load policies</h3></div>';
    }
}

function renderPolicies(policies) {
    const main = document.querySelector('main');
    let html = `
        <div class="section-header">
            <div class="section-title">Policies</div>
            <button class="btn-primary" onclick="showAddPolicy()">Add Policy</button>
        </div>
        <div id="policy-form" style="display:none"></div>`;

    if (!policies || policies.length === 0) {
        html += '<div class="empty"><h3>No policies defined</h3><p>Create a policy to enforce firmware versions or settings across your fleet</p></div>';
    } else {
        html += '<div class="update-list">';
        for (const p of policies) {
            html += `<div class="update-card">
                <div class="update-header">
                    <div>
                        <div class="device-name">${esc(p.name)}</div>
                        <div class="version-info" style="margin-top:4px">${esc(p.description)}</div>
                    </div>
                    <div style="display:flex;gap:8px;align-items:center">
                        <span class="update-status ${p.enabled ? 'current' : ''}">${p.enabled ? 'Active' : 'Disabled'}</span>
                        <button onclick="deletePolicy(${p.id})" style="color:var(--red);border-color:var(--red)">Delete</button>
                    </div>
                </div>
                <div style="margin-top:8px;font-size:12px;color:var(--gray-8)">
                    Type: <strong>${esc(p.policy_type)}</strong> |
                    Target: PID=${esc(p.target_pid)} Category=${esc(p.target_category)} |
                    Value: <strong style="color:var(--gray-11)">${esc(p.policy_value)}</strong>
                </div>
            </div>`;
        }
        html += '</div>';
    }
    main.innerHTML = html;
}

function showAddPolicy() {
    const form = document.getElementById('policy-form');
    form.style.display = 'block';
    form.innerHTML = `
        <div class="update-card" style="margin-bottom:16px">
            <div class="device-name" style="margin-bottom:16px">New Policy</div>
            <div class="settings-grid">
                <div class="setting-row"><label class="setting-label">Name</label>
                    <input id="pol-name" class="setting-select" style="width:200px" placeholder="e.g. BW3220 Firmware"></div>
                <div class="setting-row"><label class="setting-label">Description</label>
                    <input id="pol-desc" class="setting-select" style="width:200px" placeholder="Optional"></div>
                <div class="setting-row"><label class="setting-label">Type</label>
                    <select id="pol-type" class="setting-select">
                        <option value="firmware_version">Firmware Version</option>
                        <option value="setting">Setting Value</option>
                    </select></div>
                <div class="setting-row"><label class="setting-label">Target PID</label>
                    <input id="pol-pid" class="setting-select" style="width:200px" value="*" placeholder="* for all"></div>
                <div class="setting-row"><label class="setting-label">Value</label>
                    <input id="pol-value" class="setting-select" style="width:200px" placeholder="e.g. 2.25"></div>
            </div>
            <div style="margin-top:16px;display:flex;gap:8px">
                <button class="btn-primary" onclick="submitPolicy()">Create</button>
                <button onclick="document.getElementById('policy-form').style.display='none'">Cancel</button>
            </div>
        </div>`;
}

async function submitPolicy() {
    const data = {
        name: document.getElementById('pol-name').value,
        description: document.getElementById('pol-desc').value,
        policy_type: document.getElementById('pol-type').value,
        target_pid: document.getElementById('pol-pid').value || '*',
        policy_value: document.getElementById('pol-value').value,
    };
    await fetch('/api/policies', {method:'POST', headers:authHeaders(), body:JSON.stringify(data)});
    loadPolicies();
}

async function deletePolicy(id) {
    if (!confirm('Delete this policy?')) return;
    await fetch(`/api/policies/${id}`, {method:'DELETE', headers:authHeaders()});
    loadPolicies();
}

// ── Commands ────────────────────────────────────────────────────────────────

async function loadCommands() {
    const main = document.querySelector('main');
    let html = `
        <div class="section-header">
            <div class="section-title">Push Command</div>
        </div>
        <div class="update-card" style="margin-bottom:24px">
            <div class="settings-grid">
                <div class="setting-row"><label class="setting-label">Command</label>
                    <select id="cmd-type" class="setting-select">
                        <option value="set_setting">Set Setting</option>
                        <option value="apply_preset">Apply Preset</option>
                        <option value="report">Force Report</option>
                    </select></div>
                <div class="setting-row"><label class="setting-label">Target PID</label>
                    <input id="cmd-pid" class="setting-select" style="width:200px" value="*" placeholder="* for all"></div>
                <div class="setting-row"><label class="setting-label">Setting Name</label>
                    <input id="cmd-setting" class="setting-select" style="width:200px" placeholder="e.g. Sidetone Level"></div>
                <div class="setting-row"><label class="setting-label">Value</label>
                    <input id="cmd-value" class="setting-select" style="width:200px" placeholder="e.g. 5"></div>
            </div>
            <div style="margin-top:16px">
                <button class="btn-primary" onclick="pushCommand()">Push to All Agents</button>
            </div>
        </div>
        <div id="cmd-result"></div>`;
    main.innerHTML = html;
}

async function pushCommand() {
    const cmdType = document.getElementById('cmd-type').value;
    const data = {
        command_type: cmdType,
        device_pid: document.getElementById('cmd-pid').value || '*',
        agent_id: '*',
    };

    if (cmdType === 'set_setting') {
        data.command_data = {
            name: document.getElementById('cmd-setting').value,
            value: document.getElementById('cmd-value').value,
        };
        // Auto-convert numeric values
        const num = Number(data.command_data.value);
        if (!isNaN(num) && data.command_data.value.trim() !== '') {
            data.command_data.value = num;
        }
        if (data.command_data.value === 'true') data.command_data.value = true;
        if (data.command_data.value === 'false') data.command_data.value = false;
    }

    const resp = await fetch('/api/fleet/command', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify(data),
    });
    const result = await resp.json();
    document.getElementById('cmd-result').innerHTML = `
        <div class="update-card up-to-date">
            <div class="progress-text success">Command pushed to ${result.agents} agent(s)</div>
        </div>`;
}

// ── Audit Log ───────────────────────────────────────────────────────────────

async function loadAudit() {
    const main = document.querySelector('main');
    try {
        const resp = await fetch('/api/audit?limit=50');
        const data = await resp.json();
        renderAudit(data.log);
    } catch (e) {
        main.innerHTML = '<div class="empty"><h3>Failed to load audit log</h3></div>';
    }
}

function renderAudit(logs) {
    const main = document.querySelector('main');
    let html = `<div class="section-header"><div class="section-title">Audit Log</div></div>`;

    if (!logs || logs.length === 0) {
        html += '<div class="empty"><h3>No activity yet</h3></div>';
    } else {
        html += '<div class="log-list">';
        for (const l of logs) {
            html += `<div class="log-entry">
                <span class="log-time">${esc(l.timestamp)}</span>
                <span class="log-agent">${esc(l.agent_id || '—')}</span>
                <span class="log-action">${esc(l.action)}</span>
                <span class="log-detail">${esc(l.detail)}</span>
            </div>`;
        }
        html += '</div>';
    }
    main.innerHTML = html;
}
