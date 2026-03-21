#!/usr/bin/env python3
"""
PolyServer — Central headset management server.

Receives device reports from agents running on workstations,
stores inventory in SQLite, enforces policies, and provides
an admin dashboard for fleet-wide headset management.

Requires PostgreSQL. Set connection via --db flag or POLYSERVER_DB env var.

Usage:
  python3 polyserver.py                                              # localhost:5432/polytool
  python3 polyserver.py --db postgresql://user:pass@host/polytool     # Custom connection
  python3 polyserver.py --port 9000                                  # Custom port
  python3 polyserver.py --init                                       # Initialize database only

API Endpoints:
  POST /api/agent/report      Agent sends device inventory
  POST /api/agent/heartbeat   Agent check-in
  GET  /api/agent/commands     Agent polls for pending commands
  POST /api/agent/result      Agent reports command result

  GET  /api/fleet              All devices across all agents
  GET  /api/fleet/compliance   Compliance status per device
  POST /api/fleet/command      Push command to device(s)

  GET  /api/policies           List policies
  POST /api/policies           Create/update policy
  DELETE /api/policies/<id>    Delete policy

  GET  /                       Admin dashboard
"""

import json
import os
import sys
import time
import hashlib
import secrets
import hmac
import functools
from datetime import datetime, timedelta
from pathlib import Path

try:
    from flask import Flask, jsonify, request, send_from_directory, g
except ImportError:
    print("Flask required: pip install flask")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────

DATA_DIR = Path.home() / ".polytool" / "server"
CONFIG_PATH = DATA_DIR / "server.json"
WEB_DIR = Path(__file__).parent / "web_admin"

app = Flask(__name__, static_folder=None)


# ── Security ──────────────────────────────────────────────────────────────

def load_or_create_config():
    """Load server config or create with fresh API keys."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())

    config = {
        "admin_key": secrets.token_urlsafe(32),
        "agent_key": secrets.token_urlsafe(32),
        "created": datetime.utcnow().isoformat(),
    }
    CONFIG_PATH.write_text(json.dumps(config, indent=2))
    CONFIG_PATH.chmod(0o600)  # owner read/write only
    return config


SERVER_CONFIG = load_or_create_config()

# Session store: maps random session tokens to True (valid session)
_sessions = {}


def require_agent_key(f):
    """Decorator: require valid agent API key in Authorization header."""
    @functools.wraps(f)
    def wrapped(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = auth.replace("Bearer ", "").strip()
        if not token or not hmac.compare_digest(token, SERVER_CONFIG["agent_key"]):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapped


def require_admin_key(f):
    """Decorator: require valid admin API key or session token."""
    @functools.wraps(f)
    def wrapped(*args, **kwargs):
        # Check Bearer header — must be the real admin_key
        auth = request.headers.get("Authorization", "")
        bearer = auth.replace("Bearer ", "").strip()
        if bearer and hmac.compare_digest(bearer, SERVER_CONFIG["admin_key"]):
            return f(*args, **kwargs)
        # Check query param — must be the real admin_key
        qkey = request.args.get("key", "")
        if qkey and hmac.compare_digest(qkey, SERVER_CONFIG["admin_key"]):
            return f(*args, **kwargs)
        # Check session cookie — must be a valid session token
        session_token = request.cookies.get("session", "")
        if session_token and session_token in _sessions:
            return f(*args, **kwargs)
        return jsonify({"error": "Unauthorized — admin key required"}), 401
    return wrapped


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    """Admin login — exchange admin key for a session token."""
    data = request.get_json() or {}
    key = data.get("key", "")
    if hmac.compare_digest(key, SERVER_CONFIG["admin_key"]):
        session_token = secrets.token_urlsafe(32)
        _sessions[session_token] = time.time()
        resp = jsonify({"status": "ok"})
        resp.set_cookie("session", session_token, httponly=True, samesite="Strict", max_age=86400)
        return resp
    return jsonify({"error": "Invalid admin key"}), 401


@app.route("/api/auth/keys")
@require_admin_key
def auth_show_keys():
    """Show API keys (only accessible from localhost)."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"error": "Only accessible from localhost"}), 403
    return jsonify({
        "admin_key": SERVER_CONFIG["admin_key"],
        "agent_key": SERVER_CONFIG["agent_key"],
        "note": "Add to agent: --key <agent_key>",
    })


