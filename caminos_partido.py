"""
Extrae TODOS los caminos (highway) de un PARTIDO de la Provincia de Buenos Aires
desde el extracto de Argentina de OpenStreetMap (Geofabrik), y calcula los km
totales por tipo. Pensado para medir la red de caminos rurales de un partido.

OSM de Argentina no deja ensamblar el polígono del partido con el area-handler
de osmium (las relaciones grandes de partido no se arman solas), así que el
polígono se construye a mano uniendo los 'ways' de la relación boundary del
partido (shapely.polygonize). Se desambigua por centroide dentro de la provincia.

Uso:
    python caminos_partido.py "25_de_mayo"

Salida (en DIR_SALIDA):
  caminos_<slug>.geojson           LineStrings con highway, ref, nombre, km
  caminos_<slug>_resumen.json      area_km2, km_por_tipo, km_caminos_rurales, km_total
"""

import json
import math
import os
import sys
import unicodedata

import osmium
from shapely import wkb
from shapely.geometry import LineString, Point, mapping
from shapely.ops import polygonize, unary_union
from shapely.prepared import prep

DIR_DATOS = os.environ.get("DIR_DATOS", "/data")
DIR_SALIDA = os.environ.get("DIR_SALIDA", os.path.join(DIR_DATOS, "salida"))
PBF = os.path.join(DIR_DATOS, "argentina-latest.osm.pbf")

# slug -> variantes de nombre del partido en OSM (normalizadas)
PARTIDOS = {
    "25_de_mayo": ["25 de mayo", "veinticinco de mayo"],
}
PROVINCIA = "Buenos Aires"
NIVELES_PARTIDO = {"6", "7", "8"}
CAMINOS_RURALES = {"track", "unclassified", "residential"}


def log(m):
    print(m, flush=True)


