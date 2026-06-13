"""
Star Citizen Kill Log Parser — with Assist Tracking
=====================================================
Watches Game.log in real time. For every kill event it:
  1. Records the killer, victim, weapon, zone and timestamp.
  2. Looks back through a 60-second damage buffer to find anyone
     else who hit the victim before the kill — and records them
     as assists, along with how many times they landed a hit.

Usage:
    python sc_log_parser.py
    python sc_log_parser.py --log "C:/path/to/Game.log"
    python sc_log_parser.py --api http://localhost:8000/kills
    python sc_log_parser.py --demo
    python sc_log_parser.py --demo --window 30   (use a 30-second assist window)

Requirements:
    pip install requests
"""

import re
import time
import json
import argparse
import os
from datetime import datetime, timezone, timedelta
from collections import defaultdict, deque

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

DEFAULT_LOG_PATH = os.path.expandvars(
    r"%USERPROFILE%\AppData\Roberts Space Industries\StarCitizen\LIVE\Game.log"
)

# How far back (in seconds) we look for damage events when a kill happens.
# Anyone who hit the victim in this window gets credited as an assist.
DEFAULT_ASSIST_WINDOW_SECONDS = 60

# ---------------------------------------------------------------------------
# LOG LINE PATTERNS
# ---------------------------------------------------------------------------
#
# IMPORTANT NOTE FOR FUTURE MAINTENANCE:
# ----------------------------------------
# These patterns are based on community-documented Game.log formats.
# CIG can change the log format in any patch. If kills or damage events
# stop being captured after a patch, the fix is usually here — inspect
# a fresh Game.log and adjust the regex groups to match what you see.
#
# Both patterns expect a timestamp at the start of the line in the form:
#   <2026-06-11T14:23:01.123Z>

# --- Kill event ---
# Example line:
#   <2026-06-11T14:23:01.123Z> [Notice] <Actor Death> CActor::Kill:
#       'Victim_Name' [123] killed by 'Killer_Name' [456]
#       using 'Weapon_Name' [Ballistic] in zone 'Pyro_I'
KILL_PATTERN = re.compile(
    r"<(?P<timestamp>[^>]+)>"
    r".*?<Actor Death>.*?CActor::Kill"
    r".*?'(?P<victim>[^']+)'"
    r".*?killed by\s+'(?P<killer>[^']+)'"
    r".*?using\s+'(?P<weapon>[^']+)'"
    r".*?in zone\s+'(?P<zone>[^']+)'"
)

# --- Damage event ---
# Example line:
#   <2026-06-11T14:22:55.001Z> [Notice] CDamageManager::HandleDamage:
#       'Victim_Name' took 245.3 damage from 'Attacker_Name'
#       using 'Weapon_Name' [Ballistic]
#
# We capture: who was damaged, by whom, with what, and how much.
DAMAGE_PATTERN = re.compile(
    r"<(?P<timestamp>[^>]+)>"
    r".*?CDamageManager::HandleDamage"
    r".*?'(?P<victim>[^']+)'\s+took\s+(?P<amount>[\d.]+)\s+damage"
    r".*?from\s+'(?P<attacker>[^']+)'"
    r".*?using\s+'(?P<weapon>[^']+)'"
)

# ---------------------------------------------------------------------------
# TIMESTAMP PARSING
# ---------------------------------------------------------------------------

def parse_timestamp(ts_str: str) -> datetime | None:
    """
    Parse a timestamp string from the log into a Python datetime.
    Handles the ISO 8601 format Star Citizen uses: 2026-06-11T14:23:01.123Z
    Returns None if parsing fails — we never want to crash on a bad timestamp.
    """
    try:
        # Strip trailing Z and parse; treat as UTC
        clean = ts_str.rstrip("Z").split(".")[0]  # Remove milliseconds
        dt = datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None

# ---------------------------------------------------------------------------
# DAMAGE BUFFER
# ---------------------------------------------------------------------------

