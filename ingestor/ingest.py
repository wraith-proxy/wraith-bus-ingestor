import os
import time
import xml.etree.ElementTree as ET

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
                ref = clean(v.find(".//siri:VehicleRef", NS).text)
                lat = float(v.find(".//siri:Latitude", NS).text)
                lon = float(v.find(".//siri:Longitude", NS).text)
                line = clean(v.find(".//siri:LineRef", NS).text)
                bearing_el = v.find(".//siri:Bearing", NS)
                bearing = float(bearing_el.text) if bearing_el is not None else 0.0
                lines.append(
                    f"bus_positions,bus_id={ref},line={line} "
                    f"lat={lat},lon={lon},bearing={bearing}"
                )
            except (AttributeError, ValueError):
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
