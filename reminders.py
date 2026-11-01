"""
Posts a reminder about an hour before every F1 session (practice, sprint, qualifying, race),
with the start time shown in each person's own time zone.
"""

from datetime import datetime, timedelta, timezone

from common import load_json, openf1, save_json, send, weekend_label

REMINDERS_ENABLED = True
LEAD_TIME = timedelta(minutes=70)   # remind when a session starts within this window
POSTED_FILE = "reminders_posted.json"

# Who to ping in the reminder. "@everyone", "@here", or a role like "<@&123456789012345678>".
# Set to "" for no ping.
PING = "@everyone"

# Which sessions get the ping (the others still get a reminder, just without it).
PING_SESSIONS = ["Practice 1", "Practice 2", "Practice 3",
                 "Sprint Qualifying", "Sprint Shootout", "Sprint", "Qualifying", "Race"]

ICONS = {"Race": "🏁", "Sprint": "🏁", "Qualifying": "⏱️", "Sprint Qualifying": "⏱️",
         "Sprint Shootout": "⏱️"}


def main():
    if not REMINDERS_ENABLED:
        return
    now = datetime.now(timezone.utc)
    sessions = openf1("sessions", year=now.year) or []
    done = set(load_json(POSTED_FILE, []))

    upcoming = [s for s in sessions
                if not s.get("is_cancelled")
                and s["session_key"] not in done
                and timedelta(0) < datetime.fromisoformat(s["date_start"]) - now <= LEAD_TIME]

    if not upcoming:
        print("No sessions starting in the next hour.")
        return

    for s in sorted(upcoming, key=lambda s: s["date_start"]):
        meetings = openf1("meetings", meeting_key=s["meeting_key"]) or []
        meeting_name = meetings[0]["meeting_name"] if meetings else f"{s['location']} Grand Prix"
        ts = int(datetime.fromisoformat(s["date_start"]).timestamp())
        icon = ICONS.get(s["session_name"], "🔧")
        ping = f"{PING} " if PING and s["session_name"] in PING_SESSIONS else ""
        content = (f"{ping}{icon} **{s['session_name']}** at the **{meeting_name}** starts <t:{ts}:R>\n"
                   f"🕒 <t:{ts}:t> your time · 📍 {s['circuit_short_name']}")
        payload = {
            "content": content,
            # Discord only pings if the message explicitly allows it
            "allowed_mentions": {"parse": ["everyone", "roles"]} if ping else {"parse": []},
        }
        send("reminders", payload, weekend=weekend_label(meeting_name, s["year"]))
        done.add(s["session_key"])
        save_json(POSTED_FILE, sorted(done))
        print(f"Reminder posted: {s['session_name']} ({meeting_name})")


if __name__ == "__main__":
    main()
