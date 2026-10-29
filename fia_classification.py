"""
Posts official FIA documents (classifications, starting grids) to Discord as images,
as soon as the FIA publishes them. Runs on GitHub Actions next to f1_results.py.

To turn a document type on or off, change True/False in the DOCUMENTS list below.
"""

import json
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone

import pymupdf as fitz  # PyMuPDF
import requests

# ------------------------------------------------------------------
# Which FIA documents to post. Set to False to turn one off.
# (file name used by the FIA, title shown in Discord, on/off)
# ------------------------------------------------------------------
DOCUMENTS = [
    ("final_sprint_qualifying_classification", "⏱️ Sprint Qualifying Classification", True),
    ("final_sprint_starting_grid",             "🚦 Sprint Starting Grid",              True),
    ("final_sprint_classification",            "🏁 Sprint Classification",             True),
    ("final_qualifying_classification",        "⏱️ Qualifying Classification",         True),
    ("final_starting_grid",                    "🚦 Starting Grid",                     True),
    ("provisional_race_classification",        "📝 Provisional Race Classification",   False),
    ("final_race_classification",              "🏁 Final Race Classification",         True),
]

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
FORCE = os.environ.get("FORCE_REPOST", "false").lower() == "true"
OPENF1 = "https://api.openf1.org/v1"
FIA_BASE = "https://www.fia.com/system/files/decision-document/"
POSTED_FILE = "fia_posted.json"
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


def openf1(endpoint, **params):
    r = requests.get(f"{OPENF1}/{endpoint}", params=params, timeout=30)
    return r.json() if r.status_code == 200 else None


def current_weekend():
    """The race weekend whose sessions have most recently started."""
    now = datetime.now(timezone.utc)
    sessions = openf1("sessions", year=now.year) or []
    started = [s for s in sessions
               if not s.get("is_cancelled") and datetime.fromisoformat(s["date_start"]) < now]
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


def pdf_to_images(pdf_bytes):
    """Render the sheet itself (skips page 1, the stewards' cover letter)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = range(1, min(doc.page_count, 3)) if doc.page_count > 1 else range(0, 1)
    return [doc[i].get_pixmap(matrix=fitz.Matrix(2.5, 2.5)).tobytes("png") for i in pages]


def post(title, meeting_name, year, url, images):
    files = {f"files[{i}]": (f"page_{i + 1}.png", img, "image/png") for i, img in enumerate(images)}
    payload = {"content": f"**{title} — {meeting_name} {year}**\n📄 <{url}>"}
    r = requests.post(WEBHOOK_URL, data={"payload_json": json.dumps(payload)}, files=files, timeout=60)
    r.raise_for_status()


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


if __name__ == "__main__":
    main()
