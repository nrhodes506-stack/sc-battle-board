"""
Star Citizen Kill Log Parser — with Assist Tracking + System Tray
==================================================================
Watches Game.log in real time, captures kills and assists, and
submits them to the SC-Battle-Board API.

Runs as a system tray application — look for the icon near the clock.
Right-click the tray icon to open the website, check status, or exit.

Requirements:
    pip install requests pystray pillow
"""

import re
import time
import json
import argparse
import os
import threading
import webbrowser
from datetime import datetime, timezone, timedelta
from collections import defaultdict, deque

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

DEFAULT_LOG_PATH = os.path.expandvars(
    r"%USERPROFILE%\AppData\Roberts Space Industries\StarCitizen\LIVE\Game.log"
)

DEFAULT_API_URL = "https://sc-battle-board-production.up.railway.app/kills"
WEBSITE_URL     = "https://sc-battle-board.vercel.app"

DEFAULT_ASSIST_WINDOW_SECONDS = 60

# ---------------------------------------------------------------------------
# GLOBAL STATE (shared between tray and parser threads)
# ---------------------------------------------------------------------------

state = {
    "kills":        0,
    "last_kill":    "None yet",
    "status":       "Starting...",
    "running":      True,
}

# ---------------------------------------------------------------------------
# LOG LINE PATTERNS
# ---------------------------------------------------------------------------

KILL_PATTERN = re.compile(
    r"<(?P<timestamp>[^>]+)>"
    r".*?<Actor Death>.*?CActor::Kill"
    r".*?'(?P<victim>[^']+)'"
    r".*?killed by\s+'(?P<killer>[^']+)'"
    r".*?using\s+'(?P<weapon>[^']+)'"
    r".*?in zone\s+'(?P<zone>[^']+)'"
)

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

def parse_timestamp(ts_str: str):
    try:
        clean = ts_str.rstrip("Z").split(".")[0]
        dt = datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None

# ---------------------------------------------------------------------------
# DAMAGE BUFFER
# ---------------------------------------------------------------------------

class DamageBuffer:
    def __init__(self, window_seconds: int = DEFAULT_ASSIST_WINDOW_SECONDS):
        self.window = timedelta(seconds=window_seconds)
        self.events: dict[str, deque] = defaultdict(deque)

    def add(self, event: dict):
        self.events[event["victim"].lower()].append(event)

    def get_assists(self, victim: str, kill_time: datetime, killer: str) -> list:
        victim_key = victim.lower()
        killer_key = killer.lower()
        if victim_key not in self.events:
            return []
        cutoff = kill_time - self.window
        totals: dict[str, dict] = {}
        for ev in self.events[victim_key]:
            ev_time = ev.get("_dt")
            if ev_time is None or ev_time < cutoff:
                continue
            attacker = ev["attacker"]
            ak = attacker.lower()
            if ak == killer_key:
                continue
            if ak not in totals:
                totals[ak] = {"player": attacker, "hits": 0, "total_damage": 0.0}
            totals[ak]["hits"]         += 1
            totals[ak]["total_damage"] += ev.get("amount", 0.0)
        assists = sorted(totals.values(), key=lambda x: x["total_damage"], reverse=True)
        for a in assists:
            a["total_damage"] = round(a["total_damage"], 1)
        return assists

    def prune(self, now: datetime):
        cutoff = now - self.window
        for k in list(self.events.keys()):
            q = self.events[k]
            while q and (q[0].get("_dt") or datetime.min.replace(tzinfo=timezone.utc)) < cutoff:
                q.popleft()
            if not q:
                del self.events[k]

# ---------------------------------------------------------------------------
# LINE PARSERS
# ---------------------------------------------------------------------------

def parse_kill(line: str):
    m = KILL_PATTERN.search(line)
    if not m:
        return None
    return {
        "timestamp": m.group("timestamp"),
        "killer":    m.group("killer"),
        "victim":    m.group("victim"),
        "weapon":    m.group("weapon"),
        "zone":      m.group("zone"),
        "assists":   [],
        "raw":       line.strip(),
        "parsed_at": datetime.now(timezone.utc).isoformat(),
    }