class DamageBuffer:
    """
    A rolling time-window buffer of damage events.

    Every time a damage line is parsed from the log, it's added here.
    When a kill happens, we call get_assists() to find everyone who
    hit the victim in the last N seconds (excluding the killer themselves).

    Old events are automatically pruned to keep memory usage flat.
    """

    def __init__(self, window_seconds: int = DEFAULT_ASSIST_WINDOW_SECONDS):
        self.window = timedelta(seconds=window_seconds)
        # damage_events[victim_name] = deque of damage dicts, oldest first
        self.events: dict[str, deque] = defaultdict(deque)

    def add(self, event: dict):
        """Record a damage event. event must have 'victim' and 'timestamp' keys."""
        victim = event["victim"].lower()
        self.events[victim].append(event)

    def get_assists(self, victim: str, kill_time: datetime, killer: str) -> list[dict]:
        """
        Return a list of assist records for everyone who damaged `victim`
        in the window leading up to `kill_time`, excluding the killer.

        Each assist record looks like:
            { "player": "PlayerName", "hits": 3, "total_damage": 487.2 }
        """
        victim_key = victim.lower()
        killer_key = killer.lower()

        if victim_key not in self.events:
            return []

        cutoff = kill_time - self.window
        assist_totals: dict[str, dict] = {}   # player_lower → { hits, damage }

        for ev in self.events[victim_key]:
            ev_time = ev.get("_dt")
            if ev_time is None or ev_time < cutoff:
                continue
            attacker = ev["attacker"]
            attacker_key = attacker.lower()

            # Don't credit the killer as their own assist
            if attacker_key == killer_key:
                continue

            if attacker_key not in assist_totals:
                assist_totals[attacker_key] = {
                    "player": attacker,
                    "hits": 0,
                    "total_damage": 0.0,
                }
            assist_totals[attacker_key]["hits"]         += 1
            assist_totals[attacker_key]["total_damage"] += ev.get("amount", 0.0)

        # Sort by total damage contributed, highest first
        assists = sorted(
            assist_totals.values(),
            key=lambda x: x["total_damage"],
            reverse=True,
        )

        # Round damage for cleaner output
        for a in assists:
            a["total_damage"] = round(a["total_damage"], 1)

        return assists

    def prune(self, now: datetime):
        """
        Remove events older than the assist window from all victim queues.
        Call this periodically to stop the buffer growing indefinitely.
        """
        cutoff = now - self.window
        for victim_key in list(self.events.keys()):
            q = self.events[victim_key]
            while q and (q[0].get("_dt") or datetime.min.replace(tzinfo=timezone.utc)) < cutoff:
                q.popleft()
            if not q:
                del self.events[victim_key]

# ---------------------------------------------------------------------------
# LINE PARSERS
# ---------------------------------------------------------------------------

def parse_kill(line: str) -> dict | None:
    """Extract a kill event from a log line. Returns None if not a kill line."""
    match = KILL_PATTERN.search(line)
    if not match:
        return None

    return {
        "timestamp": match.group("timestamp"),
        "killer":    match.group("killer"),
        "victim":    match.group("victim"),
        "weapon":    match.group("weapon"),
        "zone":      match.group("zone"),
        "assists":   [],   # Populated by the watcher after checking the buffer
        "raw":       line.strip(),
        "parsed_at": datetime.now(timezone.utc).isoformat(),
    }


def parse_damage(line: str) -> dict | None:
    """Extract a damage event from a log line. Returns None if not a damage line."""
    match = DAMAGE_PATTERN.search(line)
    if not match:
        return None

    dt = parse_timestamp(match.group("timestamp"))
    return {
        "timestamp": match.group("timestamp"),
        "_dt":       dt,   # Parsed datetime for window comparisons (not sent to API)
        "victim":    match.group("victim"),
        "attacker":  match.group("attacker"),
        "weapon":    match.group("weapon"),
        "amount":    float(match.group("amount")),
    }

# ---------------------------------------------------------------------------
# OUTPUT HELPERS
# ---------------------------------------------------------------------------

def format_kill(kill: dict) -> str:
    """Pretty-print a kill event (with assists) to the console."""
    assists = kill.get("assists", [])
    assist_lines = ""
    if assists:
        assist_lines = "\n" + "\n".join(
            f"  Assist : {a['player']} ({a['hits']} hits, {a['total_damage']} dmg)"
            for a in assists
        )
    else:
        assist_lines = "\n  Assists: none"

    return (
        f"\n{'='*60}\n"
        f"  KILL DETECTED\n"
        f"{'='*60}\n"
        f"  Time   : {kill['timestamp']}\n"
        f"  Killer : {kill['killer']}\n"
        f"  Victim : {kill['victim']}\n"
        f"  Weapon : {kill['weapon']}\n"
        f"  Zone   : {kill['zone']}"
        f"{assist_lines}\n"
        f"{'='*60}"
    )


