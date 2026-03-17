// ── PolyLens Web Dashboard ──────────────────────────────────────────────────

let currentTab = 'devices';
let pollTimer = null;

// ── Init ────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    switchTab('devices');
    startPolling();
});

// ── Navigation ──────────────────────────────────────────────────────────────

function switchTab(tab) {
    currentTab = tab;

    // Update nav buttons
    document.querySelectorAll('nav button').forEach(b => {
        b.classList.toggle('active', b.dataset.tab === tab);
    });

    // Show content
    const main = document.querySelector('main');
    if (tab === 'devices') {
        main.innerHTML = '<div class="loading"><div class="spinner"></div> Scanning devices...</div>';
        loadDevices();
    } else if (tab === 'updates') {
        main.innerHTML = '<div class="loading"><div class="spinner"></div> Checking for updates...</div>';
        loadUpdates();
    } else if (tab === 'settings') {
        main.innerHTML = '<div class="loading"><div class="spinner"></div> Loading settings...</div>';
        loadSettings();
    } else if (tab === 'catalog') {
        showCatalog();
    }
}

// ── Polling ─────────────────────────────────────────────────────────────────

function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(() => {
        if (currentTab === 'devices') loadDevices();
    }, 5000);
}

// ── Devices ─────────────────────────────────────────────────────────────────

async function loadDevices() {
    try {
        const resp = await fetch('/api/devices');
        const data = await resp.json();
        renderDevices(data.devices);
    } catch (e) {
        console.error('Failed to load devices:', e);
    }
}

async function refreshDevices() {
    const main = document.querySelector('main');
    main.innerHTML = '<div class="loading"><div class="spinner"></div> Scanning...</div>';
    try {
        const resp = await fetch('/api/devices/refresh', { method: 'POST' });
        const data = await resp.json();
        renderDevices(data.devices);
        toast('Devices refreshed');
    } catch (e) {
        console.error('Refresh failed:', e);
    }
}

function renderDevices(devices) {
    const main = document.querySelector('main');

    if (!devices || devices.length === 0) {
        main.innerHTML = `
            <div class="empty">
                <div class="empty-icon">&#x1F3A7;</div>
                <h3>No devices found</h3>
                <p>Connect a Poly or Plantronics headset via USB to get started</p>
                <br>
                <button onclick="refreshDevices()">Scan Again</button>
            </div>`;
        return;
    }

    let html = `
        <div class="section-header">
            <div class="section-title">${devices.length} Device${devices.length > 1 ? 's' : ''} Connected</div>
            <button onclick="refreshDevices()">Refresh</button>
        </div>
        <div class="device-grid">`;

    for (const dev of devices) {
        html += renderDeviceCard(dev);
    }

    html += '</div>';
    main.innerHTML = html;
}

function renderDeviceCard(dev) {
    const batteryHtml = renderBattery(dev);
    const catColor = {
        'headset': '#4f8ff7',
        'speakerphone': '#34d399',
        'camera': '#fb923c',
        'adapter': '#a78bfa',
    }[dev.category] || '#7a7f94';

    return `
        <div class="device-card">
            <div class="device-header">
                <div class="device-name">${esc(dev.name)}</div>
                <div class="device-category" style="color:${catColor}; background:${catColor}15">
                    ${esc(dev.category)}
                </div>
            </div>
            <div class="device-props">
                <div class="prop">
                    <div class="prop-label">Firmware</div>
                    <div class="prop-value">${esc(dev.firmware)}</div>
                </div>
                <div class="prop">
                    <div class="prop-label">Battery</div>
                    <div class="prop-value">${batteryHtml}</div>
                </div>
                <div class="prop">
                    <div class="prop-label">Serial</div>
                    <div class="prop-value" title="${esc(dev.serial)}">${esc(dev.serial)}</div>
                </div>
                <div class="prop">
                    <div class="prop-label">VID:PID</div>
                    <div class="prop-value">${esc(dev.vid_pid)}</div>
                </div>
                <div class="prop">
                    <div class="prop-label">Connection</div>
                    <div class="prop-value">${esc(dev.bus_type)}</div>
                </div>
                <div class="prop">
                    <div class="prop-label">DFU</div>
                    <div class="prop-value">${esc(dev.dfu_executor)}</div>
                </div>
            </div>
        </div>`;
}

