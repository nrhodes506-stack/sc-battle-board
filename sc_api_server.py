"""
Star Citizen Killboard — API Server (Proof of Concept)
=======================================================
A simple REST API that receives kill events from the log parser,
stores them in a local SQLite database, and serves them back
to a frontend website.

Endpoints:
    POST /kills              — Submit a new kill (called by the log parser)
    GET  /kills              — Get recent kills (with optional filters)
    GET  /kills/<id>         — Get a single kill by ID
    GET  /players/<name>     — Get a player's stats and recent kills
    GET  /leaderboard        — Top killers ranked by kill count

Usage:
    pip install flask
    python sc_api_server.py

Then test it with:
    curl http://localhost:8000/kills
    curl http://localhost:8000/leaderboard

Or run the log parser alongside it:
    python sc_log_parser.py --demo --api http://localhost:8000/kills
"""

import sqlite3
import json
import os
from datetime import datetime, timezone
from flask import Flask, request, jsonify, g

# ---------------------------------------------------------------------------
# SETUP
# ---------------------------------------------------------------------------

app = Flask(__name__)
DATABASE = "killboard.db"   # SQLite file — stores everything locally


# ---------------------------------------------------------------------------
# DATABASE HELPERS
# ---------------------------------------------------------------------------

def get_db():
    """Get (or create) a database connection for this request."""
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row   # Rows behave like dicts
    return g.db


@app.teardown_appcontext
def close_db(error):
    """Close the database connection at the end of each request."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """
    Create the database tables if they don't exist yet.
    Called once when the server starts.
    """
    db = sqlite3.connect(DATABASE)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS kills (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            killer      TEXT    NOT NULL,
            victim      TEXT    NOT NULL,
            weapon      TEXT    NOT NULL,
            zone        TEXT    NOT NULL,
            timestamp   TEXT    NOT NULL,   -- From the game log
            received_at TEXT    NOT NULL,   -- When our API received it
            assists     TEXT    NOT NULL DEFAULT '[]',  -- JSON array of assist records
            raw         TEXT                -- Original log line for debugging
        );

        -- Index the columns we'll filter and sort by most often
        CREATE INDEX IF NOT EXISTS idx_kills_killer      ON kills(killer);
        CREATE INDEX IF NOT EXISTS idx_kills_victim      ON kills(victim);
        CREATE INDEX IF NOT EXISTS idx_kills_zone        ON kills(zone);
        CREATE INDEX IF NOT EXISTS idx_kills_received_at ON kills(received_at);
    """)
    db.commit()
    db.close()
    print(f"[*] Database ready: {DATABASE}")


def row_to_dict(row):
    """Convert a sqlite3.Row to a plain dict, parsing assists JSON."""
    d = dict(row)
    if "assists" in d and isinstance(d["assists"], str):
        try:
            d["assists"] = json.loads(d["assists"])
        except (json.JSONDecodeError, TypeError):
            d["assists"] = []
    return d


# ---------------------------------------------------------------------------
# VALIDATION
# ---------------------------------------------------------------------------

REQUIRED_KILL_FIELDS = ["killer", "victim", "weapon", "zone", "timestamp"]
MAX_FIELD_LENGTH = 100


