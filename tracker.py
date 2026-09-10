import os
import sys
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


POWERBI_PUSH_URL = os.environ["POWERBI_PUSH_URL"]

# Australia coverage areas.
# ADSB.lol point searches use nautical-mile radius.
AREAS = [
    ("Perth", -31.9523, 115.8613, 250),
    ("Geraldton", -28.7774, 114.6149, 250),
    ("Kalgoorlie", -30.7489, 121.4658, 250),
    ("Karratha", -20.7367, 116.8463, 250),
    ("Broome", -17.9614, 122.2359, 250),
    ("Darwin", -12.4634, 130.8456, 250),
    ("Alice Springs", -23.6980, 133.8807, 250),
    ("Mount Isa", -20.7256, 139.4927, 250),
    ("Cairns", -16.9186, 145.7781, 250),
    ("Townsville", -19.2589, 146.8169, 250),
    ("Brisbane", -27.4698, 153.0251, 250),
    ("Sydney", -33.8688, 151.2093, 250),
    ("Canberra", -35.2809, 149.1300, 250),
    ("Melbourne", -37.8136, 144.9631, 250),
    ("Hobart", -42.8821, 147.3272, 250),
    ("Adelaide", -34.9285, 138.6007, 250),
    ("Ceduna", -32.1261, 133.6763, 250),
]


# Initial conservative cargo-airline classification.
# We'll improve this later with registrations.
CARGO_OPERATORS = {
    "EFA": "Express Freighters Australia",
    "TMN": "Tasman Cargo",
    "FDX": "FedEx",
    "UPS": "UPS",
    "GTI": "Atlas Air",
    "CLX": "Cargolux",
    "CKS": "Kalitta Air",
    "BOX": "AeroLogic",
    "PAC": "Polar Air Cargo",
}


def number_or_none(value):
    if value is None or value == "ground":
        return None

    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def fetch_area(area):
    name, lat, lon, radius = area

    url = (
        f"https://api.adsb.lol/v2/point/"
        f"{lat}/{lon}/{radius}"
    )

    try:
        response = requests.get(
            url,
            timeout=30,
            headers={
                "User-Agent": "australia-flight-tracker/1.0"
            }
        )

        response.raise_for_status()

        aircraft = response.json().get("ac", [])

        print(
            f"{name}: {len(aircraft)} aircraft returned"
        )

        return name, aircraft

    except Exception as exc:
        print(
            f"ERROR querying {name}: {exc}",
            file=sys.stderr
        )

        return name, []


def create_row(ac, coverage_area, snapshot_utc):
    latitude = number_or_none(ac.get("lat"))
    longitude = number_or_none(ac.get("lon"))

    if latitude is None or longitude is None:
        return None

    seen_pos = number_or_none(ac.get("seen_pos"))

    # Ignore stale aircraft positions.
    if seen_pos is not None and seen_pos > 120:
        return None

    icao24 = str(ac.get("hex") or "").strip().lower()

    if not icao24:
        return None

    callsign = str(ac.get("flight") or "").strip().upper()
    registration = str(ac.get("r") or "").strip().upper()
    aircraft_type = str(ac.get("t") or "").strip().upper()

    operator = callsign[:3] if len(callsign) >= 3 else ""

    cargo_reason = ""
    likely_cargo = 0

    if operator in CARGO_OPERATORS:
        likely_cargo = 1
        cargo_reason = CARGO_OPERATORS[operator]

    altitude_raw = ac.get("alt_baro")

    on_ground = (
        1
        if isinstance(altitude_raw, str)
        and altitude_raw.lower() == "ground"
        else 0
    )

    return {
        "SnapshotUTC": snapshot_utc,
        "ICAO24": icao24,
        "Callsign": callsign,
        "Registration": registration,
        "AircraftType": aircraft_type,
        "Latitude": latitude,
        "Longitude": longitude,
        "AltitudeFt": number_or_none(altitude_raw),
        "GroundSpeedKt": number_or_none(ac.get("gs")),
        "TrackDeg": number_or_none(ac.get("track")),
        "VerticalRateFpm": number_or_none(
            ac.get("baro_rate")
        ),
        "SeenPosSeconds": seen_pos,
        "CoverageArea": coverage_area,
        "OperatorICAO": operator,
        "LikelyCargo": likely_cargo,
        "OnGround": on_ground,
        "CargoReason": cargo_reason,
    }


def push_rows(rows):
    # Keep requests reasonably small.
    batch_size = 25

    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]

        response = requests.post(
            POWERBI_PUSH_URL,
            json=batch,
            timeout=30
        )

        response.raise_for_status()

        print(
            f"Pushed rows "
            f"{start + 1}-"
            f"{start + len(batch)}"
        )


def main():
    snapshot_utc = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    print(f"Snapshot: {snapshot_utc}")

    results = []

    # Run several ADS-B queries concurrently instead of
    # making Power BI execute them sequentially.
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(fetch_area, area)
            for area in AREAS
        ]

        for future in as_completed(futures):
            results.append(future.result())

    # Deduplicate aircraft returned by overlapping circles.
    aircraft_by_icao = {}

    for coverage_area, aircraft_list in results:
        for ac in aircraft_list:
            row = create_row(
                ac,
                coverage_area,
                snapshot_utc
            )

            if row is None:
                continue

            icao = row["ICAO24"]

            existing = aircraft_by_icao.get(icao)

            if existing is None:
                aircraft_by_icao[icao] = row
                continue

            # Where two circles saw the same aircraft,
            # prefer the freshest position.
            existing_seen = existing.get("SeenPosSeconds")
            new_seen = row.get("SeenPosSeconds")

            if existing_seen is None:
                aircraft_by_icao[icao] = row

            elif (
                new_seen is not None
                and new_seen < existing_seen
            ):
                aircraft_by_icao[icao] = row

    rows = list(aircraft_by_icao.values())

    cargo_count = sum(
        row["LikelyCargo"] == 1
        for row in rows
    )

    print(f"Unique aircraft: {len(rows)}")
    print(f"Likely cargo: {cargo_count}")

    if not rows:
        raise RuntimeError(
            "No aircraft returned. Nothing will be pushed."
        )

    push_rows(rows)

    print("Power BI push complete.")


if __name__ == "__main__":
    main()