function renderBattery(dev) {
    if (dev.battery_level < 0) {
        return '<span style="color:var(--text-dim)">—</span>';
    }

    const pct = dev.battery_level;
    const cls = pct > 50 ? 'high' : pct > 20 ? 'mid' : 'low';
    const charging = dev.battery_charging ? ' <span class="charging">&#x26A1;</span>' : '';

    return `
        <div class="battery">
            <div class="battery-bar">
                <div class="battery-fill ${cls}" style="width:${pct}%"></div>
            </div>
            <span class="battery-text">${pct}%${charging}</span>
        </div>`;
}

// ── Updates ─────────────────────────────────────────────────────────────────

let updatePollers = {};  // device_id → interval

async function loadUpdates() {
    const main = document.querySelector('main');
    try {
        const resp = await fetch('/api/updates');
        const data = await resp.json();
        renderUpdates(data.updates);
    } catch (e) {
        main.innerHTML = '<div class="empty"><h3>Failed to check updates</h3><p>Check your internet connection</p></div>';
    }
}

function renderUpdates(updates) {
    const main = document.querySelector('main');

    // Clear any stale pollers
    Object.values(updatePollers).forEach(id => clearInterval(id));
    updatePollers = {};

    if (!updates || updates.length === 0) {
        main.innerHTML = `
            <div class="empty">
                <div class="empty-icon">&#x1F3A7;</div>
                <h3>No devices connected</h3>
                <p>Connect a device to check for firmware updates</p>
            </div>`;
        return;
    }

    let html = `
        <div class="section-header">
            <div class="section-title">Firmware Updates</div>
            <button onclick="loadUpdates()">Recheck</button>
        </div>
        <div class="update-list">`;

    for (const u of updates) {
        const dev = u.device;
        const isAvailable = u.update_available;
        const cardClass = isAvailable ? 'available' : 'up-to-date';
        const statusClass = isAvailable ? 'available' : 'current';
        const statusText = isAvailable ? 'Update Available' : 'Up to Date';

        html += `
            <div class="update-card ${cardClass}" id="update-card-${esc(dev.id)}">
                <div class="update-header">
                    <div class="device-name">${esc(dev.name)}</div>
                    <div class="update-status ${statusClass}">${statusText}</div>
                </div>
                <div class="version-info">
                    Current: <strong>${esc(u.current || dev.firmware)}</strong>`;

        if (isAvailable) {
            html += ` &rarr; Latest: <strong>${esc(u.latest)}</strong>`;
        }

        html += '</div>';

        if (u.release_notes) {
            const notes = u.release_notes.substring(0, 500);
            html += `<div class="release-notes">${esc(notes)}</div>`;
        }

        // Action buttons
        html += `<div class="update-actions" style="margin-top:12px; display:flex; gap:8px; align-items:center">`;

        if (isAvailable && !u.blocked) {
            html += `
                <button class="btn btn-primary" onclick="startUpdate('${esc(dev.id)}', false)">
                    Update Now
                </button>`;
        }

        if (u.download_url) {
            html += `
                <a href="${esc(u.download_url)}" class="btn" target="_blank"
                   style="text-decoration:none; display:inline-block">
                    Download Only
                </a>`;
        }

        if (!isAvailable && !u.blocked) {
            html += `
                <button class="btn" onclick="startUpdate('${esc(dev.id)}', true)">
                    Force Reinstall
                </button>`;
        }

        html += `</div>`;

        // Progress area (hidden until update starts)
        html += `<div id="update-progress-${esc(dev.id)}" class="update-progress" style="display:none"></div>`;

        html += '</div>';
    }

    html += '</div>';
    main.innerHTML = html;
}

