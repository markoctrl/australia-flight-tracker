import os
import sys
import time
import random
from datetime import datetime, timezone

import requests


# ============================================================
# CONFIGURATION
# ============================================================

POWERBI_PUSH_URL = os.environ["POWERBI_PUSH_URL"]

ADSB_BASE_URL = "https://api.adsb.lol/v2/point"

# Delay between each Australian area query.
# 5 seconds is intentionally conservative to reduce 420/429 responses.
REQUEST_DELAY_SECONDS = 5

# Retry an area this many times before giving up on it.
MAX_ATTEMPTS = 4

# Power BI rows per POST request.
POWERBI_BATCH_SIZE = 25


# ============================================================
# AUSTRALIAN COVERAGE AREAS
# ============================================================

AREAS = [
    ("Perth", -31.9523, 115.8613, 250),
    ("Broome", -17.9614, 122.2359, 250),
    ("Darwin", -12.4634, 130.8456, 250),
    ("Alice Springs", -23.6980, 133.8807, 250),
    ("Adelaide", -34.9285, 138.6007, 250),
    ("Melbourne", -37.8136, 144.9631, 250),
    ("Sydney", -33.8688, 151.2093, 250),
    ("Brisbane", -27.4698, 153.0251, 250),
]


# ============================================================
# CARGO CLASSIFICATION
# ============================================================

CARGO_OPERATORS = {
    "EFA": "Express Freighters Australia",
    "TMN": "Tasman Cargo Airlines",
    "FDX": "FedEx Express",
    "UPS": "UPS Airlines",
    "CLX": "Cargolux",
    "CKS": "Kalitta Air",
    "BOX": "AeroLogic",
    "PAC": "Polar Air Cargo",
    "GTI": "Atlas Air",
}

CARGO_REGISTRATIONS = {
    # Example structure:
    # "VH-ABC": "Qantas Freight",
    # "VH-XYZ": "Qantas Freight",
}


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update(
    {
        "User-Agent": "AustraliaFlightTracker/1.0"
    }
)


# ============================================================
# HELPERS
# ============================================================

def number_or_none(value):
    """
    Convert an ADS-B value into a float.

    ADSB.lol can return values such as:
        35000
        412.5
        "ground"
        null

    Power BI expects numeric values or null.
    """

    if value is None:
        return None

    if isinstance(value, str):
        if value.lower() == "ground":
            return None

    try:
        return float(value)

    except (ValueError, TypeError):
        return None


def get_retry_delay(response, attempt):
    """
    Determine how long to wait after rate limiting.

    Prefer the server's Retry-After header when available.
    Otherwise use exponential backoff with random jitter.
    """

    retry_after = response.headers.get("Retry-After")

    if retry_after:
        try:
            return min(float(retry_after), 120)

        except ValueError:
            pass

    # Attempt 1 = ~10 seconds
    # Attempt 2 = ~20 seconds
    # Attempt 3 = ~40 seconds
    # Attempt 4 = ~80 seconds

    delay = 10 * (2 ** (attempt - 1))

    delay += random.uniform(1, 4)

    return min(delay, 120)


# ============================================================
# ADSB.LOL QUERY
# ============================================================

def fetch_area(area):
    """
    Query one ADSB.lol point endpoint.

    Returns:
        area name
        aircraft list
    """

    name, lat, lon, radius = area

    url = (
        f"{ADSB_BASE_URL}/"
        f"{lat}/{lon}/{radius}"
    )

    for attempt in range(1, MAX_ATTEMPTS + 1):

        try:
            print(
                f"Querying {name} "
                f"(attempt {attempt}/{MAX_ATTEMPTS})..."
            )

            response = session.get(
                url,
                timeout=30
            )


            # ------------------------------------------------
            # RATE LIMITED
            # ------------------------------------------------

            if response.status_code in (420, 429):

                wait_seconds = get_retry_delay(
                    response,
                    attempt
                )

                print(
                    f"RATE LIMITED: {name} returned "
                    f"HTTP {response.status_code}. "
                    f"Waiting {wait_seconds:.1f} seconds."
                )

                time.sleep(wait_seconds)

                continue


            # ------------------------------------------------
            # TEMPORARY SERVER FAILURE
            # ------------------------------------------------

            if response.status_code >= 500:

                wait_seconds = get_retry_delay(
                    response,
                    attempt
                )

                print(
                    f"SERVER ERROR: {name} returned "
                    f"HTTP {response.status_code}. "
                    f"Waiting {wait_seconds:.1f} seconds."
                )

                time.sleep(wait_seconds)

                continue


            # ------------------------------------------------
            # OTHER HTTP ERRORS
            # ------------------------------------------------

            response.raise_for_status()


            # ------------------------------------------------
            # SUCCESS
            # ------------------------------------------------

            data = response.json()

            aircraft = data.get(
                "ac",
                []
            )

            print(
                f"{name}: "
                f"{len(aircraft)} aircraft returned"
            )

            return name, aircraft


        except requests.RequestException as exc:

            print(
                f"REQUEST ERROR querying {name}: {exc}",
                file=sys.stderr
            )

            if attempt < MAX_ATTEMPTS:

                wait_seconds = (
                    5 * attempt
                    + random.uniform(1, 3)
                )

                print(
                    f"Waiting {wait_seconds:.1f} seconds "
                    f"before retrying {name}."
                )

                time.sleep(wait_seconds)


        except ValueError as exc:

            print(
                f"INVALID JSON returned for {name}: {exc}",
                file=sys.stderr
            )

            return name, []


    print(
        f"GIVING UP on {name} after "
        f"{MAX_ATTEMPTS} attempts.",
        file=sys.stderr
    )

    return name, []


