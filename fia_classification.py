"""
Posts official FIA documents to Discord as images, as soon as the FIA publishes them:
practice, qualifying, sprint and race classifications, starting grids, and stewards'
decisions. Runs on GitHub Actions next to f1_results.py.

To turn something on or off, change True/False in the DOCUMENTS or STEWARDS lists below.
"""

import json
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import pymupdf as fitz  # PyMuPDF
import requests

from common import openf1, send, weekend_label

# ------------------------------------------------------------------
# Which FIA documents to post. Set to False to turn one off.
# (file name used by the FIA, title shown in Discord, on/off)
# ------------------------------------------------------------------
DOCUMENTS = [
    ("free_practice_1_classification",         "🔧 Free Practice 1 Classification",     True),
    ("free_practice_2_classification",         "🔧 Free Practice 2 Classification",     True),
    ("free_practice_3_classification",         "🔧 Free Practice 3 Classification",     True),
    ("final_sprint_qualifying_classification", "⏱️ Sprint Qualifying Classification",   True),
    ("final_sprint_starting_grid",             "🚦 Sprint Starting Grid",               True),
    ("final_sprint_classification",            "🏁 Sprint Classification",              True),
    ("final_qualifying_classification",        "⏱️ Qualifying Classification",          True),
    ("final_starting_grid",                    "🚦 Starting Grid",                      True),
    ("provisional_race_classification",        "📝 Provisional Race Classification",    False),
    ("final_race_classification",              "🏁 Final Race Classification",          True),
]

# ------------------------------------------------------------------
# Stewards' documents. These have a unique name each time, so the bot
# reads the FIA documents page to find them.
# (type at the start of the FIA title, label shown in Discord, on/off)
# ------------------------------------------------------------------
STEWARDS = [
    ("decision",     "⚖️ Stewards' Decision", True),   # penalties, investigations, "no further action"
    ("summons",      "📣 Summons",            True),   # driver/team called to the stewards
    ("offence",      "🚨 Offence",            True),
    ("infringement", "🚫 Infringement",       True),   # fines like pit lane speeding, deleted lap times
]

# Skip the most common, low-interest infringements: lap times deleted for track limits.
# (Deleted lap times for double yellow flags are still posted.)
SKIP_TRACK_LIMITS = True

# ------------------------------------------------------------------
# Technical documents, also found on the FIA documents page.
# Matched by a word that appears anywhere in the FIA file name.
# (word in the file name, label shown in Discord, on/off)
# ------------------------------------------------------------------
TECHNICAL = [
    ("pu_elements",        "🔋 Power Unit Elements Used",     True),   # season PU usage table + new parts
    ("power_unit",         "🔋 Power Unit Elements",          True),
    ("parc_ferme",         "🔧 Parc Fermé Changes",           True),   # setup/parts changed under parc fermé
    ("technical_delegate", "🛠️ Technical Delegate's Report",  True),   # other technical reports
    ("scrutineering",      "🔍 Scrutineering",                False),  # routine paperwork
]

# Post a summary of each team's car upgrades (FIA "Car Presentation Submissions", Friday morning)
UPGRADES_ENABLED = True

# Turn stewards' decisions into easy-to-read cards (False = post the PDF as an image instead)
PENALTY_CARDS = True

FORCE = os.environ.get("FORCE_REPOST", "false").lower() == "true"
FIA_BASE = "https://www.fia.com/system/files/decision-document/"
POSTED_FILE = "fia_posted.json"
LISTING_URL = "https://www.fia.com/documents/championships/fia-formula-one-world-championship-14"
HEADERS = {"User-Agent": "Mozilla/5.0 (F1 Discord results bot)"}
WEEKEND_WINDOW = timedelta(days=3)  # stop checking a race weekend 3 days after its last session


def load_posted():
    try:
        with open(POSTED_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_posted(posted):
    with open(POSTED_FILE, "w") as f:
        json.dump(sorted(posted), f)


def current_weekend():
    """The current race weekend: the latest one with a session starting before two days from now
    (so documents published before FP1, like the upgrades list, are picked up)."""
    now = datetime.now(timezone.utc)
    sessions = openf1("sessions", year=now.year) or []
    started = [s for s in sessions
               if not s.get("is_cancelled")
               and datetime.fromisoformat(s["date_start"]) < now + timedelta(days=2)]
    if not started:
        return None, None
    latest = max(started, key=lambda s: s["date_start"])
    return latest, datetime.fromisoformat(latest["date_end"])


def slugify(name):
    # "São Paulo Grand Prix" -> "sao_paulo_grand_prix", keeps hyphens like "barcelona-catalunya"
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^a-z0-9-]+", "_", name.lower())
    return name.strip("_")


