"""
Terminales portuarias de granos (exportación) georreferenciadas a mano sobre
imagen satelital (datos/puertos_granos.csv), con la distancia por ruta desde un
origen (por defecto la ciudad de 25 de Mayo).

Distancia: ruteo de OSRM sobre el mapa completo de OSM (incluye calles de acceso,
autopistas y enlaces). Si OSRM no responde, se usa la red RN/RP propia
(red_rutas.json), que es más gruesa y da distancias de más.

Si hay GOOGLE_PLACES_KEY, cada terminal se cruza con Google Maps: nombre que
devuelve Google y a cuántos metros queda de nuestro punto.

Entrada:
  datos/puertos_granos.csv
  DIR_SALIDA/red_rutas.json, DIR_SALIDA/cruces_*.csv  (solo para el respaldo)
Salida:
  DIR_SALIDA/puertos_granos.csv / .geojson
Variables:
  ORIGEN_PUERTOS="nombre;lat;lon"   (default "25 de Mayo;-35.432415;-60.171624")
  OSRM_URL                          (default https://router.project-osrm.org)
"""

import csv
import heapq
import json
import os

import requests

from plantas_sisa import DIR_SALIDA, KEY, PROVINCIAS, URL, Red, haversine_km, log

ENTRADA = os.environ.get("PUERTOS_GRANOS", "datos/puertos_granos.csv")
ORIGEN = os.environ.get("ORIGEN_PUERTOS", "25 de Mayo;-35.432415;-60.171624")
OSRM = os.environ.get("OSRM_URL", "https://router.project-osrm.org").rstrip("/")
RADIO_ACCESO_KM = 20
FACTOR_ACCESO = 1.3      # el camino de acceso real es más largo que la línea recta
CAMPOS_GOOGLE = "places.id,places.displayName,places.formattedAddress,places.location"


# ------------------------------------------------------------------ OSRM
def distancias_osrm(lat_o, lon_o, puertos):
    """km y horas por ruta desde el origen a cada terminal (una sola consulta)."""
    coords = ";".join([f"{lon_o},{lat_o}"] + [f"{p['lon']},{p['lat']}" for p in puertos])
    r = requests.get(f"{OSRM}/table/v1/driving/{coords}",
                     params={"sources": "0", "annotations": "distance,duration"}, timeout=120)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != "Ok":
        raise RuntimeError(d.get("code"))
    return [(m / 1000 if m is not None else None, s / 3600 if s is not None else None)
            for m, s in zip(d["distances"][0][1:], d["durations"][0][1:])]


# ------------------------------------------------- respaldo: red RN/RP propia
def distancias_desde(red, origen):
    dist = {origen: 0.0}
    cola = [(0.0, origen)]
    while cola:
        d, n = heapq.heappop(cola)
        if d > dist[n]:
            continue
        for m, km, _ in red.grafo[n]:
            nd = d + km
            if nd < dist.get(m, 1e18):
                dist[m] = nd
                heapq.heappush(cola, (nd, m))
    return dist