def validate_kill(data: dict) -> tuple[bool, str]:
    """
    Basic validation on an incoming kill payload.
    Returns (is_valid, error_message).
    """
    if not isinstance(data, dict):
        return False, "Request body must be a JSON object"

    for field in REQUIRED_KILL_FIELDS:
        if field not in data:
            return False, f"Missing required field: '{field}'"
        if not isinstance(data[field], str) or not data[field].strip():
            return False, f"Field '{field}' must be a non-empty string"
        if len(data[field]) > MAX_FIELD_LENGTH:
            return False, f"Field '{field}' exceeds maximum length of {MAX_FIELD_LENGTH}"

    # Basic sanity check: killer and victim shouldn't be the same
    if data["killer"].strip().lower() == data["victim"].strip().lower():
        return False, "Killer and victim cannot be the same player"

    return True, ""


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Health check — confirms the API is running."""
    return jsonify({
        "status": "online",
        "message": "SC Killboard API is running",
        "endpoints": {
            "POST /kills":           "Submit a kill event",
            "GET  /kills":           "List recent kills",
            "GET  /kills/<id>":      "Get a single kill",
            "GET  /players/<name>":  "Get player stats",
            "GET  /leaderboard":     "Top killers",
        }
    })


# ---------------------------------------------------------------------------

@app.route("/kills", methods=["POST"])
def submit_kill():
    """
    Receive a kill event from the log parser and store it.

    Expected JSON body:
        {
            "killer":    "PlayerKiller99",
            "victim":    "PlayerVictim01",
            "weapon":    "BEHR_Ballistic_Repeater_S3",
            "zone":      "Pyro_I",
            "timestamp": "2026-06-11T14:23:01.123Z",
            "raw":       "<original log line>"   (optional)
        }
    """
    data = request.get_json(silent=True)

    if data is None:
        return jsonify({"error": "Request body must be valid JSON"}), 400

    is_valid, error = validate_kill(data)
    if not is_valid:
        return jsonify({"error": error}), 400

    db = get_db()

    # Simple duplicate check: same killer+victim+timestamp already stored?
    existing = db.execute(
        "SELECT id FROM kills WHERE killer=? AND victim=? AND timestamp=?",
        (data["killer"], data["victim"], data["timestamp"])
    ).fetchone()

    if existing:
        return jsonify({
            "message": "Kill already recorded",
            "id": existing["id"]
        }), 200

    # Validate and serialise assists (optional field)
    assists = data.get("assists", [])
    if not isinstance(assists, list):
        assists = []
    assists_json = json.dumps(assists)

    # Insert the new kill
    cursor = db.execute(
        """INSERT INTO kills (killer, victim, weapon, zone, timestamp, received_at, assists, raw)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data["killer"].strip(),
            data["victim"].strip(),
            data["weapon"].strip(),
            data["zone"].strip(),
            data["timestamp"].strip(),
            datetime.now(timezone.utc).isoformat(),
            assists_json,
            data.get("raw", ""),
        )
    )
    db.commit()

    print(f"  [+] Kill recorded: {data['killer']} → {data['victim']} ({data['zone']})")

    return jsonify({
        "message": "Kill recorded",
        "id": cursor.lastrowid
    }), 201


# ---------------------------------------------------------------------------

@app.route("/kills", methods=["GET"])
def list_kills():
    """
    Return a paginated list of recent kills.

    Query parameters (all optional):
        killer  — filter by killer name
        victim  — filter by victim name
        zone    — filter by zone/system
        limit   — number of results (default 50, max 200)
        offset  — for pagination (default 0)
    """
    killer = request.args.get("killer")
    victim = request.args.get("victim")
    zone   = request.args.get("zone")
    limit  = min(int(request.args.get("limit",  50)), 200)
    offset = max(int(request.args.get("offset",  0)),   0)

    # Build the query dynamically based on which filters were provided
    conditions = []
    params     = []

    if killer:
        conditions.append("LOWER(killer) = LOWER(?)")
        params.append(killer)
    if victim:
        conditions.append("LOWER(victim) = LOWER(?)")
        params.append(victim)
    if zone:
        conditions.append("LOWER(zone) LIKE LOWER(?)")
        params.append(f"%{zone}%")

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params += [limit, offset]

    db = get_db()
    rows = db.execute(
        f"""SELECT id, killer, victim, weapon, zone, timestamp, received_at
            FROM kills
            {where_clause}
            ORDER BY received_at DESC
            LIMIT ? OFFSET ?""",
        params
    ).fetchall()

    # Also get total count for pagination info
    count_row = db.execute(
        f"SELECT COUNT(*) as total FROM kills {where_clause}",
        params[:-2]   # Exclude limit/offset
    ).fetchone()

    return jsonify({
        "kills":  [row_to_dict(r) for r in rows],
        "total":  count_row["total"],
        "limit":  limit,
        "offset": offset,
    })


# ---------------------------------------------------------------------------

@app.route("/kills/<int:kill_id>", methods=["GET"])
def get_kill(kill_id):
    """Return a single kill by its ID."""
    db  = get_db()
    row = db.execute("SELECT * FROM kills WHERE id=?", (kill_id,)).fetchone()

    if row is None:
        return jsonify({"error": f"Kill #{kill_id} not found"}), 404

    return jsonify(row_to_dict(row))