def fetch_pdf(file_base):
    # The FIA sometimes re-uploads with _0 / _1 on the end
    for suffix in ["", "_0", "_1"]:
        url = f"{FIA_BASE}{file_base}{suffix}.pdf"
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
        except requests.RequestException:
            continue
        if r.status_code == 200 and r.content[:4] == b"%PDF":
            return url, r.content
    return None, None


def content_pages(doc):
    """Page numbers of the document itself, skipping the FIA cover sheet if there is one."""
    first = doc[0].get_text()
    start = 1 if doc.page_count > 1 and "Enclosed" in first and "Title" in first else 0
    return list(range(start, doc.page_count))


def pdf_to_images(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = content_pages(doc)[:2]
    return [doc[i].get_pixmap(matrix=fitz.Matrix(2.5, 2.5)).tobytes("png") for i in pages]


# ---------------- Penalty cards ----------------

DECISION_LABELS = ["No / Driver", "Competitor", "Time", "Session", "Fact",
                   "Infringement", "Offence", "Decision", "Reason"]
DECISION_STOP = ("Competitors are reminded", "N.B.")


def parse_decision(pdf_bytes):
    """Pull the key lines out of a stewards' decision. Returns {} if the layout isn't recognised."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    text = "\n".join(doc[i].get_text() for i in content_pages(doc))
    fields, current = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if current is None and not line.startswith("No / Driver"):
            continue  # skip the letterhead (it has its own Date/Time lines)
        if line.startswith(DECISION_STOP):
            break
        label = None
        if current != "Reason":  # Reason is always last, so everything after it belongs to it
            for l in DECISION_LABELS:
                if line.startswith(l) and (len(line) == len(l) or not line[len(l)].islower()):
                    label = l
                    break
        if label and label not in fields:
            current = label
            fields[current] = line[len(label):].strip(" :")
        elif current:
            fields[current] = (fields[current] + " " + line).strip()
    return fields if fields.get("Decision") else {}


def short(text, limit=1000):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def penalty_embed(fields, url, meeting_name, year):
    who = fields.get("No / Driver", "")
    m = re.match(r"(\d+)\s*-\s*(.+)", who)
    title = f"⚖️ Car {m.group(1)} – {m.group(2)}" if m else f"⚖️ {who or 'Stewards’ Decision'}"
    decision = short(fields.get("Decision", ""), 300)
    no_action = "no further action" in decision.lower()

    embed = {
        "title": title[:256],
        "url": url,
        "description": f"**{'✅' if no_action else '🚨'} {decision}**",
        "color": 0x95A5A6 if no_action else 0xE10600,
        "fields": [],
        "footer": {"text": f"{meeting_name} {year} · FIA Stewards"},
    }
    if fields.get("Competitor"):
        embed["fields"].append({"name": "Team", "value": short(fields["Competitor"], 100), "inline": True})
    if fields.get("Session"):
        embed["fields"].append({"name": "Session", "value": short(fields["Session"], 100), "inline": True})
    if fields.get("Time"):
        embed["fields"].append({"name": "Time", "value": short(fields["Time"], 50), "inline": True})
    if fields.get("Fact"):
        embed["fields"].append({"name": "What happened", "value": short(fields["Fact"])})
    breach = fields.get("Infringement") or fields.get("Offence") or fields.get("Breach")
    if breach:
        embed["fields"].append({"name": "Rule", "value": short(breach, 300)})
    if fields.get("Reason"):
        embed["fields"].append({"name": "Stewards' reasoning", "value": short(fields["Reason"])})
    return embed


def listed_documents():
    """All PDF documents linked on the FIA's F1 documents page (latest race weekend)."""
    try:
        r = requests.get(LISTING_URL, headers=HEADERS, timeout=30)
    except requests.RequestException as e:
        print(f"Couldn't read FIA documents page: {e}")
        return []
    if r.status_code != 200:
        print(f"Couldn't read FIA documents page: HTTP {r.status_code}")
        return []
    links = re.findall(r'href="([^"]*decision-document/[^"]+?\.pdf)"[^>]*>(.*?)</a>',
                       r.text, flags=re.IGNORECASE | re.DOTALL)
    docs, seen = [], set()
    for href, inner in links:
        url = href if href.startswith("http") else "https://www.fia.com" + href
        if url in seen:
            continue
        seen.add(url)
        name = slugify(unquote(url.rsplit("/", 1)[1])[:-4])
        # Title as shown on the FIA page, e.g. "Doc 26 - Decision - Car 44 - Alleged incident with car 3"
        text = re.sub(r"<[^>]+>", " ", inner)
        text = re.sub(r"Published on.*", "", text, flags=re.DOTALL)
        text = re.sub(r"^\s*Doc\s*\d+\s*-\s*", "", " ".join(text.split()))
        docs.append((url, name, text.strip()))
    return docs


def nice_title(rest):
    # "decision_-_car_44_-_alleged_incident_with_car_3" -> "Decision - Car 44 - Alleged incident with car 3"
    parts = [p.replace("_", " ").strip() for p in rest.split("_-_")]
    return " - ".join(p[:1].upper() + p[1:] for p in parts if p)


def label_for(rest):
    """Which label (if any) a document on the FIA page should be posted under."""
    doc_type = rest.split("_-_")[0]
    if (SKIP_TRACK_LIMITS and doc_type == "infringement"
            and "deleted_lap_time" in rest and "double_yellow" not in rest):
        return None, None
    for t, label, on in STEWARDS:
        if on and doc_type == t:
            return label, "stewards"
    for word, label, on in TECHNICAL:
        if on and word in rest:
            # A stewards' ruling about PU/parc fermé changes (e.g. a pit lane start) is still a penalty
            if doc_type in {t for t, _, _ in STEWARDS}:
                return "⚖️ Stewards' Decision", "stewards"
            return label, "technical"
    return None, None


def post_stewards_docs(prefix, meeting_name, year, posted):
    """Stewards' decisions and technical reports, found on the FIA documents page."""
    docs = listed_documents()
    this_race = [d for d in docs if d[1].startswith(prefix)]
    print(f"FIA documents page: {len(docs)} documents listed, {len(this_race)} for this race")
    print(f"  Looking for names starting with: {prefix}")
    for _, name, _ in docs[:5]:
        print(f"  Listed: {name}")

    for url, name, page_title in this_race:
        rest = name[len(prefix):]
        label, kind = label_for(rest)
        if not label:
            continue
        key = name
        if key in posted and not FORCE:
            continue
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
        except requests.RequestException:
            continue
        if r.status_code != 200 or r.content[:4] != b"%PDF":
            continue

        full = page_title or nice_title(rest)
        if kind == "stewards":
            title = f"{label}: {full.split(' - ', 1)[-1]}"
        else:
            detail = full if len(full) <= 90 else ""
            title = f"{label}: {detail}" if detail else label

        category = "stewards" if kind == "stewards" else "technical"
        doc_type = rest.split("_-_")[0]
        fields = parse_decision(r.content) if (PENALTY_CARDS and kind == "stewards"
                                               and doc_type in ("decision", "offence", "infringement")) else {}
        if fields:
            send(category, {"embeds": [penalty_embed(fields, url, meeting_name, year)]},
                 weekend=weekend_label(meeting_name, year))
        else:
            post(title, meeting_name, year, url, pdf_to_images(r.content), category)
        posted.add(key)
        save_posted(posted)
        print(f"Posted: {title}" + (" (penalty card)" if fields else ""))


TEAMS = [
    ("McLaren", ["mclaren"]),
    ("Mercedes", ["mercedes"]),
    ("Red Bull", ["red bull", "rbr "]),
    ("Ferrari", ["ferrari"]),
    ("Williams", ["williams"]),
    ("Racing Bulls", ["racing bulls", "vcarb", "visa cash app"]),
    ("Aston Martin", ["aston martin"]),
    ("Haas", ["haas"]),
    ("Audi", ["audi", "sauber"]),
    ("Alpine", ["alpine"]),
    ("Cadillac", ["cadillac"]),
]


def clean(cell):
    return " ".join(str(cell or "").split())


def find_team(header_text):
    """Which team a page belongs to, based on the top of the page."""
    text = header_text.lower()
    best = None
    for team, keys in TEAMS:
        for k in keys:
            i = text.find(k)
            if i != -1 and (best is None or i < best[0]):
                best = (i, team)
    return best[1] if best else None


def parse_upgrades(pdf_bytes):
    """Returns ({team: [(component, reason), ...]}, [teams with no updates])."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    parts, no_updates, current = {}, [], None

    for page in list(doc)[1:]:  # page 1 is the FIA cover letter
        text = page.get_text()
        team = find_team(text[:400])
        if team:
            current = team

        try:
            tables = page.find_tables().tables
        except Exception:
            tables = []

        rows_found = []
        for table in tables:
            # Read each cell's text straight from the page (keeps spaces between words)
            rows = []
            for row in table.rows:
                rows.append([clean(page.get_text("text", clip=bbox)) if bbox else "" for bbox in row.cells])
            comp_col = reason_col = None
            for cells in rows:
                squashed = [c.lower().replace(" ", "") for c in cells]
                if comp_col is None:
                    for i, c in enumerate(squashed):
                        if "updatedcomponent" in c:
                            comp_col = i
                        elif "primaryreason" in c:
                            reason_col = i
                    continue
                comp = cells[comp_col] if comp_col < len(cells) else ""
                reason = cells[reason_col] if reason_col is not None and reason_col < len(cells) else ""
                if comp and not comp.isdigit():
                    rows_found.append((comp, reason))

        if rows_found and current:
            parts.setdefault(current, []).extend(rows_found)
        elif team and "no update" in text.lower() and team not in parts and team not in no_updates:
            no_updates.append(team)

    return parts, no_updates


def post_upgrades(prefix, meeting_name, year, posted):
    key = prefix + "car_presentation_submissions"
    if key in posted and not FORCE:
        return
    url, pdf = fetch_pdf(key)
    if not pdf:
        print("Not published yet: 🔧 Car Upgrades")
        return

    parts, no_updates = parse_upgrades(pdf)
    fields = []
    for team, _ in TEAMS:
        if team not in parts:
            continue
        lines, total = [], 0
        for comp, reason in parts[team]:
            reason = reason.split(" - ")[0] if reason else ""
            line = f"• {comp[:60]}" + (f" *({reason[:30]})*" if reason else "")
            if total + len(line) + 1 > 950:
                lines.append(f"…and more in the PDF")
                break
            lines.append(line)
            total += len(line) + 1
        fields.append({"name": f"{team} ({len(parts[team])})", "value": "\n".join(lines), "inline": False})

    description = f"[📄 Full FIA document]({url})"
    if no_updates:
        description += f"\n\n**No upgrades:** {', '.join(no_updates)}"
    missing = [t for t, _ in TEAMS if t not in parts and t not in no_updates]
    if missing and parts:
        description += f"\n**See PDF for:** {', '.join(missing)}"
    if not parts:
        description += "\n\nCouldn't read the upgrade tables this time. Open the full document for details."

    embed = {
        "title": f"🔧 Car Upgrades — {meeting_name} {year}",
        "description": description,
        "color": 0xE10600,
        "fields": fields[:25],
        "footer": {"text": "FIA Car Presentation Submissions"},
    }
    send("technical", {"embeds": [embed]}, weekend=weekend_label(meeting_name, year))

    posted.add(key)
    save_posted(posted)
    print(f"Posted: 🔧 Car Upgrades ({sum(len(v) for v in parts.values())} parts from {len(parts)} teams)")


def post(title, meeting_name, year, url, images, category="results"):
    files = {f"files[{i}]": (f"page_{i + 1}.png", img, "image/png") for i, img in enumerate(images)}
    payload = {"content": f"**{title} — {meeting_name} {year}**\n📄 <{url}>"}
    send(category, payload, files=files, weekend=weekend_label(meeting_name, year))


def main():
    posted = load_posted()
    session, last_end = current_weekend()
    if not session:
        print("No race weekend has started yet this year.")
        return

    if not FORCE and datetime.now(timezone.utc) - last_end > WEEKEND_WINDOW:
        print("Not a race weekend right now. Nothing to check.")
        return

    meetings = openf1("meetings", meeting_key=session["meeting_key"]) or []
    if not meetings:
        print("Couldn't get the race name from OpenF1.")
        return
    meeting_name = meetings[0]["meeting_name"]
    prefix = f"{session['year']}_{slugify(meeting_name)}_-_"

    if UPGRADES_ENABLED:
        post_upgrades(prefix, meeting_name, session["year"], posted)

    for doc_name, title, enabled in DOCUMENTS:
        if not enabled:
            continue
        key = prefix + doc_name
        if key in posted and not FORCE:
            continue

        url, pdf = fetch_pdf(key)
        if not pdf:
            print(f"Not published yet: {title}")
            continue

        post(title, meeting_name, session["year"], url, pdf_to_images(pdf))
        posted.add(key)
        save_posted(posted)
        print(f"Posted: {title}")

    post_stewards_docs(prefix, meeting_name, session["year"], posted)


if __name__ == "__main__":
    main()
