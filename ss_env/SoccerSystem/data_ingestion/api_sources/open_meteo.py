"""
Open-Meteo weather ingestor (updated with team-name stadium coordinates).

This version:
- Uses kickoff_utc (matches table column)
- Uses normalized_name from canonical_teams
- Maps team names → canonical IDs → stadium coordinates
- Inserts WeatherRecord rows compatible with your DuckDB schema
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_ingestion.base import Ingestor
from data_normalization.schemas import WeatherRecord
from data_storage.duckdb_store import DuckDBStore

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

HOURLY_VARS = (
    "temperature_2m,precipitation,wind_speed_10m,"
    "relative_humidity_2m,weather_code,weathercode"
)

_WEATHERCODE_MAP = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow",
    80: "rain showers", 81: "rain showers", 82: "violent rain showers",
    95: "thunderstorm", 96: "thunderstorm w/ hail", 99: "thunderstorm w/ heavy hail",
}

# Human-readable stadium coordinates
STADIUM_COORDS_BY_NAME = {
    "arsenal": (51.5550, -0.1085),
    "aston villa": (52.5090, -1.8850),
    "bournemouth": (50.7352, -1.8383),
    "brentford": (51.4908, -0.3025),
    "brighton": (50.8616, -0.0837),
    "brighton hove albion": (50.8616, -0.0837),
    "burnley": (53.7890, -2.2300),
    "chelsea": (51.4817, -0.1910),
    "crystal palace": (51.3983, -0.0856),
    "everton": (53.4388, -2.9663),
    "fulham": (51.4749, -0.2216),
    "ipswich town": (52.0548, 1.1456),
    "liverpool": (53.4308, -2.9608),
    "luton town": (51.8840, -0.4310),
    "man city": (53.4831, -2.2004),
    "man utd": (53.4631, -2.2913),
    "manchester city": (53.4831, -2.2004),
    "manchester united": (53.4631, -2.2913),
    "newcastle": (54.9756, -1.6216),
    "newcastle united": (54.9756, -1.6216),
    "nott m forest": (52.9399, -1.1325),
    "nottingham forest": (52.9399, -1.1325),
    "sheffield united": (53.3703, -1.4700),
    "southampton": (50.9058, -1.4030),
    "tottenham": (51.6033, -0.0657),
    "tottenham hotspur": (51.6033, -0.0657),
    "west ham": (51.5386, -0.0166),
    "west ham united": (51.5386, -0.0166),
    "wolverhampton wanderers": (52.5903, -2.1304),
    "wolves": (52.5903, -2.1304),
}


class OpenMeteoWeatherIngestor(Ingestor):
    source_name = "open-meteo"

    def _canonical_team_coords(self, store: DuckDBStore) -> dict[str, tuple[float, float]]:
        """Map canonical_id → (lat, lon) using normalized_name."""
        rows = store.conn.execute("""
            SELECT canonical_id, normalized_name
            FROM canonical_teams
        """).fetchall()

        mapping = {}
        for cid, name in rows:
            key = name.lower()
            if key in STADIUM_COORDS_BY_NAME:
                mapping[cid] = STADIUM_COORDS_BY_NAME[key]
        return mapping

    def fetch(self, matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raw_records = []
        now = datetime.now(timezone.utc)

        for m in matches:
            kickoff: datetime = m["kickoff_utc"]
            url = ARCHIVE_URL if kickoff < now else FORECAST_URL
            date_str = kickoff.strftime("%Y-%m-%d")

            params = {
                "latitude": m["lat"],
                "longitude": m["lon"],
                "start_date": date_str,
                "end_date": date_str,
                "hourly": HOURLY_VARS,
                "timezone": "UTC",
            }

            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()

            raw_records.append({
                "match_canonical_id": m["match_canonical_id"],
                "kickoff_utc": kickoff,
                "payload": resp.json(),
            })

        return raw_records

    def transform(self, raw_records: list[dict[str, Any]]) -> list[WeatherRecord]:
        out = []
        for rec in raw_records:
            hourly = rec["payload"].get("hourly", {})
            times = hourly.get("time", [])
            if not times:
                continue

            target_hour = rec["kickoff_utc"].strftime("%Y-%m-%dT%H:00")
            if target_hour in times:
                idx = times.index(target_hour)
            else:
                naive_kickoff = rec["kickoff_utc"].replace(tzinfo=None)
                idx = min(
                    range(len(times)),
                    key=lambda i: abs(datetime.fromisoformat(times[i]) - naive_kickoff),
                )

            weather_code = _safe_get(hourly, "weather_code", idx)
            if weather_code is None:
                weather_code = _safe_get(hourly, "weathercode", idx)

            out.append(WeatherRecord(
                source=self.source_name,
                match_canonical_id=rec["match_canonical_id"],
                temp_c=_safe_get(hourly, "temperature_2m", idx),
                precipitation_mm=_safe_get(hourly, "precipitation", idx),
                wind_kph=_safe_get(hourly, "wind_speed_10m", idx),
                humidity_pct=_safe_get(hourly, "relative_humidity_2m", idx),
                condition=_weathercode_to_text(weather_code),
            ))
        return out


def _safe_get(hourly: dict, key: str, idx: int):
    values = hourly.get(key)
    if not values or idx >= len(values):
        return None
    return values[idx]


def _weathercode_to_text(code):
    if code is None:
        return None
    return _WEATHERCODE_MAP.get(int(code), f"code_{int(code)}")


if __name__ == "__main__":
    store = DuckDBStore("data/soccer_smoketest.duckdb")
    ingestor = OpenMeteoWeatherIngestor()

    # Build canonical_id → (lat, lon)
    team_coords = ingestor._canonical_team_coords(store)

    # Build match contexts
    rows = store.conn.execute("""
        SELECT canonical_id, home_team_canonical_id, kickoff_utc
        FROM matches
    """).fetchall()

    matches = []
    for match_id, team_id, kickoff in rows:
        if team_id in team_coords:
            lat, lon = team_coords[team_id]
            matches.append({
                "match_canonical_id": match_id,
                "lat": lat,
                "lon": lon,
                "kickoff_utc": kickoff,
            })

    print(f"Prepared {len(matches)} match contexts")

    result = ingestor.run(store, matches=matches)
    print(result)

    store.close()
