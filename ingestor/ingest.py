import logging
import os
import signal
import sys
import threading
import xml.etree.ElementTree as ET
from datetime import datetime

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)

API_KEY = os.environ["BODS_API_KEY"]
# Mainland Great Britain. Excludes Northern Ireland (not in BODS anyway),
# Isle of Man and the Channel Islands (not part of the UK, not in BODS),
# Orkney, Shetland, the Outer Hebrides and the Scillies (a rectangular bbox
# can't carve out an enclave, so Isle of Wight -- hard against the mainland
# south coast -- still sneaks in; measured at 129 vehicles out of 24,536
# GB-wide, i.e. all the excluded islands combined cost 0.5% of the feed).
BBOX = "-6.3,49.9,1.8,58.75"
AVL_URL = (
    "https://data.bus-data.dft.gov.uk/api/v1/datafeed/"
    f"?api_key={API_KEY}&boundingBox={BBOX}"
)
# ILP over HTTP lives on the REST port (/write); 9009 is the raw TCP socket
QUESTDB_ILP_URL = os.environ["QUESTDB_URL"] + "/write"
NS = {"siri": "http://www.siri.org.uk/siri"}


def clean(sym):
    return sym.replace(" ", "_").replace(",", "_") if sym else "UKN"


def esc_tag(val):
    """ILP tag values cannot carry spaces, commas or equals signs unescaped."""
    return val.replace(" ", "_").replace(",", "_").replace("=", "_")


def esc_str(val):
    """ILP string field values are double-quoted; escape quotes and backslashes."""
    return val.replace("\\", "\\\\").replace('"', '\\"')


def text(el, path):
    node = el.find(path, NS)
    return node.text.strip() if node is not None and node.text else None


def epoch_ms(stamp):
    """SIRI timestamps are ISO8601, usually with a +00:00 offset."""
    if not stamp:
        return None
    try:
        return int(datetime.fromisoformat(stamp).timestamp() * 1000)
    except ValueError:
        return None


# SIRI element -> ILP string field. Tags and numerics are handled separately
# because ILP needs a different syntax for each.
STRING_FIELDS = (
    ("journey_ref", ".//siri:FramedVehicleJourneyRef/siri:DatedVehicleJourneyRef"),
    ("published_line", ".//siri:PublishedLineName"),
    ("origin_ref", ".//siri:OriginRef"),
    ("origin_name", ".//siri:OriginName"),
    ("dest_ref", ".//siri:DestinationRef"),
    ("dest_name", ".//siri:DestinationName"),
    ("block_ref", ".//siri:BlockRef"),
)
TIME_FIELDS = (
    ("origin_aimed_dep", ".//siri:OriginAimedDepartureTime"),
    ("dest_aimed_arr", ".//siri:DestinationAimedArrivalTime"),
    ("recorded_at", "./siri:RecordedAtTime"),
)


def build_line(v):
    """One ILP line per VehicleActivity, or None if it lacks a usable position."""
    ref = clean(text(v, ".//siri:VehicleRef"))
    lat = float(text(v, ".//siri:Latitude"))
    lon = float(text(v, ".//siri:Longitude"))
    line = clean(text(v, ".//siri:LineRef"))

    # Tags are indexed SYMBOLs in QuestDB, so only low-cardinality values go here.
    tags = [f"bus_id={ref}", f"line={line}"]
    for name, path in (("operator", ".//siri:OperatorRef"), ("direction", ".//siri:DirectionRef")):
        val = text(v, path)
        if val:
            tags.append(f"{name}={esc_tag(val)}")

    bearing = text(v, ".//siri:Bearing")
    fields = [f"lat={lat}", f"lon={lon}", f"bearing={float(bearing) if bearing else 0.0}"]
    for name, path in STRING_FIELDS:
        val = text(v, path)
        if val:
            fields.append(f'{name}="{esc_str(val)}"')
    for name, path in TIME_FIELDS:
        ms = epoch_ms(text(v, path))
        if ms is not None:
            fields.append(f"{name}={ms}i")

    return "bus_positions," + ",".join(tags) + " " + ",".join(fields)


def ingest():
    """Returns True on success (an empty feed still counts), False on a
    failure that should count toward the backoff in main()."""
    try:
        response = requests.get(AVL_URL, timeout=60)
        if response.status_code != 200:
            logging.info("Fetch failed with status: %s", response.status_code)
            return False

        root = ET.fromstring(response.content)
        lines = []
        for v in root.findall(".//siri:VehicleActivity", NS):
            try:
                lines.append(build_line(v))
            except (AttributeError, TypeError, ValueError):
                continue

        if lines:
            r = requests.post(
                QUESTDB_ILP_URL, data="\n".join(lines) + "\n", timeout=10
            )
            logging.info("Ingested %d buses (questdb %s)", len(lines), r.status_code)
        else:
            logging.info("Feed returned no vehicles")
        return True

    except Exception:
        logging.exception("Ingest failed")
        return False


# Nominal poll rate stays 60s. On consecutive failures back off (doubling)
# up to a ~15 minute ceiling so a dead upstream doesn't get hammered every
# 60s; the first success resets straight back to 60s.
POLL_S = 60
MAX_BACKOFF_S = 15 * 60


def main():
    stop = threading.Event()

    def handle_signal(signum, frame):
        stop.set()

    # So `docker stop` (SIGTERM) exits cleanly within the grace period
    # instead of blocking in the wait and getting SIGKILLed.
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    consecutive_failures = 0
    while not stop.is_set():
        if ingest():
            consecutive_failures = 0
            interval = POLL_S
        else:
            consecutive_failures += 1
            interval = min(POLL_S * (2 ** consecutive_failures), MAX_BACKOFF_S)
        # Event.wait (not time.sleep) so a signal during the wait returns
        # immediately instead of only being noticed on the next wake.
        stop.wait(interval)


if __name__ == "__main__":
    main()
