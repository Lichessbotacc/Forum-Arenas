#!/usr/bin/env python3
"""
Lichess Forum Arenas Bot
=========================
Team "Forum Arenas" / Forum "Arena Requests"

Spieler posten ein strukturiertes Key:Value-Template im Forum. Der Bot liest
neue Posts, parst/validiert die Felder, prüft das Rate-Limit, erstellt ein
Lichess-Arena-Turnier mit exakt diesen Einstellungen und antwortet im Thread
mit Bestätigung (Link) oder einer Fehlermeldung.

Erwartetes Format (Beispiel):

    Name: My Custom Blitz
    Variant: Standard
    Clock: 3+2
    Duration: 60
    Date: 25.12.2026
    Time: 18:00
    Rated: yes

State (verarbeitete Post-IDs, letzte Forumsseite, Rate-Limit-Nutzung) wird
in data/state.json gespeichert. Der Workflow committet diese Datei nach
jedem Lauf zurück.
"""

import json
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

import requests
from bs4 import BeautifulSoup, NavigableString

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

FORUM_SLUG = "team-forum-arenas/arena-requests"
FORUM_BASE = f"https://lichess.org/forum/{FORUM_SLUG}"
TEAM_ID = "forum-arenas"                      # <-- echte Team-ID eintragen
STATE_PATH = Path("data/state.json")
TOKEN = os.environ.get("LICHESS_TOKEN")

TZ = ZoneInfo("Europe/Berlin")

MAX_TOURNAMENTS_PER_USER_PER_DAY = 3

# Feste Turnier-Beschreibung. "This arena was requested by @Username" wird
# automatisch VOR diesen Text gesetzt - hier nur den Rest nach Belieben anpassen.
DESCRIPTION_TEMPLATE = (
    "Join the Forum Arenas team to take part in community-requested tournaments!\n"
    "Post your own request in the [Arena Requests](https://lichess.org/forum/team-forum-arenas/arena-requests) forum to get your own arena created."
)

VARIANT_MAP = {
    "standard": "standard",
    "chess960": "chess960",
    "960": "chess960",
    "crazyhouse": "crazyhouse",
    "antichess": "antichess",
    "atomic": "atomic",
    "horde": "horde",
    "kingofthehill": "kingOfTheHill",
    "king of the hill": "kingOfTheHill",
    "koth": "kingOfTheHill",
    "racingkings": "racingKings",
    "racing kings": "racingKings",
    "threecheck": "threeCheck",
    "three-check": "threeCheck",
    "three check": "threeCheck",
    "3check": "threeCheck",
}

POST_LINK_RE = re.compile(r"^/@/([\w-]+)$")
PERMALINK_RE = re.compile(r"\?page=\d+#([A-Za-z0-9]+)$")

session = requests.Session()
session.headers.update({"User-Agent": "forum-arenas-bot"})
if TOKEN:
    session.headers.update({"Authorization": f"Bearer {TOKEN}"})


# ---------------------------------------------------------------------------
# Forum scrapen (Lesen)
# ---------------------------------------------------------------------------

def fetch_page(page: int) -> str:
    res = session.get(f"{FORUM_BASE}?page={page}", timeout=30)
    res.raise_for_status()
    return res.text


def get_max_page(html: str) -> int:
    pages = [int(n) for n in re.findall(r"\?page=(\d+)", html)]
    return max(pages) if pages else 1


def extract_posts(html: str):
    """
    [{username, post_id, text}] in Reihenfolge des Auftretens.

    Robuster Ansatz: ein echter Post-Header ist ein Link auf /@/USERNAME,
    DIREKT gefolgt (nächstes <a>-Tag) von einem Link auf .../?page=N#POSTID.
    Mentions im Fließtext (z.B. "@DarkOnCrack") erfüllen dieses Muster nicht,
    weil danach kein Permalink-Link folgt. <blockquote>-Elemente (zitierte
    Antworten) werden vorher entfernt, damit Inhalte aus zitiertem Text nicht
    fälschlich dem Antwortenden zugeschrieben werden.
    """
    soup = BeautifulSoup(html, "html.parser")

    for bq in soup.find_all("blockquote"):
        bq.decompose()

    all_links = soup.find_all("a", href=True)

    headers = []  # (anchor_tag, username, post_id)
    for i, a in enumerate(all_links):
        m = POST_LINK_RE.match(a["href"])
        if not m:
            continue
        username = m.group(1)
        if i + 1 < len(all_links):
            pm = PERMALINK_RE.search(all_links[i + 1]["href"])
            if pm:
                headers.append((all_links[i + 1], username, pm.group(1)))

    if not headers:
        return []

    marker = "@@POST_BOUNDARY_MARKER@@"
    for anchor, _, _ in headers:
        anchor.insert_after(NavigableString(marker))

    full_text = soup.get_text("\n")
    chunks = full_text.split(marker)[1:]  # erstes Element = Text vor dem 1. Post

    posts = []
    for (_, username, post_id), chunk in zip(headers, chunks):
        text = re.sub(r"[ \t]+", " ", chunk).strip()
        posts.append({"username": username, "post_id": post_id, "text": text})

    return posts