def parse_damage(line: str):
    m = DAMAGE_PATTERN.search(line)
    if not m:
        return None
    return {
        "timestamp": m.group("timestamp"),
        "_dt":       parse_timestamp(m.group("timestamp")),
        "victim":    m.group("victim"),
        "attacker":  m.group("attacker"),
        "weapon":    m.group("weapon"),
        "amount":    float(m.group("amount")),
    }

# ---------------------------------------------------------------------------
# SUBMISSION
# ---------------------------------------------------------------------------

def submit_kill(kill: dict, api_url: str) -> bool:
    if not HAS_REQUESTS:
        return False
    try:
        payload = {k: v for k, v in kill.items() if not k.startswith("_")}
        r = requests.post(api_url, json=payload, timeout=5,
                          headers={"Content-Type": "application/json"})
        r.raise_for_status()
        return True
    except requests.RequestException:
        return False

def save_kill_locally(kill: dict, output_file: str = "kills.json"):
    try:
        kills = []
        if os.path.exists(output_file):
            with open(output_file, "r") as f:
                kills = json.load(f)
        kills.append(kill)
        with open(output_file, "w") as f:
            json.dump(kills, f, indent=2)
    except (json.JSONDecodeError, IOError):
        pass

# ---------------------------------------------------------------------------
# SYSTEM TRAY ICON
# ---------------------------------------------------------------------------

def make_tray_icon():
    """
    Draw a simple icon — a red crosshair on dark background.
    This appears in the system tray near the clock.
    """
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Dark circle background
    draw.ellipse([2, 2, size-2, size-2], fill=(15, 20, 30, 255))

    # Crosshair lines in SC orange/gold
    cx, cy = size // 2, size // 2
    colour = (232, 160, 32, 255)
    draw.line([cx, 4,      cx, cy-8],    fill=colour, width=3)
    draw.line([cx, cy+8,   cx, size-4],  fill=colour, width=3)
    draw.line([4,  cy,     cx-8, cy],    fill=colour, width=3)
    draw.line([cx+8, cy,   size-4, cy],  fill=colour, width=3)

    # Small centre dot
    draw.ellipse([cx-4, cy-4, cx+4, cy+4], fill=colour)

    return img


def build_tray_menu(tray_icon):
    """Build the right-click menu shown when you click the tray icon."""

    def open_website(icon, item):
        webbrowser.open(WEBSITE_URL)

    def show_status(icon, item):
        # pystray doesn't support popups natively on all platforms,
        # so we update the tooltip which shows on hover
        icon.title = (
            f"SC-Battle-Board\n"
            f"Status : {state['status']}\n"
            f"Kills  : {state['kills']}\n"
            f"Last   : {state['last_kill']}"
        )

    def exit_app(icon, item):
        state["running"] = False
        icon.stop()

    return pystray.Menu(
        pystray.MenuItem("SC-Battle-Board", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda _: f"Status: {state['status']}", None, enabled=False),
        pystray.MenuItem(lambda _: f"Kills submitted: {state['kills']}", None, enabled=False),
        pystray.MenuItem(lambda _: f"Last kill: {state['last_kill']}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open SC-Battle-Board", open_website),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", exit_app),
    )


def run_tray(tray_icon):
    """Run the tray icon — this blocks until the user clicks Exit."""
    tray_icon.run()

# ---------------------------------------------------------------------------
# LOG WATCHER
# ---------------------------------------------------------------------------

def watch_log(log_path: str, api_url: str, window_seconds: int):
    """
    Watch Game.log and process kill/damage events.
    Updates the shared `state` dict so the tray menu stays current.
    """
    if not os.path.exists(log_path):
        state["status"] = "Log not found — is SC running?"
        return

    state["status"] = "Watching for kills..."
    buffer       = DamageBuffer(window_seconds)
    prune_counter = 0

    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)

        while state["running"]:
            line = f.readline()
            if not line:
                time.sleep(0.2)
                continue

            damage = parse_damage(line)
            if damage:
                buffer.add(damage)
                prune_counter += 1
                if prune_counter >= 500:
                    buffer.prune(datetime.now(timezone.utc))
                    prune_counter = 0
                continue

            kill = parse_kill(line)
            if kill:
                kill_time    = parse_timestamp(kill["timestamp"]) or datetime.now(timezone.utc)
                kill["assists"] = buffer.get_assists(kill["victim"], kill_time, kill["killer"])

                ok = submit_kill(kill, api_url)
                save_kill_locally(kill)

                state["kills"]     += 1
                state["last_kill"]  = f"{kill['killer']} → {kill['victim']}"
                state["status"]     = "✓ Kill submitted" if ok else "⚠ Submit failed"

