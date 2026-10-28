"""
F1 race results -> Discord webhook, using the OpenF1 API (free tier).
Built to run on GitHub Actions: each run checks once, posts if there's
a new race result, then exits.
"""

import json
import os
from datetime import datetime, timezone

import requests

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
API = "https://api.openf1.org/v1"
POSTED_FILE = "posted.json"   # remembers which races were already posted


def load_posted():
    try:
        with open(POSTED_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_posted(posted):
    with open(POSTED_FILE, "w") as f:
        json.dump(sorted(posted), f)


def get(endpoint, **params):
    """Call an OpenF1 endpoint. Returns None if the data isn't available yet."""
    r = requests.get(f"{API}/{endpoint}", params=params, timeout=30)
    if r.status_code != 200:
        return None
    return r.json()


def latest_finished_race():
    """Find the most recent race session that has already ended."""
    now = datetime.now(timezone.utc)
    sessions = get("sessions", year=now.year, session_name="Race") or []
    finished = [
        s for s in sessions
        if not s.get("is_cancelled")
        and datetime.fromisoformat(s["date_end"]) < now
    ]
    return max(finished, key=lambda s: s["date_end"]) if finished else None


def fmt_time(seconds):
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:06.3f}"


def fmt_gap(gap):
    if isinstance(gap, (int, float)):
        return f"+{gap:.3f}s"
    return str(gap)  # e.g. "+1 LAP"


def build_message(session, results, drivers):
    by_number = {d["driver_number"]: d for d in drivers}
    lines = []
    ordered = sorted(results, key=lambda r: r.get("position") or 99)

    for r in ordered:
        d = by_number.get(r["driver_number"], {})
        name = d.get("name_acronym", str(r["driver_number"]))
        team = (d.get("team_name") or "")[:16]

        if r.get("dsq"):
            status = "DSQ"
        elif r.get("dns"):
            status = "DNS"
        elif r.get("dnf"):
            status = "DNF"
        elif r.get("position") == 1:
            status = fmt_time(r["duration"])
        else:
            status = fmt_gap(r.get("gap_to_leader"))

        pos = str(r["position"]) if r.get("position") else "NC"
        lines.append(f"{pos:>3}  {name:<4} {team:<16} {status}")

    title = f"🏁 **{session['location']} {session['year']} — Race Result**"
    return title + "\n```\n" + "\n".join(lines) + "\n```"


def main():
    posted = load_posted()
    race = latest_finished_race()

    if not race:
        print("No finished race found.")
        return
    if race["session_key"] in posted:
        print(f"Already posted session {race['session_key']}.")
        return

    results = get("session_result", session_key=race["session_key"])
    if not results:
        print("Results not available yet. Will check again next run.")
        return

    drivers = get("drivers", session_key=race["session_key"]) or []
    message = build_message(race, results, drivers)
    resp = requests.post(WEBHOOK_URL, json={"content": message}, timeout=30)
    resp.raise_for_status()

    posted.add(race["session_key"])
    save_posted(posted)
    print(f"Posted results for session {race['session_key']}.")


if __name__ == "__main__":
    main()
