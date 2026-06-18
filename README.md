# adsb-wdgw

A lightweight Docker sidecar that polls your [ultra feeder](https://github.com/sdr-enthusiasts/docker-adsb-ultrafeeder) (readsb/dump1090) for live ADS-B data and uploads it to [WDGoWars](https://wdgwars.pl) every N seconds.

No external dependencies — pure Python stdlib, single file, minimal footprint.

## Quick start

Add the service to your existing ultra feeder `docker-compose.yml`:

```yaml
adsb-wdgwars:
  image: ghcr.io/jaas666/adsb-wdgw:latest
  container_name: adsb-wdgwars
  restart: unless-stopped
  environment:
    - WDGWARS_API_KEY=${WDGWARS_API_KEY}
```

Add your API key to the `.env` file (copy `.env.example` as a starting point):

```
WDGWARS_API_KEY=your_64_char_key_from_wdgwars_profile
```

Then deploy:

```bash
docker compose up -d adsb-wdgwars
docker compose logs -f adsb-wdgwars
```

Your WDGoWars API key is available in your profile at [wdgwars.pl](https://wdgwars.pl).

## Configuration

All variables are optional except `WDGWARS_API_KEY`.

| Variable | Default | Description |
|---|---|---|
| `WDGWARS_API_KEY` | *(required)* | Your WDGoWars API key |
| `WDGWARS_ULTRA_FEEDER_URL` | `http://ultrafeeder/data/aircraft.json` | URL to your ultra feeder's `aircraft.json` |
| `WDGWARS_POLL_INTERVAL` | `30` | Seconds between polls |
| `WDGWARS_UPLOAD_URL` | `https://wdgwars.pl/api/upload/` | WDGoWars upload endpoint |
| `WDGWARS_BATCH_SIZE` | `500` | Aircraft records per upload request |
| `WDGWARS_LOG_LEVEL` | `INFO` | Log verbosity (`DEBUG`, `INFO`, `WARNING`) |
| `WDGWARS_STATION_LAT` | *(optional)* | Your real station latitude — see [Location spoofing](#location-spoofing) |
| `WDGWARS_STATION_LON` | *(optional)* | Your real station longitude |
| `WDGWARS_FAKE_LAT` | *(optional)* | Spoofed station latitude |
| `WDGWARS_FAKE_LON` | *(optional)* | Spoofed station longitude |

## Networking

If `adsb-wdgwars` is added to the **same compose stack** as ultra feeder, Docker's internal DNS resolves `ultrafeeder` automatically and no extra config is needed.

If ultra feeder runs in a **separate stack**, attach both to a shared external network:

```yaml
# In the ultra feeder compose:
networks:
  adsb-net:
    name: adsb-net

# In this compose:
services:
  adsb-wdgwars:
    networks:
      - adsb-net
    environment:
      - WDGWARS_ULTRA_FEEDER_URL=http://ultrafeeder/data/aircraft.json

networks:
  adsb-net:
    external: true
    name: adsb-net
```

Or point directly at the host:

```yaml
- WDGWARS_ULTRA_FEEDER_URL=http://host.docker.internal/data/aircraft.json
```

## Finding the right aircraft.json URL

The path varies depending on which web skin your ultra feeder runs:

```
http://<host>/data/aircraft.json            ← sdr-enthusiasts image (default)
http://<host>/tar1090/data/aircraft.json    ← tar1090 skin
http://<host>/skyaware/data/aircraft.json   ← piaware skin
```

Test from inside the Docker network:

```bash
docker run --rm --network container:ultrafeeder alpine/curl \
  -s "http://ultrafeeder/data/aircraft.json" | head -c 200
```

## Location spoofing

WDGoWars can infer your station's position from the centre of the aircraft coverage area you upload. If you want to appear at a different location, set all four variables and every aircraft position will be shifted by the delta before upload:

```yaml
- WDGWARS_STATION_LAT=52.2297
- WDGWARS_STATION_LON=21.0122
- WDGWARS_FAKE_LAT=48.8566
- WDGWARS_FAKE_LON=2.3522
```

All four must be set or spoofing is disabled.

## How it works

1. Every `WDGWARS_POLL_INTERVAL` seconds, `feeder.py` fetches `aircraft.json` from the ultra feeder over HTTP.
2. Aircraft records without a valid lat/lon are discarded.
3. The remaining records are converted to the WDGoWars schema (ICAO, callsign, lat, lon, alt, speed, heading).
4. Records are split into batches of up to `WDGWARS_BATCH_SIZE` and uploaded to `https://wdgwars.pl/api/upload/` using an HMAC-SHA256 signed envelope.
5. The server merges duplicate aircraft and averages GPS positions for better trilateration.

## Keeping up to date

The image is built automatically on every push to `main` via GitHub Actions and published to `ghcr.io/jaas666/adsb-wdgw:latest`.

To pull the latest version:

```bash
docker compose pull adsb-wdgwars
docker compose up -d adsb-wdgwars
```

## Contributing

Contributions and forks are welcome. The entire feeder logic lives in [`feeder.py`](feeder.py) — a single file with no external dependencies.

To build and run locally:

```bash
docker compose up --build
```

To test against a live ultra feeder without uploading to WDGoWars, set `WDGWARS_LOG_LEVEL=DEBUG` and temporarily point `WDGWARS_UPLOAD_URL` at a local HTTP listener (e.g. `python3 -m http.server 8000`).

Issues and pull requests are open at [github.com/jaas666/adsb-wdgw](https://github.com/jaas666/adsb-wdgw).