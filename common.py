"""
Shared helpers for the F1 Discord bot:
  - send(): posts to the right Discord channel (and into a per-weekend forum thread)
  - openf1(): reads OpenF1 data, with a saved copy to fall back on
"""

import json
import os
import time

import requests

# ------------------------------------------------------------------
# Channels. Each type of post can go to its own channel via its own
# webhook. Any secret you don't set falls back to DISCORD_WEBHOOK_URL.
# ------------------------------------------------------------------
_MAIN = os.environ.get("DISCORD_WEBHOOK_URL", "")
WEBHOOKS = {
    "results":   _MAIN,                                                  # results card, classifications, grids
    "stewards":  os.environ.get("DISCORD_WEBHOOK_STEWARDS") or _MAIN,    # penalties and decisions
    "technical": os.environ.get("DISCORD_WEBHOOK_TECHNICAL") or _MAIN,   # upgrades, power unit reports
    "reminders": os.environ.get("DISCORD_WEBHOOK_REMINDERS") or _MAIN,   # session reminders
}

THREADS_FILE = "threads.json"        # forum thread IDs per channel + race weekend
CACHE_FILE = "openf1_cache.json"     # last good OpenF1 data
OPENF1 = "https://api.openf1.org/v1"


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)


# ---------------- OpenF1 ----------------

def openf1(endpoint, **params):
    """Fetch from OpenF1. If it fails (e.g. during a live session on the free tier),
    use the last saved copy of the same request instead."""
    cache = load_json(CACHE_FILE, {})
    key = endpoint + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    try:
        r = requests.get(f"{OPENF1}/{endpoint}", params=params, timeout=30)
        if r.status_code == 200:
            data = r.json()
            if endpoint in ("sessions", "meetings"):  # only cache schedule info
                cache[key] = data
                save_json(CACHE_FILE, cache)
            return data
        print(f"OpenF1 {endpoint}: HTTP {r.status_code}")
    except requests.RequestException as e:
        print(f"OpenF1 {endpoint}: {e}")
    if key in cache:
        print(f"Using saved copy of OpenF1 {endpoint}")
        return cache[key]
    return None


def weekend_label(meeting_name, year):
    return f"🏁 {meeting_name} {year}"


# ---------------- Discord ----------------

def _post(url, payload, files=None, params=None):
    r = None
    for _ in range(3):
        if files:
            r = requests.post(url, params=params, data={"payload_json": json.dumps(payload)},
                              files=files, timeout=60)
        else:
            r = requests.post(url, params=params, json=payload, timeout=30)
        if r.status_code == 429:  # Discord says slow down
            time.sleep(float(r.headers.get("Retry-After", 5)) + 1)
            continue
        return r
    return r


def _is_forum_error(r):
    try:
        return r.status_code == 400 and r.json().get("code") == 220001
    except ValueError:
        return False


def send(category, payload, files=None, weekend=None):
    """Post to the channel for this category. If that channel is a forum,
    the post goes into a thread for the race weekend (created if needed)."""
    url = WEBHOOKS.get(category) or _MAIN
    if not url:
        raise RuntimeError("No Discord webhook configured (DISCORD_WEBHOOK_URL is missing)")

    webhook_id = url.split("/webhooks/")[-1].split("/")[0]
    threads = load_json(THREADS_FILE, {})
    key = f"{webhook_id}|{weekend}"

    # 1) Already have a thread for this weekend in this channel
    if weekend and key in threads:
        r = _post(url, payload, files, params={"thread_id": threads[key]})
        if r.ok:
            time.sleep(2)
            return
        print(f"Couldn't post in saved thread ({r.status_code}), trying again")

    # 2) Normal channel
    r = _post(url, payload, files)
    if r.ok:
        time.sleep(2)
        return

    # 3) Forum channel: start a new post (thread) for this weekend
    if _is_forum_error(r):
        name = (weekend or "F1 updates")[:100]
        r = _post(url, dict(payload, thread_name=name), files, params={"wait": "true"})
        if r.ok:
            threads[key] = r.json()["channel_id"]
            save_json(THREADS_FILE, threads)
            time.sleep(2)
            return

    print(f"Discord error {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
