"""
F1 race results -> Discord webhook (as an embed), using the OpenF1 API.
Built for GitHub Actions: each run checks once, posts if there's a new
race result, then exits.
"""

import json
import os
from datetime import datetime, timezone

import requests

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
FORCE = os.environ.get("FORCE_REPOST", "false").lower() == "true"
API = "https://api.openf1.org/v1"
POSTED_FILE = "posted.json"
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


# ---------- helpers ----------

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
    """Call an OpenF1 endpoint. Returns None if the data isn't available."""
    try:
        r = requests.get(f"{API}/{endpoint}", params=params, timeout=30)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    return r.json()


def fmt_race_time(seconds):
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:06.3f}"


def fmt_lap_time(seconds):
    m, s = divmod(seconds, 60)
    return f"{int(m)}:{s:06.3f}"


def fmt_gap(gap):
    if isinstance(gap, (int, float)):
        return f"+{gap:.3f}s"
    return str(gap)


# ---------- data ----------

def latest_finished_race():
    now = datetime.now(timezone.utc)
    sessions = get("sessions", year=now.year, session_name="Race") or []
    finished = [
        s for s in sessions
        if not s.get("is_cancelled")
        and datetime.fromisoformat(s["date_end"]) < now
    ]
    return max(finished, key=lambda s: s["date_end"]) if finished else None


def fastest_lap(session_key):
    laps = get("laps", session_key=session_key) or []
    timed = [l for l in laps if l.get("lap_duration")]
    return min(timed, key=lambda l: l["lap_duration"]) if timed else None


def championship_top5(session_key):
    rows = get("championship_drivers", session_key=session_key) or []
    rows = [r for r in rows if r.get("position_current")]
    return sorted(rows, key=lambda r: r["position_current"])[:5]


# ---------- message ----------

def status_for(r):
    if r.get("dsq"):
        return "DSQ"
    if r.get("dns"):
        return "DNS"
    if r.get("dnf"):
        return "DNF"
    if r.get("position") == 1:
        return fmt_race_time(r["duration"])
    return fmt_gap(r.get("gap_to_leader"))


def build_embed(session, results, drivers, meeting, fl, standings):
    by_num = {d["driver_number"]: d for d in drivers}
    ordered = sorted(results, key=lambda r: r.get("position") or 99)

    def acronym(num):
        return by_num.get(num, {}).get("name_acronym", str(num))

    # Podium
    podium_lines = []
    for r in ordered[:3]:
        d = by_num.get(r["driver_number"], {})
        name = f"{d.get('first_name', '')} {d.get('last_name', '')}".strip() or acronym(r["driver_number"])
        podium_lines.append(
            f"{MEDALS.get(r.get('position'), '')} **{name}** · {d.get('team_name', '')} · `{status_for(r)}`"
        )

    # Rest of the field
    table = []
    for r in ordered[3:]:
        d = by_num.get(r["driver_number"], {})
        pos = str(r["position"]) if r.get("position") else "NC"
        team = (d.get("team_name") or "")[:15]
        table.append(f"{pos:>2}  {acronym(r['driver_number']):<3}  {team:<15} {status_for(r)}")

    description = "\n".join(podium_lines)
    if table:
        description += "\n```\n" + "\n".join(table) + "\n```"

    fields = []
    if fl:
        fields.append({
            "name": "⚡ Fastest Lap",
            "value": f"**{acronym(fl['driver_number'])}** · {fmt_lap_time(fl['lap_duration'])} (lap {fl['lap_number']})",
            "inline": True,
        })
    if standings:
        lines = []
        for s in standings:
            gained = s["points_current"] - (s.get("points_start") or 0)
            lines.append(f"{s['position_current']}. **{acronym(s['driver_number'])}** {s['points_current']:g} pts (+{gained:g})")
        fields.append({"name": "🏆 Championship", "value": "\n".join(lines), "inline": True})

    winner = by_num.get(ordered[0]["driver_number"], {}) if ordered else {}
    colour = int(winner.get("team_colour") or "E10600", 16)

    title_name = (meeting or {}).get("meeting_name") or f"{session['location']} Grand Prix"
    embed = {
        "title": f"🏁 {title_name} {session['year']}",
        "description": description,
        "color": colour,
        "fields": fields,
        "footer": {"text": f"{session['circuit_short_name']} · Provisional result · Data via OpenF1"},
        "timestamp": session["date_end"],
    }
    if meeting and meeting.get("country_flag"):
        embed["thumbnail"] = {"url": meeting["country_flag"]}
    return embed


# ---------- main ----------

def main():
    posted = load_posted()
    race = latest_finished_race()

    if not race:
        print("No finished race found.")
        return
    key = race["session_key"]
    if key in posted and not FORCE:
        print(f"Already posted session {key}.")
        return

    results = get("session_result", session_key=key)
    if not results:
        print("Results not available yet. Will check again next run.")
        return

    drivers = get("drivers", session_key=key) or []
    meetings = get("meetings", meeting_key=race["meeting_key"]) or []
    meeting = meetings[0] if meetings else None
    fl = fastest_lap(key)
    standings = championship_top5(key)

    embed = build_embed(race, results, drivers, meeting, fl, standings)
    resp = requests.post(WEBHOOK_URL, json={"embeds": [embed]}, timeout=30)
    resp.raise_for_status()

    posted.add(key)
    save_posted(posted)
    print(f"Posted results for session {key}.")


if __name__ == "__main__":
    main()