# ============================================================
# CARGO CLASSIFICATION
# ============================================================

def classify_cargo(callsign, registration):
    """
    Returns:

        LikelyCargo
        CargoReason

    LikelyCargo:
        1 = identified as likely cargo
        0 = not identified as cargo

    Important:
        0 does NOT mean passenger.
    """


    # --------------------------------------------------------
    # METHOD 1:
    # Known freight aircraft registration
    # --------------------------------------------------------

    if registration in CARGO_REGISTRATIONS:

        operator_name = CARGO_REGISTRATIONS[
            registration
        ]

        return (
            1,
            f"Known freighter registration - {operator_name}"
        )


    # --------------------------------------------------------
    # METHOD 2:
    # Callsign/operator
    # --------------------------------------------------------

    if callsign and len(callsign) >= 3:

        operator_icao = callsign[:3]

        if operator_icao in CARGO_OPERATORS:

            operator_name = CARGO_OPERATORS[
                operator_icao
            ]

            return (
                1,
                f"Cargo operator - {operator_name}"
            )


    # --------------------------------------------------------
    # NOT IDENTIFIED AS CARGO
    # --------------------------------------------------------

    return (
        0,
        ""
    )


# ============================================================
# TRANSFORM AIRCRAFT
# ============================================================

def create_row(
    aircraft,
    coverage_area,
    snapshot_utc
):

    latitude = number_or_none(
        aircraft.get("lat")
    )

    longitude = number_or_none(
        aircraft.get("lon")
    )


    # --------------------------------------------------------
    # MUST HAVE A POSITION
    # --------------------------------------------------------

    if latitude is None or longitude is None:
        return None


    # --------------------------------------------------------
    # REMOVE STALE POSITIONS
    # --------------------------------------------------------

    seen_pos = number_or_none(
        aircraft.get("seen_pos")
    )

    if (
        seen_pos is not None
        and seen_pos > 120
    ):
        return None


    # --------------------------------------------------------
    # ICAO AIRCRAFT ID
    # --------------------------------------------------------

    icao24 = str(
        aircraft.get("hex") or ""
    ).strip().lower()

    if not icao24:
        return None


    # --------------------------------------------------------
    # BASIC AIRCRAFT INFORMATION
    # --------------------------------------------------------

    callsign = str(
        aircraft.get("flight") or ""
    ).strip().upper()

    registration = str(
        aircraft.get("r") or ""
    ).strip().upper()

    aircraft_type = str(
        aircraft.get("t") or ""
    ).strip().upper()


    # --------------------------------------------------------
    # OPERATOR ICAO
    # --------------------------------------------------------

    if len(callsign) >= 3:
        operator_icao = callsign[:3]

    else:
        operator_icao = ""


    # --------------------------------------------------------
    # CARGO CLASSIFICATION
    # --------------------------------------------------------

    (
        likely_cargo,
        cargo_reason
    ) = classify_cargo(
        callsign,
        registration
    )


    # --------------------------------------------------------
    # AIRBORNE / GROUND
    # --------------------------------------------------------

    altitude_raw = aircraft.get(
        "alt_baro"
    )

    on_ground = 0

    if isinstance(
        altitude_raw,
        str
    ):

        if altitude_raw.lower() == "ground":
            on_ground = 1


    # --------------------------------------------------------
    # FINAL POWER BI RECORD
    # --------------------------------------------------------

    return {

        "SnapshotUTC":
            snapshot_utc,

        "ICAO24":
            icao24,

        "Callsign":
            callsign,

        "Registration":
            registration,

        "AircraftType":
            aircraft_type,

        "Latitude":
            latitude,

        "Longitude":
            longitude,

        "AltitudeFt":
            number_or_none(
                altitude_raw
            ),

        "GroundSpeedKt":
            number_or_none(
                aircraft.get("gs")
            ),

        "TrackDeg":
            number_or_none(
                aircraft.get("track")
            ),

        "VerticalRateFpm":
            number_or_none(
                aircraft.get("baro_rate")
            ),

        "SeenPosSeconds":
            seen_pos,

        "CoverageArea":
            coverage_area,

        "OperatorICAO":
            operator_icao,

        "LikelyCargo":
            likely_cargo,

        "OnGround":
            on_ground,

        "CargoReason":
            cargo_reason,
    }


# ============================================================
# POWER BI
# ============================================================

