#!/usr/bin/env python3
"""
adsb-wdgwars-feeder
-------------------
Polls an ultra feeder (readsb/dump1090) aircraft.json endpoint and
uploads ADS-B data to WDGWars via the HMAC-SHA256 JSON API.

Zero non-stdlib dependencies. Python 3.9+.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ── Config from environment ───────────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip().strip('"').strip("'")

WDGWARS_API_KEY  = _env("WDGWARS_API_KEY")
ULTRA_FEEDER_URL = _env("WDGWARS_ULTRA_FEEDER_URL", "http://ultrafeeder/data/aircraft.json")
POLL_INTERVAL    = int(_env("WDGWARS_POLL_INTERVAL", "30"))
UPLOAD_URL       = _env("WDGWARS_UPLOAD_URL", "https://wdgwars.pl/api/upload/")
BATCH_SIZE       = int(_env("WDGWARS_BATCH_SIZE", "500"))
LOG_LEVEL        = _env("WDGWARS_LOG_LEVEL", "INFO").upper()

# ── Optional coordinate spoofing ──────────────────────────────────────────────
# If set, all aircraft positions are shifted by (FAKE - REAL) so the coverage
# blob appears centred on WDGWARS_FAKE_LAT/LON instead of the true station location.
def _parse_coord(key: str) -> float | None:
    v = _env(key)
    return float(v) if v else None

_STATION_LAT = _parse_coord("WDGWARS_STATION_LAT")
_STATION_LON = _parse_coord("WDGWARS_STATION_LON")
_FAKE_LAT    = _parse_coord("WDGWARS_FAKE_LAT")
_FAKE_LON    = _parse_coord("WDGWARS_FAKE_LON")

if all(v is not None for v in (_STATION_LAT, _STATION_LON, _FAKE_LAT, _FAKE_LON)):
    _LAT_OFFSET = _FAKE_LAT - _STATION_LAT
    _LON_OFFSET = _FAKE_LON - _STATION_LON
else:
    _LAT_OFFSET = 0.0
    _LON_OFFSET = 0.0

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("feeder")

# ── Helpers ───────────────────────────────────────────────────────────────────

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def coerce_int(v) -> int:
    """readsb encodes on-ground aircraft as alt_baro='ground'. Treat that
    and any other non-numeric value as 0."""
    if v is None:
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def norm_record(icao: str, *, callsign: str = "", lat=None, lon=None,
                alt_ft: int = 0, speed_kt: int = 0, heading: int = 0,
                first_seen: str = "") -> dict | None:
    """
    Build a WDGWars aircraft record. Returns None if position is missing or out of bounds.

    Critical: ICAO must be exactly 6 uppercase hex chars with leading zeros
    preserved. The server validates ^[0-9A-F]{6}$ — a stripped ICAO like
    'DB36A' (should be '0DB36A') is silently dropped on import.
    """
    if lat is None or lon is None:
        return None
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None

    lat = max(-90.0, min(90.0,  lat + _LAT_OFFSET))
    lon = max(-180.0, min(180.0, lon + _LON_OFFSET))

    # Pad/truncate to exactly 6 hex chars, uppercase
    icao = (icao or "").upper().strip()
    if not icao:
        icao = "000000"
    elif len(icao) < 6:
        icao = icao.zfill(6)

    return {
        "icao":       icao,
        "callsign":   (callsign or "").strip(),
        "lat":        round(lat, 6),
        "lon":        round(lon, 6),
        "alt_ft":     int(alt_ft),
        "speed_kt":   int(speed_kt),
        "heading":    int(heading),
        "first_seen": first_seen or utc_now(),
        "type":       "ADSB",
    }


def fetch_aircraft(url: str) -> dict | None:
    """Fetch and parse aircraft.json from ultra feeder."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "adsb-wdgwars-feeder/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        log.warning("Ultra feeder returned HTTP %s for %s", e.code, url)
    except urllib.error.URLError as e:
        log.warning("Could not reach ultra feeder: %s", e.reason)
    except json.JSONDecodeError as e:
        log.warning("Invalid JSON from ultra feeder: %s", e)
    except Exception as e:
        log.warning("Fetch error: %s", e)
    return None