def normnombre(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("partido de ", "").replace("municipio de ", "").replace("distrito ", "")
    return " ".join(s.split())


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def area_km2(poly):
    lat0 = poly.centroid.y
    kx = 111.320 * math.cos(math.radians(lat0))
    ky = 110.574
    return abs(poly.area) * kx * ky


# --------- Pasada 1: polígono de la provincia (area) + relaciones candidatas
class Paso1(osmium.SimpleHandler):
    def __init__(self, variantes, nombre_provincia):
        super().__init__()
        self.fab = osmium.geom.WKBFactory()
        self.variantes = variantes
        self.nombre_provincia = nombre_provincia
        self.provincia = None
        self.relaciones = []   # [{name, lvl, ways:[(ref, role)]}]
        self.pistas = []       # relaciones admin con 'mayo'/'veinti'/'25' en algún nombre

    def area(self, a):
        t = a.tags
        if (t.get("boundary") == "administrative" and t.get("admin_level") == "4"
                and t.get("name") == self.nombre_provincia and self.provincia is None):
            try:
                self.provincia = wkb.loads(self.fab.create_multipolygon(a), hex=True)
                log(f"  provincia cargada: {t.get('name')}")
            except Exception as e:  # noqa
                log(f"  no se pudo armar provincia: {e}")

    def relation(self, r):
        t = r.tags
        lvl = t.get("admin_level")
        if lvl not in {"5", "6", "7", "8", "9"}:
            return
        es_admin = (t.get("boundary") == "administrative" or t.get("type") == "boundary")
        if not es_admin:
            return
        nombres = [t.get(k, "") for k in ("name", "name:es", "official_name",
                                          "alt_name", "short_name")]
        nns = [normnombre(x) for x in nombres if x]
        ways = [(m.ref, m.role) for m in r.members if m.type == "w"]
        # diagnóstico: cualquier admin con pista de "mayo"/"veinti"/"25 de"
        if any(("mayo" in x or "veinti" in x or "25 de" in x) for x in nns):
            self.pistas.append({"nombres": [x for x in nombres if x], "lvl": lvl,
                                "boundary": t.get("boundary", ""), "type": t.get("type", ""),
                                "n_ways": len(ways)})
        # candidato si algún nombre matchea una variante
        if any(any(v == x or v in x for v in self.variantes) for x in nns) and ways:
            self.relaciones.append({"name": t.get("name", "") or (nombres[0] if nombres else ""),
                                    "lvl": lvl, "ways": ways})
            log(f"  relación candidata: '{self.relaciones[-1]['name']}' (lvl {lvl}, "
                f"{len(ways)} ways)")


# --------- Pasada 2: geometría de los ways de las relaciones candidatas
class Paso2(osmium.SimpleHandler):
    def __init__(self, needed_ids):
        super().__init__()
        self.needed = needed_ids
        self.geom = {}   # way_id -> [(lon, lat), ...]

    def way(self, w):
        if w.id not in self.needed:
            return
        coords = []
        try:
            for nd in w.nodes:
                loc = nd.location
                if loc.valid():
                    coords.append((loc.lon, loc.lat))
        except osmium.InvalidLocationError:
            return
        if len(coords) >= 2:
            self.geom[w.id] = coords


def construir_poligono(rel, geom):
    lineas = []
    for ref, role in rel["ways"]:
        if role in ("", "outer", "inner") and ref in geom:
            lineas.append(LineString(geom[ref]))
    if not lineas:
        return None
    try:
        polys = list(polygonize(unary_union(lineas)))
    except Exception:  # noqa
        return None
    if not polys:
        return None
    return unary_union(polys)


# --------- Pasada 3: caminos dentro del partido
class Caminos(osmium.SimpleHandler):
    def __init__(self, poligono_prep):
        super().__init__()
        self.poly = poligono_prep
        self.features = []
        self.km_por_tipo = {}
        self.km_por_tipo_ref = {}
        self.n = 0

    def way(self, w):
        t = w.tags
        hw = t.get("highway")
        if not hw:
            return
        self.n += 1
        if self.n % 3_000_000 == 0:
            log(f"  vías recorridas: {self.n // 1_000_000} M")
        coords = []
        try:
            for nd in w.nodes:
                loc = nd.location
                if loc.valid():
                    coords.append((loc.lon, loc.lat))
        except osmium.InvalidLocationError:
            return
        if len(coords) < 2:
            return
        # se incluye la via si CUALQUIER tramo toca el partido (antes solo se miraba el
        # primer nodo, y se perdian los caminos que arrancan fuera y entran al partido)
        if not self.poly.intersects(LineString(coords)):
            return
        km = 0.0
        for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
            km += haversine_km(lat1, lon1, lat2, lon2)
        ref = t.get("ref", "")
        es_ruta = any(ref.upper().replace(" ", "").startswith(p) for p in ("RN", "RP"))
        self.km_por_tipo[hw] = self.km_por_tipo.get(hw, 0.0) + km
        if es_ruta:
            self.km_por_tipo_ref[hw] = self.km_por_tipo_ref.get(hw, 0.0) + km
        self.features.append({
            "type": "Feature",
            "geometry": {"type": "LineString",
                         "coordinates": [[round(x, 6), round(y, 6)] for x, y in coords]},
            "properties": {"highway": hw, "ref": ref, "nombre": t.get("name", ""),
                           "km": round(km, 3), "ruta": es_ruta},
        })


AGUAS = {"river", "stream", "canal", "drain", "ditch"}


class Aguas(osmium.SimpleHandler):
    """Rios, arroyos, canales y desagues dentro del partido."""

    def __init__(self, poligono_prep):
        super().__init__()
        self.poly = poligono_prep
        self.features = []

    def way(self, w):
        t = w.tags
        ww = t.get("waterway")
        if ww not in AGUAS:
            return
        coords = []
        try:
            for nd in w.nodes:
                loc = nd.location
                if loc.valid():
                    coords.append((loc.lon, loc.lat))
        except osmium.InvalidLocationError:
            return
        if len(coords) < 2:
            return
        if not any(self.poly.contains(Point(x, y)) for x, y in coords[::max(1, len(coords) // 5)]):
            return
        self.features.append({
            "type": "Feature",
            "geometry": {"type": "LineString",
                         "coordinates": [[round(x, 6), round(y, 6)] for x, y in coords]},
            "properties": {"waterway": ww, "nombre": t.get("name", "")},
        })


def _puntos(g):
    if g.is_empty:
        return []
    if g.geom_type == "Point":
        return [g]
    if hasattr(g, "geoms"):
        return [p for sub in g.geoms for p in _puntos(sub)]
    return []


def calcular_cruces(caminos, aguas):
    """Puntos donde un camino rural cruza un curso de agua (alcantarilla/puente)
    o empalma con una ruta RN/RP. Devuelve features Point."""
    from shapely.strtree import STRtree
    rurales, rutas = [], []
    for f in caminos:
        p = f["properties"]
        geom = LineString(f["geometry"]["coordinates"])
        if p["ruta"]:
            rutas.append((geom, p))
        elif p["highway"] in CAMINOS_RURALES:
            rurales.append((geom, p))
    out = []

    def cruzar(lista_a, lista_b, tipo, nombre_b):
        if not lista_a or not lista_b:
            return
        arbol = STRtree([g for g, _ in lista_b])
        vistos = set()
        for ga, pa in lista_a:
            for i in arbol.query(ga):
                gb, pb = lista_b[i]
                for pt in _puntos(ga.intersection(gb)):
                    clave = (round(pt.x, 5), round(pt.y, 5), tipo)
                    if clave in vistos:
                        continue
                    vistos.add(clave)
                    out.append({"type": "Feature",
                                "geometry": {"type": "Point", "coordinates": [round(pt.x, 6), round(pt.y, 6)]},
                                "properties": {"tipo": tipo,
                                               "camino": pa.get("nombre") or pa.get("highway"),
                                               "cruza": nombre_b(pb)}})

    cruzar(rurales, [(LineString(f["geometry"]["coordinates"]), f["properties"]) for f in aguas],
           "camino_x_agua", lambda p: p.get("nombre") or p.get("waterway"))
    cruzar(rurales, rutas, "camino_x_ruta", lambda p: p.get("ref") or p.get("nombre"))
    return out


def main():
    slug = sys.argv[1] if len(sys.argv) > 1 else "25_de_mayo"
    variantes = PARTIDOS.get(slug)
    if not variantes:
        log(f"Partido desconocido: {slug}. Conocidos: {list(PARTIDOS)}")
        sys.exit(1)
    if not os.path.exists(PBF):
        log(f"No está el mapa {PBF}")
        sys.exit(1)
    os.makedirs(DIR_SALIDA, exist_ok=True)

    log(f"Pasada 1: provincia + relaciones del partido {variantes}...")
    p1 = Paso1(variantes, PROVINCIA)
    p1.apply_file(PBF, locations=True, idx="flex_mem")
    if p1.provincia is None:
        log(f"No se encontró la provincia {PROVINCIA}.")
        sys.exit(1)
    # volcar pistas de diagnóstico siempre
    try:
        with open(os.path.join(DIR_SALIDA, "caminos_debug.json"), "w", encoding="utf-8") as f:
            json.dump({"pistas": p1.pistas, "n_candidatas": len(p1.relaciones)}, f,
                      ensure_ascii=False, indent=1)
    except Exception:  # noqa
        pass
    if not p1.relaciones:
        log(f"No se encontró ninguna relación boundary con nombre {variantes}.")
        sys.exit(1)

    needed = set()
    for rel in p1.relaciones:
        for ref, role in rel["ways"]:
            needed.add(ref)
    log(f"Pasada 2: geometría de {len(needed)} ways de {len(p1.relaciones)} relaciones...")
    p2 = Paso2(needed)
    p2.apply_file(PBF, locations=True, idx="flex_mem")

    prov_prep = prep(p1.provincia)
    en_prov = []   # (area, poly, nombre, lvl)
    for rel in p1.relaciones:
        poly = construir_poligono(rel, p2.geom)
        if poly is None or poly.is_empty:
            log(f"  '{rel['name']}': no se pudo armar polígono")
            continue
        c = poly.centroid
        dentro = prov_prep.contains(c)
        ak = area_km2(poly)
        log(f"  '{rel['name']}' (lvl {rel['lvl']}): área≈{ak:.0f} km², "
            f"centroide={c.y:.3f},{c.x:.3f}, en {PROVINCIA}={dentro}")
        if dentro:
            en_prov.append((ak, poly, rel["name"], rel["lvl"]))
    if not en_prov:
        log(f"Ninguna relación {variantes} cae dentro de {PROVINCIA}.")
        sys.exit(1)
    # el partido es el polígono más grande dentro de la provincia (la ciudad es chica)
    en_prov.sort(key=lambda x: -x[0])
    a_km2, elegido, nombre, lvl_el = en_prov[0]
    if a_km2 < 1000:
        log(f"El mayor polígono en {PROVINCIA} tiene solo {a_km2:.0f} km² "
            f"('{nombre}', lvl {lvl_el}): es la localidad, no el partido. "
            f"El partido no está como relación nombrada en el extracto OSM.")
        sys.exit(2)
    log(f"Partido elegido: '{nombre}' (lvl {lvl_el}) — área ≈ {a_km2:.0f} km²")
    a_km2 = area_km2(elegido)
    a_km2 = area_km2(elegido)
    log(f"Partido elegido: '{nombre}' — área ≈ {a_km2:.0f} km²")

    log("Pasada 3: caminos dentro del partido...")
    cam = Caminos(prep(elegido))
    cam.apply_file(PBF, locations=True, idx="flex_mem")

    log("Pasada 4: rios, arroyos y canales dentro del partido...")
    ag = Aguas(prep(elegido))
    ag.apply_file(PBF, locations=True, idx="flex_mem")
    cruces = calcular_cruces(cam.features, ag.features)
    log(f"  {len(ag.features)} cursos de agua, {len(cruces)} cruces")

    km_total = sum(cam.km_por_tipo.values())
    km_rurales = sum(v for k, v in cam.km_por_tipo.items() if k in CAMINOS_RURALES)
    resumen = {
        "partido": nombre,
        "slug": slug,
        "area_km2": round(a_km2, 1),
        "cantidad_vias": len(cam.features),
        "km_total": round(km_total, 1),
        "km_caminos_rurales": round(km_rurales, 1),
        "tipos_rurales": sorted(CAMINOS_RURALES),
        "km_por_tipo": {k: round(v, 1) for k, v in sorted(cam.km_por_tipo.items(),
                                                          key=lambda x: -x[1])},
        "km_por_tipo_con_ref_ruta": {k: round(v, 1) for k, v in sorted(
            cam.km_por_tipo_ref.items(), key=lambda x: -x[1])},
    }
    with open(os.path.join(DIR_SALIDA, f"caminos_{slug}_resumen.json"), "w", encoding="utf-8") as f:
        json.dump(resumen, f, ensure_ascii=False, indent=1)
    with open(os.path.join(DIR_SALIDA, f"caminos_{slug}.geojson"), "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": cam.features}, f, ensure_ascii=False)
    with open(os.path.join(DIR_SALIDA, f"aguas_{slug}.geojson"), "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": ag.features}, f, ensure_ascii=False)
    with open(os.path.join(DIR_SALIDA, f"cruces_{slug}.geojson"), "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": cruces}, f, ensure_ascii=False)
    # polígono del partido (para dibujarlo y encuadrar el mapa)
    with open(os.path.join(DIR_SALIDA, f"caminos_{slug}_limite.geojson"), "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": [{
            "type": "Feature", "geometry": mapping(elegido),
            "properties": {"partido": nombre, "area_km2": round(a_km2, 1)}}]},
            f, ensure_ascii=False)

    log("RESUMEN " + json.dumps(resumen, ensure_ascii=False))
    log(f"Caminos rurales (track+unclassified+residential): {km_rurales:.1f} km")
    log(f"Total de todas las vías: {km_total:.1f} km en {len(cam.features)} tramos")


if __name__ == "__main__":
    main()