def mejor_acceso(red, dist, lat, lon, radio_km=RADIO_ACCESO_KM):
    """Nodo de ruta por el que conviene entrar: minimiza km por la red + tramo de acceso
    (línea recta x FACTOR_ACCESO). Evita quedar enganchado a un pedazo de ruta suelto."""
    mejor = None
    ci, cj = int(lat // 0.05), int(lon // 0.05)
    r = int(radio_km / 5) + 1
    for i in range(ci - r, ci + r + 1):
        for j in range(cj - r, cj + r + 1):
            for nid in red.grilla.get((i, j), ()):
                if nid not in dist:
                    continue
                acc = haversine_km(lat, lon, *red.nodos[nid])
                if acc > radio_km:
                    continue
                total = dist[nid] + acc * FACTOR_ACCESO
                if mejor is None or total < mejor[0]:
                    mejor = (total, nid, acc)
    return mejor


def distancias_red(lat_o, lon_o, puertos):
    ruta_red = os.path.join(DIR_SALIDA, "red_rutas.json")
    if not os.path.exists(ruta_red):
        return [None] * len(puertos)
    red = Red(ruta_red, [os.path.join(DIR_SALIDA, f"cruces_{s}.csv") for s in PROVINCIAS.values()
                         if os.path.exists(os.path.join(DIR_SALIDA, f"cruces_{s}.csv"))])
    nodo_o, acc_o = red.nodo_mas_cercano(lat_o, lon_o, RADIO_ACCESO_KM)
    dist = distancias_desde(red, nodo_o)
    out = []
    for p in puertos:
        m = mejor_acceso(red, dist, float(p["lat"]), float(p["lon"]))
        out.append(round(m[0] + acc_o * FACTOR_ACCESO, 1) if m else None)
    return out


# ---------------------------------------------------------- Google Places
def verificar_google(p):
    cuerpo = {"textQuery": f"{p['empresa']} {p['terminal']} {p['localidad']}", "languageCode": "es",
              "regionCode": "AR", "pageSize": 5,
              "locationBias": {"circle": {"center": {"latitude": float(p["lat"]),
                                                     "longitude": float(p["lon"])},
                                          "radius": 3000}}}
    r = requests.post(URL, json=cuerpo, timeout=60,
                      headers={"X-Goog-Api-Key": KEY, "X-Goog-FieldMask": CAMPOS_GOOGLE,
                               "Content-Type": "application/json"})
    if r.status_code != 200:
        log(f"  Google {r.status_code} para {p['terminal']}")
        return {}
    mejor = None
    for pl in r.json().get("places", []):
        loc = pl.get("location", {})
        d = haversine_km(float(p["lat"]), float(p["lon"]), loc.get("latitude", 0), loc.get("longitude", 0))
        if mejor is None or d < mejor[0]:
            mejor = (d, pl)
    if not mejor:
        return {}
    d, pl = mejor
    return {"nombre_google": pl.get("displayName", {}).get("text", ""),
            "direccion_google": pl.get("formattedAddress", ""),
            "dist_google_m": round(d * 1000),
            "google_place": f"https://www.google.com/maps/place/?q=place_id:{pl.get('id', '')}"}


def main():
    with open(ENTRADA, encoding="utf-8") as f:
        puertos = list(csv.DictReader(f))
    log(f"Terminales portuarias: {len(puertos)}")
    nombre_o, lat_o, lon_o = ORIGEN.split(";")
    lat_o, lon_o = float(lat_o), float(lon_o)

    try:
        osrm = distancias_osrm(lat_o, lon_o, puertos)
        fuente = "OSRM (OpenStreetMap)"
    except Exception as e:  # noqa
        log(f"OSRM no disponible ({e}); se usa la red RN/RP propia")
        osrm = [(km, None) for km in distancias_red(lat_o, lon_o, puertos)]
        fuente = "red RN/RP propia (aprox.)"

    for p, (km, horas) in zip(puertos, osrm):
        la, lo = float(p["lat"]), float(p["lon"])
        p["origen"] = nombre_o
        p["km_linea_recta"] = round(haversine_km(lat_o, lon_o, la, lo), 1)
        p["km_por_ruta"] = round(km, 1) if km is not None else ""
        p["horas_camion"] = round(horas * 1.25, 1) if horas is not None else ""
        p["distancia_fuente"] = fuente
        p["google_maps"] = f"https://www.google.com/maps?q={la},{lo}"
        p["como_llegar"] = (f"https://www.google.com/maps/dir/?api=1&origin={lat_o},{lon_o}"
                            f"&destination={la},{lo}&travelmode=driving")
        if KEY:
            p.update(verificar_google(p))
        log(f"  {p['terminal']}: {p['km_por_ruta'] or '-'} km por ruta, "
            f"{p['km_linea_recta']} en línea recta")

    campos = ["id", "terminal", "empresa", "localidad", "provincia", "zona", "tipo", "lat", "lon",
              "origen", "km_por_ruta", "horas_camion", "km_linea_recta", "distancia_fuente",
              "google_maps", "como_llegar", "nombre_google", "direccion_google", "dist_google_m",
              "google_place", "fuente"]
    os.makedirs(DIR_SALIDA, exist_ok=True)
    with open(os.path.join(DIR_SALIDA, "puertos_granos.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(puertos)
    feats = [{"type": "Feature",
              "geometry": {"type": "Point", "coordinates": [float(p["lon"]), float(p["lat"])]},
              "properties": {k: p.get(k, "") for k in campos if k not in ("lat", "lon")}}
             for p in puertos]
    with open(os.path.join(DIR_SALIDA, "puertos_granos.geojson"), "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f, ensure_ascii=False)
    log("puertos_granos.csv / .geojson escritos")


if __name__ == "__main__":
    main()
