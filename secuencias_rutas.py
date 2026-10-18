"""
A partir de tramos_<provincia>.csv arma, por ruta, la secuencia ordenada de
cruces a lo largo de la ruta con km acumulados: DIR_SALIDA/rutas/<prov>/<ruta>.csv
y un índice rutas/index.html. No necesita reprocesar el mapa.
"""

import csv
import glob
import html
import os
from collections import defaultdict

DIR_SALIDA = os.environ.get("DIR_SALIDA", "/data/salida")
DIR_RUTAS = os.path.join(DIR_SALIDA, "rutas")


def secuencias(tramos):
    """tramos: lista de (a, b, km). Devuelve lista de cadenas [(nodo, km_acum), ...]."""
    ady = defaultdict(list)
    for a, b, km in tramos:
        ady[a].append((b, km))
        ady[b].append((a, km))
    vistos, cadenas = set(), []
    # arrancar por extremos (grado 1); luego lo que quede (anillos)
    inicios = [n for n in ady if len(ady[n]) == 1] + list(ady)
    for ini in inicios:
        if ini in vistos:
            continue
        cadena, acum, prev, actual = [(ini, 0.0)], 0.0, None, ini
        vistos.add(ini)
        while True:
            sig = [(n, km) for n, km in ady[actual] if n != prev and n not in vistos]
            if not sig:
                break
            n, km = sig[0]
            acum += km
            cadena.append((n, round(acum, 2)))
            vistos.add(n)
            prev, actual = actual, n
        cadenas.append(cadena)
    cadenas.sort(key=len, reverse=True)
    return cadenas


def main():
    os.makedirs(DIR_RUTAS, exist_ok=True)
    indice = []
    for ruta_csv in sorted(glob.glob(os.path.join(DIR_SALIDA, "tramos_*.csv"))):
        prov = os.path.basename(ruta_csv)[len("tramos_"):-4]
        por_ruta, desc, coords = defaultdict(list), {}, {}
        with open(ruta_csv, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                a, b = int(r["cruce_a"]), int(r["cruce_b"])
                por_ruta[r["ruta"]].append((a, b, float(r["km"])))
                desc[a], desc[b] = r["cruce_a_desc"], r["cruce_b_desc"]
                coords[a] = (r["cruce_a_lat"], r["cruce_a_lon"])
                coords[b] = (r["cruce_b_lat"], r["cruce_b_lon"])
        os.makedirs(os.path.join(DIR_RUTAS, prov), exist_ok=True)
        for ruta, tramos in sorted(por_ruta.items()):
            nombre = ruta.replace(" ", "_") + ".csv"
            with open(os.path.join(DIR_RUTAS, prov, nombre), "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["segmento", "orden", "km_acum", "km_desde_anterior", "cruce",
                            "lat", "lon", "nodo_osm", "google_maps"])
                for s, cadena in enumerate(secuencias(tramos), 1):
                    prev_km = 0.0
                    for i, (n, km) in enumerate(cadena, 1):
                        la, lo = coords[n]
                        w.writerow([s, i, km, round(km - prev_km, 2), desc[n], la, lo, n,
                                    f"https://www.google.com/maps?q={la},{lo}"])
                        prev_km = km
            total = sum(km for _, _, km in tramos)
            indice.append((prov, ruta, nombre, len(tramos), round(total, 1)))
    with open(os.path.join(DIR_RUTAS, "index.html"), "w", encoding="utf-8") as f:
        f.write('<!doctype html><meta charset="utf-8"><title>Rutas</title>'
                '<body style="font-family:sans-serif;padding:2em"><h2>Secuencia de cruces por ruta</h2>')
        prov_actual = None
        for prov, ruta, nombre, n, total in indice:
            if prov != prov_actual:
                if prov_actual:
                    f.write("</ul>")
                f.write(f"<h3>{html.escape(prov)}</h3><ul>")
                prov_actual = prov
            f.write(f'<li><a href="{prov}/{nombre}">{html.escape(ruta)}</a> '
                    f'({n} tramos, {total} km)</li>')
        f.write("</ul></body>")
    print(f"Secuencias por ruta: {len(indice)} rutas", flush=True)


if __name__ == "__main__":
    main()
