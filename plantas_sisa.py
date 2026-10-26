"""
Plantas del padrón SISA (acopiadores) ubicadas con Google Places y enganchadas
a la red de rutas: para cada planta, el punto de ruta más cercano y los dos
primeros cruces de ruta saliendo para cada lado, con la distancia por ruta.

Entrada:
  datos/padron_sisa.xlsx   (columnas: N° de planta, CUIT del titular, Titular,
                            Actividades, Localidad, Provincia, ...)
  datos/padron_sisa_extra.csv  (mismas columnas; plantas de otras provincias o actividades
                            sacadas del listado público de ARCA, consulta SISA plantas)
  DIR_SALIDA/red_rutas.json  (lo genera procesar_osm.py)
  DIR_SALIDA/cruces_*.csv
Salida:
  DIR_SALIDA/plantas_sisa.csv / .geojson
Requiere GOOGLE_PLACES_KEY. Guarda el avance en plantas_sisa_cache.jsonl.
"""

import csv
import difflib
import json
import math
import os
import re
import time
import unicodedata
from collections import defaultdict

import openpyxl
import requests

DIR_SALIDA = os.environ.get("DIR_SALIDA", "/data/salida")
PADRON = os.environ.get("PADRON_SISA", "datos/padron_sisa.xlsx")
PADRON_EXTRA = os.environ.get("PADRON_SISA_EXTRA", "datos/padron_sisa_extra.csv")
VERIFICADAS = os.environ.get("PLANTAS_VERIFICADAS", "datos/plantas_verificadas.csv")
KEY = os.environ.get("GOOGLE_PLACES_KEY", "")
URL = "https://places.googleapis.com/v1/places:searchText"
CAMPOS = "places.id,places.displayName,places.formattedAddress,places.location,places.types"
RADIO_BUSQUEDA_M = 25000
MAX_KM_RUTA = 40          # si la ruta más cercana está más lejos, no se engancha
CACHE = os.path.join(DIR_SALIDA, "plantas_sisa_cache.jsonl")

PROVINCIAS = {
    "Buenos Aires": "buenos_aires", "Santa Fe": "santa_fe", "Entre Ríos": "entre_rios",
    "Córdoba": "cordoba", "La Pampa": "la_pampa", "Santiago del Estero": "santiago_del_estero",
}
RUIDO = re.compile(r"\b(s\.?a\.?|s\.?r\.?l\.?|sociedad anonima|sociedad de responsabilidad"
                   r"( limitada| ltda?)?|s\.?c\.?a\.?|s\.?h\.?|srl|sa|y|e|de|del|la|el|los|las)\b")


def log(m):
    print(m, flush=True)


def norm(t):
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = RUIDO.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


