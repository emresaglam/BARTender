#!/usr/bin/env python3
"""
BARTender — Real-time BART arrivals in your terminal.

Usage:
    python bartender.py                           # Default: MONT + WCRK
    python bartender.py --station EMBR            # Single station
    python bartender.py --station EMBR --station DALY
    python bartender.py --list-stations           # Print known stations
"""

import argparse
import csv
import curses
import io
import json
import os
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests
from google.transit import gtfs_realtime_pb2

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_STATIONS = ["MONT", "WCRK"]  # Easy to change

TRIP_UPDATE_URL = "http://api.bart.gov/gtfsrt/tripupdate.aspx"
ALERTS_URL = "http://api.bart.gov/gtfsrt/alerts.aspx"
GTFS_ZIP_URL = "https://www.bart.gov/dev/schedules/google_transit.zip"

CACHE_DIR = Path.home() / ".cache" / "bartender"
GTFS_CACHE_PATH = CACHE_DIR / "gtfs_data.json"
CACHE_MAX_AGE = 7 * 24 * 3600  # Re-download GTFS static data weekly

REFRESH_INTERVAL = 15  # seconds
MAX_ARRIVALS = 8
MIN_MINUTES = 1
MAX_MINUTES = 45

# Route ID -> line color (from BART's GTFS routes.txt)
ROUTE_COLORS = {
    "1": "yellow",   # Yellow-S: Antioch -> SFO/Millbrae
    "2": "yellow",   # Yellow-N: Millbrae/SFO -> Antioch
    "3": "orange",   # Orange-N: Berryessa -> Richmond
    "4": "orange",   # Orange-S: Richmond -> Berryessa
    "5": "green",    # Green-S:  Berryessa -> Daly City
    "6": "green",    # Green-N:  Daly City -> Berryessa
    "7": "red",      # Red-S:    Richmond -> SFO/Millbrae
    "8": "red",      # Red-N:    Millbrae/SFO -> Richmond
    "11": "blue",    # Blue-S:   Dublin/Pleasanton -> Daly City
    "12": "blue",    # Blue-N:   Daly City -> Dublin/Pleasanton
    "19": "white",   # Grey-N:   OAK Airport -> Coliseum
    "20": "white",   # Grey-S:   Coliseum -> OAK Airport
}

# Fallback: guess line color from destination station
DEST_LINE_COLORS = {
    "RICH": "orange",
    "MLBR": "yellow",
    "BERY": "green",
    "DALY": "blue",
    "DUBL": "blue",
    "ANTC": "yellow",
    "SFIA": "yellow",
    "WARM": "green",
    "PITT": "yellow",
    "PCTR": "yellow",
    "OAKL": "white",
    "MLPT": "green",
}

# Hardcoded fallback station names (used if GTFS download fails)
KNOWN_STATIONS = {
    "12TH": "12th St. Oakland City Center",
    "16TH": "16th St. Mission",
    "19TH": "19th St. Oakland",
    "24TH": "24th St. Mission",
    "ANTC": "Antioch",
    "ASHB": "Ashby",
    "BALB": "Balboa Park",
    "BAYF": "Bay Fair",
    "BERY": "Berryessa / North San Jose",
    "CAST": "Castro Valley",
    "CIVC": "Civic Center / UN Plaza",
    "COLS": "Coliseum",
    "COLM": "Colma",
    "CONC": "Concord",
    "DALY": "Daly City",
    "DBRK": "Downtown Berkeley",
    "DUBL": "Dublin / Pleasanton",
    "DELN": "El Cerrito del Norte",
    "PLZA": "El Cerrito Plaza",
    "EMBR": "Embarcadero",
    "FRMT": "Fremont",
    "FTVL": "Fruitvale",
    "GLEN": "Glen Park",
    "HAYW": "Hayward",
    "LAFY": "Lafayette",
    "LAKE": "Lake Merritt",
    "MCAR": "MacArthur",
    "MLBR": "Millbrae",
    "MLPT": "Milpitas",
    "MONT": "Montgomery St.",
    "NBRK": "North Berkeley",
    "NCON": "North Concord / Martinez",
    "OAKL": "Oakland International Airport",
    "ORIN": "Orinda",
    "PITT": "Pittsburg / Bay Point",
    "PCTR": "Pittsburg Center",
    "PHIL": "Pleasant Hill / Contra Costa Centre",
    "POWL": "Powell St.",
    "RICH": "Richmond",
    "ROCK": "Rockridge",
    "SBRN": "San Bruno",
    "SANL": "San Leandro",
    "SFIA": "San Francisco International Airport",
    "SHAY": "South Hayward",
    "SSAN": "South San Francisco",
    "UCTY": "Union City",
    "WARM": "Warm Springs / South Fremont",
    "WCRK": "Walnut Creek",
    "WDUB": "West Dublin / Pleasanton",
    "WOAK": "West Oakland",
}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ArrivalInfo:
    destination: str       # Station ID of the trip's final stop
    dest_name: str         # Human-readable destination name
    arrival_time: float    # Unix timestamp of arrival
    delay_seconds: int     # Delay in seconds (0 = on time, negative = early)
    minutes_away: int      # Minutes from now
    line_color: str        # Color name for the route
    direction: str         # "North" or "South" (from GTFS direction_id)