def parse_aircraft(raw: dict) -> list[dict]:
    """
    Convert readsb/dump1090 aircraft.json into WDGWars records.

    readsb fields:
      hex       - ICAO 24-bit address (e.g. "a1b2c3")
      flight    - callsign / flight number
      lat       - latitude
      lon       - longitude
      alt_baro  - barometric altitude in feet, or "ground"
      gs        - ground speed in knots
      track     - true track in degrees
      rssi      - signal strength (unused in schema but logged)
    """
    now_str = utc_now()
    records = []

    for ac in raw.get("aircraft", []):
        rec = norm_record(
            icao      = ac.get("hex", ""),
            callsign  = (ac.get("flight") or "").strip(),
            lat       = ac.get("lat"),
            lon       = ac.get("lon"),
            alt_ft    = coerce_int(ac.get("alt_baro", 0)),
            speed_kt  = coerce_int(ac.get("gs", 0)),
            heading   = coerce_int(ac.get("track", 0)),
            first_seen= now_str,
        )
        if rec:
            records.append(rec)

    return records


def build_envelope(payload: dict, api_key: str) -> bytes:
    """
    Build the HMAC-SHA256 signed envelope as documented in the WDGWars API.

    data  = base64( json(payload) )
    nonce = random 8-byte hex string
    sig   = HMAC-SHA256(key=api_key, msg=nonce+data).hexdigest()

    POST body: {"data": "...", "nonce": "...", "sig": "..."}
    """
    data_b64 = base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()
    nonce = secrets.token_hex(8)
    sig = hmac.new(
        api_key.encode(),
        (nonce + data_b64).encode(),
        hashlib.sha256,
    ).hexdigest()
    return json.dumps({"data": data_b64, "nonce": nonce, "sig": sig}).encode()


def upload_batch(records: list[dict], api_key: str, url: str) -> bool:
    """POST one batch to WDGWars. Payload key is 'aircraft' (not 'networks')."""
    # gungnir sends aircraft under the 'aircraft' key
    payload = {"networks": [], "aircraft": records, "meshcore_nodes": []}
    body    = build_envelope(payload, api_key)

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "X-API-Key":    api_key,
            "Content-Type": "application/json",
            "User-Agent":   "adsb-wdgwars-feeder/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            log.debug("Upload response: %s", result)
            return True
    except urllib.error.HTTPError as e:
        body_err = e.read().decode(errors="replace")
        log.warning("Upload HTTP %s: %s", e.code, body_err[:300])
    except urllib.error.URLError as e:
        log.warning("Upload network error: %s", e.reason)
    except Exception as e:
        log.warning("Upload error: %s", e)
    return False


def upload_records(records: list[dict], api_key: str, url: str) -> None:
    """Split into BATCH_SIZE chunks and upload each."""
    if not records:
        return
    total = len(records)
    uploaded = 0
    failed   = 0
    for i in range(0, total, BATCH_SIZE):
        batch = records[i : i + BATCH_SIZE]
        if upload_batch(batch, api_key, url):
            uploaded += len(batch)
        else:
            failed += len(batch)
    if failed:
        log.warning("Uploaded %d/%d records (%d failed)", uploaded, total, failed)
    else:
        log.info("Uploaded %d aircraft records", uploaded)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("adsb-wdgwars-feeder starting")
    log.info("  Ultra feeder : %s", ULTRA_FEEDER_URL)
    log.info("  Upload URL   : %s", UPLOAD_URL)
    log.info("  Poll interval: %ds", POLL_INTERVAL)
    log.info("  Batch size   : %d", BATCH_SIZE)
    if _LAT_OFFSET or _LON_OFFSET:
        log.info("  Coord spoof  : offset (%.6f, %.6f) → fake station (%.6f, %.6f)",
                 _LAT_OFFSET, _LON_OFFSET, _FAKE_LAT, _FAKE_LON)
    else:
        log.info("  Coord spoof  : disabled")

    prev_icaos: set[str] = set()

    while True:
        raw = fetch_aircraft(ULTRA_FEEDER_URL)

        if raw is None:
            log.warning("No data this cycle, skipping")
            time.sleep(POLL_INTERVAL)
            continue

        records = parse_aircraft(raw)
        log.debug("Parsed %d aircraft with valid position", len(records))

        if not records:
            log.info("No aircraft with position data this cycle")
            time.sleep(POLL_INTERVAL)
            continue

        current_icaos = {r["icao"] for r in records}
        new_count = len(current_icaos - prev_icaos)

        log.info(
            "%d aircraft in view, %d new — uploading all for position accuracy",
            len(records), new_count,
        )
        upload_records(records, WDGWARS_API_KEY, UPLOAD_URL)

        prev_icaos = current_icaos
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    if not WDGWARS_API_KEY:
        log.error("WDGWARS_API_KEY environment variable is required")
        sys.exit(1)
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped")