def save_kill_locally(kill: dict, output_file: str = "kills.json"):
    """Append a kill to a local JSON file as a backup."""
    try:
        kills = []
        if os.path.exists(output_file):
            with open(output_file, "r") as f:
                kills = json.load(f)
        kills.append(kill)
        with open(output_file, "w") as f:
            json.dump(kills, f, indent=2)
    except (json.JSONDecodeError, IOError) as e:
        print(f"  [!] Could not save kill locally: {e}")


def submit_kill(kill: dict, api_url: str) -> bool:
    """POST a kill event to the API server."""
    if not HAS_REQUESTS:
        print("  [!] 'requests' not installed — skipping submission.")
        return False
    try:
        # Remove internal fields before sending
        payload = {k: v for k, v in kill.items() if not k.startswith("_")}
        response = requests.post(api_url, json=payload, timeout=5,
                                 headers={"Content-Type": "application/json"})
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"  [!] API submission failed: {e}")
        return False

# ---------------------------------------------------------------------------
# LOG WATCHER
# ---------------------------------------------------------------------------

def watch_log(log_path: str, api_url: str | None = None,
              window_seconds: int = DEFAULT_ASSIST_WINDOW_SECONDS):
    """
    Tail Game.log in real time.

    Every line is checked against both patterns:
      - Damage lines → added to the rolling DamageBuffer
      - Kill lines   → assists looked up from the buffer, then submitted
    """
    print(f"[*] Watching  : {log_path}")
    print(f"[*] API       : {api_url or 'None (console only)'}")
    print(f"[*] Assist window: {window_seconds} seconds")
    print("[*] Waiting for events... (Ctrl+C to stop)\n")

    if not os.path.exists(log_path):
        print(f"[!] Log file not found: {log_path}")
        print("    Is Star Citizen installed? Check the path with --log.")
        return

    buffer = DamageBuffer(window_seconds)
    prune_counter = 0

    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)   # Start from end — only catch new events

        while True:
            line = f.readline()

            if not line:
                time.sleep(0.2)
                continue

            # --- Try damage first (more common, cheaper check) ---
            damage = parse_damage(line)
            if damage:
                buffer.add(damage)
                prune_counter += 1
                # Prune old events every 500 damage lines to keep memory tidy
                if prune_counter >= 500:
                    buffer.prune(datetime.now(timezone.utc))
                    prune_counter = 0
                continue

            # --- Then try kill ---
            kill = parse_kill(line)
            if kill:
                kill_time = parse_timestamp(kill["timestamp"]) or datetime.now(timezone.utc)
                kill["assists"] = buffer.get_assists(
                    victim=kill["victim"],
                    kill_time=kill_time,
                    killer=kill["killer"],
                )
                print(format_kill(kill))
                save_kill_locally(kill)

                if api_url:
                    ok = submit_kill(kill, api_url)
                    print(f"  API: {'✓ Submitted' if ok else '✗ Failed'}")

# ---------------------------------------------------------------------------
# DEMO MODE
# ---------------------------------------------------------------------------
#
# Simulates a realistic sequence of log lines:
#   several players damage a target → one lands the killing blow
# This lets you see assist tracking in action without running the game.

