"""
Caminos rurales de un partido sacados del CATASTRO de ARBA (no de OpenStreetMap).

En el catastro rural los caminos son de dominio público: no tienen parcela.
Entonces el espacio que queda entre las parcelas, dentro del límite del
partido, son los caminos (y los arroyos/lagunas). Se rasteriza ese hueco a
5 m, se saca el eje (esqueleto) y se vectoriza. Se descartan los huecos
anchos (lagunas, cascos urbanos) y los tramos que van por un curso de agua.

Cada tramo se compara con los caminos de OpenStreetMap: en_osm = "si" si OSM
ya lo tiene, "no" si es un camino que falta en OSM, "sin_dato" si no hay OSM
bajado para ese partido. Sin OSM tampoco se filtran los arroyos: probado en 25 de
Mayo, eso agrega ~60 km de falsos sobre 2.250 (un filtro por sinuosidad sacaba más
caminos reales que arroyos).

categoria (los caminos rurales no vienen clasificados en el catastro; se
deduce del ancho de la franja pública y de cómo lo marca OSM):
  principal   franja de 30 m o más, u OSM la marca como ruta (terciaria o más)
  secundario  franja de 17 a 29 m (el camino rural común, ~20 m)
  vecinal     franja angosta (~10 m): caminos vecinales y huellas

Uso:
    python caminos_catastro.py                      # 25 de Mayo
    python caminos_catastro.py --cache D            # usa/guarda las descargas en D
    python caminos_catastro.py --partido 012 --slug bragado --nombre Bragado --sin-parcelas
    python caminos_catastro.py --vecinos            # los 10 partidos linderos de 25 de Mayo

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
NOMBRE = "25 de Mayo"
# partidos linderos de 25 de Mayo: código ARBA -> (slug, nombre)
VECINOS = {"002": ("alberti", "Alberti"), "011": ("bolivar", "Bolívar"), "012": ("bragado", "Bragado"),
           "028": ("chivilcoy", "Chivilcoy"), "034": ("general_alvear", "General Alvear"),
           "062": ("lobos", "Lobos"), "075": ("navarro", "Navarro"), "077": ("nueve_de_julio", "Nueve de Julio"),
           "091": ("roque_perez", "Roque Pérez"), "093": ("saladillo", "Saladillo")}
ANCHO_PRINCIPAL, ANCHO_SECUNDARIO = 30, 17
MIN_RURALES = 30         # partidos sin campo (conurbano) se saltean
OSM_PRINCIPAL = {"primary", "secondary", "tertiary", "trunk", "motorway",
                 "primary_link", "secondary_link", "tertiary_link"}
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
    ap.add_argument("--partido", default=PARTIDO, help="código ARBA del partido (3 dígitos)")
    ap.add_argument("--slug", default=SLUG)
    ap.add_argument("--nombre", default=NOMBRE)
    ap.add_argument("--sin-parcelas", action="store_true", help="no guardar las parcelas (solo caminos y límite)")
    ap.add_argument("--vecinos", action="store_true", help="calcular los partidos linderos de 25 de Mayo")
    ap.add_argument("--todos", action="store_true",
                    help="todos los partidos de la provincia que todavía no estén en datos/")
    ap.add_argument("--indice", action="store_true",
                    help="solo armar datos/indice_catastro.json y datos/limites_partidos.geojson")
    args = ap.parse_args()
    if args.indice:
        recortar()
        return indice()
    if args.todos:
        import unicodedata
        deptos = wfs("idera:Departamento", "gna='Partido'", args.cache, "departamentos.json")
        hechos = []
        zona = zona_excluida()
        for f in sorted(deptos, key=lambda f: f["properties"]["cca"]):
            cod, nombre = f["properties"]["cca"], f["properties"]["nam"]
            if cod == PARTIDO:
                continue   # 25 de Mayo ya está, con parcelas y comparado con OSM
            if zona is not None and shape(f["geometry"]).buffer(0).difference(zona).area < 1e-4:
                continue   # todo el partido queda adentro de la Ruta 6 o entre la Ruta 2 y la costa
            slug = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode().lower()
            slug = "_".join("".join(ch if ch.isalnum() else " " for ch in slug).split())
            if os.path.exists(os.path.join(AQUI, "datos", f"caminos_catastro_{slug}.geojson")):
                continue
            cache = os.path.join(args.cache, slug) if args.cache else None
            log(f"===== {nombre} ({cod}) =====")
            try:
                calcular(cod, slug, nombre, cache, sin_parcelas=True, manzanas=True)
                hechos.append(slug)
            except Exception as e:  # noqa  un partido que falla no corta el resto
                log(f"ERROR en {nombre}: {e}")
        log(f"LISTO {len(hechos)} partidos")
        recortar()
        return indice()
    if args.vecinos:
        for cod, (slug, nombre) in VECINOS.items():
            cache = os.path.join(args.cache, slug) if args.cache else None
            if cache:
                os.makedirs(cache, exist_ok=True)
            log(f"===== {nombre} ({cod}) =====")
            calcular(cod, slug, nombre, cache, sin_parcelas=True)
        return
    calcular(args.partido, args.slug, args.nombre, args.cache, args.sin_parcelas)


def zona_excluida():
    """Zona que JULIO pidió dejar afuera: adentro del anillo de la Ruta 6 (conurbano) y de la
    Ruta 2 hacia la costa atlántica. datos/zona_excluida.geojson se armó con las rutas de
    red_rutas.json (RP 6 y RP 2) cerrando por el río y el mar."""
    p = os.path.join(AQUI, "datos", "zona_excluida.geojson")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return unary_union([shape(ft["geometry"]).buffer(0) for ft in json.load(f)["features"]])


def recortar():
    """Saca de datos/caminos_catastro_*.geojson los tramos que caen en la zona excluida y
    borra los partidos que quedan vacíos."""
    import glob
    from shapely.prepared import prep
    zona = zona_excluida()
    if zona is None:
        return
    z = prep(zona)
    datos = os.path.join(AQUI, "datos")
    for p in sorted(glob.glob(os.path.join(datos, "caminos_catastro_*.geojson"))):
        with open(p, encoding="utf-8") as f:
            gj = json.load(f)
        feats = [ft for ft in gj["features"]
                 if not z.contains(shape(ft["geometry"]).interpolate(0.5, normalized=True))]
        if len(feats) == len(gj["features"]):
            continue
        slug = os.path.basename(p)[len("caminos_catastro_"):-len(".geojson")]
        if not feats:
            os.remove(p)
            lp = os.path.join(datos, f"limite_arba_{slug}.geojson")
            if os.path.exists(lp):
                os.remove(lp)
            log(f"recorte: {slug} queda entero en la zona excluida, se saca")
            continue
        r = gj.get("resumen") or {}
        r["km_caminos"] = round(sum(ft["properties"]["km"] for ft in feats), 1)
        r["tramos"] = len(feats)
        r["km_por_categoria"] = {k: round(sum(ft["properties"]["km"] for ft in feats
                                              if ft["properties"].get("categoria") == k), 1)
                                 for k in ("principal", "secundario", "vecinal")}
        r["recortado"] = "sin la zona adentro de la Ruta 6 ni de la Ruta 2 a la costa"
        gj["features"], gj["resumen"] = feats, r
        with open(p, "w", encoding="utf-8") as f:
            json.dump(gj, f, ensure_ascii=False, separators=(",", ":"))
        log(f"recorte: {slug} queda con {r['km_caminos']} km")


def indice():
    """Índice liviano para el mapa: por partido el recuadro y los km, y los límites simplificados.
    El mapa baja los caminos de cada partido recién cuando entra en pantalla."""
    import glob
    datos = os.path.join(AQUI, "datos")
    partidos, limites = [], []
    for p in sorted(glob.glob(os.path.join(datos, "caminos_catastro_*.geojson"))):
        slug = os.path.basename(p)[len("caminos_catastro_"):-len(".geojson")]
        with open(p, encoding="utf-8") as f:
            gj = json.load(f)
        r = gj.get("resumen") or {}
        xs = [c[0] for ft in gj["features"] for c in ft["geometry"]["coordinates"]]
        ys = [c[1] for ft in gj["features"] for c in ft["geometry"]["coordinates"]]
        if not xs:
            continue
        partidos.append({"slug": slug, "nombre": r.get("partido") or slug, "codigo": r.get("codigo_arba"),
                         "km": r.get("km_caminos"), "km_por_categoria": r.get("km_por_categoria"),
                         "tramos": r.get("tramos"),
                         "bbox": [round(min(xs), 4), round(min(ys), 4), round(max(xs), 4), round(max(ys), 4)]})
        lp = os.path.join(datos, f"limite_arba_{slug}.geojson")
        if os.path.exists(lp):
            with open(lp, encoding="utf-8") as f:
                g = shape(json.load(f)["features"][0]["geometry"]).simplify(0.003)
            geom = json.loads(json.dumps(mapping(g)))

            def red(o):
                return [red(x) for x in o] if isinstance(o, (list, tuple)) else round(o, 4)
            geom["coordinates"] = red(geom["coordinates"])
            limites.append({"type": "Feature", "properties": {"slug": slug, "nombre": partidos[-1]["nombre"],
                                                              "km": partidos[-1]["km"]}, "geometry": geom})
    tot = {"partidos": len(partidos), "km_caminos": round(sum(p["km"] or 0 for p in partidos), 1),
           "km_por_categoria": {k: round(sum((p["km_por_categoria"] or {}).get(k, 0) for p in partidos), 1)
                                for k in ("principal", "secundario", "vecinal")}}
    with open(os.path.join(datos, "indice_catastro.json"), "w", encoding="utf-8") as f:
        json.dump({"total": tot, "partidos": partidos}, f, ensure_ascii=False, indent=0)
    with open(os.path.join(datos, "limites_partidos.geojson"), "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": limites}, f, ensure_ascii=False, separators=(",", ":"))
    log("INDICE " + json.dumps(tot, ensure_ascii=False))


def categoria(ancho, osm_principal):
    if osm_principal or ancho >= ANCHO_PRINCIPAL:
        return "principal"
    return "secundario" if ancho >= ANCHO_SECUNDARIO else "vecinal"


def calcular(PARTIDO, SLUG, NOMBRE, cache, sin_parcelas=False, manzanas=False):
    """manzanas=True: la zona urbana se arma con manzanas, quintas y chacras de ARBA en vez de
    con las parcelas urbanas (son ~10 veces menos para bajar; en el conurbano son cientos de miles)."""

    import rasterio.features
    from rasterio.transform import from_origin
    from scipy import ndimage
    from skan import Skeleton
    from skimage.morphology import remove_small_objects, skeletonize

    log("Bajando de ARBA límite y parcelas…")
    depto = wfs("idera:Departamento", f"cca='{PARTIDO}'", cache, "depto.json")
    rurales = wfs("idera:Parcela", f"pda LIKE '{PARTIDO}%' AND tpa='Rural'", cache, "rurales.json")
    if manzanas:
        urbanas = []
        for capa in ("Manzana", "Quinta", "Chacra"):
            urbanas += wfs(f"idera:{capa}", f"cca LIKE '{PARTIDO}%'", cache, f"{capa.lower()}.json")
    else:
        urbanas = wfs("idera:Parcela", f"pda LIKE '{PARTIDO}%' AND tpa<>'Rural'", cache, "urbanas.json")
    if len(rurales) < MIN_RURALES:
        log(f"{NOMBRE}: solo {len(rurales)} parcelas rurales, se saltea")
        return None
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
    osm_f = leer_osm(f"caminos_{SLUG}.geojson")
    osm = [transform(a_m, shape(f["geometry"])) for f in osm_f]
    osm_buf = unary_union([g.buffer(35) for g in osm]) if osm else None
    osm_ppal = [g for g, f in zip(osm, osm_f) if (f.get("properties") or {}).get("highway") in OSM_PRINCIPAL]
    ppal_buf = unary_union([g.buffer(35) for g in osm_ppal]) if osm_ppal else None
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
    km_cat = {"principal": 0.0, "secundario": 0.0, "vecinal": 0.0}
    for ln, w, _ in lineas:
        largo = ln.length
        a = extremos[(round(ln.coords[0][0] / RES), round(ln.coords[0][1] / RES))]
        b = extremos[(round(ln.coords[-1][0] / RES), round(ln.coords[-1][1] / RES))]
        if largo < LARGO_MIN and (a == 1 or b == 1):
            continue
        if agua_buf is not None and ln.intersection(agua_buf).length > 0.6 * largo:
            continue
        en_osm = "no" if osm_buf is not None else "sin_dato"
        if osm_buf is not None and ln.intersection(osm_buf).length > 0.6 * largo:
            en_osm = "si"
        if en_osm != "si" and ln.intersection(borde_buf).length > 0.5 * largo:
            continue
        ppal = ppal_buf is not None and ln.intersection(ppal_buf).length > 0.6 * largo
        cat = categoria(w, ppal)
        i = arbol.nearest(ln.interpolate(0.5, normalized=True))
        g = transform(a_geo, ln)
        km = largo / 1000
        km_tot += km
        if en_osm == "no":
            km_nuevo += km
        km_cat[cat] += km
        feats.append({"type": "Feature", "geometry": mapping(g),
                      "properties": {"km": round(km, 2), "ancho_m": round(w), "categoria": cat,
                                     "en_osm": en_osm, "cuartel": cuartel_de[int(i)],
                                     "partido": NOMBRE, "fuente": "catastro ARBA"}})
    for f in feats:
        f["geometry"]["coordinates"] = [[round(x, 6), round(y, 6)] for x, y in f["geometry"]["coordinates"]]
    log(f"{len(feats)} tramos, {km_tot:.1f} km; {km_nuevo:.1f} km no están en OSM")

    resumen = {"fuente": "catastro ARBA (huecos entre parcelas)", "partido": NOMBRE,
               "codigo_arba": PARTIDO, "tramos": len(feats), "km_caminos": round(km_tot, 1),
               "km_por_categoria": {k: round(v, 1) for k, v in km_cat.items()},
               "km_que_faltan_en_osm": round(km_nuevo, 1) if osm_buf is not None else None,
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
        if isinstance(o, dict):
            return {k: redondear(v) for k, v in o.items()}
        return o
    for f in parcelas:
        f["geometry"]["coordinates"] = redondear(f["geometry"]["coordinates"])

    salidas = {f"caminos_catastro_{SLUG}.geojson": {"type": "FeatureCollection", "resumen": resumen,
                                                     "features": feats},
               f"limite_arba_{SLUG}.geojson": {"type": "FeatureCollection", "features": [
                   {"type": "Feature", "properties": {"nombre": f"Partido de {NOMBRE}"},
                    "geometry": redondear(json.loads(json.dumps(mapping(limite.simplify(0.0001)))))}]}}
    if not sin_parcelas:
        salidas[f"parcelas_arba_{SLUG}.geojson"] = {"type": "FeatureCollection", "features": parcelas}
    for d in [os.path.join(AQUI, "datos")] + ([DIR_SALIDA] if os.path.isdir(DIR_SALIDA) else []):
        for nombre, obj in salidas.items():
            with open(os.path.join(d, nombre), "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    log("RESUMEN " + json.dumps(resumen, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
