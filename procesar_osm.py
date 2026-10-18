"""
Cruces de rutas + silos y puertos, a partir del mapa completo de Argentina
de OpenStreetMap (extracto de Geofabrik), procesado localmente con pyosmium.

Provincias: Buenos Aires (sin el Gran Buenos Aires, hasta la RP 6 inclusive),
Santa Fe, Entre Ríos, Córdoba, La Pampa, Santiago del Estero.

Salida (en DIR_SALIDA):
  cruces_<provincia>.csv / .geojson      cruces ruta-ruta y ruta-acceso
  silos_puertos.csv / .geojson            plantas de silos/acopio y puertos
  resumen.json                            conteos

Uso:
    pip install -r requirements.txt
    python procesar_osm.py [ruta/al/argentina-latest.osm.pbf]
Si no se pasa el archivo, se descarga de Geofabrik a DIR_DATOS.
"""

import csv
import json
import math
import os
import sys
import time
from collections import defaultdict

import osmium
import requests
from shapely import wkb
from shapely.geometry import Point
from shapely.prepared import prep

DIR_DATOS = os.environ.get("DIR_DATOS", "/data")
DIR_SALIDA = os.environ.get("DIR_SALIDA", os.path.join(DIR_DATOS, "salida"))
PBF_URL = "https://download.geofabrik.de/south-america/argentina-latest.osm.pbf"
PBF = os.path.join(DIR_DATOS, "argentina-latest.osm.pbf")

PROVINCIAS = {
    "Buenos Aires": "buenos_aires",
    "Santa Fe": "santa_fe",
    "Entre Ríos": "entre_rios",
    "Córdoba": "cordoba",
    "La Pampa": "la_pampa",
    "Santiago del Estero": "santiago_del_estero",
}
# bbox que cubre las seis provincias: se ignora todo lo que queda afuera
BBOX = (-41.2, -66.6, -25.4, -57.4)   # lat_min, lon_min, lat_max, lon_max

RADIO_LOCALIDAD_KM = 5
CLASES_ACCESO = {"primary", "secondary", "tertiary", "unclassified"}
CLASES_ACCESO_SOLO_CON_NOMBRE = {"residential", "track"}

# Gran Buenos Aires: adentro del anillo de la RP 6 (polígono aprox. lon,lat)
POLIGONO_GBA = [
    (-59.03, -34.09), (-58.95, -34.17), (-58.96, -34.36), (-58.99, -34.50),
    (-58.96, -34.63), (-58.89, -34.79), (-58.76, -35.06), (-58.43, -35.04),
    (-58.19, -35.16), (-57.97, -34.96), (-57.84, -34.84), (-58.05, -34.72),
    (-58.33, -34.57), (-58.56, -34.40), (-58.75, -34.30), (-58.90, -34.20),
]
RUTA_LIMITE_GBA = "RP 6"


# ----------------------------------------------------------------- utilidades
def log(msg):
    print(msg, flush=True)


def en_bbox(lat, lon):
    return BBOX[0] <= lat <= BBOX[2] and BBOX[1] <= lon <= BBOX[3]


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