# ---------------------------------------------------------------------------

@app.route("/players/<string:name>", methods=["GET"])
def get_player(name):
    """
    Return stats and recent kills for a named player.
    Stats include: total kills, total deaths, K/D ratio, favourite weapon.
    """
    db = get_db()

    kills_rows = db.execute(
        """SELECT id, killer, victim, weapon, zone, timestamp
           FROM kills WHERE LOWER(killer) = LOWER(?)
           ORDER BY received_at DESC LIMIT 20""",
        (name,)
    ).fetchall()

    deaths_rows = db.execute(
        """SELECT id, killer, victim, weapon, zone, timestamp
           FROM kills WHERE LOWER(victim) = LOWER(?)
           ORDER BY received_at DESC LIMIT 20""",
        (name,)
    ).fetchall()

    total_kills  = db.execute(
        "SELECT COUNT(*) as c FROM kills WHERE LOWER(killer) = LOWER(?)", (name,)
    ).fetchone()["c"]

    total_deaths = db.execute(
        "SELECT COUNT(*) as c FROM kills WHERE LOWER(victim) = LOWER(?)", (name,)
    ).fetchone()["c"]

    # Most used weapon
    fav_weapon_row = db.execute(
        """SELECT weapon, COUNT(*) as c FROM kills
           WHERE LOWER(killer) = LOWER(?)
           GROUP BY weapon ORDER BY c DESC LIMIT 1""",
        (name,)
    ).fetchone()

    # Count kills where this player appears as an assist
    all_kills = db.execute("SELECT assists FROM kills").fetchall()
    total_assists = sum(
        1 for row in all_kills
        for a in json.loads(row["assists"] or "[]")
        if a.get("player", "").lower() == name.lower()
    )

    kd_ratio = round(total_kills / total_deaths, 2) if total_deaths > 0 else total_kills

    return jsonify({
        "player": name,
        "stats": {
            "kills":            total_kills,
            "deaths":           total_deaths,
            "assists":          total_assists,
            "kd_ratio":         kd_ratio,
            "favourite_weapon": fav_weapon_row["weapon"] if fav_weapon_row else None,
        },
        "recent_kills":  [row_to_dict(r) for r in kills_rows],
        "recent_deaths": [row_to_dict(r) for r in deaths_rows],
    })


# ---------------------------------------------------------------------------

@app.route("/leaderboard", methods=["GET"])
def leaderboard():
    """
    Return the top killers ranked by kill count.

    Query parameters:
        limit — number of players to return (default 10, max 100)
    """
    limit = min(int(request.args.get("limit", 10)), 100)

    db   = get_db()
    rows = db.execute(
        """SELECT killer as player, COUNT(*) as kills
           FROM kills
           GROUP BY LOWER(killer)
           ORDER BY kills DESC
           LIMIT ?""",
        (limit,)
    ).fetchall()

    leaderboard_data = []
    for rank, row in enumerate(rows, start=1):
        leaderboard_data.append({
            "rank":   rank,
            "player": row["player"],
            "kills":  row["kills"],
        })

    return jsonify({
        "leaderboard": leaderboard_data,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/leaderboard/assists", methods=["GET"])
def assists_leaderboard():
    """
    Return the top players ranked by assist count.
    Scans the assists JSON field on every kill to tally up who
    helped the most without getting the final blow.
    """
    limit = min(int(request.args.get("limit", 10)), 100)

    db   = get_db()
    rows = db.execute("SELECT assists FROM kills").fetchall()

    tally: dict[str, int] = {}
    for row in rows:
        try:
            assists = json.loads(row["assists"] or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        for a in assists:
            player = a.get("player", "").strip()
            if player:
                tally[player] = tally.get(player, 0) + 1

    sorted_players = sorted(tally.items(), key=lambda x: x[1], reverse=True)[:limit]

    return jsonify({
        "leaderboard": [
            {"rank": i+1, "player": p, "assists": c}
            for i, (p, c) in enumerate(sorted_players)
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# START
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    print("[*] Starting SC Killboard API on http://localhost:8000")
    print("[*] Press Ctrl+C to stop\n")
    port = int(os.environ.get("PORT", 8000))
app.run(host="0.0.0.0", port=port, debug=False)