async function startUpdate(deviceId, force) {
    const action = force ? 'force reinstall' : 'update';
    if (!confirm(`Start firmware ${action} for this device?\n\nDO NOT disconnect the device during the update.`)) {
        return;
    }

    const progressEl = document.getElementById(`update-progress-${deviceId}`);
    if (progressEl) {
        progressEl.style.display = 'block';
        progressEl.innerHTML = '<div class="loading"><div class="spinner"></div> Starting update...</div>';
    }

    try {
        const resp = await fetch('/api/update/start', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({device_id: deviceId, force: force}),
        });
        const data = await resp.json();

        if (data.error) {
            if (progressEl) {
                progressEl.innerHTML = `<div class="update-error">${esc(data.error)}</div>`;
            }
            return;
        }

        // Start polling for status
        pollUpdateStatus(deviceId);

    } catch (e) {
        if (progressEl) {
            progressEl.innerHTML = '<div class="update-error">Failed to start update</div>';
        }
    }
}

function pollUpdateStatus(deviceId) {
    // Clear existing poller
    if (updatePollers[deviceId]) clearInterval(updatePollers[deviceId]);

    updatePollers[deviceId] = setInterval(async () => {
        try {
            const resp = await fetch(`/api/update/status/${deviceId}`);
            const job = await resp.json();
            renderUpdateProgress(deviceId, job);

            // Stop polling when done
            if (job.status === 'done' || job.status === 'error' || job.status === 'up_to_date') {
                clearInterval(updatePollers[deviceId]);
                delete updatePollers[deviceId];
            }
        } catch (e) {
            // Keep polling on network error
        }
    }, 1000);
}

function renderUpdateProgress(deviceId, job) {
    const el = document.getElementById(`update-progress-${deviceId}`);
    if (!el) return;

    el.style.display = 'block';

    if (job.status === 'done') {
        el.innerHTML = `
            <div class="progress-bar-wrap">
                <div class="progress-bar done" style="width:100%"></div>
            </div>
            <div class="progress-text success">${esc(job.message)}</div>`;
        toast('Firmware update complete!');
    } else if (job.status === 'error') {
        el.innerHTML = `
            <div class="progress-text error-text">${esc(job.message)}</div>
            ${job.error ? `<div class="progress-detail">${esc(job.error)}</div>` : ''}`;
    } else if (job.status === 'up_to_date') {
        el.innerHTML = `<div class="progress-text success">${esc(job.message)}</div>`;
    } else {
        const pct = job.progress || 0;
        const statusLabel = {
            'checking': 'Checking...',
            'downloading': 'Downloading...',
            'flashing': 'Flashing...',
        }[job.status] || job.status;

        el.innerHTML = `
            <div class="progress-bar-wrap">
                <div class="progress-bar active" style="width:${pct}%"></div>
            </div>
            <div class="progress-text">${esc(job.message)}</div>`;
    }
}

// ── Catalog ─────────────────────────────────────────────────────────────────

function showCatalog() {
    const main = document.querySelector('main');
    main.innerHTML = `
        <div class="section-title">Firmware Catalog</div>
        <div class="search-bar">
            <input type="text" id="catalog-search" placeholder="Search products (e.g. voyager, blackwire, savi...)"
                   onkeydown="if(event.key==='Enter') searchCatalog()">
            <button onclick="searchCatalog()">Search</button>
        </div>
        <div id="catalog-results">
            <div class="loading"><div class="spinner"></div> Loading catalog...</div>
        </div>`;
    searchCatalog();
}

async function searchCatalog() {
    const input = document.getElementById('catalog-search');
    const results = document.getElementById('catalog-results');
    const q = input ? input.value : '';

    results.innerHTML = '<div class="loading"><div class="spinner"></div> Searching...</div>';

    try {
        const resp = await fetch(`/api/catalog?q=${encodeURIComponent(q)}`);
        const data = await resp.json();
        renderCatalog(data.products);
    } catch (e) {
        results.innerHTML = '<div class="empty"><h3>Search failed</h3></div>';
    }
}