# ------------------------------------------------------------- red de rutas
class Red:
    def __init__(self, ruta_json, cruces_csv):
        d = json.load(open(ruta_json, encoding="utf-8"))
        self.nodos = {int(k): (v[0], v[1]) for k, v in d["nodos"].items()}
        self.grafo = defaultdict(list)
        for a, b, km, ruta in d["aristas"]:
            self.grafo[a].append((b, km, ruta))
            self.grafo[b].append((a, km, ruta))
        self.desc, self.cruces, self.cruces_rr = {}, set(), set()
        for ruta_csv in cruces_csv:
            with open(ruta_csv, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    n = int(r["nodo_osm"])
                    self.desc[n] = f"{r['ruta']} x {r['cruza_con']}"
                    if r["tipo"] == "ruta-ruta":
                        self.cruces_rr.add(n)
                    if r["tipo"] in ("ruta-ruta", "acceso"):   # los urbanos (calles) no cuentan
                        self.cruces.add(n)
        # índice por grilla de 0.05° (~5 km)
        self.grilla = defaultdict(list)
        for nid, (la, lo) in self.nodos.items():
            self.grilla[(int(la // 0.05), int(lo // 0.05))].append(nid)

    def nodo_mas_cercano(self, lat, lon, max_km):
        mejor, dmin = None, max_km
        ci, cj = int(lat // 0.05), int(lon // 0.05)
        radio = int(max_km / 5) + 1
        for i in range(ci - radio, ci + radio + 1):
            for j in range(cj - radio, cj + radio + 1):
                for nid in self.grilla.get((i, j), ()):
                    la, lo = self.nodos[nid]
                    d = haversine_km(lat, lon, la, lo)
                    if d < dmin:
                        mejor, dmin = nid, d
        return mejor, dmin

    def cruces_cercanos(self, origen, k=2, max_km=150.0, conjunto=None):
        """Los k cruces más cercanos por la red, en direcciones distintas: el 2º no
        puede alcanzarse pasando por el 1º (y así sucesivamente)."""
        import heapq
        dist, prev = {origen: 0.0}, {}
        conjunto = self.cruces if conjunto is None else conjunto
        cola, hallados = [(0.0, origen)], []
        while cola and len(hallados) < k:
            d, n = heapq.heappop(cola)
            if d > dist.get(n, 1e18) or d > max_km:
                continue
            if n in conjunto and n != origen:
                la, lo = self.nodos[n]
                # misma intersección repetida (calzada doble, rotonda): a menos de 400 m de una ya hallada
                if all(haversine_km(la, lo, *self.nodos[h]) > 0.4 for h, _ in hallados):
                    hallados.append((n, d))
                continue   # no se sigue expandiendo a través de un cruce hallado
            for m, km, _ in self.grafo[n]:
                nd = d + km
                if nd < dist.get(m, 1e18):
                    dist[m], prev[m] = nd, n
                    heapq.heappush(cola, (nd, m))
        return hallados

    def enganchar(self, lat, lon):
        nid, d_ruta = self.nodo_mas_cercano(lat, lon, MAX_KM_RUTA)
        if nid is None:
            return None
        rutas = sorted({x for _, _, r in self.grafo[nid] for x in r.split(";")})
        rla, rlo = self.nodos[nid]
        out = {"ruta": " / ".join(rutas), "dist_a_ruta_km": round(d_ruta, 2),
               "nodo_ruta": nid, "ruta_lat": rla, "ruta_lon": rlo,
               "ruta_maps": f"https://www.google.com/maps?q={rla},{rlo}"}
        for k, (c, km) in enumerate(self.cruces_cercanos(nid, k=2), 1):
            out[f"cruce_{k}"] = c
            out[f"cruce_{k}_desc"] = self.desc.get(c, "")
            out[f"cruce_{k}_km"] = round(km, 2)
        for k, (c, km) in enumerate(self.cruces_cercanos(nid, k=2, conjunto=self.cruces_rr), 1):
            out[f"rr_{k}"] = c
            out[f"rr_{k}_desc"] = self.desc.get(c, "")
            out[f"rr_{k}_km"] = round(km, 2)
        return out


# ----------------------------------------------------------- Google Places
def buscar_planta(titular, localidad, provincia, centro):
    cuerpo = {"textQuery": f"{titular} {localidad} {provincia}", "languageCode": "es",
              "regionCode": "AR", "pageSize": 5}
    if centro:
        cuerpo["locationBias"] = {"circle": {"center": {"latitude": centro[0],
                                                        "longitude": centro[1]},
                                             "radius": RADIO_BUSQUEDA_M}}
    for intento in range(3):
        r = requests.post(URL, json=cuerpo, timeout=60,
                          headers={"X-Goog-Api-Key": KEY, "X-Goog-FieldMask": CAMPOS,
                                   "Content-Type": "application/json"})
        if r.status_code == 200:
            return r.json().get("places", [])
        if r.status_code in (429, 500, 503):
            time.sleep(5 * (intento + 1))
            continue
        raise RuntimeError(f"Places {r.status_code}: {r.text[:200]}")
    raise RuntimeError("Places: demasiados reintentos")


def centros_localidades():
    acum = defaultdict(list)
    for prov, slug in PROVINCIAS.items():
        ruta = os.path.join(DIR_SALIDA, f"cruces_{slug}.csv")
        if not os.path.exists(ruta):
            continue
        with open(ruta, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["localidad_cercana"]:
                    acum[(prov, norm(r["localidad_cercana"]))].append(
                        (float(r["lat"]), float(r["lon"])))
    return {k: (sum(p[0] for p in v) / len(v), sum(p[1] for p in v) / len(v))
            for k, v in acum.items()}


def main():
    if not KEY:
        log("Falta GOOGLE_PLACES_KEY; se omite plantas_sisa.")
        return
    if not os.path.exists(PADRON):
        log(f"No está el padrón {PADRON}; se omite plantas_sisa.")
        return
    os.makedirs(DIR_SALIDA, exist_ok=True)
    wb = openpyxl.load_workbook(PADRON, read_only=True)
    filas = list(wb.worksheets[0].iter_rows(values_only=True))
    hdr = [str(h).strip() for h in filas[0]]
    plantas = [dict(zip(hdr, r)) for r in filas[1:] if r and r[0]]
    if os.path.exists(PADRON_EXTRA):
        ya = {str(p.get("N° de planta")).strip() for p in plantas}
        with open(PADRON_EXTRA, encoding="utf-8") as f:
            plantas += [r for r in csv.DictReader(f) if r["N° de planta"].strip() not in ya]
    log(f"Plantas en el padrón: {len(plantas)}")

    verificadas = {}
    if os.path.exists(VERIFICADAS):
        with open(VERIFICADAS, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("lat") and r.get("lon"):
                    verificadas[str(r["n_planta"]).strip()] = (float(r["lat"]), float(r["lon"]),
                                                               r.get("nota", ""))
    log(f"Ubicaciones verificadas a mano: {len(verificadas)}")
    centros = centros_localidades()
    red = Red(os.path.join(DIR_SALIDA, "red_rutas.json"),
              [os.path.join(DIR_SALIDA, f"cruces_{s}.csv") for s in PROVINCIAS.values()
               if os.path.exists(os.path.join(DIR_SALIDA, f"cruces_{s}.csv"))])
    log(f"Red: {len(red.nodos)} nodos, {len(red.cruces)} cruces")

    cache = {}
    if os.path.exists(CACHE):
        for linea in open(CACHE, encoding="utf-8"):
            d = json.loads(linea)
            cache[d["clave"]] = d["res"]

    salida = []
    with open(CACHE, "a", encoding="utf-8") as fc:
        for i, p in enumerate(plantas, 1):
            clave = f"{p.get('N° de planta')}|{p.get('CUIT del titular')}"
            titular, loc = str(p.get("Titular") or ""), str(p.get("Localidad") or "")
            prov = str(p.get("Provincia") or "Buenos Aires")
            if clave not in cache:
                centro = centros.get((prov, norm(loc)))
                try:
                    res = buscar_planta(titular, loc, prov, centro)
                except RuntimeError as e:
                    log(f"  {titular} / {loc}: {e}")
                    if any(c in str(e) for c in ("401", "403", "API keys are not supported",
                                                   "PERMISSION", "not enabled")):
                        log("La clave de Google no sirve o la API no está habilitada; "
                            "se detiene sin guardar nada.")
                        return
                    continue   # error transitorio: no se cachea, se reintenta en otra corrida
                cache[clave] = {"places": res, "centro": centro}
                fc.write(json.dumps({"clave": clave, "res": cache[clave]}, ensure_ascii=False) + "\n")
                fc.flush()
                time.sleep(0.2)
            if i % 50 == 0:
                log(f"  {i}/{len(plantas)}")

            res, centro = cache[clave]["places"], cache[clave]["centro"]
            fila = {"n_planta": p.get("N° de planta"), "cuit": p.get("CUIT del titular"),
                    "titular": titular, "localidad": loc, "provincia": prov,
                    "actividades": p.get("Actividades", "")}
            if str(p.get("N° de planta")).strip() in verificadas:
                lat, lon, nota = verificadas[str(p.get("N° de planta")).strip()]
                fila.update({"lat": lat, "lon": lon, "confianza": "verificada",
                             "nombre_google": nota or "ubicación cargada a mano",
                             "google_maps": f"https://www.google.com/maps?q={lat},{lon}"})
                eng = red.enganchar(lat, lon)
                if eng:
                    fila.update(eng)
                salida.append(fila)
                continue
            elegido, mejor_score = None, 0
            for cand in res:
                nombre = cand.get("displayName", {}).get("text", "")
                sim = difflib.SequenceMatcher(None, norm(titular), norm(nombre)).ratio()
                lat = cand.get("location", {}).get("latitude")
                lon = cand.get("location", {}).get("longitude")
                if lat is None:
                    continue
                d_loc = haversine_km(lat, lon, *centro) if centro else None
                score = sim - (0.3 if d_loc is not None and d_loc > 30 else 0)
                if score > mejor_score:
                    elegido, mejor_score = (cand, nombre, lat, lon, sim, d_loc), score
            if elegido:
                cand, nombre, lat, lon, sim, d_loc = elegido
                fila.update({
                    "nombre_google": nombre, "direccion_google": cand.get("formattedAddress", ""),
                    "lat": lat, "lon": lon, "place_id": cand.get("id", ""),
                    "similitud_nombre": round(sim, 2),
                    "dist_localidad_km": round(d_loc, 1) if d_loc is not None else "",
                    "confianza": ("alta" if sim >= 0.75 and d_loc is not None and d_loc <= 30
                                  else "media" if sim >= 0.45 else "baja"),
                    "google_maps": f"https://www.google.com/maps/place/?q=place_id:{cand.get('id', '')}",
                })
                eng = red.enganchar(lat, lon)
                if eng:
                    fila.update(eng)
            else:
                fila.update({"confianza": "no encontrada"})
            salida.append(fila)

    campos = ["n_planta", "cuit", "titular", "localidad", "provincia", "actividades",
              "confianza", "nombre_google", "direccion_google", "lat", "lon",
              "similitud_nombre", "dist_localidad_km", "ruta", "dist_a_ruta_km",
              "cruce_1_desc", "cruce_1_km", "cruce_2_desc", "cruce_2_km",
              "rr_1_desc", "rr_1_km", "rr_2_desc", "rr_2_km",
              "ruta_lat", "ruta_lon", "ruta_maps",
              "cruce_1", "cruce_2", "rr_1", "rr_2", "nodo_ruta", "place_id", "google_maps"]
    with open(os.path.join(DIR_SALIDA, "plantas_sisa.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(salida)
    gj = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
        "properties": {k: r.get(k, "") for k in campos if k not in ("lat", "lon")},
    } for r in salida if r.get("lat") not in (None, "")]}
    with open(os.path.join(DIR_SALIDA, "plantas_sisa.geojson"), "w", encoding="utf-8") as f:
        json.dump(gj, f, ensure_ascii=False)
    conf = defaultdict(int)
    for r in salida:
        conf[r["confianza"]] += 1
    log(f"Plantas SISA: {dict(conf)}")


if __name__ == "__main__":
    main()
