#!/usr/bin/env python3
"""
Lichess Forum Arenas Bot
=========================
Team "Forum Arenas" / Forum "Arena Requests"

Spieler posten ein strukturiertes Key:Value-Template im Forum. Der Bot liest
neue Posts, parst/validiert die Felder (sehr tolerant gegenüber unter-
schiedlichen Schreibweisen, inkl. Zeitzonen), prüft das Rate-Limit und
erstellt ein Lichess-Arena-Turnier mit exakt diesen Einstellungen.

Erwartetes Format (Beispiel):

    Name: My Custom Blitz
    Variant: Standard
    Clock: 3+2
    Duration: 60
    Date: 25.12.2026
    Time: 18:00
    Rated: yes

Akzeptierte Varianten (Beispiele):
    Clock:    3+2, 3:2, 3-2, 3 2, 1/4+0, 0.25+0
    Duration: 90, 90min, 1h30, 1.5h, 2 hours, 1:30, 12 HOURS
    Date:     25.12.2026, 25/12/26, 2026-12-25, 25 December, Aug 16th, Dec 25 2026
    Time:     18:00, 18.00, 6pm, 6:30 PM, noon, 5 PM EST, 20:00 CET
    Rated:    yes/y/true/1/rated, no/n/false/0/casual/unrated

Zeitzonen (EST, PST, CET, IST, ...) werden erkannt und automatisch nach
Europe/Berlin umgerechnet. Fehlt eine Zeitzone, wird die Eingabe als
deutsche Zeit interpretiert.

State (verarbeitete Post-IDs, letzte Forumsseite, Rate-Limit-Nutzung) wird
in data/state.json gespeichert. Der Workflow committet diese Datei nach
jedem Lauf zurück.
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup, NavigableString
from dateutil import parser as dateparser
from dateutil import tz as dateutil_tz

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

FORUM_SLUG = "team-forum-arenas/arena-requests"
FORUM_BASE = f"https://lichess.org/forum/{FORUM_SLUG}"
TEAM_ID = "forum-arenas"
STATE_PATH = Path("data/state.json")
TOKEN = os.environ.get("LICHESS_TOKEN")

TZ = ZoneInfo("Europe/Berlin")

MAX_TOURNAMENTS_PER_USER_PER_DAY = 3

# Mindest-Vorlaufzeit zwischen "jetzt" (Verarbeitungszeitpunkt) und Turnierstart.
MIN_LEAD_MINUTES = 1

# Lichess-Grenzen für Arena-Turniere
MIN_DURATION_MINUTES = 10
MAX_DURATION_MINUTES = 720  # 12 Stunden

# Schutz gegen Burst-Spam: max. so viele neue Posts pro Workflow-Lauf
# verarbeiten (Rest wird beim nächsten Lauf abgearbeitet)
MAX_POSTS_PER_RUN = 25

# Feste Turnier-Beschreibung. "This arena was requested by @Username" wird
# automatisch VOR diesen Text gesetzt - hier nur den Rest nach Belieben anpassen.
DESCRIPTION_TEMPLATE = (
    "Join the Forum Arenas team to take part in community-requested tournaments!\n"
    "Post your own request in the [Arena Requests forum](https://lichess.org/forum/team-forum-arenas/arena-requests) to get your own arena created."
)

VARIANT_MAP = {
    "standard": "standard", "chess": "standard", "normal": "standard",
    "chess960": "chess960", "960": "chess960", "fischerandom": "chess960",
    "fischer random": "chess960", "fischer": "chess960",
    "crazyhouse": "crazyhouse", "house": "crazyhouse", "zh": "crazyhouse",
    "crazy house": "crazyhouse",
    "antichess": "antichess", "anti": "antichess", "losers": "antichess",
    "anti chess": "antichess", "giveaway": "antichess",
    "atomic": "atomic",
    "horde": "horde",
    "kingofthehill": "kingOfTheHill", "king of the hill": "kingOfTheHill",
    "koth": "kingOfTheHill", "kingofhill": "kingOfTheHill",
    "racingkings": "racingKings", "racing kings": "racingKings",
    "racingking": "racingKings", "racing king": "racingKings",
    "threecheck": "threeCheck", "three check": "threeCheck",
    "3check": "threeCheck", "3-check": "threeCheck",
    "3 check": "threeCheck", "threecheckchess": "threeCheck",
}

# Von Lichess erlaubte clockTime-Werte (Minuten), inkl. UltraBullet-Bruchwerte
# und 0 (reines Inkrement, z.B. "0+1")
ALLOWED_CLOCK_TIMES = {
    0, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4, 5, 6, 7, 8, 9, 10,
    15, 20, 25, 30, 40, 50, 60,
}

RATED_TRUE_VALUES = {"yes", "y", "true", "1", "rated", "ja"}
RATED_FALSE_VALUES = {"no", "n", "false", "0", "casual", "unrated", "nein"}

# Häufige Zeitzonen-Abkürzungen -> UTC-Offset in Stunden. dateutil kennt
# diese Kürzel nicht zuverlässig von selbst (viele sind mehrdeutig),
# daher explizite Zuordnung. Bei Bedarf einfach weitere ergänzen.
TZ_ABBREVIATIONS_HOURS = {
    "UTC": 0, "GMT": 0,
    "CET": 1, "CEST": 2, "BST": 1, "WET": 0, "WEST": 1,
    "EST": -5, "EDT": -4,
    "CST": -6, "CDT": -5,
    "MST": -7, "MDT": -6,
    "PST": -8, "PDT": -7,
    "AEST": 10, "AEDT": 11,
    "ACST": 9.5, "ACDT": 10.5,
    "AWST": 8,
    "NZST": 12, "NZDT": 13,
    "IST": 5.5,  # India Standard Time
    "JST": 9,
    "KST": 9,
    "MSK": 3,
    "EET": 2, "EEST": 3,
}


def _build_tzinfos():
    tzinfos = {}
    for name, hours in TZ_ABBREVIATIONS_HOURS.items():
        tzinfos[name] = dateutil_tz.tzoffset(name, int(hours * 3600))
    return tzinfos


TZINFOS = _build_tzinfos()

YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

POST_LINK_RE = re.compile(r"^/@/([\w-]+)$")
# ?page=N ist optional - auf Seite 1 lässt Lichess den Query-Parameter weg
PERMALINK_RE = re.compile(r"(?:\?page=\d+)?#([A-Za-z0-9]+)$")

session = requests.Session()
session.headers.update({"User-Agent": "forum-arenas-bot"})
if TOKEN:
    session.headers.update({"Authorization": f"Bearer {TOKEN}"})


# ---------------------------------------------------------------------------
# Forum scrapen (Lesen)
# ---------------------------------------------------------------------------

def fetch_page(page: int, retries: int = 3) -> str:
    """Holt eine Forumsseite, mit kurzem Retry bei transienten Netzwerkfehlern."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            res = session.get(f"{FORUM_BASE}?page={page}", timeout=30)
            res.raise_for_status()
            return res.text
        except requests.exceptions.RequestException as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 * attempt)
    raise last_err


