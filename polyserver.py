#!/usr/bin/env python3
"""
PolyServer — Central headset management server.

Receives device reports from agents running on workstations,
stores inventory in SQLite, enforces policies, and provides
an admin dashboard for fleet-wide headset management.

Usage:
  python3 polyserver.py                    # Start on port 8421
  python3 polyserver.py --port 9000        # Custom port
  python3 polyserver.py --init             # Initialize database

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
import sqlite3
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

try:
    from flask import Flask, jsonify, request, send_from_directory
except ImportError:
    print("Flask required: pip install flask")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────

DATA_DIR = Path.home() / ".polytool" / "server"
DB_PATH = DATA_DIR / "polytool.db"
WEB_DIR = Path(__file__).parent / "web_admin"

app = Flask(__name__, static_folder=None)

# ── Database ──────────────────────────────────────────────────────────────

def get_db():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS agents (
            agent_id TEXT PRIMARY KEY,
            hostname TEXT,
            username TEXT,
            platform TEXT,
            ip_address TEXT,
            agent_version TEXT,
            last_seen TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT,
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
            last_seen TEXT,
            first_seen TEXT DEFAULT (datetime('now')),
            settings_json TEXT DEFAULT '{}',
            UNIQUE(agent_id, serial, pid),
            FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
        );

        CREATE TABLE IF NOT EXISTS policies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            target_pid TEXT DEFAULT '*',
            target_category TEXT DEFAULT '*',
            policy_type TEXT NOT NULL,
            policy_value TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT,
            device_serial TEXT DEFAULT '*',
            device_pid TEXT DEFAULT '*',
            command_type TEXT NOT NULL,
            command_data TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            result TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT,
            FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            agent_id TEXT,
            device_serial TEXT,
            action TEXT,
            detail TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_devices_agent ON devices(agent_id);
        CREATE INDEX IF NOT EXISTS idx_devices_pid ON devices(pid);
        CREATE INDEX IF NOT EXISTS idx_commands_agent ON commands(agent_id, status);
    """)
    db.commit()
    db.close()


# ── Agent API ─────────────────────────────────────────────────────────────

@app.route("/api/agent/report", methods=["POST"])
def agent_report():
    """Agent reports its device inventory."""
    data = request.get_json()
    if not data or "agent_id" not in data:
        return jsonify({"error": "agent_id required"}), 400

    agent_id = data["agent_id"]
    now = datetime.utcnow().isoformat()

    db = get_db()

    # Update agent info
    db.execute("""
        INSERT INTO agents (agent_id, hostname, username, platform, ip_address, agent_version, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
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

        db.execute("""
            INSERT INTO devices (agent_id, pid, pid_hex, serial, product_name,
                friendly_name, firmware, category, dfu_executor, family,
                battery_level, last_seen, settings_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
              json.dumps(dev.get("settings", {}))))

    # Log
    db.execute("INSERT INTO audit_log (agent_id, action, detail) VALUES (?, ?, ?)",
               (agent_id, "report", f"{len(devices)} device(s)"))

    db.commit()
    db.close()

    return jsonify({"status": "ok", "devices_received": len(devices)})


@app.route("/api/agent/heartbeat", methods=["POST"])
def agent_heartbeat():
    """Agent check-in."""
    data = request.get_json() or {}
    agent_id = data.get("agent_id", "")
    if not agent_id:
        return jsonify({"error": "agent_id required"}), 400

    db = get_db()
    db.execute("UPDATE agents SET last_seen = ? WHERE agent_id = ?",
               (datetime.utcnow().isoformat(), agent_id))
    db.commit()
    db.close()

    return jsonify({"status": "ok"})


@app.route("/api/agent/commands", methods=["GET"])
def agent_get_commands():
    """Agent polls for pending commands."""
    agent_id = request.args.get("agent_id", "")
    if not agent_id:
        return jsonify({"error": "agent_id required"}), 400

    db = get_db()
    rows = db.execute("""
        SELECT id, command_type, command_data, device_serial, device_pid
        FROM commands
        WHERE agent_id = ? AND status = 'pending'
        ORDER BY created_at ASC
    """, (agent_id,)).fetchall()
    db.close()

    return jsonify({
        "commands": [dict(r) for r in rows]
    })


@app.route("/api/agent/result", methods=["POST"])
def agent_command_result():
    """Agent reports command result."""
    data = request.get_json() or {}
    cmd_id = data.get("command_id")
    if not cmd_id:
        return jsonify({"error": "command_id required"}), 400

    db = get_db()
    db.execute("""
        UPDATE commands SET status = ?, result = ?, completed_at = ?
        WHERE id = ?
    """, (data.get("status", "done"), data.get("result", ""),
          datetime.utcnow().isoformat(), cmd_id))

    # Audit log
    db.execute("INSERT INTO audit_log (agent_id, device_serial, action, detail) VALUES (?, ?, ?, ?)",
               (data.get("agent_id", ""), data.get("device_serial", ""),
                "command_result", json.dumps(data)))
    db.commit()
    db.close()

    return jsonify({"status": "ok"})