function renderCatalog(products) {
    const results = document.getElementById('catalog-results');

    if (!products || products.length === 0) {
        results.innerHTML = '<div class="empty"><h3>No products found</h3></div>';
        return;
    }

    let html = `
        <table class="catalog-table">
            <thead>
                <tr>
                    <th>PID</th>
                    <th>Product</th>
                    <th>Latest Firmware</th>
                    <th>DFU Support</th>
                </tr>
            </thead>
            <tbody>`;

    for (const p of products) {
        html += `
            <tr>
                <td style="color:var(--text-dim)">${esc(p.id)}</td>
                <td><strong>${esc(p.name)}</strong></td>
                <td style="color:var(--green)">${esc(p.version || 'n/a')}</td>
                <td>${esc(p.dfu_support || 'n/a')}</td>
            </tr>`;
    }

    html += '</tbody></table>';
    html += `<div style="margin-top:12px; color:var(--text-dim); font-size:13px">${products.length} products</div>`;
    results.innerHTML = html;
}

// ── Firmware Library ────────────────────────────────────────────────────────

async function loadLibrary() {
    const main = document.querySelector('main');
    try {
        const resp = await fetch('/api/firmware/library');
        const data = await resp.json();
        renderLibrary(data);
    } catch (e) {
        main.innerHTML = '<div class="empty"><h3>Failed to load firmware library</h3></div>';
    }
}

function renderLibrary(data) {
    const main = document.querySelector('main');
    const pkgs = data.packages || [];

    if (pkgs.length === 0) {
        main.innerHTML = `
            <div class="empty">
                <div class="empty-icon">&#x1F4E6;</div>
                <h3>No firmware cached</h3>
                <p>Check the Firmware Updates tab to download firmware</p>
            </div>`;
        return;
    }

    const fmtColors = {
        'FIRMWARE': '#4f8ff7',
        'FWU': '#34d399',
        'CSR-dfu2': '#fb923c',
        'APPUHDR5': '#a78bfa',
        'S-record': '#fbbf24',
    };

    const typeLabels = {
        'usb': 'Application',
        'bootloader': 'Bootloader',
        'hs': 'Headset',
        'base': 'Base Station',
        'lang': 'Language Pack',
        'bt': 'Bluetooth',
        'pic': 'PIC MCU',
        'bc05': 'DECT Radio',
        'bc5': 'DECT Radio',
        'tuning': 'DSP Tuning',
        'tuningpackage': 'DSP Tuning',
        'setid': 'Set ID',
    };

    let html = `
        <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:20px">
            <div class="section-title">Firmware Library (${pkgs.length} packages, ${data.total_mb} MB)</div>
        </div>`;

    for (const pkg of pkgs) {
        const fmtBadges = (pkg.formats || []).map(f => {
            const color = fmtColors[f] || 'var(--text-dim)';
            return `<span class="fmt-badge" style="color:${color}; border-color:${color}">${esc(f)}</span>`;
        }).join(' ');

        html += `
            <div class="update-card" style="margin-bottom:12px">
                <div class="update-header">
                    <div>
                        <div class="device-name">${esc(pkg.product)}</div>
                        <div class="version-info" style="margin-top:4px">
                            v${esc(pkg.version)} &mdash; ${pkg.size_mb} MB
                        </div>
                    </div>
                    <div>${fmtBadges}</div>
                </div>`;

        if (pkg.components.length > 0) {
            html += `<div class="comp-grid">`;
            for (const c of pkg.components) {
                const typeLabel = typeLabels[c.type] || c.type || '?';
                html += `
                    <div class="comp-item">
                        <span class="comp-type">${esc(typeLabel)}</span>
                        <span class="comp-desc" title="${esc(c.description)}">${esc(c.description || c.fileName)}</span>
                        ${c.version ? `<span class="comp-ver">v${esc(c.version)}</span>` : ''}
                    </div>`;
            }
            html += `</div>`;
        }

        html += `</div>`;
    }

    main.innerHTML = html;
}


// ── Settings ────────────────────────────────────────────────────────────────