def push_rows(rows):
    """
    Send processed aircraft records to Power BI
    in small batches.
    """

    total_rows = len(rows)

    print(
        f"Preparing to push "
        f"{total_rows} rows to Power BI."
    )

    for start in range(
        0,
        total_rows,
        POWERBI_BATCH_SIZE
    ):

        batch = rows[
            start:
            start + POWERBI_BATCH_SIZE
        ]

        try:

            response = requests.post(
                POWERBI_PUSH_URL,
                json=batch,
                timeout=30
            )

            response.raise_for_status()

            print(
                f"Power BI: pushed rows "
                f"{start + 1}-"
                f"{start + len(batch)} "
                f"of {total_rows}"
            )


        except requests.RequestException as exc:

            print(
                f"POWER BI PUSH FAILED "
                f"for rows "
                f"{start + 1}-"
                f"{start + len(batch)}: "
                f"{exc}",
                file=sys.stderr
            )

            raise


# ============================================================
# MAIN
# ============================================================

def main():

    # One timestamp for the whole Australian snapshot.

    snapshot_utc = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    print("=" * 60)

    print(
        f"Australia Flight Tracker"
    )

    print(
        f"Snapshot UTC: {snapshot_utc}"
    )

    print("=" * 60)


    # --------------------------------------------------------
    # COLLECT AREAS SEQUENTIALLY
    # --------------------------------------------------------

    results = []

    successful_areas = 0

    failed_areas = 0


    for index, area in enumerate(AREAS):

        area_name = area[0]

        name, aircraft = fetch_area(area)

        results.append(
            (
                name,
                aircraft
            )
        )

        if aircraft:
            successful_areas += 1

        else:
            failed_areas += 1


        # Do not hit ADSB.lol with another request immediately.

        if index < len(AREAS) - 1:

            print(
                f"Waiting "
                f"{REQUEST_DELAY_SECONDS} seconds "
                f"before next area..."
            )

            time.sleep(
                REQUEST_DELAY_SECONDS
            )


    # --------------------------------------------------------
    # TRANSFORM + DEDUPLICATE
    # --------------------------------------------------------

    aircraft_by_icao = {}


    for (
        coverage_area,
        aircraft_list
    ) in results:

        for aircraft in aircraft_list:

            row = create_row(
                aircraft,
                coverage_area,
                snapshot_utc
            )

            if row is None:
                continue


            icao24 = row[
                "ICAO24"
            ]


            existing = aircraft_by_icao.get(
                icao24
            )


            # First time we've seen this aircraft.

            if existing is None:

                aircraft_by_icao[
                    icao24
                ] = row

                continue


            # ------------------------------------------------
            # SAME AIRCRAFT FOUND BY OVERLAPPING AREAS
            #
            # Keep whichever observation has the freshest
            # ADS-B position.
            # ------------------------------------------------

            existing_seen = existing.get(
                "SeenPosSeconds"
            )

            new_seen = row.get(
                "SeenPosSeconds"
            )


            existing_score = (
                existing_seen
                if existing_seen is not None
                else 999999
            )

            new_score = (
                new_seen
                if new_seen is not None
                else 999999
            )


            if new_score < existing_score:

                aircraft_by_icao[
                    icao24
                ] = row


    rows = list(
        aircraft_by_icao.values()
    )


    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    cargo_rows = [
        row
        for row in rows
        if row["LikelyCargo"] == 1
    ]


    airborne_rows = [
        row
        for row in rows
        if row["OnGround"] == 0
    ]


    print("")
    print("=" * 60)
    print("SNAPSHOT SUMMARY")
    print("=" * 60)

    print(
        f"Successful coverage areas: "
        f"{successful_areas}/{len(AREAS)}"
    )

    print(
        f"Failed/empty coverage areas: "
        f"{failed_areas}"
    )

    print(
        f"Unique aircraft: "
        f"{len(rows)}"
    )

    print(
        f"Airborne aircraft: "
        f"{len(airborne_rows)}"
    )

    print(
        f"Likely cargo aircraft: "
        f"{len(cargo_rows)}"
    )


    # Show cargo matches in GitHub logs.

    if cargo_rows:

        print("")
        print("LIKELY CARGO:")

        for row in sorted(
            cargo_rows,
            key=lambda x: x["Callsign"]
        ):

            print(
                f"  "
                f"{row['Callsign'] or 'NO CALLSIGN'} | "
                f"{row['Registration'] or 'NO REG'} | "
                f"{row['AircraftType'] or 'UNKNOWN'} | "
                f"{row['CargoReason']}"
            )


    # --------------------------------------------------------
    # SAFETY CHECK
    # --------------------------------------------------------

    if not rows:

        raise RuntimeError(
            "No valid aircraft were collected. "
            "Nothing will be pushed to Power BI."
        )


    # --------------------------------------------------------
    # PUSH TO POWER BI
    # --------------------------------------------------------

    print("")
    print("=" * 60)
    print("POWER BI PUSH")
    print("=" * 60)

    push_rows(rows)

    print("")
    print(
        "Australia flight snapshot successfully "
        "pushed to Power BI."
    )


if __name__ == "__main__":
    main()