def build_demo_lines(window_seconds: int) -> list[tuple[str, int]]:
    """
    Return (log_line, delay_seconds_from_start) pairs that simulate
    a fight with assists.
    """
    # All timestamps relative to a fixed base
    base = "2026-06-11T14:23"

    lines = [
        # -- Fight 1: 3 players attack Victim01, PirateKing42 gets the kill --
        # Damage events leading up to the kill
        (f"<{base}:00.000Z> [Notice] CDamageManager::HandleDamage: "
         f"'PlayerVictim01' took 312.5 damage from 'Aegis_Warden' "
         f"using 'BEHR_FS_S3' [Ballistic]", 0),

        (f"<{base}:10.000Z> [Notice] CDamageManager::HandleDamage: "
         f"'PlayerVictim01' took 198.0 damage from 'VoidRunner_77' "
         f"using 'MNVR_Distortion_S2' [Distortion]", 10),

        (f"<{base}:20.000Z> [Notice] CDamageManager::HandleDamage: "
         f"'PlayerVictim01' took 287.3 damage from 'Aegis_Warden' "
         f"using 'BEHR_FS_S3' [Ballistic]", 20),

        (f"<{base}:25.000Z> [Notice] CDamageManager::HandleDamage: "
         f"'PlayerVictim01' took 155.0 damage from 'VoidRunner_77' "
         f"using 'MNVR_Distortion_S2' [Distortion]", 25),

        # Kill lands at 30 seconds — both helpers should be credited
        (f"<{base}:30.000Z> [Notice] <Actor Death> CActor::Kill: "
         f"'PlayerVictim01' [123] killed by 'PirateKing42' [456] "
         f"using 'KRIG_Ballistic_Cannon_S4' [Ballistic] in zone 'Pyro_I'", 30),

        # -- Non-kill line (should be silently ignored) --
        (f"<{base}:32.000Z> [Notice] Entity 'MISC_Freelancer' spawned at (1234, 5678)", 32),

        # -- Fight 2: solo kill, no assists --
        (f"<{base}:50.000Z> [Notice] <Actor Death> CActor::Kill: "
         f"'LoneWolf_Pilot' [789] killed by 'NovaSerpent' [321] "
         f"using 'TALN_Combine_S3' [Ballistic] in zone 'Stanton_ArcCorp'", 50),

        # -- Fight 3: damage from OUTSIDE the assist window, should NOT count --
        # Damage happens at T+55, kill at T+55+window+5 (outside window)
        (f"<{base}:55.000Z> [Notice] CDamageManager::HandleDamage: "
         f"'FarAway_Victim' took 400.0 damage from 'OldAttacker' "
         f"using 'BEHR_FS_S3' [Ballistic]", 55),
    ]

    # Add the kill for fight 3 just outside the assist window
    outside_offset = 55 + window_seconds + 5
    mins, secs = divmod(outside_offset, 60)
    lines.append((
        f"<2026-06-11T14:{23+mins:02d}:{secs:02d}.000Z> [Notice] <Actor Death> CActor::Kill: "
        f"'FarAway_Victim' [999] killed by 'LateKiller' [111] "
        f"using 'BEHR_FS_S3' [Ballistic] in zone 'Hurston'",
        outside_offset
    ))

    return lines


def run_demo(window_seconds: int = DEFAULT_ASSIST_WINDOW_SECONDS,
             api_url: str | None = None):
    """
    Replay simulated log lines through the full parser pipeline,
    including the damage buffer and assist detection.
    """
    print("=" * 60)
    print("  DEMO MODE — simulating a fight with assists")
    print(f"  Assist window: {window_seconds} seconds")
    print("=" * 60)

    demo_lines = build_demo_lines(window_seconds)
    buffer = DamageBuffer(window_seconds)
    kills_found = 0

    # Use a fake "now" that advances with each line's offset
    base_time = datetime(2026, 6, 11, 14, 23, 0, tzinfo=timezone.utc)

    for line, offset in demo_lines:
        fake_now = base_time + timedelta(seconds=offset)
        print(f"\n[T+{offset:>3}s] {line[:90]}...")

        damage = parse_damage(line)
        if damage:
            buffer.add(damage)
            print(f"         → Damage logged: {damage['attacker']} hit "
                  f"{damage['victim']} for {damage['amount']} dmg")
            continue

        kill = parse_kill(line)
        if kill:
            kills_found += 1
            kill_time = parse_timestamp(kill["timestamp"]) or fake_now
            kill["assists"] = buffer.get_assists(
                victim=kill["victim"],
                kill_time=kill_time,
                killer=kill["killer"],
            )
            print(format_kill(kill))
            save_kill_locally(kill, "demo_kills.json")

            if api_url:
                ok = submit_kill(kill, api_url)
                print(f"  API: {'✓ Submitted' if ok else '✗ Failed'}")
            continue

        print("         → Ignored (not a kill or damage event)")

    print(f"\n[*] Demo complete. {kills_found} kill(s) processed.")
    print("[*] Results saved to demo_kills.json")

# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="SC-Battle-Board log parser with assist tracking"
    )
    p.add_argument("--log",    default=DEFAULT_LOG_PATH,
                   help="Path to Game.log")
    p.add_argument("--api",    default=None,
                   help="API endpoint to POST kills to")
    p.add_argument("--demo",   action="store_true",
                   help="Run demo mode with simulated log data")
    p.add_argument("--window", type=int, default=DEFAULT_ASSIST_WINDOW_SECONDS,
                   help=f"Assist time window in seconds (default: {DEFAULT_ASSIST_WINDOW_SECONDS})")
    args = p.parse_args()

    try:
        if args.demo:
            run_demo(window_seconds=args.window, api_url=args.api)
        else:
            watch_log(log_path=args.log, api_url=args.api,
                      window_seconds=args.window)
    except KeyboardInterrupt:
        print("\n\n[*] Parser stopped.")