def normalizar_ref(ref):
    """'RP 51' / 'RP51' / 'RP 51;RN 3' -> {'RP 51', 'RN 3'}"""
    out = set()
    for parte in (ref or "").replace(",", ";").split(";"):
        parte = parte.strip().upper().replace(" ", "")
        if parte.startswith(("RN", "RP")) and parte[2:3].isdigit():
            out.add(f"{parte[:2]} {parte[2:]}")
    return out


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def descargar_pbf():
    if os.path.exists(PBF) and os.path.getsize(PBF) > 100_000_000:
        log(f"Mapa ya descargado: {PBF} ({os.path.getsize(PBF) // 1_000_000} MB)")
        return
    os.makedirs(DIR_DATOS, exist_ok=True)
    tmp = PBF + ".parcial"
    log(f"Descargando {PBF_URL} ...")
    with requests.get(PBF_URL, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        bajado, ultimo = 0, time.time()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                bajado += len(chunk)
                if time.time() - ultimo > 15:
                    log(f"  {bajado // 1_000_000} / {total // 1_000_000} MB")
                    ultimo = time.time()
    os.replace(tmp, PBF)
    log(f"Descarga completa: {bajado // 1_000_000} MB")


# ------------------------------------------------------ pasada 1: límites prov.
class Limites(osmium.SimpleHandler):
    """Polígonos de las provincias (relaciones admin_level=4)."""

    def __init__(self):
        super().__init__()
        self.fab = osmium.geom.WKBFactory()
        self.poligonos = {}

    def area(self, a):
        t = a.tags
        if t.get("boundary") != "administrative" or t.get("admin_level") != "4":
            return
        nombre = t.get("name", "")
        if nombre in PROVINCIAS and nombre not in self.poligonos:
            try:
                self.poligonos[nombre] = wkb.loads(self.fab.create_multipolygon(a), hex=True)
                log(f"  límite cargado: {nombre}")
            except Exception as e:  # noqa
                log(f"  no se pudo armar el polígono de {nombre}: {e}")


# ------------------------------------------------------- pasada 2: vías y POIs
class Vias(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.nodos = {}                          # nid -> (lat, lon) (solo los que importan)
        self.rutas_por_nodo = defaultdict(set)   # nid -> {'RN 5', ...}
        self.combos_por_nodo = defaultdict(set)  # nid -> {frozenset(refs de cada vía)}
        self.accesos_por_nodo = defaultdict(set)  # nid -> {'Acceso a X', ...}
        self.localidades = []                    # (nombre, lat, lon)
        self.pois = []                           # silos y puertos
        self.vias_ruta = []                      # (refs, [nid, ...]) de rutas RN/RP
        self.n_ways = 0

    # --- nodos: localidades y POIs puntuales
    def node(self, n):
        t = n.tags
        if not t:
            return
        if not n.location.valid():
            return
        lat, lon = n.location.lat, n.location.lon
        if not en_bbox(lat, lon):
            return
        if t.get("place") in ("city", "town", "village") and "name" in t:
            self.localidades.append((t["name"], lat, lon))
        self._poi(t, lat, lon, "node", n.id)

    # --- vías: rutas, accesos y POIs areales
    def way(self, w):
        t = w.tags
        if not t:
            return
        self.n_ways += 1
        if self.n_ways % 1_000_000 == 0:
            log(f"  vías procesadas: {self.n_ways // 1_000_000} M")
        nl = w.nodes
        if len(nl) == 0:
            return
        try:
            loc0 = nl[0].location
        except osmium.InvalidLocationError:
            return
        if not loc0.valid() or not en_bbox(loc0.lat, loc0.lon):
            return

        hw = t.get("highway")
        if hw:
            refs = normalizar_ref(t.get("ref"))
            if refs:
                nids = []
                for nr in nl:
                    if nr.location.valid():
                        self.nodos[nr.ref] = (nr.location.lat, nr.location.lon)
                        self.rutas_por_nodo[nr.ref] |= refs
                        self.combos_por_nodo[nr.ref].add(frozenset(refs))
                        nids.append(nr.ref)
                if len(nids) >= 2:
                    self.vias_ruta.append((refs, nids))
                return
            nombre = t.get("name", "")
            es_acceso = hw in CLASES_ACCESO or (
                hw in CLASES_ACCESO_SOLO_CON_NOMBRE and "acceso" in nombre.lower())
            if es_acceso:
                etiqueta = nombre or f"camino sin nombre ({hw}, way {w.id})"
                for nr in (nl[0], nl[-1]):
                    if nr.location.valid():
                        self.nodos[nr.ref] = (nr.location.lat, nr.location.lon)
                        self.accesos_por_nodo[nr.ref].add(etiqueta)
            return

        # POI areal: centroide aproximado
        lats = [nr.location.lat for nr in nl if nr.location.valid()]
        lons = [nr.location.lon for nr in nl if nr.location.valid()]
        if lats:
            self._poi(t, sum(lats) / len(lats), sum(lons) / len(lons), "way", w.id)

    def _poi(self, t, lat, lon, tipo_osm, oid):
        nombre = t.get("name", "")
        nl = nombre.lower()
        cat = None
        if t.get("man_made") == "silo" or t.get("industrial") in ("grain", "grain_elevator") \
                or "silo" in nl or "acopio" in nl or "cereal" in nl:
            cat = "silos/acopio"
        elif t.get("harbour") == "yes" or t.get("industrial") == "port" \
                or t.get("landuse") == "port" or t.get("man_made") == "pier" and "puerto" in nl \
                or nl.startswith("puerto ") or "terminal portuaria" in nl:
            cat = "puerto"
        if cat:
            self.pois.append({"categoria": cat, "nombre": nombre, "lat": lat, "lon": lon,
                              "operador": t.get("operator", ""),
                              "osm": f"{tipo_osm}/{oid}"})


# ----------------------------------------------------------------- salida
def escribir(nombre, filas, campos):
    os.makedirs(DIR_SALIDA, exist_ok=True)
    with open(os.path.join(DIR_SALIDA, nombre + ".csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(filas)
    gj = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
        "properties": {k: r.get(k, "") for k in campos if k not in ("lat", "lon")},
    } for r in filas]}
    with open(os.path.join(DIR_SALIDA, nombre + ".geojson"), "w", encoding="utf-8") as f:
        json.dump(gj, f, ensure_ascii=False)


def main():
    pbf = sys.argv[1] if len(sys.argv) > 1 else PBF
    if pbf == PBF:
        descargar_pbf()

    log("Pasada 1: límites provinciales...")
    lim = Limites()
    lim.apply_file(pbf, locations=True, idx="flex_mem")
    faltan = [p for p in PROVINCIAS if p not in lim.poligonos]
    if faltan:
        log(f"  ATENCIÓN: no se encontró límite de {faltan}")
    prov_prep = {p: prep(g) for p, g in lim.poligonos.items()}

    log("Pasada 2: rutas, accesos, localidades, silos y puertos...")
    v = Vias()
    v.apply_file(pbf, locations=True, idx="flex_mem")
    log(f"  nodos de interés: {len(v.nodos)}, localidades: {len(v.localidades)}, POIs: {len(v.pois)}")

    def provincia_de(lat, lon):
        p = Point(lon, lat)
        for nombre, pg in prov_prep.items():
            if pg.contains(p):
                return nombre
        return None

    def localidad_cercana(lat, lon):
        mejor, dmin = "", 1e9
        for nombre, la, lo in v.localidades:
            if abs(la - lat) > 0.1 or abs(lo - lon) > 0.1:
                continue
            d = haversine_km(lat, lon, la, lo)
            if d < dmin:
                mejor, dmin = nombre, d
        return (mejor, round(dmin, 2)) if dmin <= RADIO_LOCALIDAD_KM else ("", "")

    # --- cruces
    log("Calculando cruces...")
    por_prov = defaultdict(list)
    for nid, refs in v.rutas_por_nodo.items():
        accs = v.accesos_por_nodo.get(nid, set())
        es_cruce_rutas = len(refs) >= 2 and len(v.combos_por_nodo[nid]) >= 2
        if not es_cruce_rutas and not accs:
            continue
        lat, lon = v.nodos[nid]
        prov = provincia_de(lat, lon)
        if not prov:
            continue
        if prov == "Buenos Aires" and RUTA_LIMITE_GBA not in refs \
                and punto_en_poligono(lon, lat, POLIGONO_GBA):
            continue
        refs_o = sorted(refs)
        loc, dist = localidad_cercana(lat, lon)
        por_prov[prov].append({
            "tipo": "ruta-ruta" if es_cruce_rutas else "acceso",
            "ruta": refs_o[0],
            "cruza_con": " ; ".join(refs_o[1:] + sorted(accs)),
            "lat": lat, "lon": lon,
            "localidad_cercana": loc, "dist_localidad_km": dist,
            "nodo_osm": nid,
            "google_maps": f"https://www.google.com/maps?q={lat},{lon}",
        })

    campos = ["tipo", "ruta", "cruza_con", "lat", "lon", "localidad_cercana",
              "dist_localidad_km", "nodo_osm", "google_maps"]
    resumen = {}
    for prov, filas in por_prov.items():
        filas.sort(key=lambda r: (r["tipo"], r["ruta"], r["cruza_con"]))
        escribir("cruces_" + PROVINCIAS[prov], filas, campos)
        resumen[prov] = {"ruta-ruta": sum(r["tipo"] == "ruta-ruta" for r in filas),
                         "accesos": sum(r["tipo"] == "acceso" for r in filas)}
        log(f"  {prov}: {resumen[prov]}")

    # --- silos y puertos
    log("Silos y puertos...")
    pois = []
    for p in v.pois:
        prov = provincia_de(p["lat"], p["lon"])
        if not prov:
            continue
        loc, dist = localidad_cercana(p["lat"], p["lon"])
        p.update({"provincia": prov, "localidad_cercana": loc, "dist_localidad_km": dist,
                  "google_maps": f"https://www.google.com/maps?q={p['lat']},{p['lon']}"})
        pois.append(p)
    pois.sort(key=lambda r: (r["provincia"], r["categoria"], r["nombre"]))
    escribir("silos_puertos", pois,
             ["provincia", "categoria", "nombre", "operador", "lat", "lon",
              "localidad_cercana", "dist_localidad_km", "osm", "google_maps"])
    resumen["silos_puertos"] = {"silos/acopio": sum(p["categoria"] == "silos/acopio" for p in pois),
                                "puertos": sum(p["categoria"] == "puerto" for p in pois)}
    log(f"  {resumen['silos_puertos']}")

    # --- red vial: aristas nodo-nodo de las rutas y tramos cruce-a-cruce
    log("Red de rutas y tramos entre cruces...")
    cruces_ids = {r["nodo_osm"] for filas in por_prov.values() for r in filas}
    # localidades también como nodos de la red (punto de ruta más cercano) no;
    # solo cruces. Grafo: nid -> [(vecino, km, ruta)]
    grafo = defaultdict(list)
    for refs, nids in v.vias_ruta:
        ruta = ";".join(sorted(refs))
        for a, b in zip(nids, nids[1:]):
            la, lo = v.nodos[a]
            lb, lob = v.nodos[b]
            d = haversine_km(la, lo, lb, lob)
            grafo[a].append((b, d, ruta))
            grafo[b].append((a, d, ruta))
    # guardar la red cruda (para el cálculo de plantas) en JSON compacto
    with open(os.path.join(DIR_SALIDA, "red_rutas.json"), "w", encoding="utf-8") as f:
        json.dump({"nodos": {str(k): [round(a, 7), round(b, 7)] for k, (a, b) in v.nodos.items()
                             if k in grafo},
                   "aristas": [[a, b, round(d, 4), r] for a, lst in grafo.items()
                               for b, d, r in lst if a < b],
                   "cruces": sorted(cruces_ids)}, f, ensure_ascii=False)

    # tramos: desde cada cruce, caminar por nodos no-cruce hasta el próximo cruce
    tramos, vistos_tramos = [], set()
    for c in cruces_ids:
        for vecino, d0, ruta in grafo.get(c, []):
            prev, actual, acum, pasos = c, vecino, d0, 1
            while actual not in cruces_ids:
                sig = [(n, d, r) for n, d, r in grafo[actual] if n != prev]
                if len(sig) != 1:   # fin de ruta o bifurcación sin cruce: cortar
                    break
                prev, (actual, d, r) = actual, sig[0]
                acum += d
                pasos += 1
                if pasos > 100000:
                    break
            if actual in cruces_ids and actual != c:
                clave = (min(c, actual), max(c, actual), ruta)
                if clave not in vistos_tramos:
                    vistos_tramos.add(clave)
                    tramos.append((c, actual, ruta, round(acum, 3)))
    info_cruce = {r["nodo_osm"]: (prov, r) for prov, filas in por_prov.items() for r in filas}
    tramos_prov = defaultdict(list)
    for a, b, ruta, km in tramos:
        pa, ra = info_cruce[a]
        pb, rb = info_cruce[b]
        tramos_prov[pa].append({
            "ruta": ruta, "km": km,
            "cruce_a": a, "cruce_a_desc": f"{ra['ruta']} x {ra['cruza_con']}",
            "cruce_a_lat": ra["lat"], "cruce_a_lon": ra["lon"],
            "cruce_b": b, "cruce_b_desc": f"{rb['ruta']} x {rb['cruza_con']}",
            "cruce_b_lat": rb["lat"], "cruce_b_lon": rb["lon"],
            "provincia_b": pb,
        })
    for prov, filas in tramos_prov.items():
        filas.sort(key=lambda r: (r["ruta"], r["cruce_a"]))
        with open(os.path.join(DIR_SALIDA, "tramos_" + PROVINCIAS[prov] + ".csv"), "w",
                  newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(filas[0].keys()))
            w.writeheader()
            w.writerows(filas)
        resumen[prov]["tramos"] = len(filas)
        log(f"  {prov}: {len(filas)} tramos")

    with open(os.path.join(DIR_SALIDA, "resumen.json"), "w", encoding="utf-8") as f:
        json.dump(resumen, f, ensure_ascii=False, indent=1)
    log("Listo.")


if __name__ == "__main__":
    main()
