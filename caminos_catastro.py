"""
Caminos rurales de un partido sacados del CATASTRO de ARBA (no de OpenStreetMap).

En el catastro rural los caminos son de dominio público: no tienen parcela.
Entonces el espacio que queda entre las parcelas, dentro del límite del
partido, son los caminos (y los arroyos/lagunas). Se rasteriza ese hueco a
5 m, se saca el eje (esqueleto) y se vectoriza. Se descartan los huecos
anchos (lagunas, cascos urbanos) y los tramos que van por un curso de agua.

Cada tramo se compara con los caminos de OpenStreetMap: en_osm = "si" si OSM
ya lo tiene, "no" si es un camino que falta en OSM.

Uso:
    python caminos_catastro.py            # baja de ARBA y calcula (25 de Mayo)
    python caminos_catastro.py --cache D  # usa/guarda las descargas en D

Salida (en datos/ y en DIR_SALIDA si existe):
  caminos_catastro_25_de_mayo.geojson   LineStrings: km, ancho_m, en_osm, cuartel
  parcelas_arba_25_de_mayo.geojson      parcelas rurales: partida, cuartel, ha
  limite_arba_25_de_mayo.geojson        límite del partido según ARBA
"""

import argparse
import json
import math
import os
import sys
import time

import numpy as np
import requests
from shapely.geometry import LineString, mapping, shape
import warnings
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree

warnings.filterwarnings("ignore", category=DeprecationWarning)
AQUI = os.path.dirname(os.path.abspath(__file__))
DIR_SALIDA = os.environ.get("DIR_SALIDA", os.path.join(os.environ.get("DIR_DATOS", "/data"), "salida"))
WFS = "https://geo.arba.gov.ar/geoserver/idera/wfs"
PARTIDO = "109"          # código ARBA de 25 de Mayo
SLUG = "25_de_mayo"
RES = 5.0                # metros por pixel
ANCHO_MIN, ANCHO_MAX = 7.0, 70.0   # ancho del hueco para ser camino (m)
LARGO_MIN = 150.0        # tramos sueltos más cortos que esto se descartan (m)
CUARTELES = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII",
             "XIV", "XV"]


def log(m):
    print(m, flush=True)


def wfs(tipo, filtro, cache=None, nombre=None, pagina=1000):
    if cache and nombre and os.path.exists(os.path.join(cache, nombre)):
        with open(os.path.join(cache, nombre), encoding="utf-8") as f:
            return json.load(f)["features"]
    feats, inicio = [], 0
    while True:
        params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature",
                  "typeNames": tipo, "outputFormat": "application/json",
                  "srsName": "EPSG:4326", "sortBy": "cca", "count": pagina,
                  "startIndex": inicio, "CQL_FILTER": filtro}
        for intento in range(5):
            try:
                r = requests.get(WFS, params=params, timeout=300)
                r.raise_for_status()
                lote = r.json()["features"]
                break
            except Exception as e:  # noqa
                log(f"  reintento {intento + 1} ({e})")
                time.sleep(2 ** intento)
        else:
            raise RuntimeError("ARBA no responde")
        feats += lote
        log(f"  {tipo}: {len(feats)}")
        if len(lote) < pagina:
            break
        inicio += pagina
    if cache and nombre:
        os.makedirs(cache, exist_ok=True)
        with open(os.path.join(cache, nombre), "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": feats}, f)
    return feats


def proyectores(lat0, lon0):
    """Proyección local equirectangular en metros (suficiente para un partido)."""
    kx = 111320.0 * math.cos(math.radians(lat0))
    ky = 110574.0

    def a_m(x, y, z=None):
        return (np.asarray(x) - lon0) * kx, (np.asarray(y) - lat0) * ky

    def a_geo(x, y, z=None):
        return np.asarray(x) / kx + lon0, np.asarray(y) / ky + lat0
    return a_m, a_geo


