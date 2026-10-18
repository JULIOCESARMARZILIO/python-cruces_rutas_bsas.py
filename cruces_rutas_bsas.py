"""
Cruces de rutas de la Provincia de Buenos Aires, georreferenciados.

Baja de OpenStreetMap (API Overpass) todas las rutas nacionales (RN) y
provinciales (RP) dentro de la provincia, detecta los nodos que comparten
dos o más rutas con número distinto (= cruce real, excluye puentes/pasos
a distinto nivel sin conexión) y exporta:

  - cruces_rutas_bsas.csv      (lat, lon, rutas que se cruzan, localidad cercana)
  - cruces_rutas_bsas.geojson  (para abrir en QGIS / Google Earth / Leaflet)

Uso:
    pip install requests
    python cruces_rutas_bsas.py

La localidad cercana se obtiene de los lugares (place=town/village/city)
de OSM, dentro de ~5 km del cruce.
"""

import csv
import json
import math
import time
from collections import defaultdict

import requests

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
AREA_BSAS = 3601632167  # relación OSM de la Provincia de Buenos Aires (1632167 + 3.6e9)
RADIO_LOCALIDAD_KM = 5

# Rutas numeradas + cualquier camino (sin número) que toque esas rutas
QUERY_RUTAS = f"""
[out:json][timeout:900];
area({AREA_BSAS})->.bsas;
way["highway"]["ref"~"^(RN|RP)\\s?[0-9]+"](area.bsas)->.rutas;
way(around.rutas:0)["highway"~"^(primary|secondary|tertiary|unclassified|residential|track)$"](area.bsas)->.acc;
(.rutas; .acc;);
out body;
>;
out skel qt;
"""

# Tipos de camino sin número que se consideran acceso a un pueblo.
# 'residential' y 'track' solo cuentan si el nombre dice "Acceso" (evita
# marcar cada calle de un pueblo que toca la ruta).
CLASES_ACCESO = {"primary", "secondary", "tertiary", "unclassified"}
CLASES_ACCESO_SOLO_CON_NOMBRE = {"residential", "track"}

QUERY_LOCALIDADES = f"""
[out:json][timeout:300];
area({AREA_BSAS})->.bsas;
node["place"~"^(city|town|village)$"]["name"](area.bsas);
out body;
"""


def overpass(query, reintentos=3):
    for i in range(reintentos):
        for url in OVERPASS_URLS:
            try:
                print(f"Consultando {url} ...", flush=True)
                r = requests.post(url, data={"data": query}, timeout=900)
            except requests.RequestException as e:
                print(f"  falló: {e}", flush=True)
                continue
            if r.status_code == 200:
                return r.json()
            print(f"  respondió {r.status_code}", flush=True)
        print(f"Reintento {i + 1} en 30 s...", flush=True)
        time.sleep(30)
    raise RuntimeError("No se pudo consultar ningún servidor Overpass")


def normalizar_ref(ref):
    """'RP 51' / 'RP51' / 'RP 51;RN 3' -> conjunto {'RP 51', 'RN 3'}"""
    out = set()
    for parte in ref.replace(",", ";").split(";"):
        parte = parte.strip().upper().replace(" ", "")
        if parte.startswith(("RN", "RP")):
            out.add(f"{parte[:2]} {parte[2:]}")
    return out


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def main():
    print("Descargando rutas de OSM (puede tardar unos minutos)...")
    datos = overpass(QUERY_RUTAS)

    nodos = {}                           # id -> (lat, lon)
    rutas_por_nodo = defaultdict(set)    # id nodo -> {'RN 5', 'RP 51', ...}
    accesos_por_nodo = defaultdict(set)  # id nodo -> {'Acceso a Roque Pérez', ...}

    for el in datos["elements"]:
        if el["type"] == "node":
            nodos[el["id"]] = (el["lat"], el["lon"])

    for el in datos["elements"]:
        if el["type"] != "way":
            continue
        tags = el.get("tags", {})
        refs = normalizar_ref(tags.get("ref", ""))
        if refs:
            for nid in el["nodes"]:
                rutas_por_nodo[nid] |= refs
            continue
        clase = tags.get("highway", "")
        nombre = tags.get("name", "")
        es_acceso = clase in CLASES_ACCESO or (
            clase in CLASES_ACCESO_SOLO_CON_NOMBRE and "acceso" in nombre.lower())
        if not es_acceso:
            continue
        etiqueta = nombre or f"camino sin nombre ({clase}, way {el['id']})"
        # solo los extremos del camino: ahí es donde empalma con la ruta
        for nid in (el["nodes"][0], el["nodes"][-1]):
            accesos_por_nodo[nid].add(etiqueta)

    cruces = []
    for nid, refs in rutas_por_nodo.items():
        if nid not in nodos:
            continue
        accs = accesos_por_nodo.get(nid, set())
        if len(refs) >= 2 or accs:
            lat, lon = nodos[nid]
            cruces.append({"nodo_osm": nid, "lat": lat, "lon": lon,
                           "rutas": sorted(refs), "accesos": sorted(accs),
                           "tipo": "ruta-ruta" if len(refs) >= 2 else "acceso"})

    print(f"Cruces encontrados: {len(cruces)} "
          f"({sum(c['tipo']=='ruta-ruta' for c in cruces)} ruta-ruta, "
          f"{sum(c['tipo']=='acceso' for c in cruces)} accesos)")

    print("Descargando localidades...")
    locs = [(e["tags"]["name"], e["lat"], e["lon"])
            for e in overpass(QUERY_LOCALIDADES)["elements"]]

    for c in cruces:
        mejor, dmin = "", 1e9
        for nombre, la, lo in locs:
            d = haversine_km(c["lat"], c["lon"], la, lo)
            if d < dmin:
                mejor, dmin = nombre, d
        c["localidad_cercana"] = mejor if dmin <= RADIO_LOCALIDAD_KM else ""
        c["dist_localidad_km"] = round(dmin, 2) if dmin <= RADIO_LOCALIDAD_KM else ""

    cruces.sort(key=lambda c: (c["tipo"], c["rutas"], c["accesos"]))

    def cruza_con(c):
        return " ; ".join(c["rutas"][1:] + c["accesos"])

    # ---- CSV ----
    with open("cruces_rutas_bsas.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["tipo", "ruta", "cruza_con", "lat", "lon",
                    "localidad_cercana", "dist_localidad_km", "nodo_osm", "google_maps"])
        for c in cruces:
            w.writerow([c["tipo"], c["rutas"][0], cruza_con(c), c["lat"], c["lon"],
                        c["localidad_cercana"], c["dist_localidad_km"], c["nodo_osm"],
                        f"https://www.google.com/maps?q={c['lat']},{c['lon']}"])

    # ---- GeoJSON ----
    geojson = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [c["lon"], c["lat"]]},
            "properties": {
                "tipo": c["tipo"],
                "ruta": c["rutas"][0],
                "cruza_con": cruza_con(c),
                "localidad_cercana": c["localidad_cercana"],
                "dist_localidad_km": c["dist_localidad_km"],
                "nodo_osm": c["nodo_osm"],
            },
        } for c in cruces],
    }
    with open("cruces_rutas_bsas.geojson", "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False, indent=1)

    print("Listo: cruces_rutas_bsas.csv y cruces_rutas_bsas.geojson")


if __name__ == "__main__":
    main()
