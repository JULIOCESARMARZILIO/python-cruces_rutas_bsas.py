"""
Secuencia ordenada de cruces a lo largo de cada ruta, con km acumulados,
calculada sobre la red ya guardada (red_rutas.json + cruces_*.csv):
  DIR_SALIDA/rutas/<prov>/<ruta>.csv  e índice rutas/index.html
No necesita reprocesar el mapa.

Método: para cada ruta se toma su subgrafo (aristas con ese número), se
condensa a "nodos clave" (cruces con otras rutas/accesos + empalmes donde la
propia ruta se bifurca o termina), y se busca el recorrido más largo de punta
a punta (doble barrido de Dijkstra). Ese es el segmento 1 (troncal); lo que
queda afuera (ramales, calzadas paralelas) va en segmentos siguientes.
"""

import csv
import glob
import heapq
import html
import json
import os
from collections import defaultdict

DIR_SALIDA = os.environ.get("DIR_SALIDA", "/data/salida")
DIR_RUTAS = os.path.join(DIR_SALIDA, "rutas")


def log(m):
    print(m, flush=True)


def dijkstra(ady, origen):
    dist, prev = {origen: 0.0}, {}
    cola = [(0.0, origen)]
    while cola:
        d, n = heapq.heappop(cola)
        if d > dist.get(n, 1e18):
            continue
        for m, km in ady[n]:
            nd = d + km
            if nd < dist.get(m, 1e18):
                dist[m], prev[m] = nd, n
                heapq.heappush(cola, (nd, m))
    return dist, prev


def camino(prev, destino):
    c = [destino]
    while c[-1] in prev:
        c.append(prev[c[-1]])
    return c[::-1]


def condensar(ady_ruta, claves):
    """Grafo nodo-clave a nodo-clave caminando por nodos de paso (grado 2)."""
    cond = defaultdict(list)
    for c in claves:
        for vecino, d0 in ady_ruta[c]:
            prev, actual, acum = c, vecino, d0
            while actual not in claves:
                sig = [(n, d) for n, d in ady_ruta[actual] if n != prev]
                if len(sig) != 1:
                    break
                prev, (actual, d) = actual, sig[0]
                acum += d
            if actual in claves and actual != c:
                cond[c].append((actual, acum))
    return cond


def main():
    red = json.load(open(os.path.join(DIR_SALIDA, "red_rutas.json"), encoding="utf-8"))
    nodos = {int(k): v for k, v in red["nodos"].items()}
    cruces = set(red["cruces"])
    por_ruta = defaultdict(lambda: defaultdict(list))
    for a, b, km, etiqueta in red["aristas"]:
        for ruta in etiqueta.split(";"):   # tramos compartidos pertenecen a ambas rutas
            por_ruta[ruta][a].append((b, km))
            por_ruta[ruta][b].append((a, km))

    desc, prov_de = {}, {}
    for ruta_csv in glob.glob(os.path.join(DIR_SALIDA, "cruces_*.csv")):
        prov = os.path.basename(ruta_csv)[len("cruces_"):-4]
        with open(ruta_csv, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                n = int(r["nodo_osm"])
                desc[n] = f"{r['ruta']} x {r['cruza_con']}"
                prov_de[n] = prov

    indice = []
    for ruta, ady in sorted(por_ruta.items()):
        claves = {n for n in ady if n in cruces or len(ady[n]) != 2}
        if not claves:
            continue
        cond = condensar(ady, claves)
        # provincia(s) de la ruta según sus cruces
        provs = defaultdict(int)
        for n in claves:
            if n in prov_de:
                provs[prov_de[n]] += 1
        if not provs:
            continue
        filas, restantes, seg = [], set(cond), 0
        while restantes:
            seg += 1
            ini = next(iter(restantes))
            sub = {n: [(m, km) for m, km in cond[n] if m in restantes] for n in restantes}
            d1, _ = dijkstra(sub, ini)
            lejos = max(d1, key=d1.get)
            d2, prev = dijkstra(sub, lejos)
            fin = max(d2, key=d2.get)
            ruta_nodos = camino(prev, fin)
            # orientar de sur a norte / oeste a este (lat menor primero)
            if nodos[ruta_nodos[0]][0] > nodos[ruta_nodos[-1]][0]:
                ruta_nodos = ruta_nodos[::-1]
            acum, prev_km, orden = 0.0, 0.0, 0
            for i, n in enumerate(ruta_nodos):
                if i:
                    acum += next(km for m, km in sub[ruta_nodos[i - 1]] if m == n)
                es_cruce = n in cruces
                if seg > 1 and len(ruta_nodos) == 1:
                    break
                orden += 1
                la, lo = nodos[n]
                filas.append([seg, orden, round(acum, 2), round(acum - prev_km, 2),
                              "cruce" if es_cruce else "empalme",
                              desc.get(n, f"empalme/bifurcación de {ruta}"),
                              la, lo, n, f"https://www.google.com/maps?q={la},{lo}"])
                prev_km = acum
            restantes -= set(ruta_nodos)
            if seg >= 40:
                break
        for prov in provs:
            os.makedirs(os.path.join(DIR_RUTAS, prov), exist_ok=True)
            nombre = ruta.replace(" ", "_") + ".csv"
            with open(os.path.join(DIR_RUTAS, prov, nombre), "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["segmento", "orden", "km_acum", "km_desde_anterior", "tipo",
                            "cruce", "lat", "lon", "nodo_osm", "google_maps"])
                w.writerows(filas)
            troncal = [r for r in filas if r[0] == 1]
            indice.append((prov, ruta, nombre, len(troncal),
                           troncal[-1][2] if troncal else 0, seg))

    indice.sort()
    with open(os.path.join(DIR_RUTAS, "index.html"), "w", encoding="utf-8") as f:
        f.write('<!doctype html><meta charset="utf-8"><title>Rutas</title>'
                '<body style="font-family:sans-serif;padding:2em">'
                '<h2>Secuencia de cruces por ruta</h2>'
                '<p>Segmento 1 = recorrido troncal; el resto son ramales o tramos sueltos. '
                'Filas "empalme" son bifurcaciones de la propia ruta (calzada doble, rotondas).</p>')
        prov_actual = None
        for prov, ruta, nombre, n, km, segs in indice:
            if prov != prov_actual:
                if prov_actual:
                    f.write("</ul>")
                f.write(f"<h3>{html.escape(prov)}</h3><ul>")
                prov_actual = prov
            f.write(f'<li><a href="{prov}/{nombre}">{html.escape(ruta)}</a> '
                    f'(troncal: {n} nodos, {km} km; {segs} segmentos)</li>')
        f.write("</ul></body>")
    log(f"Secuencias por ruta: {len(indice)} archivos")


if __name__ == "__main__":
    main()
