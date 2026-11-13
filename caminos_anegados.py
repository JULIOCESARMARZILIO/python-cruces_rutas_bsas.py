"""
Caminos con agua: detecta con el radar Sentinel-1 qué tramos de los caminos rurales de un
partido están anegados.

El radar ve el suelo aunque esté nublado y pasa cada pocos días. El agua quieta refleja la
señal lejos del satélite y aparece muy oscura (VV por debajo de UMBRAL_DB, -18 dB por omisión);
la tierra, los cultivos y los caminos secos devuelven bastante más.

Fuente: colección «sentinel-1-rtc» de Microsoft Planetary Computer (gratis, sin cuenta ni clave;
las direcciones de los archivos se firman solas con la librería planetary_computer).

Para cada punto de los caminos (cada PASO_M metros) toma el valor de la pasada más nueva que lo
cubre dentro de los últimos DIAS días, con una mediana de 3x3 píxeles (30 m) para sacar el ruido
del radar. Un tramo se marca con agua si tiene al menos MIN_PUNTOS puntos seguidos bajo el umbral.

También deja anotada la imagen óptica Sentinel-2 más nueva con poca nube, para la capa
«Satélite reciente» del mapa.

Salida en DIR_SALIDA:
  anegados_<partido>.geojson   tramos con agua (fecha de la pasada, km, categoría, cuartel)
  satelite_reciente_<partido>.json  imagen Sentinel-2 más nueva (fecha, nubes, dirección de teselas)

Uso:  python caminos_anegados.py [partido]    (por omisión 25_de_mayo)
"""

import datetime as dt
import glob
import json
import math
import os
import sys

import numpy as np

DIR_SALIDA = os.environ.get("DIR_SALIDA", "/data/salida")
DIR_DATOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "datos")
STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
TESELAS = "https://planetarycomputer.microsoft.com/api/data/v1/item/tiles/WebMercatorQuad/{z}/{x}/{y}@1x"
UMBRAL_DB = float(os.environ.get("ANEGADOS_UMBRAL_DB", "-18"))
DIAS = int(os.environ.get("ANEGADOS_DIAS", "12"))
PASO_M = 15.0
MIN_PUNTOS = 3        # 3 puntos seguidos = unos 45 m de camino con agua
FRANJA = 512          # filas del raster que se leen de una vez


