import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

API_KEY = os.environ["BODS_API_KEY"]
# North of England: Liverpool / Manchester / Leeds / Lancaster
BBOX = "-4.0,53.0,-1.0,54.6"
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
    try:
        response = requests.get(AVL_URL, timeout=60)
        if response.status_code != 200:
            print(f"Fetch failed with status: {response.status_code}")
            return

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
            print(f"Ingested {len(lines)} buses (questdb {r.status_code})")
        else:
            print("Feed returned no vehicles")

    except Exception as e:
        print(f"Ingest failed: {e}")


if __name__ == "__main__":
    while True:
        ingest()
        time.sleep(30)