# ---------------------------------------------------------------------------
# DEMO MODE
# ---------------------------------------------------------------------------

def run_demo(window_seconds: int, api_url: str):
    print("=" * 60)
    print("  DEMO MODE")
    print("=" * 60)

    base = "2026-06-11T14:23"
    demo_lines = [
        (f"<{base}:00.000Z> [Notice] CDamageManager::HandleDamage: "
         f"'PlayerVictim01' took 312.5 damage from 'Aegis_Warden' "
         f"using 'BEHR_FS_S3' [Ballistic]", 0),
        (f"<{base}:20.000Z> [Notice] CDamageManager::HandleDamage: "
         f"'PlayerVictim01' took 287.3 damage from 'Aegis_Warden' "
         f"using 'BEHR_FS_S3' [Ballistic]", 20),
        (f"<{base}:30.000Z> [Notice] <Actor Death> CActor::Kill: "
         f"'PlayerVictim01' [123] killed by 'PirateKing42' [456] "
         f"using 'KRIG_Ballistic_Cannon_S4' [Ballistic] in zone 'Pyro_I'", 30),
        (f"<{base}:50.000Z> [Notice] <Actor Death> CActor::Kill: "
         f"'LoneWolf_Pilot' [789] killed by 'NovaSerpent' [321] "
         f"using 'TALN_Combine_S3' [Ballistic] in zone 'Stanton_ArcCorp'", 50),
    ]

    buffer = DamageBuffer(window_seconds)
    base_time = datetime(2026, 6, 11, 14, 23, 0, tzinfo=timezone.utc)

    for line, offset in demo_lines:
        print(f"\n[T+{offset:>3}s] {line[:80]}...")
        damage = parse_damage(line)
        if damage:
            buffer.add(damage)
            print(f"         → Damage: {damage['attacker']} hit {damage['victim']} for {damage['amount']} dmg")
            continue
        kill = parse_kill(line)
        if kill:
            kill_time = parse_timestamp(kill["timestamp"]) or base_time
            kill["assists"] = buffer.get_assists(kill["victim"], kill_time, kill["killer"])
            print(f"\n  KILL: {kill['killer']} → {kill['victim']} ({kill['zone']})")
            for a in kill["assists"]:
                print(f"  Assist: {a['player']} ({a['hits']} hits, {a['total_damage']} dmg)")
            ok = submit_kill(kill, api_url)
            print(f"  API: {'✓ Submitted' if ok else '✗ Failed / offline'}")

    print("\n[*] Demo complete.")

# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="SC-Battle-Board parser")
    p.add_argument("--log",    default=DEFAULT_LOG_PATH)
    p.add_argument("--api",    default=DEFAULT_API_URL)
    p.add_argument("--demo",   action="store_true")
    p.add_argument("--window", type=int, default=DEFAULT_ASSIST_WINDOW_SECONDS)
    args = p.parse_args()

    if args.demo:
        run_demo(args.window, args.api)

    elif HAS_TRAY:
        # Run parser in background thread, tray icon on main thread
        parser_thread = threading.Thread(
            target=watch_log,
            args=(args.log, args.api, args.window),
            daemon=True,
        )
        parser_thread.start()

        icon = pystray.Icon(
            name  = "SC-Battle-Board",
            icon  = make_tray_icon(),
            title = "SC-Battle-Board — Running",
            menu  = None,
        )
        icon.menu = build_tray_menu(icon)
        run_tray(icon)

    else:
        # Fallback: no tray, just run in console
        print("[*] pystray not installed — running in console mode")
        print("[*] Press Ctrl+C to stop\n")
        try:
            watch_log(args.log, args.api, args.window)
        except KeyboardInterrupt:
            print("\n[*] Stopped.")