# ---------------------------------------------------------------------------
# Template parsen & validieren
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    pass


def parse_fields(text: str) -> dict:
    """Extrahiert Key:Value-Paare aus dem Post-Text, egal in welcher Reihenfolge."""
    fields = {}
    for line in text.splitlines():
        m = re.match(r"\s*([A-Za-z]+)\s*:\s*(.+?)\s*$", line)
        if m:
            key = m.group(1).strip().lower()
            value = m.group(2).strip()
            fields[key] = value
    return fields


def parse_request(text: str) -> dict:
    """Wirft ValidationError mit verständlicher Meldung, wenn etwas fehlt/ungültig ist."""
    fields = parse_fields(text)

    required = ["name", "variant", "clock", "duration", "date", "time", "rated"]
    missing = [f for f in required if f not in fields]
    if missing:
        raise ValidationError(
            f"missing field(s): {', '.join(missing)}. "
            f"Please use the exact template (see pinned post)."
        )

    # --- Name ---
    name = fields["name"].strip()
    if not name:
        raise ValidationError("'Name' cannot be empty.")
    if len(name) > 30:
        name = name[:30]

    # --- Variant ---
    variant_raw = fields["variant"].strip().lower()
    variant = VARIANT_MAP.get(variant_raw)
    if variant is None:
        raise ValidationError(
            f"unknown variant '{fields['variant']}'. Valid options: "
            f"Standard, Chess960, Crazyhouse, Antichess, Atomic, Horde, "
            f"King of the Hill, Racing Kings, Three-check."
        )

    # --- Clock (z.B. "3+2") ---
    m = re.match(r"^(\d+)\s*\+\s*(\d+)$", fields["clock"].strip())
    if not m:
        raise ValidationError(
            f"invalid 'Clock' value '{fields['clock']}'. "
            f"Use the format MINUTES+INCREMENT, e.g. '3+2' or '5+0'."
        )
    clock_time = int(m.group(1))
    clock_increment = int(m.group(2))
    if not (0 < clock_time <= 60) or not (0 <= clock_increment <= 60):
        raise ValidationError("'Clock' values out of allowed range (time 1-60, increment 0-60).")

    # --- Duration (Minuten) ---
    if not re.match(r"^\d+$", fields["duration"].strip()):
        raise ValidationError(f"invalid 'Duration' value '{fields['duration']}'. Must be a number in minutes.")
    duration = int(fields["duration"].strip())
    if not (10 <= duration <= 360):
        raise ValidationError("'Duration' must be between 10 and 360 minutes.")

    # --- Date + Time -> Startdatum in Europe/Berlin ---
    date_str = fields["date"].strip()
    time_str = fields["time"].strip()

    dm = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", date_str)
    if not dm:
        raise ValidationError(f"invalid 'Date' value '{date_str}'. Use the format DD.MM.YYYY, e.g. 25.12.2026.")
    day, month, year = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))

    tm = re.match(r"^(\d{1,2}):(\d{2})$", time_str)
    if not tm:
        raise ValidationError(f"invalid 'Time' value '{time_str}'. Use 24h format HH:MM, e.g. 18:00.")
    hour, minute = int(tm.group(1)), int(tm.group(2))

    try:
        start = datetime(year, month, day, hour, minute, tzinfo=TZ)
    except ValueError:
        raise ValidationError(f"'{date_str} {time_str}' is not a valid date/time.")

    now = datetime.now(TZ)
    if start <= now:
        raise ValidationError(
            f"the requested start ({start.strftime('%d.%m.%Y %H:%M')} German time) "
            f"is in the past. Please choose a future date/time."
        )

    # --- Rated ---
    rated_raw = fields["rated"].strip().lower()
    if rated_raw not in ("yes", "no"):
        raise ValidationError(f"invalid 'Rated' value '{fields['rated']}'. Use 'yes' or 'no'.")
    rated = rated_raw == "yes"

    return {
        "name": name,
        "variant": variant,
        "clockTime": clock_time,
        "clockIncrement": clock_increment,
        "duration": duration,
        "start": start,
        "rated": rated,
    }


# ---------------------------------------------------------------------------
# Rate-Limiting (3 Turniere pro Nutzer pro Kalendertag, deutsche Zeit)
# ---------------------------------------------------------------------------

def today_key() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def user_tournament_count_today(state: dict, username: str) -> int:
    day = today_key()
    usage = state.get("usage", {})
    user_usage = usage.get(username.lower(), {})
    return user_usage.get(day, 0)


def record_tournament_created(state: dict, username: str):
    day = today_key()
    usage = state.setdefault("usage", {})
    user_usage = usage.setdefault(username.lower(), {})
    user_usage[day] = user_usage.get(day, 0) + 1

    # alte Tage aufräumen, damit die Datei nicht endlos wächst
    usage[username.lower()] = {d: c for d, c in user_usage.items() if d == day}


# ---------------------------------------------------------------------------
# Turnier erstellen
# ---------------------------------------------------------------------------