# ── Rate Limiting ─────────────────────────────────────────────────────────

_rate_limit_store = {}  # ip → (count, window_start)
RATE_LIMIT_MAX = 120    # requests per window
RATE_LIMIT_WINDOW = 60  # seconds


_rate_limit_last_cleanup = 0.0


@app.before_request
def rate_limit():
    """Simple in-memory rate limiter with periodic cleanup."""
    global _rate_limit_last_cleanup
    ip = request.remote_addr
    now = time.time()

    # Periodic cleanup: purge stale entries every 5 minutes
    if now - _rate_limit_last_cleanup > 300:
        _rate_limit_last_cleanup = now
        stale = [k for k, (_, ws) in _rate_limit_store.items()
                 if now - ws > RATE_LIMIT_WINDOW]
        for k in stale:
            del _rate_limit_store[k]

    if ip in _rate_limit_store:
        count, window_start = _rate_limit_store[ip]
        if now - window_start > RATE_LIMIT_WINDOW:
            _rate_limit_store[ip] = (1, now)
        elif count >= RATE_LIMIT_MAX:
            return jsonify({"error": "Rate limit exceeded"}), 429
        else:
            _rate_limit_store[ip] = (count + 1, window_start)
    else:
        _rate_limit_store[ip] = (1, now)


# ── Input Validation ─────────────────────────────────────────────────────

def sanitize_string(s, max_len=256):
    """Sanitize string input."""
    if not isinstance(s, str):
        return str(s)[:max_len]
    return s.strip()[:max_len]


def validate_agent_report(data):
    """Validate agent report payload."""
    if not isinstance(data, dict):
        return False, "Invalid payload"
    if "agent_id" not in data:
        return False, "agent_id required"
    if len(data.get("agent_id", "")) > 64:
        return False, "agent_id too long"
    devices = data.get("devices", [])
    if not isinstance(devices, list):
        return False, "devices must be a list"
    if len(devices) > 50:
        return False, "Too many devices (max 50)"
    return True, ""

# ── Database ──────────────────────────────────────────────────────────────
# Supports PostgreSQL (recommended for production) or SQLite (zero-config).
# Set POLYSERVER_DB env var or --db flag:
#   PostgreSQL: postgresql://user:pass@host:5432/polytool
#   SQLite:     sqlite:///path/to/db  or  (empty for default)

_db_url = os.environ.get("POLYSERVER_DB", "postgresql://localhost:5432/polytool")

try:
    import psycopg2
    import psycopg2.extras
    _has_psycopg2 = True
except ImportError:
    _has_psycopg2 = False


class DBRow(dict):
    """Dict that also supports attribute access like sqlite3.Row."""
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def get_db():
    """Get a PostgreSQL connection."""
    return psycopg2.connect(_db_url)