def leer_osm(nombre):
    for d in (DIR_SALIDA, os.path.join(AQUI, "datos")):
        p = os.path.join(d, nombre)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)["features"]
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=None)
    args = ap.parse_args()

    import rasterio.features
    from rasterio.transform import from_origin
    from scipy import ndimage
    from skan import Skeleton
    from skimage.morphology import remove_small_objects, skeletonize

    log("Bajando de ARBA límite y parcelas…")
    depto = wfs("idera:Departamento", f"cca='{PARTIDO}'", args.cache, "depto.json")
    rurales = wfs("idera:Parcela", f"pda LIKE '{PARTIDO}%' AND tpa='Rural'", args.cache, "rurales.json")
    urbanas = wfs("idera:Parcela", f"pda LIKE '{PARTIDO}%' AND tpa<>'Rural'", args.cache, "urbanas.json")
    limite = unary_union([shape(f["geometry"]) for f in depto])
    log(f"{len(rurales)} parcelas rurales, {len(urbanas)} urbanas")

    c = limite.centroid
    a_m, a_geo = proyectores(c.y, c.x)
    lim_m = transform(a_m, limite)
    par_r = [transform(a_m, shape(f["geometry"])) for f in rurales]
    par_u = [transform(a_m, shape(f["geometry"])) for f in urbanas]

    minx, miny, maxx, maxy = lim_m.bounds
    minx, miny, maxx, maxy = minx - 50, miny - 50, maxx + 50, maxy + 50
    ancho, alto = int((maxx - minx) / RES) + 1, int((maxy - miny) / RES) + 1
    tr = from_origin(minx, maxy, RES, RES)
    log(f"Raster {ancho}x{alto} a {RES} m")

    dentro = rasterio.features.rasterize([(lim_m, 1)], out_shape=(alto, ancho), transform=tr,
                                         dtype="uint8")
    ocupado = rasterio.features.rasterize([(g, 1) for g in par_r + par_u], out_shape=(alto, ancho),
                                          transform=tr, dtype="uint8")
    hueco = (dentro == 1) & (ocupado == 0)
    del ocupado, dentro
    # zona urbana (manzanas + 150 m): ahí los huecos son calles de pueblo, no caminos rurales
    urb = rasterio.features.rasterize([(g, 1) for g in par_u], out_shape=(alto, ancho),
                                      transform=tr, dtype="uint8")

    # por bloques con solapamiento (el partido entero no entra en memoria a 5 m)
    log("Distancia y esqueleto por bloques…")
    esq = np.zeros((alto, ancho), dtype=bool)
    ancho_m = np.zeros((alto, ancho), dtype=np.uint8)
    B, S = 3000, 200
    for r0 in range(0, alto, B):
        for c0 in range(0, ancho, B):
            ra, rb = max(r0 - S, 0), min(r0 + B + S, alto)
            ca, cb = max(c0 - S, 0), min(c0 + B + S, ancho)
            h = hueco[ra:rb, ca:cb]
            if not h.any():
                continue
            h = remove_small_objects(h, max_size=int(2000 / RES ** 2))
            w = np.minimum(ndimage.distance_transform_edt(h) * RES * 2, 255).astype(np.uint8)
            e = skeletonize(h)
            cerca_urb = ndimage.distance_transform_edt(urb[ra:rb, ca:cb] == 0) * RES < 150
            e &= (w >= ANCHO_MIN) & (w <= ANCHO_MAX) & ~cerca_urb
            ir, ic = r0 - ra, c0 - ca
            nr, nc = min(B, alto - r0), min(B, ancho - c0)
            esq[r0:r0 + nr, c0:c0 + nc] = e[ir:ir + nr, ic:ic + nc]
            ancho_m[r0:r0 + nr, c0:c0 + nc] = w[ir:ir + nr, ic:ic + nc]
    del hueco, urb
    esq = remove_small_objects(esq, max_size=int(LARGO_MIN / RES), connectivity=2)

    log("Vectorizando…")
    sk = Skeleton(esq)
    lineas = []
    for i in range(sk.n_paths):
        rc = sk.path_coordinates(i)
        if len(rc) < 2:
            continue
        xs = minx + (rc[:, 1] + 0.5) * RES
        ys = maxy - (rc[:, 0] + 0.5) * RES
        ln = LineString(np.column_stack([xs, ys])).simplify(RES * 1.5)
        anchos = ancho_m[rc[:, 0], rc[:, 1]]
        lineas.append((ln, float(np.median(anchos)), None))
    del esq, ancho_m
    log(f"{len(lineas)} tramos crudos")

    # agua de OSM: descartar tramos que van por un arroyo
    aguas = [transform(a_m, shape(f["geometry"])) for f in leer_osm(f"aguas_{SLUG}.geojson")]
    agua_buf = unary_union([g.buffer(40) for g in aguas]) if aguas else None
    osm = [transform(a_m, shape(f["geometry"])) for f in leer_osm(f"caminos_{SLUG}.geojson")]
    osm_buf = unary_union([g.buffer(35) for g in osm]) if osm else None
    # franja pegada al límite: casi siempre es el arroyo limítrofe, no un camino
    borde_buf = lim_m.boundary.buffer(60)

    # cuartel (circunscripción) de cada tramo por la parcela más cercana
    arbol = STRtree(par_r)
    cuartel_de = [CUARTELES[int(f["properties"]["cca"][3:5])] if f["properties"]["cca"][3:5].isdigit()
                  and int(f["properties"]["cca"][3:5]) < len(CUARTELES) else "" for f in rurales]

    # grado de cada extremo, para sacar espolones cortos (puntas muertas de <LARGO_MIN)
    from collections import Counter
    extremos = Counter()
    for ln, _, _ in lineas:
        for p in (ln.coords[0], ln.coords[-1]):
            extremos[(round(p[0] / RES), round(p[1] / RES))] += 1

    feats, km_tot, km_nuevo = [], 0.0, 0.0
    for ln, w, _ in lineas:
        largo = ln.length
        a = extremos[(round(ln.coords[0][0] / RES), round(ln.coords[0][1] / RES))]
        b = extremos[(round(ln.coords[-1][0] / RES), round(ln.coords[-1][1] / RES))]
        if largo < LARGO_MIN and (a == 1 or b == 1):
            continue
        if agua_buf is not None and ln.intersection(agua_buf).length > 0.6 * largo:
            continue
        en_osm = "no"
        if osm_buf is not None and ln.intersection(osm_buf).length > 0.6 * largo:
            en_osm = "si"
        if en_osm == "no" and ln.intersection(borde_buf).length > 0.5 * largo:
            continue
        i = arbol.nearest(ln.interpolate(0.5, normalized=True))
        g = transform(a_geo, ln)
        km = largo / 1000
        km_tot += km
        if en_osm == "no":
            km_nuevo += km
        feats.append({"type": "Feature", "geometry": mapping(g),
                      "properties": {"km": round(km, 2), "ancho_m": round(w),
                                     "en_osm": en_osm, "cuartel": cuartel_de[int(i)],
                                     "fuente": "catastro ARBA"}})
    for f in feats:
        f["geometry"]["coordinates"] = [[round(x, 6), round(y, 6)] for x, y in f["geometry"]["coordinates"]]
    log(f"{len(feats)} tramos, {km_tot:.1f} km; {km_nuevo:.1f} km no están en OSM")

    resumen = {"fuente": "catastro ARBA (huecos entre parcelas)", "tramos": len(feats),
               "km_caminos": round(km_tot, 1), "km_que_faltan_en_osm": round(km_nuevo, 1),
               "parcelas_rurales": len(rurales)}
    parcelas = []
    for f, g in zip(rurales, par_r):
        p = f["properties"]
        cu = p["cca"][3:5]
        parcelas.append({"type": "Feature",
                         "geometry": json.loads(json.dumps(mapping(transform(a_geo, g.simplify(2))))),
                         "properties": {"partida": p["pda"], "nomenclatura": p["cca"],
                                        "cuartel": CUARTELES[int(cu)] if cu.isdigit() and int(cu) < len(CUARTELES) else "",
                                        "ha": round((p.get("ara1") or 0) / 1e4, 1)}})

    def redondear(o):
        if isinstance(o, float):
            return round(o, 6)
        if isinstance(o, (list, tuple)):
            return [redondear(x) for x in o]
        return o
    for f in parcelas:
        f["geometry"]["coordinates"] = redondear(f["geometry"]["coordinates"])

    salidas = {f"caminos_catastro_{SLUG}.geojson": {"type": "FeatureCollection", "resumen": resumen,
                                                     "features": feats},
               f"parcelas_arba_{SLUG}.geojson": {"type": "FeatureCollection", "features": parcelas},
               f"limite_arba_{SLUG}.geojson": {"type": "FeatureCollection", "features": [
                   {"type": "Feature", "properties": {"nombre": "Partido de 25 de Mayo"},
                    "geometry": json.loads(json.dumps(mapping(limite)))}]}}
    for d in [os.path.join(AQUI, "datos")] + ([DIR_SALIDA] if os.path.isdir(DIR_SALIDA) else []):
        for nombre, obj in salidas.items():
            with open(os.path.join(d, nombre), "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    log("RESUMEN " + json.dumps(resumen, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