@dataclass
class GtfsStaticData:
    """Parsed and cached data from the GTFS static feed."""
    station_names: dict[str, str]       # Station code -> display name
    platform_to_station: dict[str, str] # Platform ID (e.g. "M16-1") -> station code (e.g. "EMBR")
    trip_to_route: dict[str, str]       # Trip ID -> route ID
    route_direction: dict[str, str]     # Route ID -> "North" or "South"


# ---------------------------------------------------------------------------
# GTFS static data: download, parse, cache
# ---------------------------------------------------------------------------


def _cache_is_fresh() -> bool:
    """Check if the GTFS cache exists and is less than CACHE_MAX_AGE old."""
    if not GTFS_CACHE_PATH.exists():
        return False
    age = time.time() - GTFS_CACHE_PATH.stat().st_mtime
    return age < CACHE_MAX_AGE


def _download_and_parse_gtfs() -> GtfsStaticData | None:
    """Download the BART GTFS zip and extract the data we need."""
    try:
        resp = requests.get(GTFS_ZIP_URL, timeout=30)
        resp.raise_for_status()
    except Exception:
        return None

    station_names: dict[str, str] = {}
    platform_to_station: dict[str, str] = {}
    trip_to_route: dict[str, str] = {}
    route_direction: dict[str, str] = {}

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        # Parse stops.txt — build station names and platform mapping
        with zf.open("stops.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f))
            for row in reader:
                sid = row["stop_id"]
                parent = row.get("parent_station", "")
                loc_type = row.get("location_type", "")

                if loc_type == "1":
                    station_names[sid] = row["stop_name"]
                elif parent:
                    platform_to_station[sid] = parent

        # Parse trips.txt — build trip_id -> route_id
        with zf.open("trips.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f))
            for row in reader:
                trip_to_route[row["trip_id"]] = row["route_id"]

        # Parse directions.txt — build route_id -> direction label
        with zf.open("directions.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f))
            for row in reader:
                route_direction[row["route_id"]] = row["direction"]

    return GtfsStaticData(
        station_names=station_names,
        platform_to_station=platform_to_station,
        trip_to_route=trip_to_route,
        route_direction=route_direction,
    )


def _save_cache(data: GtfsStaticData) -> None:
    """Save parsed GTFS data to disk cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "station_names": data.station_names,
        "platform_to_station": data.platform_to_station,
        "trip_to_route": data.trip_to_route,
        "route_direction": data.route_direction,
    }
    with open(GTFS_CACHE_PATH, "w") as f:
        json.dump(payload, f)


def _load_cache() -> GtfsStaticData | None:
    """Load GTFS data from disk cache."""
    try:
        with open(GTFS_CACHE_PATH) as f:
            payload = json.load(f)
        return GtfsStaticData(
            station_names=payload["station_names"],
            platform_to_station=payload["platform_to_station"],
            trip_to_route=payload["trip_to_route"],
            route_direction=payload.get("route_direction", {}),
        )
    except Exception:
        return None


def load_gtfs_static() -> GtfsStaticData:
    """Load GTFS static data, using cache if fresh, otherwise downloading."""
    if _cache_is_fresh():
        cached = _load_cache()
        if cached:
            return cached

    # Try downloading fresh data
    data = _download_and_parse_gtfs()
    if data:
        _save_cache(data)
        return data

    # Try stale cache
    cached = _load_cache()
    if cached:
        return cached

    # Last resort: hardcoded stations, no platform mapping
    return GtfsStaticData(
        station_names=dict(KNOWN_STATIONS),
        platform_to_station={},
        trip_to_route={},
        route_direction={},
    )


# ---------------------------------------------------------------------------
# GTFS-RT helpers
# ---------------------------------------------------------------------------


def fetch_gtfsrt(url: str) -> gtfs_realtime_pb2.FeedMessage | None:
    """Fetch and parse a GTFS-RT protobuf feed. Returns None on error."""
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(resp.content)
        return feed
    except Exception:
        return None


def _resolve_station(stop_id: str, gtfs: GtfsStaticData) -> str:
    """Resolve a GTFS-RT stop_id (platform like 'M16-1') to a station code ('EMBR')."""
    return gtfs.platform_to_station.get(stop_id, stop_id)


def _get_line_color(trip_id: str, dest_station: str, gtfs: GtfsStaticData) -> str:
    """Determine line color from route_id (via trip mapping) or destination fallback."""
    route_id = gtfs.trip_to_route.get(trip_id, "")
    color = ROUTE_COLORS.get(route_id)
    if color:
        return color
    return DEST_LINE_COLORS.get(dest_station, "white")


def _get_direction(trip_id: str, gtfs: GtfsStaticData) -> str:
    """Determine direction label (North/South) for a trip."""
    route_id = gtfs.trip_to_route.get(trip_id, "")
    return gtfs.route_direction.get(route_id, "")


def get_arrivals(
    feed: gtfs_realtime_pb2.FeedMessage,
    station_id: str,
    gtfs: GtfsStaticData,
) -> list[ArrivalInfo]:
    """Extract upcoming arrivals at *station_id* from a trip-update feed."""
    now = time.time()
    results: list[ArrivalInfo] = []
    station_id = station_id.upper()

    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update

        if not tu.stop_time_update:
            continue

        trip_id = tu.trip.trip_id

        # Find the last stop to determine destination
        last_stop = tu.stop_time_update[-1]
        dest_station = _resolve_station(last_stop.stop_id, gtfs)

        for stu in tu.stop_time_update:
            sid = _resolve_station(stu.stop_id, gtfs)
            if sid != station_id:
                continue

            # Determine arrival time
            arrival_time = None
            delay = 0

            if stu.HasField("arrival"):
                if stu.arrival.time and stu.arrival.time > 0:
                    arrival_time = stu.arrival.time
                    delay = stu.arrival.delay if stu.arrival.delay else 0
                elif stu.arrival.delay is not None:
                    delay = stu.arrival.delay

            # Fall back to departure if no arrival
            if arrival_time is None and stu.HasField("departure"):
                if stu.departure.time and stu.departure.time > 0:
                    arrival_time = stu.departure.time
                    delay = stu.departure.delay if stu.departure.delay else 0

            if arrival_time is None:
                continue

            minutes_away = int((arrival_time - now) / 60)
            if minutes_away < MIN_MINUTES or minutes_away > MAX_MINUTES:
                continue

            dest_name = gtfs.station_names.get(dest_station, dest_station)
            line_color = _get_line_color(trip_id, dest_station, gtfs)
            direction = _get_direction(trip_id, gtfs)

            results.append(ArrivalInfo(
                destination=dest_station,
                dest_name=dest_name,
                arrival_time=arrival_time,
                delay_seconds=delay,
                minutes_away=minutes_away,
                line_color=line_color,
                direction=direction,
            ))

    results.sort(key=lambda a: a.arrival_time)
    return results[:MAX_ARRIVALS]


def get_alerts(feed: gtfs_realtime_pb2.FeedMessage | None) -> list[str]:
    """Extract alert text from the alerts feed.
    Prefers description over header, since BART's headers are often generic
    (e.g. just 'BART.gov Alert')."""
    if feed is None:
        return []
    alerts: list[str] = []
    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        alert = entity.alert
        # Try description first (has the actual info), fall back to header
        text = None
        if alert.description_text and alert.description_text.translation:
            text = alert.description_text.translation[0].text
        elif alert.header_text and alert.header_text.translation:
            text = alert.header_text.translation[0].text
        if text:
            # Collapse whitespace for cleaner display
            alerts.append(" ".join(text.split()))
    return alerts


# ---------------------------------------------------------------------------
# Curses UI
# ---------------------------------------------------------------------------

# Curses color pair IDs
PAIR_HEADER = 1
PAIR_SECTION = 2
PAIR_ONTIME = 3
PAIR_LATE_MILD = 4
PAIR_LATE_BAD = 5
PAIR_RED = 6
PAIR_YELLOW = 7
PAIR_BLUE = 8
PAIR_GREEN = 9
PAIR_WHITE = 10
PAIR_ALERT = 11
PAIR_ORANGE = 12

LINE_COLOR_PAIR = {
    "red": PAIR_RED,
    "yellow": PAIR_YELLOW,
    "blue": PAIR_BLUE,
    "green": PAIR_GREEN,
    "orange": PAIR_ORANGE,
    "white": PAIR_WHITE,
}

# Short labels for the line color tags in the UI
LINE_COLOR_TAG = {
    "red": "RED",
    "yellow": "YEL",
    "blue": "BLU",
    "green": "GRN",
    "orange": "ORG",
    "white": "WHT",
}

# Emoji equivalents for line color tags
LINE_COLOR_EMOJI = {
    "red": "\U0001f534",      # 🔴
    "yellow": "\U0001f7e1",   # 🟡
    "blue": "\U0001f535",     # 🔵
    "green": "\U0001f7e2",    # 🟢
    "orange": "\U0001f7e0",   # 🟠
    "white": "\u26aa",        # ⚪
}


def _init_colors():
    """Set up curses color pairs."""
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(PAIR_HEADER, curses.COLOR_WHITE, curses.COLOR_BLUE)
    curses.init_pair(PAIR_SECTION, curses.COLOR_CYAN, -1)
    curses.init_pair(PAIR_ONTIME, curses.COLOR_GREEN, -1)
    curses.init_pair(PAIR_LATE_MILD, curses.COLOR_YELLOW, -1)
    curses.init_pair(PAIR_LATE_BAD, curses.COLOR_RED, -1)
    curses.init_pair(PAIR_RED, curses.COLOR_RED, -1)
    curses.init_pair(PAIR_YELLOW, curses.COLOR_YELLOW, -1)
    curses.init_pair(PAIR_BLUE, curses.COLOR_BLUE, -1)
    curses.init_pair(PAIR_GREEN, curses.COLOR_GREEN, -1)
    curses.init_pair(PAIR_WHITE, curses.COLOR_WHITE, -1)
    curses.init_pair(PAIR_ALERT, curses.COLOR_RED, -1)
    curses.init_pair(PAIR_ORANGE, curses.COLOR_YELLOW, -1)  # orange ~ bold yellow


def _delay_text(delay_seconds: int, emoji: bool = False) -> tuple[str, int]:
    """Return (display string, color pair) for a delay value."""
    if delay_seconds <= 0:
        label = "\u2705 on time" if emoji else "on time"
        return (label, PAIR_ONTIME)
    minutes = delay_seconds // 60
    if minutes <= 3:
        label = f"\U0001f7e1 +{minutes}m" if emoji else f"+{minutes} min late"
        return (label, PAIR_LATE_MILD)
    label = f"\U0001f525 +{minutes}m" if emoji else f"+{minutes} min late"
    return (label, PAIR_LATE_BAD)


def _safe_addstr(win, y: int, x: int, text: str, attr: int = 0):
    """Write to window, silently ignoring out-of-bounds writes."""
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    max_len = w - x
    if max_len <= 0:
        return
    try:
        win.addnstr(y, x, text, max_len, attr)
    except curses.error:
        pass


def draw_ui(
    stdscr,
    station_ids: list[str],
    arrivals_by_station: dict[str, list[ArrivalInfo]],
    gtfs: GtfsStaticData,
    countdown: int,
    error_msg: str | None,
    alerts: list[str],
    emoji: bool = False,
):
    """Render the full curses UI."""
    stdscr.erase()
    height, width = stdscr.getmaxyx()

    if height < 10 or width < 40:
        _safe_addstr(stdscr, 0, 0, "Terminal too small. Resize to at least 40x10.")
        stdscr.refresh()
        return

    now_str = time.strftime("%H:%M:%S")

    # -- Header bar --
    if emoji:
        header = f" \U0001f68b BARTender \u2014 {now_str}   \u23f3 {countdown}s"
    else:
        header = f" BARTender \u2014 {now_str}   (refresh in {countdown}s)"
    header = header.ljust(width)
    _safe_addstr(stdscr, 0, 0, header, curses.color_pair(PAIR_HEADER) | curses.A_BOLD)

    row = 2

    # -- Error banner --
    if error_msg:
        prefix = "\u26a0\ufe0f " if emoji else "! "
        _safe_addstr(stdscr, row, 2, f"{prefix}{error_msg}", curses.color_pair(PAIR_LATE_BAD))
        row += 2

    # -- Station sections --
    for sid in station_ids:
        if row >= height - 2:
            break

        name = gtfs.station_names.get(sid, sid)
        if emoji:
            section_title = f" \U0001f689 {name} ({sid}) "
        else:
            section_title = f" {name} ({sid}) "
        _safe_addstr(
            stdscr, row, 1, section_title,
            curses.color_pair(PAIR_SECTION) | curses.A_BOLD,
        )
        row += 1

        arrivals = arrivals_by_station.get(sid, [])

        if not arrivals:
            no_data = "\U0001f3dc\ufe0f  No upcoming arrivals" if emoji else "No upcoming arrivals"
            _safe_addstr(stdscr, row, 3, no_data, curses.A_DIM)
            row += 2
            continue

        # Group arrivals by direction, preserving time order within each
        directions_seen: list[str] = []
        by_direction: dict[str, list[ArrivalInfo]] = {}
        for arr in arrivals:
            d = arr.direction or "Other"
            if d not in by_direction:
                directions_seen.append(d)
                by_direction[d] = []
            by_direction[d].append(arr)

        for direction in directions_seen:
            if row >= height - 2:
                break

            # Direction subheader
            if emoji:
                arrow = "\u2b06\ufe0f " if direction == "North" else "\u2b07\ufe0f " if direction == "South" else "\u25c6 "
            else:
                arrow = "\u25b2" if direction == "North" else "\u25bc" if direction == "South" else "\u25c6"
            _safe_addstr(
                stdscr, row, 3, f"{arrow} {direction}bound",
                curses.A_BOLD | curses.A_DIM,
            )
            row += 1

            for arr in by_direction[direction]:
                if row >= height - 2:
                    break

                if emoji:
                    # Emoji colored circle for line
                    dot = LINE_COLOR_EMOJI.get(arr.line_color, "\u26aa")
                    _safe_addstr(stdscr, row, 5, dot)
                    # Destination name (offset after emoji)
                    dest_display = arr.dest_name[:26]
                    _safe_addstr(stdscr, row, 8, dest_display)
                else:
                    # Line color tag, e.g. [RED]
                    lc_pair = LINE_COLOR_PAIR.get(arr.line_color, PAIR_WHITE)
                    tag = LINE_COLOR_TAG.get(arr.line_color, "???")
                    _safe_addstr(stdscr, row, 5, f"[{tag}]", curses.color_pair(lc_pair) | curses.A_BOLD)
                    # Destination name
                    dest_display = arr.dest_name[:26]
                    _safe_addstr(stdscr, row, 11, dest_display)

                # ETA column (right-aligned area)
                eta_col = max(width - 30, 39)
                eta_str = f"{arr.minutes_away} min"
                _safe_addstr(stdscr, row, eta_col, eta_str.rjust(7))

                # Delay status
                delay_str, delay_pair = _delay_text(arr.delay_seconds, emoji)
                _safe_addstr(
                    stdscr, row, eta_col + 9, f"({delay_str})",
                    curses.color_pair(delay_pair),
                )

                row += 1

        row += 1  # spacing between stations

    # -- Alerts section --
    if alerts and row < height - 3:
        alerts_label = " \U0001f6a8 Service Alerts " if emoji else " Service Alerts "
        _safe_addstr(
            stdscr, row, 1, alerts_label,
            curses.color_pair(PAIR_ALERT) | curses.A_BOLD,
        )
        row += 1
        indent = 3
        max_line_len = width - indent - 2
        for alert_text in alerts[:3]:
            if row >= height - 2:
                break
            # Wrap long alerts across multiple lines
            words = alert_text.split()
            line = ""
            for word in words:
                if line and len(line) + 1 + len(word) > max_line_len:
                    _safe_addstr(stdscr, row, indent, line, curses.color_pair(PAIR_ALERT))
                    row += 1
                    if row >= height - 2:
                        break
                    line = word
                else:
                    line = f"{line} {word}" if line else word
            if line and row < height - 2:
                _safe_addstr(stdscr, row, indent, line, curses.color_pair(PAIR_ALERT))
                row += 1

    # -- Footer bar --
    if emoji:
        footer = " \U0001f6aa q: quit  \U0001f504 r: refresh  \U0001f50d s: station code  \U0001f4cb l: station list "
    else:
        footer = " q: quit  r: refresh  s: station code  l: station list "
    footer = footer.ljust(width)
    _safe_addstr(
        stdscr, height - 1, 0, footer,
        curses.color_pair(PAIR_HEADER) | curses.A_BOLD,
    )

    stdscr.refresh()


def _prompt_station(stdscr, height: int, width: int) -> str | None:
    """Prompt user to type a station code. Returns code or None on cancel."""
    prompt = "Enter station code: "
    row = height - 3
    _safe_addstr(stdscr, row, 1, " " * (width - 2))  # clear line
    _safe_addstr(stdscr, row, 1, prompt, curses.A_BOLD)
    stdscr.refresh()
    curses.echo()
    curses.curs_set(1)
    try:
        win = curses.newwin(1, 10, row, len(prompt) + 1)
        win.timeout(-1)
        code = win.getstr(0, 0, 8).decode("utf-8").strip().upper()
        return code if code else None
    except Exception:
        return None
    finally:
        curses.noecho()
        curses.curs_set(0)


def _station_picker(stdscr, gtfs: GtfsStaticData, emoji: bool = False) -> str | None:
    """Full-screen scrollable station picker. Returns selected station code or None."""
    curses.curs_set(0)
    stations = sorted(gtfs.station_names.items(), key=lambda x: x[1])
    selected = 0
    scroll_offset = 0
    search = ""

    while True:
        # Filter stations by search string
        if search:
            filtered = [
                (code, name) for code, name in stations
                if search in code.lower() or search in name.lower()
            ]
        else:
            filtered = stations

        # Clamp selection
        if not filtered:
            selected = 0
        elif selected >= len(filtered):
            selected = len(filtered) - 1

        stdscr.erase()
        height, width = stdscr.getmaxyx()

        # Header
        if emoji:
            title = " \U0001f50d Select a Station"
        else:
            title = " Select a Station"
        title = title.ljust(width)
        _safe_addstr(stdscr, 0, 0, title, curses.color_pair(PAIR_HEADER) | curses.A_BOLD)

        # Search bar
        search_display = f" Search: {search}\u2588"
        _safe_addstr(stdscr, 1, 1, search_display, curses.A_BOLD)

        # List area
        list_top = 3
        list_height = height - 5  # room for header, search, footer

        # Adjust scroll to keep selection visible
        if selected < scroll_offset:
            scroll_offset = selected
        elif selected >= scroll_offset + list_height:
            scroll_offset = selected - list_height + 1

        for i in range(list_height):
            idx = scroll_offset + i
            if idx >= len(filtered):
                break
            row = list_top + i
            code, name = filtered[idx]

            if idx == selected:
                # Highlighted row
                if emoji:
                    line = f"  \U0001f449 {code:5s}  {name}"
                else:
                    line = f"  > {code:5s}  {name}"
                line = line.ljust(width - 1)
                _safe_addstr(stdscr, row, 0, line, curses.A_REVERSE | curses.A_BOLD)
            else:
                if emoji:
                    line = f"     {code:5s}  {name}"
                else:
                    line = f"    {code:5s}  {name}"
                _safe_addstr(stdscr, row, 0, line)

        # Scrollbar hint
        if len(filtered) > list_height:
            pct = (scroll_offset / max(1, len(filtered) - list_height))
            bar_pos = list_top + int(pct * (list_height - 1))
            _safe_addstr(stdscr, bar_pos, width - 1, "\u2588", curses.A_DIM)

        # Footer
        if not filtered:
            hint = " No matches. "
        else:
            hint = f" {len(filtered)} stations "
        if emoji:
            footer = f"{hint} | \u2b06\ufe0f\u2b07\ufe0f /jk: navigate  \u23ce: select  esc: cancel  type to search"
        else:
            footer = f"{hint} | up/down/jk: navigate  enter: select  esc: cancel  type to search"
        footer = footer.ljust(width)
        _safe_addstr(stdscr, height - 1, 0, footer, curses.color_pair(PAIR_HEADER) | curses.A_BOLD)

        stdscr.refresh()

        key = stdscr.getch()

        if key == 27:  # Escape
            return None
        elif key in (curses.KEY_ENTER, 10, 13):  # Enter
            if filtered:
                return filtered[selected][0]
            return None
        elif key == curses.KEY_UP or key == ord("k"):
            selected = max(0, selected - 1)
        elif key == curses.KEY_DOWN or key == ord("j"):
            if filtered:
                selected = min(len(filtered) - 1, selected + 1)
        elif key == curses.KEY_PPAGE:  # Page Up
            selected = max(0, selected - list_height)
        elif key == curses.KEY_NPAGE:  # Page Down
            if filtered:
                selected = min(len(filtered) - 1, selected + list_height)
        elif key == curses.KEY_HOME:
            selected = 0
        elif key == curses.KEY_END:
            if filtered:
                selected = len(filtered) - 1
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            if search:
                search = search[:-1]
                selected = 0
                scroll_offset = 0
        elif 32 <= key <= 126:
            search += chr(key)
            selected = 0
            scroll_offset = 0
        elif key == curses.KEY_RESIZE:
            pass


def curses_main(stdscr, station_ids: list[str], gtfs: GtfsStaticData, emoji: bool = False):
    """Main curses event loop."""
    _init_colors()
    curses.curs_set(0)
    stdscr.timeout(1000)  # 1-second getch timeout for countdown updates

    arrivals_by_station: dict[str, list[ArrivalInfo]] = {}
    alerts: list[str] = []
    error_msg: str | None = None
    last_fetch = 0.0

    while True:
        now = time.time()

        # Fetch data when interval expires (or on first run)
        if (now - last_fetch) >= REFRESH_INTERVAL or last_fetch == 0:
            feed = fetch_gtfsrt(TRIP_UPDATE_URL)
            alerts_feed = fetch_gtfsrt(ALERTS_URL)
            if feed is None:
                error_msg = "Failed to fetch BART data \u2014 will retry"
            else:
                error_msg = None
                arrivals_by_station = {
                    sid: get_arrivals(feed, sid, gtfs) for sid in station_ids
                }
            alerts = get_alerts(alerts_feed)
            last_fetch = time.time()

        countdown = max(0, REFRESH_INTERVAL - int(time.time() - last_fetch))
        draw_ui(stdscr, station_ids, arrivals_by_station, gtfs, countdown, error_msg, alerts, emoji)

        key = stdscr.getch()
        if key == ord("q"):
            break
        elif key == ord("r"):
            last_fetch = 0  # force refresh next iteration
        elif key == ord("s"):
            height, width = stdscr.getmaxyx()
            code = _prompt_station(stdscr, height, width)
            if code and code in gtfs.station_names:
                if code not in station_ids:
                    station_ids.append(code)
                    if len(station_ids) > 2:
                        station_ids.pop(0)
                last_fetch = 0
            elif code:
                error_msg = f"Unknown station: {code}"
        elif key == ord("l"):
            code = _station_picker(stdscr, gtfs, emoji)
            if code:
                if code not in station_ids:
                    station_ids.append(code)
                    if len(station_ids) > 2:
                        station_ids.pop(0)
                last_fetch = 0
        elif key == curses.KEY_RESIZE:
            pass  # redraw on next loop


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        prog="bartender",
        description="BARTender \u2014 Real-time BART arrivals in your terminal.",
    )
    parser.add_argument(
        "-s", "--station", action="append", dest="stations", metavar="CODE",
        help="Station code to display (can be repeated). Default: MONT WCRK",
    )
    parser.add_argument(
        "--list-stations", action="store_true",
        help="Print all known station codes and exit.",
    )
    parser.add_argument(
        "-e", "--emoji", action="store_true",
        help="Enable emoji mode for a cuter interface \U0001f60d",
    )
    args = parser.parse_args()

    # Load/cache GTFS static data (stations, platform mapping, trip routes)
    print("Loading BART station data...", end="", flush=True)
    gtfs = load_gtfs_static()
    print(f" {len(gtfs.station_names)} stations loaded.")

    if args.list_stations:
        for code in sorted(gtfs.station_names):
            print(f"  {code:5s}  {gtfs.station_names[code]}")
        return

    station_ids = args.stations if args.stations else list(DEFAULT_STATIONS)
    station_ids = [s.upper() for s in station_ids]

    # Validate station codes
    unknown = [s for s in station_ids if s not in gtfs.station_names]
    if unknown:
        print(f"Warning: unknown station(s): {', '.join(unknown)}", file=sys.stderr)
        print("Use --list-stations to see valid codes.", file=sys.stderr)

    try:
        curses.wrapper(curses_main, station_ids, gtfs, args.emoji)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