async function loadSettings() {
    const main = document.querySelector('main');

    // Get devices first
    let devices;
    try {
        const resp = await fetch('/api/devices');
        const data = await resp.json();
        devices = data.devices;
    } catch (e) {
        main.innerHTML = '<div class="empty"><h3>Failed to load devices</h3></div>';
        return;
    }

    if (!devices || devices.length === 0) {
        main.innerHTML = `
            <div class="empty">
                <div class="empty-icon">&#x2699;</div>
                <h3>No devices connected</h3>
                <p>Connect a device to manage its settings</p>
            </div>`;
        return;
    }

    let html = '<div class="section-title">Device Settings</div>';

    for (const dev of devices) {
        html += `
            <div class="update-card" id="settings-card-${esc(dev.id)}" style="margin-bottom:16px">
                <div class="update-header">
                    <div class="device-name">${esc(dev.name)}</div>
                    <div class="device-category">${esc(dev.category)}</div>
                </div>
                <div id="settings-body-${esc(dev.id)}" class="loading" style="margin-top:12px">
                    <div class="spinner"></div> Reading settings...
                </div>
            </div>`;
    }

    main.innerHTML = html;

    // Load settings for each device
    for (const dev of devices) {
        loadDeviceSettings(dev.id);
    }
}

async function loadDeviceSettings(devId) {
    const body = document.getElementById(`settings-body-${devId}`);
    if (!body) return;

    try {
        const resp = await fetch(`/api/settings/${devId}`);
        const data = await resp.json();
        renderDeviceSettings(devId, data.settings || []);
    } catch (e) {
        body.innerHTML = '<span style="color:var(--text-dim)">Could not read settings</span>';
    }
}

function renderDeviceSettings(devId, settings) {
    const body = document.getElementById(`settings-body-${devId}`);
    if (!body) return;

    if (!settings || settings.length === 0) {
        body.innerHTML = '<span style="color:var(--text-dim)">No readable settings for this device</span>';
        return;
    }

    let html = '<div class="settings-grid">';

    for (const s of settings) {
        const readOnly = s.read_only || (s.writable === false);
        html += `<div class="setting-row">`;
        html += `<div class="setting-label">${esc(s.name)}</div>`;

        if (s.type === 'bool') {
            const checked = s.value ? 'checked' : '';
            const disabled = readOnly ? 'disabled' : '';
            html += `
                <label class="toggle">
                    <input type="checkbox" ${checked} ${disabled}
                           onchange="writeSetting('${esc(devId)}', '${esc(s.name)}', this.checked)">
                    <span class="toggle-slider"></span>
                </label>`;
        } else if (s.type === 'range') {
            const disabled = readOnly ? 'disabled' : '';
            html += `
                <div class="setting-range">
                    <input type="range" min="${s.min}" max="${s.max}" value="${s.value}" ${disabled}
                           oninput="this.nextElementSibling.textContent=this.value"
                           onchange="writeSetting('${esc(devId)}', '${esc(s.name)}', Number(this.value))">
                    <span class="range-value">${s.value}</span>
                </div>`;
        } else if (s.type === 'choice' && s.choices) {
            const disabled = readOnly ? 'disabled' : '';
            html += `
                <select class="setting-select" ${disabled}
                        onchange="writeSetting('${esc(devId)}', '${esc(s.name)}', this.value)">`;
            for (const c of s.choices) {
                const selected = c === s.value ? 'selected' : '';
                html += `<option value="${esc(c)}" ${selected}>${esc(c)}</option>`;
            }
            html += `</select>`;
        } else {
            html += `<span class="prop-value" style="color:var(--text-dim)">${esc(String(s.value))}</span>`;
        }

        html += `</div>`;
    }

    html += '</div>';
    body.innerHTML = html;
}

async function writeSetting(devId, name, value) {
    try {
        const resp = await fetch(`/api/settings/${devId}`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name, value}),
        });
        const data = await resp.json();
        if (data.success) {
            toast(`${name} updated`);
        } else {
            toast(`Failed to update ${name}`);
        }
    } catch (e) {
        toast(`Error updating ${name}`);
    }
}


// ── Helpers ─────────────────────────────────────────────────────────────────

function esc(s) {
    if (s == null) return '';
    const div = document.createElement('div');
    div.textContent = String(s);
    return div.innerHTML;
}

function toast(msg) {
    let el = document.querySelector('.toast');
    if (!el) {
        el = document.createElement('div');
        el.className = 'toast';
        document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add('show');
    setTimeout(() => el.classList.remove('show'), 2500);
}