def get_max_page(html: str) -> int:
    pages = [int(n) for n in re.findall(r"\?page=(\d+)", html)]
    return max(pages) if pages else 1


def extract_posts(html: str):
    """
    [{username, post_id, text}] in Reihenfolge des Auftretens.

    Ein echter Post-Header ist ein Link auf /@/USERNAME, DIREKT gefolgt
    (nächstes <a>-Tag) von einem Link auf .../[?page=N]#POSTID. Mentions im
    Fließtext (z.B. "@DarkOnCrack") erfüllen dieses Muster nicht, weil danach
    kein Permalink-Link folgt. <blockquote>-Elemente (zitierte Antworten)
    werden vorher entfernt, damit Inhalte aus zitiertem Text nicht fälschlich
    dem Antwortenden zugeschrieben werden.
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


def normalize_text(text: str) -> str:
    """Entfernt unsichtbare/Sonderleerzeichen, die beim Copy-Paste (v.a. mobil)
    oft mitkopiert werden und Regex-Matches sonst stillschweigend brechen."""
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = text.replace("\xa0", " ").replace("\u3000", " ")
    return text


def parse_fields(text: str) -> dict:
    """Extrahiert Key:Value-Paare aus dem Post-Text, egal in welcher Reihenfolge."""
    text = normalize_text(text)
    fields = {}
    for line in text.splitlines():
        m = re.match(r"\s*([A-Za-z]+)\s*:\s*(.+?)\s*$", line)
        if m:
            key = m.group(1).strip().lower()
            value = m.group(2).strip()
            fields[key] = value
    return fields


# --- Clock -------------------------------------------------------------

def parse_clock_field(raw: str):
    """Trennt 'TIME<sep>INCREMENT' - Trennzeichen: '+', ':', '-' oder Leerzeichen."""
    raw = raw.strip()

    m = re.match(r"^(.+?)\s*[+:\-]\s*(\d+)$", raw)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    parts = raw.split()
    if len(parts) == 2:
        return parts[0], parts[1]

    return None


def parse_clock_time(raw: str) -> float:
    """Akzeptiert '3', '0', '0.25' oder '1/4' (Bruch) als Minutenangabe."""
    raw = raw.strip().replace(",", ".")

    frac_match = re.match(r"^(\d+)\s*/\s*(\d+)$", raw)
    if frac_match:
        numerator, denominator = int(frac_match.group(1)), int(frac_match.group(2))
        if denominator == 0:
            raise ValueError("division by zero")
        return numerator / denominator

    dec_match = re.match(r"^\d+(\.\d+)?$", raw)
    if dec_match:
        return float(raw)

    raise ValueError(f"unrecognized number format '{raw}'")


def format_clock_time(value: float) -> str:
    """Gibt ganze Zahlen ohne .0 zurück, sonst den Dezimalwert (z.B. 0.25)."""
    if value == int(value):
        return str(int(value))
    return str(value)


# --- Duration --------------------------------------------------------------

def parse_duration_minutes(raw: str) -> int:
    """
    Akzeptiert u.a.: '90', '90min', '90 minutes', '1h30', '1h30m',
    '1.5h', '2 hours', '12 HOURS', '1:30'.
    """
    raw = raw.strip().lower().replace(",", ".")

    if re.match(r"^\d+$", raw):
        return int(raw)

    m = re.match(r"^(\d+)\s*h\s*(\d{1,2})\s*m?$", raw)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))

    m = re.match(r"^(\d+):(\d{2})$", raw)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))

    total = 0.0
    found = False

    h_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours|std|stunden|stunde)\b", raw)
    if h_match:
        total += float(h_match.group(1)) * 60
        found = True

    m_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:m|min|mins|minute|minutes)\b", raw)
    if m_match:
        total += float(m_match.group(1))
        found = True

    if found:
        return round(total)

    raise ValueError(f"unrecognized duration format '{raw}'")


# --- Date + Time -------------------------------------------------------

def parse_date_time(date_str: str, time_str: str, now: datetime) -> datetime:
    """
    Parst Datum und Uhrzeit GETRENNT (nicht als kombinierten String), da
    dateutil sonst z.B. "13.10" fälschlich als Dezimalzahl statt als
    Tag.Monat interpretieren kann, wenn es zusammen mit der Uhrzeit steht.

    Für eindeutige numerische Datumsformate wird zuerst eine exakte Regex
    versucht (zuverlässiger). Nur für Wortformate (z.B. "Aug 16th") wird auf
    die tolerante dateutil-Fuzzy-Erkennung zurückgegriffen.
    """
    date_str_clean = date_str.strip()

    day = month = year = None

    # 1) Exakte numerische Formate: DD.MM(.YYYY), DD/MM(/YYYY), DD-MM(-YYYY)
    m = re.match(r"^(\d{1,2})[./\-](\d{1,2})(?:[./\-](\d{2,4}))?$", date_str_clean)
    if m:
        day = int(m.group(1))
        month = int(m.group(2))
        if m.group(3):
            year = int(m.group(3))
            if year < 100:
                year += 2000

    # 2) ISO-Format: YYYY-MM-DD
    if day is None:
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", date_str_clean)
        if m:
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))

    # 3) Fallback: dateutil-Fuzzy-Erkennung (Monatsnamen, "Aug 16th" usw.)
    if day is None:
        try:
            date_parsed = dateparser.parse(
                date_str_clean, dayfirst=True, fuzzy=True,
                default=datetime(now.year, 1, 1),
            )
        except (ValueError, OverflowError):
            date_parsed = None

        if date_parsed is None:
            raise ValidationError(
                f"couldn't understand 'Date' value '{date_str}'. Try formats "
                f"like 25.12.2026, 25/12/2026, 2026-12-25, or '25 December'."
            )
        day, month = date_parsed.day, date_parsed.month
        if YEAR_RE.search(date_str_clean):
            year = date_parsed.year

    if year is None:
        year = now.year

    # --- Uhrzeit separat parsen (inkl. optionaler Zeitzonen-Abkürzung) ---
    try:
        time_parsed = dateparser.parse(
            time_str, fuzzy=True, tzinfos=TZINFOS,
            default=datetime(2000, 1, 1, 0, 0),
        )
    except (ValueError, OverflowError):
        time_parsed = None

    if time_parsed is None:
        raise ValidationError(
            f"couldn't understand 'Time' value '{time_str}'. Try formats "
            f"like 18:00, 6:30pm, or 5 PM EST."
        )

    try:
        naive_start = datetime(year, month, day, time_parsed.hour, time_parsed.minute)
    except ValueError:
        raise ValidationError(f"'{date_str} {time_str}' is not a valid date/time.")

    if time_parsed.tzinfo is not None:
        # Zeitzone wurde in der Uhrzeit erkannt -> umrechnen nach Europe/Berlin
        start = naive_start.replace(tzinfo=time_parsed.tzinfo).astimezone(TZ)
    else:
        # Keine Zeitzone erkannt -> als deutsche Zeit interpretieren
        start = naive_start.replace(tzinfo=TZ)

    # Kein Jahr angegeben und Datum liegt schon in der Vergangenheit -> nächstes Jahr
    if not YEAR_RE.search(date_str_clean) and start < now:
        start = start.replace(year=start.year + 1)

    return start

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
    raw_name = fields["name"].strip()
    # Nur Buchstaben, Zahlen und Leerzeichen erlaubt (Lichess-Einschränkung für
    # Turniernamen) - alles andere (!!!, ?, Emojis, Sonderzeichen usw.) wird
    # stillschweigend entfernt statt einen Error zu werfen.
    name = re.sub(r"[^a-zA-Z0-9 ]", "", raw_name)
    name = re.sub(r"\s+", " ", name).strip()

    if len(name) < 2:
        raise ValidationError(
            "'Name' must contain at least 2 usable letters/numbers after "
            "removing special characters/symbols."
        )
    if len(name) > 30:
        name = name[:30].rstrip()

    # --- Variant --- (tolerant gegenüber Satzzeichen/Extra-Leerzeichen)
    variant_clean = re.sub(r"[^a-z0-9 ]", " ", fields["variant"].strip().lower())
    variant_clean = re.sub(r"\s+", " ", variant_clean).strip()
    variant = VARIANT_MAP.get(variant_clean) or VARIANT_MAP.get(variant_clean.replace(" ", ""))
    if variant is None:
        raise ValidationError(
            f"unknown variant '{fields['variant']}'. Valid options: "
            f"Standard, Chess960, Crazyhouse, Antichess, Atomic, Horde, "
            f"King of the Hill, Racing Kings, Three-check."
        )

    # --- Clock ---
    clock_parts = parse_clock_field(fields["clock"])
    if clock_parts is None:
        raise ValidationError(
            f"invalid 'Clock' value '{fields['clock']}'. "
            f"Use TIME+INCREMENT, e.g. '3+2', '3:2', '3-2', '0+1', or '1/4+0' / '0.25+0' for UltraBullet."
        )
    time_part, inc_part = clock_parts

    try:
        clock_time = parse_clock_time(time_part)
    except ValueError:
        raise ValidationError(
            f"invalid 'Clock' time value '{time_part}'. "
            f"Use a whole number, a decimal (e.g. 0.25), or a fraction (e.g. 1/4)."
        )

    if not re.match(r"^\d+$", inc_part):
        raise ValidationError(f"invalid 'Clock' increment value '{inc_part}'. Must be a whole number.")
    clock_increment = int(inc_part)

    if not any(abs(clock_time - allowed) < 0.001 for allowed in ALLOWED_CLOCK_TIMES):
        allowed_str = ", ".join(
            str(int(v)) if v == int(v) else str(v) for v in sorted(ALLOWED_CLOCK_TIMES)
        )
        raise ValidationError(
            f"invalid 'Clock' time '{time_part}'. Allowed values: {allowed_str} "
            f"(minutes; use 1/4 or 0.25 for UltraBullet)."
        )

    if clock_time == 0 and clock_increment == 0:
        raise ValidationError("'Clock' 0+0 is not allowed - increment must be greater than 0 when time is 0.")

    if not (0 <= clock_increment <= 60):
        raise ValidationError("'Clock' increment out of allowed range (0-60).")

    # --- Duration ---
    try:
        duration = parse_duration_minutes(fields["duration"])
    except ValueError:
        raise ValidationError(
            f"invalid 'Duration' value '{fields['duration']}'. "
            f"Try formats like 90, 90min, 1h30, 1.5h, or 2 hours."
        )
    if not (MIN_DURATION_MINUTES <= duration <= MAX_DURATION_MINUTES):
        raise ValidationError(
            f"'Duration' must be between {MIN_DURATION_MINUTES} and {MAX_DURATION_MINUTES} minutes."
        )

    # --- Date + Time -> Startdatum in Europe/Berlin (Zeitzonen werden erkannt) ---
    now = datetime.now(TZ)
    start = parse_date_time(fields["date"], fields["time"], now)

    min_start = now + timedelta(minutes=MIN_LEAD_MINUTES)
    if start < min_start:
        raise ValidationError(
            f"the requested start ({start.strftime('%d.%m.%Y %H:%M')} German time) is too soon "
            f"or in the past. Please choose a time at least {MIN_LEAD_MINUTES} minute(s) from now."
        )

    # --- Rated ---
    rated_raw = fields["rated"].strip().lower()
    if rated_raw in RATED_TRUE_VALUES:
        rated = True
    elif rated_raw in RATED_FALSE_VALUES:
        rated = False
    else:
        raise ValidationError(
            f"invalid 'Rated' value '{fields['rated']}'. Use 'yes' or 'no'."
        )

    return {
        "name": name,
        "variant": variant,
        "clockTime": clock_time,        # float (z.B. 0.25 für UltraBullet, 0 für reines Inkrement)
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
        "clockTime": format_clock_time(req["clockTime"]),
        "clockIncrement": str(req["clockIncrement"]),
        "minutes": str(req["duration"]),
        "startDate": str(start_millis),
        "variant": req["variant"],
        "rated": "true" if req["rated"] else "false",
        "conditions.teamMember.teamId": TEAM_ID,
        "description": description,
    }

    print(f'-> Creating "{req["name"]}" ({req["variant"]}, '
          f'{format_clock_time(req["clockTime"])}+{req["clockIncrement"]}, {req["duration"]}min) '
          f'for {req["start"].strftime("%d.%m.%Y %H:%M")} (Berlin)')

    res = session.post("https://lichess.org/api/tournament", data=data, timeout=30)
    if not res.ok:
        raise RuntimeError(f"Lichess API error ({res.status_code}): {res.text}")

    tournament = res.json()
    print(f"   ✓ https://lichess.org/tournament/{tournament['id']}")
    return tournament


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"lastPage": 1, "processedIds": [], "usage": {}}
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    state.setdefault("usage", {})
    state.setdefault("lastPage", 1)
    state.setdefault("processedIds", [])
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
        save_state(state)
        return

    if len(new_posts) > MAX_POSTS_PER_RUN:
        print(f"{len(new_posts)} new posts found, processing only the first "
              f"{MAX_POSTS_PER_RUN} this run (rest will be handled next run).")
        new_posts = new_posts[:MAX_POSTS_PER_RUN]

    created_count = 0
    rejected_count = 0

    for post in new_posts:
        username = post["username"]
        print(f'Processing post from {username}...')

        # --- Format validieren ---
        try:
            req = parse_request(post["text"])
        except ValidationError as e:
            print(f"   ✗ Invalid request: {e}")
            rejected_count += 1
            state["processedIds"].append(post["post_id"])
            continue
        except Exception as e:
            # Absicherung gegen unerwartete Parsing-Fehler, damit ein einzelner
            # kaputter Post nicht den ganzen Workflow-Lauf abbrechen lässt.
            print(f"   ✗ Unexpected parsing error: {e}", file=sys.stderr)
            rejected_count += 1
            state["processedIds"].append(post["post_id"])
            continue

        # --- Rate-Limit prüfen ---
        used = user_tournament_count_today(state, username)
        if used >= MAX_TOURNAMENTS_PER_USER_PER_DAY:
            print(f"   ✗ Rate limit reached ({used}/{MAX_TOURNAMENTS_PER_USER_PER_DAY} today)")
            rejected_count += 1
            state["processedIds"].append(post["post_id"])
            continue

        # --- Turnier erstellen ---
        try:
            create_tournament(req, username)
            record_tournament_created(state, username)
            created_count += 1
        except Exception as e:
            print(f"   ✗ Error creating tournament: {e}", file=sys.stderr)
            rejected_count += 1

        state["processedIds"].append(post["post_id"])
        # kleine Pause zwischen API-Calls, um Rate-Limits bei mehreren
        # Turnieren in einem Lauf nicht zu triggern
        time.sleep(1)

    save_state(state)
    print(f"Done. Created: {created_count}, Rejected: {rejected_count}, "
          f"Total new posts seen: {len(new_posts)}")


if __name__ == "__main__":
    main()