def create_tournament(req: dict, requester: str) -> dict:
    start_millis = int(req["start"].astimezone(ZoneInfo("UTC")).timestamp() * 1000)

    description = f"This arena was requested by @{requester}\n\n{DESCRIPTION_TEMPLATE}"

    data = {
        "name": req["name"],
        "clockTime": str(req["clockTime"]),
        "clockIncrement": str(req["clockIncrement"]),
        "minutes": str(req["duration"]),
        "startDate": str(start_millis),
        "variant": req["variant"],
        "rated": "true" if req["rated"] else "false",
        "conditions.teamMember.teamId": TEAM_ID,
        "description": description,
    }

    print(f'-> Creating "{req["name"]}" ({req["variant"]}, '
          f'{req["clockTime"]}+{req["clockIncrement"]}, {req["duration"]}min) '
          f'for {req["start"].strftime("%d.%m.%Y %H:%M")} (Berlin)')

    res = session.post("https://lichess.org/api/tournament", data=data, timeout=30)
    if not res.ok:
        raise RuntimeError(f"Lichess API error ({res.status_code}): {res.text}")

    tournament = res.json()
    print(f"   ✓ https://lichess.org/tournament/{tournament['id']}")
    return tournament


# ---------------------------------------------------------------------------
# Forum-Antwort posten (EXPERIMENTELL - keine offizielle API)
# ---------------------------------------------------------------------------

def post_forum_reply(message: str) -> bool:
    try:
        form_res = session.get(FORUM_BASE, timeout=30)
        form_res.raise_for_status()

        csrf_match = re.search(r'name="csrfToken"[^>]*value="([^"]+)"', form_res.text)
        if not csrf_match:
            print("   ! Could not find CSRF token, skipping forum reply.")
            return False
        csrf_token = csrf_match.group(1)

        reply_res = session.post(
            f"{FORUM_BASE}/reply",
            data={"text": message, "csrfToken": csrf_token},
            headers={"Referer": FORUM_BASE},
            timeout=30,
        )

        if reply_res.ok:
            print("   ✓ Forum reply posted.")
            return True

        print(f"   ! Forum reply failed ({reply_res.status_code}).")
        return False

    except Exception as e:
        print(f"   ! Forum reply failed ({e}).")
        return False


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"lastPage": 1, "processedIds": [], "usage": {}}
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    state.setdefault("usage", {})
    return state


def save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["processedIds"] = state["processedIds"][-500:]
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not TOKEN:
        print("Error: LICHESS_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)

    state = load_state()

    html = fetch_page(state["lastPage"])
    max_page = get_max_page(html)

    pages_to_check = [state["lastPage"]]
    if max_page > state["lastPage"]:
        pages_to_check.append(max_page)
        state["lastPage"] = max_page

    all_posts = []
    for i, page in enumerate(pages_to_check):
        page_html = html if i == 0 else fetch_page(page)
        all_posts.extend(extract_posts(page_html))

    processed = set(state["processedIds"])
    new_posts = [p for p in all_posts if p["post_id"] not in processed]

    if not new_posts:
        print("No new requests found.")

    for post in new_posts:
        username = post["username"]
        print(f'Processing post from {username}...')

        # --- Format validieren ---
        try:
            req = parse_request(post["text"])
        except ValidationError as e:
            print(f"   ✗ Invalid request: {e}")
            msg = (
                f"@{username} ❌ Your arena request couldn't be created: {e}\n\n"
                f"Please check the pinned template and post a corrected request."
            )
            post_forum_reply(msg)
            state["processedIds"].append(post["post_id"])
            continue

        # --- Rate-Limit prüfen ---
        used = user_tournament_count_today(state, username)
        if used >= MAX_TOURNAMENTS_PER_USER_PER_DAY:
            print(f"   ✗ Rate limit reached ({used}/{MAX_TOURNAMENTS_PER_USER_PER_DAY} today)")
            msg = (
                f"@{username} ❌ You've reached the daily limit of "
                f"{MAX_TOURNAMENTS_PER_USER_PER_DAY} arenas per user. "
                f"Please try again tomorrow."
            )
            post_forum_reply(msg)
            state["processedIds"].append(post["post_id"])
            continue

        # --- Turnier erstellen ---
        try:
            tournament = create_tournament(req, username)
            record_tournament_created(state, username)
            url = f"https://lichess.org/tournament/{tournament['id']}"
            remaining = MAX_TOURNAMENTS_PER_USER_PER_DAY - user_tournament_count_today(state, username)
            msg = (
                f"@{username} ✅ Your arena \"{req['name']}\" was created: {url}\n"
                f"(You have {remaining} arena request(s) left today.)"
            )
            post_forum_reply(msg)
        except Exception as e:
            print(f"   ✗ Error creating tournament: {e}", file=sys.stderr)
            post_forum_reply(
                f"@{username} ❌ Something went wrong creating your arena. "
                f"Please try again or contact a team leader."
            )

        state["processedIds"].append(post["post_id"])

    save_state(state)


if __name__ == "__main__":
    main()
