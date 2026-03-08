# 🚋 BARTender

Real-time BART train arrivals in your terminal, powered by GTFS-RT.

![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue)

BARTender is a curses-based terminal app that shows live arrival times for any BART station. It pulls real-time data directly from BART's GTFS-RT protobuf feeds — no API key required.

## Features

- **Live arrivals** — fetched from BART's GTFS-RT feed every 15 seconds
- **Direction grouping** — arrivals split into Northbound / Southbound sections
- **Color-coded lines** — Red, Yellow, Blue, Green, Orange lines shown with ANSI colors
- **Delay indicators** — on time (green), mildly late (yellow), very late (red)
- **Service alerts** — active BART advisories shown at the bottom
- **Interactive station picker** — press `l` to browse and search all 50 stations
- **Emoji mode** — `--emoji` for a cuter interface with 🔴🟡🔵🟢🟠 and more
- **Smart caching** — GTFS static data downloaded once and cached for a week

## Install

```bash
pip install requests gtfs-realtime-bindings
```

That's it. Everything else (`curses`, `argparse`, `csv`, `json`, `zipfile`) is in the Python stdlib.

## Usage

```bash
# Show default stations (Embarcadero + Balboa Park)
python bartender.py

# Show a specific station
python bartender.py -s EMBR

# Show two stations
python bartender.py -s EMBR -s DALY

# Emoji mode
python bartender.py --emoji

# List all station codes
python bartender.py --list-stations
```

### CLI Flags

| Flag | Short | Description |
|------|-------|-------------|
| `--station CODE` | `-s` | Station to display (repeatable). Defaults to EMBR + BALB |
| `--emoji` | `-e` | Enable emoji mode 😍 |
| `--list-stations` | | Print all station codes and names, then exit |

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `q` | Quit |
| `r` | Force refresh now |
| `s` | Enter a station code manually |
| `l` | Open interactive station picker |

In the station picker:

| Key | Action |
|-----|--------|
| `↑`/`↓` or `j`/`k` | Navigate |
| `Page Up`/`Page Down` | Jump by page |
| `Home`/`End` | Jump to top/bottom |
| Type anything | Live search filter |
| `Enter` | Select station |
| `Esc` | Cancel |

## How It Works

### Data Sources

BARTender uses two types of GTFS data:

1. **Static GTFS** (downloaded once, cached at `~/.cache/bartender/gtfs_data.json`):
   - `stops.txt` — maps platform IDs (like `M16-1`) to station codes (like `EMBR`) via `parent_station`
   - `trips.txt` — maps trip IDs to route IDs for line color lookup
   - `directions.txt` — maps route IDs to North/South direction labels

2. **GTFS-RT** (fetched live every 15 seconds, no auth required):
   - **Trip updates**: `http://api.bart.gov/gtfsrt/tripupdate.aspx` — protobuf feed with arrival times and delays for every active trip
   - **Alerts**: `http://api.bart.gov/gtfsrt/alerts.aspx` — service advisories

### The Platform ID Problem

BART's GTFS-RT feed doesn't use human-friendly station codes like `EMBR`. Instead, it uses platform IDs like `M16-1` (Embarcadero platform 1) and `M16-2` (Embarcadero platform 2). BARTender resolves these to station codes using the `parent_station` field from the static GTFS `stops.txt`.

### Line Colors

The GTFS-RT feed doesn't include `route_id`, but it does include `trip_id`. BARTender maps `trip_id → route_id` (from static `trips.txt`), then `route_id → color` using BART's published route color scheme. If the trip isn't found in the static data, it falls back to guessing by destination station.

### Architecture

```
main()
 ├─ argparse CLI
 ├─ load_gtfs_static()      # download/cache GTFS zip → GtfsStaticData
 └─ curses.wrapper(curses_main)
     └─ loop every 1s:
         ├─ fetch_gtfsrt()   # trip updates + alerts (every 15s)
         ├─ get_arrivals()   # parse feed → list[ArrivalInfo]
         ├─ get_alerts()     # parse alerts feed
         ├─ draw_ui()        # render to curses screen
         └─ handle keypress  # q/r/s/l
```

## Changing Default Stations

Edit the constant at the top of `bartender.py`:

```python
DEFAULT_STATIONS = ["EMBR", "BALB"]  # Change these
```

## Station Codes

Run `python bartender.py --list-stations` for the full list. Common ones:

| Code | Station |
|------|---------|
| `EMBR` | Embarcadero |
| `MONT` | Montgomery St. |
| `POWL` | Powell St. |
| `CIVC` | Civic Center / UN Plaza |
| `16TH` | 16th St. Mission |
| `24TH` | 24th St. Mission |
| `BALB` | Balboa Park |
| `DALY` | Daly City |
| `MCAR` | MacArthur |
| `12TH` | 12th St. Oakland City Center |
| `19TH` | 19th St. Oakland |
| `WOAK` | West Oakland |
| `SFIA` | San Francisco International Airport |

## License

Do whatever you want with it.
