"""
Posts the official FIA "Final Race Classification" sheet to Discord as an image.
Runs on GitHub Actions next to f1_results.py.

How it works:
  1. Uses OpenF1 to find the most recent finished race and its name.
  2. Builds the FIA document link, e.g.
     https://www.fia.com/system/files/decision-document/2026_united_states_grand_prix_-_final_race_classification.pdf
  3. If the PDF is published, turns the classification page(s) into PNG images
     and posts them to the Discord webhook.
"""

import json
import os
import re
import unicodedata
from datetime import datetime, timezone

import pymupdf as fitz  # PyMuPDF
import requests

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
FORCE = os.environ.get("FORCE_REPOST", "false").lower() == "true"
OPENF1 = "https://api.openf1.org/v1"
FIA_BASE = "https://www.fia.com/system/files/decision-document/"
POSTED_FILE = "fia_posted.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (F1 Discord results bot)"}


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


def latest_finished_race():
    now = datetime.now(timezone.utc)
    sessions = openf1("sessions", year=now.year, session_name="Race") or []
    done = [s for s in sessions
            if not s.get("is_cancelled") and datetime.fromisoformat(s["date_end"]) < now]
    return max(done, key=lambda s: s["date_end"]) if done else None


def slugify(name):
    # "São Paulo Grand Prix" -> "sao_paulo_grand_prix"
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^a-zA-Z0-9]+", "_", name.lower())
    return name.strip("_")


def find_pdf(year, meeting_name):
    base = f"{year}_{slugify(meeting_name)}_-_final_race_classification"
    # The FIA sometimes re-uploads with _0 / _1 on the end
    for suffix in ["", "_0", "_1"]:
        url = f"{FIA_BASE}{base}{suffix}.pdf"
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 200 and r.content[:4] == b"%PDF":
            return url, r.content
    return None, None


def pdf_to_images(pdf_bytes):
    """Render the classification pages (skips page 1, the stewards' cover letter)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = range(1, min(doc.page_count, 3)) if doc.page_count > 1 else range(0, 1)
    images = []
    for i in pages:
        pix = doc[i].get_pixmap(matrix=fitz.Matrix(2.5, 2.5))  # sharp enough to read
        images.append(pix.tobytes("png"))
    return images


def main():
    posted = load_posted()
    race = latest_finished_race()
    if not race:
        print("No finished race found.")
        return

    meetings = openf1("meetings", meeting_key=race["meeting_key"]) or []
    if not meetings:
        print("Couldn't get the race name from OpenF1.")
        return
    meeting_name = meetings[0]["meeting_name"]

    key = f"{race['year']}_{slugify(meeting_name)}"
    if key in posted and not FORCE:
        print(f"Already posted FIA classification for {meeting_name}.")
        return

    url, pdf = find_pdf(race["year"], meeting_name)
    if not pdf:
        print(f"FIA final classification for {meeting_name} not published yet.")
        return

    images = pdf_to_images(pdf)
    files = {f"files[{i}]": (f"classification_{i + 1}.png", img, "image/png")
             for i, img in enumerate(images)}
    payload = {"content": f"📄 **Official FIA Final Race Classification — {meeting_name} {race['year']}**\n<{url}>"}

    resp = requests.post(WEBHOOK_URL, data={"payload_json": json.dumps(payload)},
                         files=files, timeout=60)
    resp.raise_for_status()

    posted.add(key)
    save_posted(posted)
    print(f"Posted FIA classification for {meeting_name}.")


if __name__ == "__main__":
    main()
