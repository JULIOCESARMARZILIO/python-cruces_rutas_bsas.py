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

# Bounding box de la provincia y grilla (~200 celdas) para no pedir todo junto
LAT_MIN, LAT_MAX = -41.1, -33.2
LON_MIN, LON_MAX = -63.5, -56.6
FILAS, COLS = 16, 13   # 208 celdas de ~0.5° x ~0.53°
PAUSA_ENTRE_CELDAS = 3  # segundos

# Rutas numeradas + cualquier camino (sin número) que toque esas rutas,
# dentro de una celda (bbox) y de la provincia
def query_rutas(s, w, n, e):
    return f"""
[out:json][timeout:300][bbox:{s},{w},{n},{e}];
area({AREA_BSAS})->.bsas;
way["highway"]["ref"~"^(RN|RP)\\s?[0-9]+"](area.bsas)->.rutas;
way(around.rutas:0)["highway"~"^(primary|secondary|tertiary|unclassified|residential|track)$"](area.bsas)->.acc;
(.rutas; .acc;);
out body;
>;
out skel qt;
"""

# Gran Buenos Aires: se excluye todo lo que queda adentro del anillo de la
# RP 6 (polígono aproximado lon,lat), salvo los cruces sobre la propia RP 6.
POLIGONO_GBA = [
    (-59.03, -34.09), (-58.95, -34.17), (-58.96, -34.36), (-58.99, -34.50),
    (-58.96, -34.63), (-58.89, -34.79), (-58.76, -35.06), (-58.43, -35.04),
    (-58.19, -35.16), (-57.97, -34.96), (-57.84, -34.84), (-58.05, -34.72),
    (-58.33, -34.57), (-58.56, -34.40), (-58.75, -34.30), (-58.90, -34.20),
]
RUTA_LIMITE = "RP 6"


def punto_en_poligono(lon, lat, poly):
    adentro = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            adentro = not adentro
        j = i
    return adentro


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


HEADERS = {"User-Agent": "cruces-rutas-bsas/1.0 (proyecto de georreferenciacion; contacto via GitHub JULIOCESARMARZILIO)"}


def overpass(query, reintentos=4):
    espera = 30
    for i in range(reintentos):
        for url in OVERPASS_URLS:
            try:
                r = requests.post(url, data={"data": query}, headers=HEADERS, timeout=900)
            except requests.RequestException as e:
                print(f"  {url}: falló ({e.__class__.__name__})", flush=True)
                continue
            if r.status_code == 200:
                return r.json()
            print(f"  {url}: respondió {r.status_code}: {r.text[:300].strip()!r}", flush=True)
        print(f"Reintento {i + 1} en {espera} s...", flush=True)
        time.sleep(espera)
        espera = min(espera * 2, 240)
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
    print("Descargando rutas de OSM por celdas...", flush=True)
    dlat = (LAT_MAX - LAT_MIN) / FILAS
    dlon = (LON_MAX - LON_MIN) / COLS
    elementos, vistos = [], set()
    total = FILAS * COLS
    k = 0
    for i in range(FILAS):
        for j in range(COLS):
            k += 1
            s_, n_ = LAT_MIN + i * dlat, LAT_MIN + (i + 1) * dlat
            w_, e_ = LON_MIN + j * dlon, LON_MIN + (j + 1) * dlon
            print(f"Celda {k}/{total}", flush=True)
            try:
                res = overpass(query_rutas(s_, w_, n_, e_))
            except RuntimeError as ex:
                print(f"  celda {k} omitida: {ex}", flush=True)
                continue
            for el in res.get("elements", []):
                clave = (el["type"], el["id"])
                if clave not in vistos:
                    vistos.add(clave)
                    elementos.append(el)
            time.sleep(PAUSA_ENTRE_CELDAS)
    datos = {"elements": elementos}
    print(f"Elementos únicos descargados: {len(elementos)}", flush=True)

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
            if RUTA_LIMITE not in refs and punto_en_poligono(lon, lat, POLIGONO_GBA):
                continue  # Gran Buenos Aires: afuera del alcance
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
