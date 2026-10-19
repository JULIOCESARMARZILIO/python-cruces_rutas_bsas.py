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
from shapely.geometry import LineString, Point
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
        # algunas relaciones de partido no llevan boundary=administrative en la
        # relación (está en los ways); basta admin_level + nombre.
        if t.get("admin_level") not in NIVELES_PARTIDO:
            return
        es_admin = (t.get("boundary") == "administrative" or t.get("type") == "boundary")
        if not es_admin:
            return
        nn = normnombre(t.get("name", ""))
        if not any(v == nn or v in nn for v in self.variantes):
            return
        ways = [(m.ref, m.role) for m in r.members if m.type == "w"]
        if ways:
            self.relaciones.append({"name": t.get("name", ""), "lvl": t.get("admin_level"),
                                    "ways": ways})
            log(f"  relación candidata: '{t.get('name')}' (lvl {t.get('admin_level')}, "
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
        if not self.poly.contains(Point(coords[0][0], coords[0][1])):
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
    log(f"Partido elegido: '{nombre}' (lvl {lvl_el}) — área ≈ {a_km2:.0f} km²")
    a_km2 = area_km2(elegido)
    a_km2 = area_km2(elegido)
    log(f"Partido elegido: '{nombre}' — área ≈ {a_km2:.0f} km²")

    log("Pasada 3: caminos dentro del partido...")
    cam = Caminos(prep(elegido))
    cam.apply_file(PBF, locations=True, idx="flex_mem")

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

    log("RESUMEN " + json.dumps(resumen, ensure_ascii=False))
    log(f"Caminos rurales (track+unclassified+residential): {km_rurales:.1f} km")
    log(f"Total de todas las vías: {km_total:.1f} km en {len(cam.features)} tramos")


if __name__ == "__main__":
    main()
