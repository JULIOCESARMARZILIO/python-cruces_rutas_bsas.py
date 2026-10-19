"""
Extrae TODOS los caminos (highway) de un PARTIDO de la Provincia de Buenos Aires
desde el extracto de Argentina de OpenStreetMap (Geofabrik), y calcula los km
totales por tipo. Pensado para medir la red de caminos rurales de un partido.

Uso:
    python caminos_partido.py "25_de_mayo"
El argumento es el slug del partido (se mapea a su nombre OSM en PARTIDOS).

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
from shapely.geometry import Point
from shapely.prepared import prep

DIR_DATOS = os.environ.get("DIR_DATOS", "/data")
DIR_SALIDA = os.environ.get("DIR_SALIDA", os.path.join(DIR_DATOS, "salida"))
PBF = os.path.join(DIR_DATOS, "argentina-latest.osm.pbf")

# slug -> variantes de nombre del partido en OSM (boundary admin_level 6/7).
# Se comparan normalizadas (sin acentos, minúsculas, sin "partido de ").
PARTIDOS = {
    "25_de_mayo": ["25 de mayo", "veinticinco de mayo"],
}
PROVINCIA = "Buenos Aires"   # para descartar homónimos (admin_level=4)
NIVELES_PARTIDO = {"6", "7"}


def normnombre(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("partido de ", "").replace("municipio de ", "")
    return " ".join(s.split())

# caminos de tierra / agrícolas que consideramos "rurales"
CAMINOS_RURALES = {"track", "unclassified", "residential"}


def log(m):
    print(m, flush=True)


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def area_km2(poly):
    """Área aproximada de un polígono lon/lat en km² (proyección equirectangular local)."""
    lat0 = poly.centroid.y
    kx = 111.320 * math.cos(math.radians(lat0))
    ky = 110.574
    # shapely .area está en grados²; escalamos por kx*ky
    return abs(poly.area) * kx * ky


# ---------------------------------------------------- pasada 1: límites
class Limites(osmium.SimpleHandler):
    """Busca el polígono del partido (admin_level=6) y el de la provincia (=4)."""

    def __init__(self, variantes_partido, nombre_provincia):
        super().__init__()
        self.fab = osmium.geom.WKBFactory()
        self.variantes = set(variantes_partido)
        self.nombre_provincia = nombre_provincia
        self.provincia = None
        self.partidos = []   # [(nombre, lvl, geom)] todos los admin_level 6/7

    def area(self, a):
        t = a.tags
        if t.get("boundary") != "administrative":
            return
        lvl = t.get("admin_level")
        nombre = t.get("name", "")
        if lvl == "4" and nombre == self.nombre_provincia and self.provincia is None:
            try:
                self.provincia = wkb.loads(self.fab.create_multipolygon(a), hex=True)
                log(f"  provincia cargada: {nombre}")
            except Exception as e:  # noqa
                log(f"  no se pudo armar provincia {nombre}: {e}")
        elif lvl in NIVELES_PARTIDO and nombre:
            try:
                g = wkb.loads(self.fab.create_multipolygon(a), hex=True)
                self.partidos.append((nombre, lvl, g))
            except Exception:  # noqa
                pass


# ---------------------------------------------------- pasada 2: caminos
class Caminos(osmium.SimpleHandler):
    def __init__(self, poligono_prep):
        super().__init__()
        self.poly = poligono_prep
        self.features = []
        self.km_por_tipo = {}
        self.km_por_tipo_ref = {}   # tipos con ref de ruta (RN/RP)
        self.n = 0

    def way(self, w):
        t = w.tags
        hw = t.get("highway")
        if not hw:
            return
        self.n += 1
        if self.n % 2_000_000 == 0:
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
        # filtro rápido: primer nodo dentro del partido
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
            "geometry": {"type": "LineString", "coordinates": [[round(x, 6), round(y, 6)] for x, y in coords]},
            "properties": {"highway": hw, "ref": ref, "nombre": t.get("name", ""),
                           "km": round(km, 3), "ruta": es_ruta},
        })


def main():
    slug = sys.argv[1] if len(sys.argv) > 1 else "25_de_mayo"
    variantes = PARTIDOS.get(slug)
    if not variantes:
        log(f"Partido desconocido: {slug}. Conocidos: {list(PARTIDOS)}")
        sys.exit(1)
    nombre = variantes[0]
    if not os.path.exists(PBF):
        log(f"No está el mapa {PBF}")
        sys.exit(1)
    os.makedirs(DIR_SALIDA, exist_ok=True)

    log(f"Pasada 1: límites (partido {variantes} en {PROVINCIA})...")
    lim = Limites(variantes, PROVINCIA)
    lim.apply_file(PBF, locations=True, idx="flex_mem")
    if lim.provincia is None:
        log(f"No se encontró la provincia {PROVINCIA} (admin_level=4).")
        sys.exit(1)
    # quedarse con los partidos cuyo centroide cae dentro de la provincia
    prov_prep = prep(lim.provincia)
    en_prov = [(nom, lvl, g) for nom, lvl, g in lim.partidos if prov_prep.contains(g.centroid)]
    log(f"  partidos de {PROVINCIA}: {len(en_prov)}")
    pistas = sorted({f"{nom} (lvl {lvl})" for nom, lvl, g in en_prov if "mayo" in normnombre(nom)})
    if pistas:
        log("  con 'mayo': " + "; ".join(pistas))
    elegido = None
    for nom, lvl, g in en_prov:
        if normnombre(nom) in lim.variantes:
            elegido = g
            nombre = nom
            log(f"  elegido: '{nom}' (lvl {lvl}) centroide={g.centroid.y:.3f},{g.centroid.x:.3f}")
            break
    if elegido is None:
        log(f"No se encontró el partido {variantes} dentro de {PROVINCIA} "
            f"(de {len(en_prov)} partidos).")
        sys.exit(1)
    a_km2 = area_km2(elegido)
    log(f"  partido elegido: área ≈ {a_km2:.0f} km²")

    log("Pasada 2: caminos dentro del partido...")
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