def hav_m(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371000.0 * math.asin(min(1.0, math.sqrt(a)))


def _leer(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def caminos(partido):
    """Tramos del catastro ARBA del partido y los marcados a mano: [(coords, propiedades)]."""
    out = []
    for path in (os.path.join(DIR_SALIDA, f"caminos_catastro_{partido}.geojson"),
                 os.path.join(DIR_DATOS, f"caminos_catastro_{partido}.geojson")):
        if os.path.exists(path):
            for f in _leer(path)["features"]:
                g = f["geometry"]
                if g["type"] == "LineString":
                    out.append((g["coordinates"], f.get("properties") or {}))
            break
    marcados = os.path.join(DIR_SALIDA, "caminos_usuario.geojson")
    if os.path.exists(marcados):
        for f in _leer(marcados).get("features", []):
            g = f.get("geometry") or {}
            if g.get("type") == "LineString":
                pr = dict(f.get("properties") or {})
                pr.setdefault("categoria", "marcado")
                out.append((g["coordinates"], pr))
    return out


def densificar(coords, paso=PASO_M):
    """Puntos cada `paso` metros sobre la línea (incluye los vértices)."""
    pts = [tuple(coords[0][:2])]
    for (x1, y1, *_), (x2, y2, *_) in zip(coords, coords[1:]):
        d = hav_m(x1, y1, x2, y2)
        n = max(1, int(d // paso))
        for i in range(1, n + 1):
            t = i / n
            pts.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
    return pts


def limite(partido):
    from shapely.geometry import shape
    gj = _leer(os.path.join(DIR_DATOS, f"limite_arba_{partido}.geojson"))
    return shape(gj["features"][0]["geometry"])


def _catalogo():
    import planetary_computer as pc
    import pystac_client
    return pystac_client.Client.open(STAC, modifier=pc.sign_inplace)


def muestrear_radar(cat, bbox, lons, lats, hoy):
    """dB de VV (mediana 3x3) y fecha de la pasada para cada punto; NaN si ninguna lo cubre."""
    import rasterio
    from rasterio.warp import transform
    from rasterio.windows import Window
    from scipy.ndimage import median_filter

    n = len(lons)
    db = np.full(n, np.nan, dtype=np.float32)
    fecha = np.full(n, "", dtype=object)
    desde = (hoy - dt.timedelta(days=DIAS)).isoformat()
    items = sorted(cat.search(collections=["sentinel-1-rtc"], bbox=bbox,
                              datetime=f"{desde}/{hoy.isoformat()}").items(),
                   key=lambda it: it.datetime, reverse=True)
    usadas = []
    for it in items:
        falta = np.isnan(db)
        if not falta.any():
            break
        with rasterio.open(it.assets["vv"].href) as r:
            xs, ys = transform("EPSG:4326", r.crs, lons[falta].tolist(), lats[falta].tolist())
            fil, col = rasterio.transform.rowcol(r.transform, xs, ys)
            fil, col = np.asarray(fil), np.asarray(col)
            idx = np.flatnonzero(falta)
            dentro = (fil >= 1) & (col >= 1) & (fil < r.height - 1) & (col < r.width - 1)
            if not dentro.any():
                continue
            idx, fil, col = idx[dentro], fil[dentro], col[dentro]
            c0, c1 = int(col.min()) - 1, int(col.max()) + 2
            puso = 0
            for f0 in range(int(fil.min()) - 1, int(fil.max()) + 2, FRANJA - 2):
                en = (fil >= f0 + 1) & (fil < f0 + FRANJA - 1)
                if not en.any():
                    continue
                a = r.read(1, window=Window(c0, f0, c1 - c0, FRANJA), boundless=True,
                           fill_value=r.nodata).astype(np.float32)
                a[(a == r.nodata) | (a <= 0)] = np.nan
                a = median_filter(np.nan_to_num(a, nan=-1.0), size=3)
                v = a[fil[en] - f0, col[en] - c0]
                ok = v > 0                          # -1 = sin dato en algún vecino
                sel = idx[en][ok]
                db[sel] = 10 * np.log10(v[ok])
                fecha[sel] = it.datetime.date().isoformat()
                puso += int(ok.sum())
            if puso:
                usadas.append({"pasada": it.id, "fecha": it.datetime.date().isoformat(),
                               "puntos": puso})
                print(f"  {it.id}: {puso} puntos", flush=True)
    return db, fecha, usadas


def satelite_reciente(cat, bbox, hoy):
    """Imágenes Sentinel-2 del día más nuevo con menos de 20 % de nubes sobre el partido
    (el partido cae en varias teselas de 110 km: se devuelven todas las de ese día)."""
    desde = (hoy - dt.timedelta(days=45)).isoformat()
    items = list(cat.search(collections=["sentinel-2-l2a"], bbox=bbox,
                            datetime=f"{desde}/{hoy.isoformat()}",
                            query={"eo:cloud_cover": {"lt": 20}}).items())
    if not items:
        return None
    dia = max(it.datetime.date() for it in items)
    del_dia = [it for it in items if it.datetime.date() == dia]
    return {"fecha": dia.isoformat(),
            "nubes_pct": round(max(it.properties["eo:cloud_cover"] for it in del_dia), 1),
            "imagenes": [it.id for it in del_dia],
            "teselas": [f"{TESELAS}?collection=sentinel-2-l2a&item={it.id}&assets=visual"
                        "&asset_bidx=visual%7C1%2C2%2C3&nodata=0" for it in del_dia],
            "fuente": "Sentinel-2 (Copernicus) vía Microsoft Planetary Computer"}


def main(partido="25_de_mayo"):
    hoy = dt.date.today()
    lim = limite(partido)
    bbox = list(lim.bounds)
    tramos = caminos(partido)
    if not tramos:
        print(f"No hay caminos de catastro para {partido}")
        return 1
    lons, lats, tramo_de = [], [], []
    for i, (coords, _) in enumerate(tramos):
        for x, y in densificar(coords):
            lons.append(x)
            lats.append(y)
            tramo_de.append(i)
    lons, lats, tramo_de = np.array(lons), np.array(lats), np.array(tramo_de)
    print(f"{partido}: {len(tramos)} tramos, {len(lons)} puntos; radar de los últimos {DIAS} días",
          flush=True)

    cat = _catalogo()
    db, fecha, usadas = muestrear_radar(cat, bbox, lons, lats, hoy)
    agua = db < UMBRAL_DB

    feats = []
    km_total = 0.0
    for i, (coords, pr) in enumerate(tramos):
        sel = np.flatnonzero(tramo_de == i)
        j = 0
        while j < len(sel):
            if not agua[sel[j]]:
                j += 1
                continue
            k = j
            while k + 1 < len(sel) and agua[sel[k + 1]]:
                k += 1
            if k - j + 1 >= MIN_PUNTOS:
                linea = [[round(float(lons[s]), 6), round(float(lats[s]), 6)] for s in sel[j:k + 1]]
                m = sum(hav_m(*a, *b) for a, b in zip(linea, linea[1:]))
                km_total += m / 1000
                feats.append({"type": "Feature",
                              "geometry": {"type": "LineString", "coordinates": linea},
                              "properties": {"metros": round(m), "fecha_radar": fecha[sel[j]],
                                             "db_min": round(float(np.nanmin(db[sel[j:k + 1]])), 1),
                                             "categoria": pr.get("categoria", ""),
                                             "cuartel": pr.get("cuartel", ""),
                                             "nombre": pr.get("nombre", "")}})
            j = k + 1

    sin_dato = int(np.isnan(db).sum())
    resumen = {"partido": partido, "calculado": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
               "umbral_db": UMBRAL_DB, "dias": DIAS, "pasadas": usadas,
               "tramos_con_agua": len(feats), "km_con_agua": round(km_total, 1),
               "puntos": int(len(lons)), "puntos_sin_dato": sin_dato,
               "fuente": "Sentinel-1 RTC (Copernicus) vía Microsoft Planetary Computer"}
    os.makedirs(DIR_SALIDA, exist_ok=True)
    with open(os.path.join(DIR_SALIDA, f"anegados_{partido}.geojson"), "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "resumen": resumen, "features": feats}, f,
                  ensure_ascii=False)
    print(json.dumps({k: v for k, v in resumen.items() if k != "pasadas"}, ensure_ascii=False))

    try:
        sat = satelite_reciente(cat, bbox, hoy)
    except Exception as e:  # noqa: la capa óptica es un extra, no frena lo anterior
        print(f"Sentinel-2: {e}")
        sat = None
    if sat:
        with open(os.path.join(DIR_SALIDA, f"satelite_reciente_{partido}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(sat, f, ensure_ascii=False)
        print(f"Satélite reciente: {sat['fecha']} ({sat['nubes_pct']} % nubes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "25_de_mayo"))