# ── Fleet API ─────────────────────────────────────────────────────────────

@app.route("/api/fleet")
def fleet_devices():
    """Get all devices across all agents."""
    db = get_db()
    rows = db.execute("""
        SELECT d.*, a.hostname, a.username, a.platform, a.last_seen as agent_last_seen
        FROM devices d
        LEFT JOIN agents a ON d.agent_id = a.agent_id
        ORDER BY d.last_seen DESC
    """).fetchall()
    db.close()

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
    db = get_db()
    devices = db.execute("SELECT * FROM devices").fetchall()
    policies = db.execute("SELECT * FROM policies WHERE enabled = 1").fetchall()
    db.close()

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

    db = get_db()

    # If agent_id is *, push to all agents
    if agent_id == "*":
        agents = db.execute("SELECT agent_id FROM agents").fetchall()
        agent_ids = [a["agent_id"] for a in agents]
    else:
        agent_ids = [agent_id]

    cmd_ids = []
    for aid in agent_ids:
        cursor = db.execute("""
            INSERT INTO commands (agent_id, device_serial, device_pid, command_type, command_data)
            VALUES (?, ?, ?, ?, ?)
        """, (aid, device_serial, device_pid, command_type,
              json.dumps(command_data) if isinstance(command_data, dict) else command_data))
        cmd_ids.append(cursor.lastrowid)

    db.execute("INSERT INTO audit_log (action, detail) VALUES (?, ?)",
               ("push_command", json.dumps({
                   "type": command_type, "agents": len(agent_ids),
                   "pid": device_pid, "serial": device_serial,
               })))
    db.commit()
    db.close()

    return jsonify({"status": "ok", "command_ids": cmd_ids, "agents": len(agent_ids)})


# ── Policy API ────────────────────────────────────────────────────────────

@app.route("/api/policies", methods=["GET"])
def list_policies():
    db = get_db()
    rows = db.execute("SELECT * FROM policies ORDER BY created_at DESC").fetchall()
    db.close()
    return jsonify({"policies": [dict(r) for r in rows]})


@app.route("/api/policies", methods=["POST"])
def create_policy():
    data = request.get_json() or {}
    db = get_db()
    cursor = db.execute("""
        INSERT INTO policies (name, description, target_pid, target_category,
            policy_type, policy_value, enabled)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (data.get("name", ""), data.get("description", ""),
          data.get("target_pid", "*"), data.get("target_category", "*"),
          data.get("policy_type", ""), data.get("policy_value", ""),
          1 if data.get("enabled", True) else 0))
    db.execute("INSERT INTO audit_log (action, detail) VALUES (?, ?)",
               ("create_policy", json.dumps(data)))
    db.commit()
    policy_id = cursor.lastrowid
    db.close()
    return jsonify({"status": "ok", "id": policy_id})


@app.route("/api/policies/<int:policy_id>", methods=["DELETE"])
def delete_policy(policy_id):
    db = get_db()
    db.execute("DELETE FROM policies WHERE id = ?", (policy_id,))
    db.execute("INSERT INTO audit_log (action, detail) VALUES (?, ?)",
               ("delete_policy", str(policy_id)))
    db.commit()
    db.close()
    return jsonify({"status": "ok"})


# ── Stats API ─────────────────────────────────────────────────────────────

@app.route("/api/stats")
def fleet_stats():
    db = get_db()
    total_devices = db.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    total_agents = db.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    online_agents = db.execute("""
        SELECT COUNT(*) FROM agents
        WHERE datetime(last_seen) > datetime('now', '-5 minutes')
    """).fetchone()[0]

    by_category = db.execute("""
        SELECT category, COUNT(*) as count FROM devices GROUP BY category
    """).fetchall()

    by_firmware = db.execute("""
        SELECT pid_hex, friendly_name, firmware, COUNT(*) as count
        FROM devices GROUP BY pid, firmware ORDER BY count DESC
    """).fetchall()

    recent_log = db.execute("""
        SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT 20
    """).fetchall()

    db.close()

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
    db = get_db()
    rows = db.execute("SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?",
                       (limit,)).fetchall()
    db.close()
    return jsonify({"log": [dict(r) for r in rows]})


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
    args = parser.parse_args()

    init_db()

    if args.init:
        print(f"Database initialized at {DB_PATH}")
        return

    print(f"\n  PolyServer — Headset Management")
    print(f"  Dashboard: http://localhost:{args.port}")
    print(f"  Agent API: http://0.0.0.0:{args.port}/api/agent/")
    print(f"  Database:  {DB_PATH}\n")

    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
