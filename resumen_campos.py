"""
Campos del partido de 25 de Mayo: une la tabla transcripta del plano catastral
(datos/parcelas_plano.csv) con las parcelas de ARBA (datos/parcelas_arba_25_de_mayo.geojson,
ver caminos_catastro.py), ubica cada campo y calcula hectáreas por propietario y promedios.

Columnas de datos/parcelas_plano.csv:
  id, zona, cuartel, propietario, tipo, hectareas, confianza, nota, lat, lon, partidas
  - tipo: uso del campo (agricola / ganadero / mixto / tambo / ...), libre
  - partidas: partidas ARBA del campo separadas por espacio (si se conocen)
  - lat/lon: opcional, un punto dentro del campo

Cómo se ubica un campo (en este orden):
  1. asignaciones hechas desde el mapa (DIR_SALIDA/asignaciones_campos.json)
  2. la columna partidas del CSV
  3. automático: única parcela ARBA del mismo cuartel con la misma superficie (±2 %)
     que ningún otro campo del cuartel reclama
  4. lat/lon del CSV

Salida (en DIR_SALIDA):
  campos_25_de_mayo.csv             la tabla + tipo de titular + partidas + cómo se ubicó
  campos_25_de_mayo.geojson         campos ubicados (polígono de sus parcelas o punto)
  propietarios_25_de_mayo.csv       un renglón por propietario: campos, ha totales, ha promedio
  campos_25_de_mayo_resumen.json    totales, promedios, por cuartel, por tipo y ranking
  campos_25_de_mayo_lista.json      lista corta de campos para asignarlos desde el mapa
"""

import csv
import json
import os
import re
import statistics
import unicodedata
from collections import Counter, defaultdict

AQUI = os.path.dirname(os.path.abspath(__file__))
DIR_DATOS = os.environ.get("DIR_DATOS", "/data")
DIR_SALIDA = os.environ.get("DIR_SALIDA", os.path.join(DIR_DATOS, "salida"))
TABLA = os.path.join(AQUI, "datos", "parcelas_plano.csv")
ARBA = os.path.join(AQUI, "datos", "parcelas_arba_25_de_mayo.geojson")
ASIGNACIONES = os.path.join(DIR_SALIDA, "asignaciones_campos.json")
PARCELAS = os.path.join(DIR_SALIDA, "parcelas.geojson")
SLUG = "25_de_mayo"
TOLERANCIA = 0.02
# I = ciudad, II = quintas y chacras del ejido: el plano rural no trae sus dueños
CUARTELES_FUERA = {"I", "II"}

RE_SOCIEDAD = re.compile(r"\b(s\.?\s?a\.?|s\.?r\.?l\.?|s\.?c\.?a\.?|s\.?a\.?s\.?|s\.?c\.?|"
                         r"sacaif|saic\w*|saca\w*|sacif\w*|cia|hnos|sociedad|y cia)\b|\bs\.a\.", re.I)
RE_INSTITUCION = re.compile(r"universidad|escuela|municipalidad|provincia|estado|iglesia|"
                            r"obispado|fundaci|cooperativa|club|inta|institucion", re.I)


