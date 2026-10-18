"""
Plantas de silos / acopio / puertos cerealeros según Google Places (API nueva).

Hace una búsqueda de texto por localidad (centro estimado a partir de los
cruces ya calculados en cruces_<provincia>.csv) con sesgo de 15 km, y junta
los resultados sin repetir. Guarda el avance localidad por localidad, así
una corrida interrumpida se reanuda donde quedó.

Requiere la variable de entorno GOOGLE_PLACES_KEY.
Salida (en DIR_SALIDA): silos_google.csv / .geojson
"""

import csv
import json
import os
import sys
import time
from collections import defaultdict

import requests

DIR_SALIDA = os.environ.get("DIR_SALIDA", "/data/salida")
KEY = os.environ.get("GOOGLE_PLACES_KEY", "")
URL = "https://places.googleapis.com/v1/places:searchText"
CAMPOS = ("places.id,places.displayName,places.formattedAddress,places.location,"
          "places.types,places.businessStatus,nextPageToken")
CONSULTAS = ["acopio de cereales", "planta de silos"]
RADIO_M = 15000
MAX_PAGINAS = 2          # cada página es una consulta facturable
PAUSA_S = 0.25

PROVINCIAS = {
    "buenos_aires": "Buenos Aires", "santa_fe": "Santa Fe", "entre_rios": "Entre Ríos",
    "cordoba": "Córdoba", "la_pampa": "La Pampa", "santiago_del_estero": "Santiago del Estero",
}
PALABRAS_CLAVE = ("acopio", "silo", "cereal", "grano", "agro", "semilla", "puerto",
                  "terminal", "molino", "aceitera", "cooperativa", "oleagin", "agrícola",
                  "agricola", "agropecuari", "campo", "rural", "soja", "trigo", "maíz", "maiz")

SALIDA_CSV = os.path.join(DIR_SALIDA, "silos_google.csv")
SALIDA_GJ = os.path.join(DIR_SALIDA, "silos_google.geojson")
AVANCE = os.path.join(DIR_SALIDA, "silos_google_avance.json")
CRUDO = os.path.join(DIR_SALIDA, "silos_google_crudo.jsonl")


def log(m):
    print(m, flush=True)


def localidades():
    """{(provincia, localidad): (lat, lon)} a partir de los cruces calculados."""
    acum = defaultdict(list)
    for slug, prov in PROVINCIAS.items():
        ruta = os.path.join(DIR_SALIDA, f"cruces_{slug}.csv")
        if not os.path.exists(ruta):
            continue
        with open(ruta, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["localidad_cercana"]:
                    acum[(prov, r["localidad_cercana"])].append((float(r["lat"]), float(r["lon"])))
    return {k: (sum(p[0] for p in v) / len(v), sum(p[1] for p in v) / len(v))
            for k, v in acum.items()}


def buscar(texto, lat, lon):
    resultados, token = [], None
    for _ in range(MAX_PAGINAS):
        cuerpo = {
            "textQuery": texto, "languageCode": "es", "regionCode": "AR", "pageSize": 20,
            "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lon},
                                        "radius": RADIO_M}},
        }
        if token:
            cuerpo["pageToken"] = token
        for intento in range(3):
            r = requests.post(URL, json=cuerpo, timeout=60,
                              headers={"X-Goog-Api-Key": KEY, "X-Goog-FieldMask": CAMPOS,
                                       "Content-Type": "application/json"})
            if r.status_code == 200:
                break
            if r.status_code in (429, 500, 503):
                time.sleep(5 * (intento + 1))
                continue
            raise RuntimeError(f"Places {r.status_code}: {r.text[:300]}")
        else:
            raise RuntimeError("Places: demasiados reintentos")
        d = r.json()
        resultados += d.get("places", [])
        token = d.get("nextPageToken")
        if not token:
            break
        time.sleep(PAUSA_S)
    return resultados


def main():
    if not KEY:
        log("Falta GOOGLE_PLACES_KEY; se omite la capa de Google.")
        return
    os.makedirs(DIR_SALIDA, exist_ok=True)
    locs = localidades()
    log(f"Localidades a consultar: {len(locs)}")

    hechas = set()
    if os.path.exists(AVANCE):
        hechas = set(tuple(x) for x in json.load(open(AVANCE, encoding="utf-8")))

    n_consultas = 0
    with open(CRUDO, "a", encoding="utf-8") as crudo:
        for i, ((prov, loc), (lat, lon)) in enumerate(sorted(locs.items()), 1):
            if (prov, loc) in hechas:
                continue
            try:
                for q in CONSULTAS:
                    for p in buscar(f"{q} {loc} {prov}", lat, lon):
                        p["_prov"], p["_loc"], p["_q"] = prov, loc, q
                        crudo.write(json.dumps(p, ensure_ascii=False) + "\n")
                    n_consultas += 1
                    time.sleep(PAUSA_S)
            except RuntimeError as e:
                log(f"  {prov} / {loc}: {e}")
                if any(c in str(e) for c in ("401", "403", "API keys are not supported", "PERMISSION")):
                    log("Clave sin permiso o API no habilitada; se detiene.")
                    return
                continue
            hechas.add((prov, loc))
            crudo.flush()
            json.dump(sorted(hechas), open(AVANCE, "w", encoding="utf-8"), ensure_ascii=False)
            if i % 50 == 0:
                log(f"  {i}/{len(locs)} localidades ({n_consultas} consultas en esta corrida)")

    # consolidar
    vistos, filas = {}, []
    with open(CRUDO, encoding="utf-8") as f:
        for linea in f:
            p = json.loads(linea)
            pid = p.get("id")
            if not pid or pid in vistos:
                continue
            nombre = p.get("displayName", {}).get("text", "")
            tipos = ",".join(p.get("types", []))
            texto = (nombre + " " + tipos).lower()
            relevante = any(k in texto for k in PALABRAS_CLAVE)
            vistos[pid] = True
            filas.append({
                "provincia_busqueda": p["_prov"], "localidad_busqueda": p["_loc"],
                "nombre": nombre, "direccion": p.get("formattedAddress", ""),
                "lat": p.get("location", {}).get("latitude", ""),
                "lon": p.get("location", {}).get("longitude", ""),
                "tipos": tipos, "estado": p.get("businessStatus", ""),
                "relevante": "si" if relevante else "revisar",
                "place_id": pid,
                "google_maps": f"https://www.google.com/maps/place/?q=place_id:{pid}",
            })
    filas.sort(key=lambda r: (r["provincia_busqueda"], r["localidad_busqueda"], r["nombre"]))
    campos = list(filas[0].keys()) if filas else ["nombre"]
    with open(SALIDA_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos)
        w.writeheader()
        w.writerows(filas)
    gj = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
        "properties": {k: r[k] for k in campos if k not in ("lat", "lon")},
    } for r in filas if r["lat"] != ""]}
    with open(SALIDA_GJ, "w", encoding="utf-8") as f:
        json.dump(gj, f, ensure_ascii=False)
    log(f"Google Places: {len(filas)} lugares únicos "
        f"({sum(r['relevante'] == 'si' for r in filas)} con palabras clave del rubro)")


if __name__ == "__main__":
    main()