def db_execute(conn, sql, params=None):
    """Execute SQL with psycopg2."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    return cur


def db_fetchall(conn, sql, params=None):
    """Execute and fetch all rows as list of dicts."""
    cur = db_execute(conn, sql, params)
    return [DBRow(r) for r in cur.fetchall()]


def db_fetchone(conn, sql, params=None):
    """Execute and fetch one row."""
    cur = db_execute(conn, sql, params)
    row = cur.fetchone()
    return DBRow(row) if row else None


def init_db():
    """Initialize PostgreSQL database schema."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    conn = psycopg2.connect(_db_url)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            agent_id TEXT PRIMARY KEY,
            hostname TEXT,
            username TEXT,
            platform TEXT,
            ip_address TEXT,
            agent_version TEXT,
            last_seen TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            id SERIAL PRIMARY KEY,
            agent_id TEXT REFERENCES agents(agent_id),
            pid TEXT,
            pid_hex TEXT,
            serial TEXT,
            product_name TEXT,
            friendly_name TEXT,
            firmware TEXT,
            category TEXT,
            dfu_executor TEXT,
            family TEXT,
            battery_level INTEGER DEFAULT -1,
            last_seen TIMESTAMP,
            first_seen TIMESTAMP DEFAULT NOW(),
            settings_json TEXT DEFAULT '{}',
            UNIQUE(agent_id, serial, pid)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS policies (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            target_pid TEXT DEFAULT '*',
            target_category TEXT DEFAULT '*',
            policy_type TEXT NOT NULL,
            policy_value TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS commands (
            id SERIAL PRIMARY KEY,
            agent_id TEXT,
            device_serial TEXT DEFAULT '*',
            device_pid TEXT DEFAULT '*',
            command_type TEXT NOT NULL,
            command_data TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            result TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW(),
            completed_at TIMESTAMP,
            FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMP DEFAULT NOW(),
            agent_id TEXT,
            device_serial TEXT,
            action TEXT,
            detail TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings_history (
            id SERIAL PRIMARY KEY,
            agent_id TEXT,
            device_serial TEXT,
            device_pid TEXT,
            setting_name TEXT,
            old_value TEXT,
            new_value TEXT,
            changed_by TEXT DEFAULT 'agent',
            timestamp TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id SERIAL PRIMARY KEY,
            alert_type TEXT NOT NULL,
            device_serial TEXT,
            agent_id TEXT,
            message TEXT,
            severity TEXT DEFAULT 'info',
            acknowledged INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_devices_agent ON devices(agent_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_devices_pid ON devices(pid)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_commands_agent ON commands(agent_id, status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_settings_hist_device ON settings_history(device_serial, setting_name)")

    conn.commit()
    conn.close()


# ── Agent API ─────────────────────────────────────────────────────────────

@app.route("/api/agent/report", methods=["POST"])
@require_agent_key
def agent_report():
    """Agent reports its device inventory."""
    data = request.get_json()
    ok, err = validate_agent_report(data)
    if not ok:
        return jsonify({"error": err}), 400

    agent_id = data["agent_id"]
    now = datetime.utcnow().isoformat()

    conn = get_db()

    # Update agent info
    db_execute(conn, """
        INSERT INTO agents (agent_id, hostname, username, platform, ip_address, agent_version, last_seen)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(agent_id) DO UPDATE SET
            hostname=excluded.hostname, username=excluded.username,
            platform=excluded.platform, ip_address=excluded.ip_address,
            agent_version=excluded.agent_version, last_seen=excluded.last_seen
    """, (agent_id, data.get("hostname", ""), data.get("username", ""),
          data.get("platform", ""), request.remote_addr,
          data.get("agent_version", ""), now))

    # Update devices
    devices = data.get("devices", [])
    for dev in devices:
        serial = dev.get("serial", "")
        pid = dev.get("pid", "")
        if not serial and not pid:
            continue

        new_settings = dev.get("settings", {})
        new_settings_json = json.dumps(new_settings)

        # Fetch existing settings to detect changes
        existing = db_fetchone(conn, """
            SELECT settings_json FROM devices
            WHERE agent_id = %s AND serial = %s AND pid = %s
        """, (agent_id, serial, pid))

        if existing:
            try:
                old_settings = json.loads(existing.get("settings_json", "{}") or "{}")
            except (json.JSONDecodeError, TypeError):
                old_settings = {}
            # Track changed settings
            all_keys = set(list(old_settings.keys()) + list(new_settings.keys()))
            for sname in all_keys:
                old_val = old_settings.get(sname)
                new_val = new_settings.get(sname)
                if old_val != new_val:
                    db_execute(conn, """
                        INSERT INTO settings_history
                            (agent_id, device_serial, device_pid, setting_name, old_value, new_value, changed_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (agent_id, serial, pid, sname,
                          str(old_val) if old_val is not None else None,
                          str(new_val) if new_val is not None else None,
                          "agent"))

        db_execute(conn, """
            INSERT INTO devices (agent_id, pid, pid_hex, serial, product_name,
                friendly_name, firmware, category, dfu_executor, family,
                battery_level, last_seen, settings_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(agent_id, serial, pid) DO UPDATE SET
                product_name=excluded.product_name, friendly_name=excluded.friendly_name,
                firmware=excluded.firmware, category=excluded.category,
                dfu_executor=excluded.dfu_executor, family=excluded.family,
                battery_level=excluded.battery_level, last_seen=excluded.last_seen,
                settings_json=excluded.settings_json
        """, (agent_id, pid, dev.get("pid_hex", ""), serial,
              dev.get("product_name", ""), dev.get("friendly_name", ""),
              dev.get("firmware", ""), dev.get("category", ""),
              dev.get("dfu_executor", ""), dev.get("family", ""),
              dev.get("battery_level", -1), now,
              new_settings_json))

    # Check for alert conditions
    for dev in devices:
        serial = dev.get("serial", "")
        pid = dev.get("pid", "")
        battery = dev.get("battery_level", -1)

        if battery >= 0 and battery < 5:
            db_execute(conn, """
                INSERT INTO alerts (alert_type, device_serial, agent_id, message, severity)
                VALUES (%s, %s, %s, %s, %s)
            """, ("low_battery", serial, agent_id,
                  f"Critical battery level: {battery}% on {dev.get('friendly_name', dev.get('product_name', pid))}",
                  "critical"))
        elif battery >= 0 and battery < 20:
            db_execute(conn, """
                INSERT INTO alerts (alert_type, device_serial, agent_id, message, severity)
                VALUES (%s, %s, %s, %s, %s)
            """, ("low_battery", serial, agent_id,
                  f"Low battery: {battery}% on {dev.get('friendly_name', dev.get('product_name', pid))}",
                  "warning"))

        # Check firmware against policies
        fw = dev.get("firmware", "")
        if fw:
            fw_policies = db_fetchall(conn, """
                SELECT * FROM policies WHERE policy_type = 'firmware_version' AND enabled = 1
            """)
            for pol in fw_policies:
                p = dict(pol)
                if p["target_pid"] != "*" and p["target_pid"] != pid:
                    continue
                if p["target_category"] != "*" and p["target_category"] != dev.get("category", ""):
                    continue
                if fw != p["policy_value"]:
                    db_execute(conn, """
                        INSERT INTO alerts (alert_type, device_serial, agent_id, message, severity)
                        VALUES (%s, %s, %s, %s, %s)
                    """, ("firmware_mismatch", serial, agent_id,
                          f"Firmware {fw} does not match policy '{p['name']}' (expected {p['policy_value']})",
                          "warning"))

    # Log
    db_execute(conn, "INSERT INTO audit_log (agent_id, action, detail) VALUES (%s, %s, %s)",
               (agent_id, "report", f"{len(devices)} device(s)"))

    conn.commit()
    conn.close()

    return jsonify({"status": "ok", "devices_received": len(devices)})


@app.route("/api/agent/heartbeat", methods=["POST"])
@require_agent_key
def agent_heartbeat():
    """Agent check-in."""
    data = request.get_json() or {}
    agent_id = data.get("agent_id", "")
    if not agent_id:
        return jsonify({"error": "agent_id required"}), 400

    conn = get_db()
    db_execute(conn, "UPDATE agents SET last_seen = %s WHERE agent_id = %s",
               (datetime.utcnow().isoformat(), agent_id))
    conn.commit()
    conn.close()

    return jsonify({"status": "ok"})


@app.route("/api/agent/commands", methods=["GET"])
@require_agent_key
def agent_get_commands():
    """Agent polls for pending commands."""
    agent_id = request.args.get("agent_id", "")
    if not agent_id:
        return jsonify({"error": "agent_id required"}), 400

    conn = get_db()
    rows = db_fetchall(conn, """
        SELECT id, command_type, command_data, device_serial, device_pid
        FROM commands
        WHERE agent_id = %s AND status = 'pending'
        ORDER BY created_at ASC
    """, (agent_id,))
    conn.close()

    return jsonify({
        "commands": [dict(r) for r in rows]
    })


@app.route("/api/agent/result", methods=["POST"])
@require_agent_key
def agent_command_result():
    """Agent reports command result."""
    data = request.get_json() or {}
    cmd_id = data.get("command_id")
    if not cmd_id:
        return jsonify({"error": "command_id required"}), 400

    conn = get_db()
    db_execute(conn, """
        UPDATE commands SET status = %s, result = %s, completed_at = %s
        WHERE id = %s
    """, (data.get("status", "done"), data.get("result", ""),
          datetime.utcnow().isoformat(), cmd_id))

    # Audit log
    db_execute(conn, "INSERT INTO audit_log (agent_id, device_serial, action, detail) VALUES (%s, %s, %s, %s)",
               (data.get("agent_id", ""), data.get("device_serial", ""),
                "command_result", json.dumps(data)))
    conn.commit()
    conn.close()

    return jsonify({"status": "ok"})


# ── Fleet API ─────────────────────────────────────────────────────────────

@app.route("/api/fleet")
def fleet_devices():
    """Get all devices across all agents."""
    conn = get_db()
    rows = db_fetchall(conn, """
        SELECT d.*, a.hostname, a.username, a.platform, a.last_seen as agent_last_seen
        FROM devices d
        LEFT JOIN agents a ON d.agent_id = a.agent_id
        ORDER BY d.last_seen DESC
    """)
    conn.close()

    devices = []
    for r in rows:
        d = dict(r)
        d["settings"] = json.loads(d.get("settings_json", "{}"))
        d.pop("settings_json", None)
        # Mark as online/offline (5 min threshold)
        try:
            last = datetime.fromisoformat(d.get("agent_last_seen", ""))
            d["online"] = (datetime.utcnow() - last).total_seconds() < 300
        except:
            d["online"] = False
        devices.append(d)

    return jsonify({"devices": devices, "count": len(devices)})


@app.route("/api/fleet/compliance")
def fleet_compliance():
    """Check all devices against active policies."""
    conn = get_db()
    devices = db_fetchall(conn, "SELECT * FROM devices")
    policies = db_fetchall(conn, "SELECT * FROM policies WHERE enabled = 1")
    conn.close()

    results = []
    for dev in devices:
        d = dict(dev)
        d["violations"] = []
        d["compliant"] = True

        for pol in policies:
            p = dict(pol)
            # Check if policy applies to this device
            if p["target_pid"] != "*" and p["target_pid"] != d["pid"]:
                continue
            if p["target_category"] != "*" and p["target_category"] != d.get("category", ""):
                continue

            # Check policy
            if p["policy_type"] == "firmware_version":
                if d.get("firmware", "") != p["policy_value"]:
                    d["violations"].append({
                        "policy": p["name"],
                        "type": "firmware_version",
                        "expected": p["policy_value"],
                        "actual": d.get("firmware", "unknown"),
                    })
                    d["compliant"] = False

            elif p["policy_type"] == "setting":
                try:
                    setting_rule = json.loads(p["policy_value"])
                    settings = json.loads(d.get("settings_json", "{}"))
                    name = setting_rule.get("name", "")
                    expected = setting_rule.get("value")
                    actual = settings.get(name)
                    if actual is not None and actual != expected:
                        d["violations"].append({
                            "policy": p["name"],
                            "type": "setting",
                            "setting": name,
                            "expected": expected,
                            "actual": actual,
                        })
                        d["compliant"] = False
                except:
                    pass

        results.append(d)

    total = len(results)
    compliant = sum(1 for r in results if r["compliant"])

    return jsonify({
        "devices": results,
        "total": total,
        "compliant": compliant,
        "non_compliant": total - compliant,
        "compliance_pct": round(compliant / total * 100) if total > 0 else 100,
    })


@app.route("/api/fleet/command", methods=["POST"])
@require_admin_key
def fleet_push_command():
    """Push a command to device(s)."""
    data = request.get_json() or {}

    agent_id = data.get("agent_id", "*")
    device_pid = data.get("device_pid", "*")
    device_serial = data.get("device_serial", "*")
    command_type = data.get("command_type", "")
    command_data = data.get("command_data", "{}")

    if not command_type:
        return jsonify({"error": "command_type required"}), 400

    conn = get_db()

    # If agent_id is *, push to all agents
    if agent_id == "*":
        agents = db_fetchall(conn, "SELECT agent_id FROM agents")
        agent_ids = [a["agent_id"] for a in agents]
    else:
        agent_ids = [agent_id]

    cmd_ids = []
    for aid in agent_ids:
        cur = db_execute(conn, """
            INSERT INTO commands (agent_id, device_serial, device_pid, command_type, command_data)
            VALUES (%s, %s, %s, %s, %s)
        """, (aid, device_serial, device_pid, command_type,
              json.dumps(command_data) if isinstance(command_data, dict) else command_data))
        cmd_ids.append(cur.lastrowid)

    db_execute(conn, "INSERT INTO audit_log (action, detail) VALUES (%s, %s)",
               ("push_command", json.dumps({
                   "type": command_type, "agents": len(agent_ids),
                   "pid": device_pid, "serial": device_serial,
               })))
    conn.commit()
    conn.close()

    return jsonify({"status": "ok", "command_ids": cmd_ids, "agents": len(agent_ids)})


# ── Policy API ────────────────────────────────────────────────────────────

@app.route("/api/policies", methods=["GET"])
def list_policies():
    conn = get_db()
    rows = db_fetchall(conn, "SELECT * FROM policies ORDER BY created_at DESC")
    conn.close()
    return jsonify({"policies": [dict(r) for r in rows]})


@app.route("/api/policies", methods=["POST"])
@require_admin_key
def create_policy():
    data = request.get_json() or {}
    conn = get_db()
    cur = db_execute(conn, """
        INSERT INTO policies (name, description, target_pid, target_category,
            policy_type, policy_value, enabled)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (data.get("name", ""), data.get("description", ""),
          data.get("target_pid", "*"), data.get("target_category", "*"),
          data.get("policy_type", ""), data.get("policy_value", ""),
          1 if data.get("enabled", True) else 0))
    db_execute(conn, "INSERT INTO audit_log (action, detail) VALUES (%s, %s)",
               ("create_policy", json.dumps(data)))
    conn.commit()
    policy_id = cur.lastrowid
    conn.close()
    return jsonify({"status": "ok", "id": policy_id})


@app.route("/api/policies/<int:policy_id>", methods=["DELETE"])
@require_admin_key
def delete_policy(policy_id):
    conn = get_db()
    db_execute(conn, "DELETE FROM policies WHERE id = %s", (policy_id,))
    db_execute(conn, "INSERT INTO audit_log (action, detail) VALUES (%s, %s)",
               ("delete_policy", str(policy_id)))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ── Stats API ─────────────────────────────────────────────────────────────

@app.route("/api/stats")
def fleet_stats():
    conn = get_db()
    total_devices = db_fetchone(conn, "SELECT COUNT(*) FROM devices")[0]
    total_agents = db_fetchone(conn, "SELECT COUNT(*) FROM agents")[0]
    online_agents = db_fetchone(conn, """
        SELECT COUNT(*) as cnt FROM agents
        WHERE last_seen > NOW() - INTERVAL '5 minutes'
    """)["cnt"]

    by_category = db_fetchall(conn, """
        SELECT category, COUNT(*) as count FROM devices GROUP BY category
    """)

    by_firmware = db_fetchall(conn, """
        SELECT pid_hex, friendly_name, firmware, COUNT(*) as count
        FROM devices GROUP BY pid, firmware ORDER BY count DESC
    """)

    recent_log = db_fetchall(conn, """
        SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT 20
    """)

    conn.close()

    return jsonify({
        "total_devices": total_devices,
        "total_agents": total_agents,
        "online_agents": online_agents,
        "by_category": [dict(r) for r in by_category],
        "by_firmware": [dict(r) for r in by_firmware],
        "recent_activity": [dict(r) for r in recent_log],
    })


# ── Audit Log ─────────────────────────────────────────────────────────────

@app.route("/api/audit")
def audit_log():
    limit = request.args.get("limit", 100, type=int)
    conn = get_db()
    rows = db_fetchall(conn, "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT %s",
                       (limit,))
    conn.close()
    return jsonify({"log": [dict(r) for r in rows]})


# ── Fleet Detail / Health / Enforce / Alerts API ─────────────────────────

@app.route("/api/fleet/device/<int:device_id>")
@require_admin_key
def fleet_device_detail(device_id):
    """Return detailed info for a single device."""
    conn = get_db()
    dev = db_fetchone(conn, """
        SELECT d.*, a.hostname, a.username, a.platform, a.last_seen as agent_last_seen
        FROM devices d
        LEFT JOIN agents a ON d.agent_id = a.agent_id
        WHERE d.id = %s
    """, (device_id,))
    if not dev:
        conn.close()
        return jsonify({"error": "Device not found"}), 404

    d = dict(dev)
    d["settings"] = json.loads(d.get("settings_json", "{}"))
    d.pop("settings_json", None)
    try:
        last = datetime.fromisoformat(d.get("agent_last_seen", ""))
        d["online"] = (datetime.utcnow() - last).total_seconds() < 300
    except Exception:
        d["online"] = False

    # Compliance
    policies = db_fetchall(conn, "SELECT * FROM policies WHERE enabled = 1")
    d["violations"] = []
    d["compliant"] = True
    for pol in policies:
        p = dict(pol)
        if p["target_pid"] != "*" and p["target_pid"] != d["pid"]:
            continue
        if p["target_category"] != "*" and p["target_category"] != d.get("category", ""):
            continue
        if p["policy_type"] == "firmware_version":
            if d.get("firmware", "") != p["policy_value"]:
                d["violations"].append({"policy": p["name"], "type": "firmware_version",
                                         "expected": p["policy_value"], "actual": d.get("firmware", "unknown")})
                d["compliant"] = False
        elif p["policy_type"] == "setting":
            try:
                rule = json.loads(p["policy_value"])
                actual = d["settings"].get(rule.get("name", ""))
                if actual is not None and actual != rule.get("value"):
                    d["violations"].append({"policy": p["name"], "type": "setting",
                                             "setting": rule["name"], "expected": rule["value"], "actual": actual})
                    d["compliant"] = False
            except Exception:
                pass

    # Command history
    cmds = db_fetchall(conn, """
        SELECT * FROM commands
        WHERE (device_pid = %s OR device_pid = '*')
          AND (agent_id = %s OR agent_id = '*')
        ORDER BY created_at DESC LIMIT 20
    """, (d.get("pid", ""), d.get("agent_id", "")))
    d["command_history"] = [dict(c) for c in cmds]

    conn.close()
    return jsonify(d)


@app.route("/api/fleet/settings-summary")
@require_admin_key
def fleet_settings_summary():
    """Return summary of all unique settings across the fleet with value distributions."""
    conn = get_db()
    devices = db_fetchall(conn, "SELECT settings_json FROM devices")
    conn.close()

    distributions = {}  # {setting_name: {value: count}}
    for dev in devices:
        try:
            settings = json.loads(dev.get("settings_json", "{}") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        for name, value in settings.items():
            if name not in distributions:
                distributions[name] = {}
            val_str = str(value)
            distributions[name][val_str] = distributions[name].get(val_str, 0) + 1

    summary = []
    for name in sorted(distributions.keys()):
        summary.append({"setting": name, "values": distributions[name],
                        "total_devices": sum(distributions[name].values())})

    return jsonify({"settings": summary, "unique_settings": len(summary)})


@app.route("/api/fleet/enforce", methods=["POST"])
@require_admin_key
def fleet_enforce():
    """Find non-compliant devices and push set_setting commands to fix them."""
    data = request.get_json() or {}
    policy_id = data.get("policy_id")
    if not policy_id:
        return jsonify({"error": "policy_id required"}), 400

    conn = get_db()
    policy = db_fetchone(conn, "SELECT * FROM policies WHERE id = %s AND enabled = 1", (policy_id,))
    if not policy:
        conn.close()
        return jsonify({"error": "Policy not found or disabled"}), 404

    p = dict(policy)
    devices = db_fetchall(conn, "SELECT * FROM devices")
    pushed = 0

    for dev in devices:
        d = dict(dev)
        if p["target_pid"] != "*" and p["target_pid"] != d["pid"]:
            continue
        if p["target_category"] != "*" and p["target_category"] != d.get("category", ""):
            continue

        if p["policy_type"] == "setting":
            try:
                rule = json.loads(p["policy_value"])
                settings = json.loads(d.get("settings_json", "{}") or "{}")
                sname = rule.get("name", "")
                expected = rule.get("value")
                actual = settings.get(sname)
                if actual is not None and actual != expected:
                    db_execute(conn, """
                        INSERT INTO commands (agent_id, device_serial, device_pid, command_type, command_data)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (d["agent_id"], d.get("serial", "*"), d["pid"],
                          "set_setting", json.dumps({"name": sname, "value": expected})))
                    pushed += 1
            except Exception:
                pass
        elif p["policy_type"] == "firmware_version":
            if d.get("firmware", "") != p["policy_value"]:
                # Firmware enforcement is informational only — cannot auto-push DFU
                pass

    db_execute(conn, "INSERT INTO audit_log (action, detail) VALUES (%s, %s)",
               ("enforce_policy", json.dumps({"policy_id": policy_id, "commands_pushed": pushed})))
    conn.commit()
    conn.close()

    return jsonify({"status": "ok", "commands_pushed": pushed, "policy": p["name"]})


@app.route("/api/fleet/health")
@require_admin_key
def fleet_health():
    """Return fleet health metrics."""
    conn = get_db()

    low_battery = db_fetchall(conn, """
        SELECT d.*, a.hostname FROM devices d
        LEFT JOIN agents a ON d.agent_id = a.agent_id
        WHERE d.battery_level >= 0 AND d.battery_level < 20
    """)

    offline_24h = db_fetchall(conn, """
        SELECT d.*, a.hostname, a.last_seen as agent_last_seen FROM devices d
        LEFT JOIN agents a ON d.agent_id = a.agent_id
        WHERE a.last_seen < NOW() - INTERVAL '24 hours'
    """)

    # Devices with firmware not matching any active policy
    devices = db_fetchall(conn, "SELECT * FROM devices")
    policies = db_fetchall(conn, "SELECT * FROM policies WHERE enabled = 1 AND policy_type = 'firmware_version'")
    outdated = []
    for dev in devices:
        d = dict(dev)
        for pol in policies:
            p = dict(pol)
            if p["target_pid"] != "*" and p["target_pid"] != d["pid"]:
                continue
            if p["target_category"] != "*" and p["target_category"] != d.get("category", ""):
                continue
            if d.get("firmware", "") != p["policy_value"]:
                outdated.append(d)
                break

    # Compliance violations count
    violation_count = 0
    setting_policies = db_fetchall(conn, "SELECT * FROM policies WHERE enabled = 1 AND policy_type = 'setting'")
    for dev in devices:
        d = dict(dev)
        for pol in setting_policies:
            p = dict(pol)
            if p["target_pid"] != "*" and p["target_pid"] != d["pid"]:
                continue
            if p["target_category"] != "*" and p["target_category"] != d.get("category", ""):
                continue
            try:
                rule = json.loads(p["policy_value"])
                settings = json.loads(d.get("settings_json", "{}") or "{}")
                if settings.get(rule.get("name", "")) is not None and settings.get(rule["name"]) != rule.get("value"):
                    violation_count += 1
            except Exception:
                pass

    conn.close()

    return jsonify({
        "low_battery": [dict(d) for d in low_battery],
        "low_battery_count": len(low_battery),
        "offline_24h": [dict(d) for d in offline_24h],
        "offline_24h_count": len(offline_24h),
        "outdated_firmware": [dict(d) for d in outdated],
        "outdated_firmware_count": len(outdated),
        "compliance_violations": violation_count,
    })


@app.route("/api/alerts")
@require_admin_key
def get_alerts():
    """Return unacknowledged alerts with optional severity filter."""
    severity = request.args.get("severity", "")
    conn = get_db()
    if severity:
        rows = db_fetchall(conn, """
            SELECT * FROM alerts WHERE acknowledged = 0 AND severity = %s
            ORDER BY created_at DESC
        """, (severity,))
    else:
        rows = db_fetchall(conn, """
            SELECT * FROM alerts WHERE acknowledged = 0
            ORDER BY created_at DESC
        """)
    conn.close()
    return jsonify({"alerts": [dict(r) for r in rows], "count": len(rows)})


@app.route("/api/alerts/<int:alert_id>/acknowledge", methods=["POST"])
@require_admin_key
def acknowledge_alert(alert_id):
    """Mark an alert as acknowledged."""
    conn = get_db()
    db_execute(conn, "UPDATE alerts SET acknowledged = 1 WHERE id = %s", (alert_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ── Admin Dashboard ───────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return send_from_directory(WEB_DIR, "index.html")

@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(WEB_DIR, filename)


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="PolyServer — Headset Management Server")
    parser.add_argument("--port", type=int, default=8421)
    parser.add_argument("--init", action="store_true", help="Initialize database")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--db", default=os.environ.get("POLYSERVER_DB", ""),
                        help="Database URL (postgresql://... or empty for SQLite)")
    args = parser.parse_args()

    # Configure database backend
    global _db_url
    if args.db:
        _db_url = args.db
    if not _has_psycopg2:
        print("Error: psycopg2 required. Install with: pip install psycopg2-binary")
        sys.exit(1)
    db_display = _db_url.split("@")[-1] if "@" in _db_url else _db_url
    print(f"  Database: PostgreSQL ({db_display})")

    init_db()

    if args.init:
        print(f"Database initialized at {db_display}")
        return

    print(f"\n  PolyServer — Headset Management")
    print(f"  Dashboard:  http://localhost:{args.port}")
    print(f"  Agent API:  http://0.0.0.0:{args.port}/api/agent/")
    print(f"  Database:   {db_display}")
    print(f"")
    print(f"  Admin Key:  {SERVER_CONFIG['admin_key']}")
    print(f"  Agent Key:  {SERVER_CONFIG['agent_key']}")
    print(f"")
    print(f"  Start agents with:")
    print(f"    python3 polyagent.py --server http://THIS_IP:{args.port} --key {SERVER_CONFIG['agent_key']}")
    print(f"")

    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