def clave(nombre):
    s = unicodedata.normalize("NFKD", nombre or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", "", s.replace(".", ""))
    return " ".join(s.split())


def tipo_titular(nombre):
    if RE_INSTITUCION.search(nombre):
        return "institucion"
    if RE_SOCIEDAD.search(nombre):
        return "sociedad"
    return "persona"


def num(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def r1(x):
    return None if x is None else round(x, 1)


def leer_json(path, defecto):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa
            pass
    return defecto


def leer_tabla():
    with open(TABLA, encoding="utf-8") as f:
        filas = list(csv.DictReader(f))
    for r in filas:
        r["ha"] = num(r.get("hectareas"))
        r["tipo"] = (r.get("tipo") or "").strip().lower()
        r["tipo_titular"] = tipo_titular(r["propietario"])
        r["partidas"] = (r.get("partidas") or "").split()
        r["ubicado"] = "plano" if r["partidas"] else ""
    return filas


def ubicar(campos, arba):
    """Asignaciones del mapa y match automático por superficie dentro del cuartel."""
    asig = leer_json(ASIGNACIONES, {})
    for c in campos:
        if str(c["id"]) in asig:
            c["partidas"] = list(asig[str(c["id"])])
            c["ubicado"] = "mapa"
    tomadas = {p for c in campos for p in c["partidas"]}
    por_cuartel = defaultdict(list)
    for pda, p in arba.items():
        if pda not in tomadas:
            por_cuartel[p["cuartel"]].append(pda)
    pendientes = [c for c in campos if not c["partidas"] and c["ha"]]
    cand = {}
    for c in pendientes:
        tol = max(TOLERANCIA * c["ha"], 1.5)
        cand[c["id"]] = [pda for pda in por_cuartel[c["cuartel"]]
                         if abs(arba[pda]["ha"] - c["ha"]) <= tol]
    reclamos = Counter(p for v in cand.values() for p in v)
    for c in pendientes:
        v = cand[c["id"]]
        if len(v) == 1 and reclamos[v[0]] == 1:
            c["partidas"] = v
            c["ubicado"] = "automatico"


def stats(valores):
    v = [x for x in valores if x]
    if not v:
        return {"cantidad": 0, "ha_total": 0, "promedio": None, "mediana": None}
    return {"cantidad": len(v), "ha_total": r1(sum(v)), "promedio": r1(sum(v) / len(v)),
            "mediana": r1(statistics.median(v)), "minimo": r1(min(v)), "maximo": r1(max(v))}


def main():
    os.makedirs(DIR_SALIDA, exist_ok=True)
    campos = leer_tabla()
    arba_fc = leer_json(ARBA, {"features": []})
    arba = {f["properties"]["partida"]: dict(f["properties"], geometry=f["geometry"])
            for f in arba_fc["features"] if f["properties"].get("cuartel") not in CUARTELES_FUERA}
    ubicar(campos, arba)

    # parcelas dibujadas a mano en el mapa que no son del plano
    for ft in leer_json(PARCELAS, {"features": []}).get("features", []):
        p = ft.get("properties") or {}
        campos.append({"id": "d%s" % p.get("id", ""), "zona": "", "cuartel": p.get("cuartel") or "",
                       "propietario": p.get("propietario") or "Sin nombre",
                       "tipo": (p.get("tipo") or "").strip().lower(), "ha": num(p.get("hectareas")),
                       "confianza": "", "nota": "dibujada en el mapa", "lat": "", "lon": "",
                       "partidas": [], "ubicado": "dibujado", "geometria": ft.get("geometry"),
                       "tipo_titular": tipo_titular(p.get("propietario") or "")})

    prop = defaultdict(lambda: {"nombre": "", "campos": 0, "con_ha": 0, "ha": 0.0,
                                "cuarteles": set(), "tipos": set(), "titular": ""})
    for c in campos:
        p = prop[clave(c["propietario"])]
        p["nombre"] = p["nombre"] or c["propietario"]
        p["titular"] = c["tipo_titular"]
        p["campos"] += 1
        if c["cuartel"]:
            p["cuarteles"].add(c["cuartel"])
        if c["tipo"]:
            p["tipos"].add(c["tipo"])
        if c["ha"]:
            p["con_ha"] += 1
            p["ha"] += c["ha"]
    props = sorted(prop.values(), key=lambda p: -p["ha"])

    cols = ["id", "zona", "cuartel", "propietario", "tipo_titular", "tipo", "hectareas",
            "confianza", "nota", "partidas", "ha_arba", "ubicado"]
    feats = []
    with open(os.path.join(DIR_SALIDA, f"campos_{SLUG}.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for c in campos:
            c["hectareas"] = "" if c["ha"] is None else ("%g" % c["ha"])
            c["ha_arba"] = r1(sum(arba[p]["ha"] for p in c["partidas"] if p in arba)) if c["partidas"] else ""
            fila = dict(c, partidas=" ".join(c["partidas"]))
            w.writerow([fila.get(k, "") for k in cols])
            props_f = {k: fila.get(k, "") for k in cols}
            geoms = [arba[p]["geometry"] for p in c["partidas"] if p in arba]
            if geoms:
                polys = []
                for g in geoms:
                    polys += g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
                feats.append({"type": "Feature", "properties": props_f,
                              "geometry": {"type": "MultiPolygon", "coordinates": polys}})
            elif c.get("geometria"):
                feats.append({"type": "Feature", "properties": props_f, "geometry": c["geometria"]})
            elif num(c.get("lat")) is not None and num(c.get("lon")) is not None:
                feats.append({"type": "Feature", "properties": props_f,
                              "geometry": {"type": "Point", "coordinates": [num(c["lon"]), num(c["lat"])]}})
    # lista liviana para el selector del mapa: [id, cuartel, propietario, ha, partidas, tipo]
    with open(os.path.join(DIR_SALIDA, f"campos_{SLUG}_lista.json"), "w", encoding="utf-8") as f:
        json.dump([[c["id"], c["cuartel"], c["propietario"], c["ha"], c["partidas"], c["tipo"]]
                   for c in campos if not str(c["id"]).startswith("d")], f,
                  ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(DIR_SALIDA, f"campos_{SLUG}.geojson"), "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f, ensure_ascii=False,
                  separators=(",", ":"))

    with open(os.path.join(DIR_SALIDA, f"propietarios_{SLUG}.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["propietario", "tipo_titular", "campos", "ha_total", "ha_promedio_por_campo",
                    "campos_sin_ha", "cuarteles", "tipos"])
        for p in props:
            w.writerow([p["nombre"], p["titular"], p["campos"], "%g" % round(p["ha"], 1),
                        "%g" % round(p["ha"] / p["con_ha"], 1) if p["con_ha"] else "",
                        p["campos"] - p["con_ha"], " ".join(sorted(p["cuarteles"])),
                        " ".join(sorted(p["tipos"]))])

    def agrupar(campo):
        g = defaultdict(list)
        for c in campos:
            g[c[campo] or "sin dato"].append(c)
        out = {}
        for k, lst in sorted(g.items()):
            s = stats([c["ha"] for c in lst])
            s["campos"] = len(lst)
            s["propietarios"] = len({clave(c["propietario"]) for c in lst})
            out[k] = s
        return out

    ha_prop = [p["ha"] for p in props if p["ha"]]
    total_ha = sum(c["ha"] for c in campos if c["ha"])
    top10 = sum(sorted(ha_prop, reverse=True)[:10])
    resumen = {
        "partido": "25 de Mayo",
        "campos": len(campos),
        "campos_con_ha": sum(1 for c in campos if c["ha"]),
        "campos_sin_ha": sum(1 for c in campos if not c["ha"]),
        "campos_con_tipo": sum(1 for c in campos if c["tipo"]),
        "campos_ubicados": len(feats),
        "ubicados_por": dict(Counter(c["ubicado"] for c in campos if c["ubicado"])),
        "propietarios": len(props),
        "ha_total": r1(total_ha),
        "ha_rural_arba": r1(sum(p["ha"] for p in arba.values())),
        "parcelas_arba": len(arba),
        "por_campo": stats([c["ha"] for c in campos]),
        "por_propietario": stats(ha_prop),
        "campos_por_propietario": r1(len(campos) / len(props)) if props else None,
        "top10_pct_superficie": r1(100 * top10 / total_ha) if total_ha else None,
        "por_cuartel": agrupar("cuartel"),
        "por_tipo": agrupar("tipo"),
        "por_tipo_titular": agrupar("tipo_titular"),
        "ranking": [{"propietario": p["nombre"], "campos": p["campos"], "ha": r1(p["ha"]),
                     "titular": p["titular"]} for p in props],
    }
    with open(os.path.join(DIR_SALIDA, f"campos_{SLUG}_resumen.json"), "w", encoding="utf-8") as f:
        json.dump(resumen, f, ensure_ascii=False, indent=1)
    print("CAMPOS " + json.dumps({k: resumen[k] for k in
          ("campos", "propietarios", "ha_total", "campos_ubicados", "por_campo", "por_propietario")},
          ensure_ascii=False))


if __name__ == "__main__":
    main()
