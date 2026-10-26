"""
Distancia por camino entre puntos cualesquiera (campos, acopios, plantas, puertos).

Arma un grafo con todo lo que ya está calculado en DIR_SALIDA:
  caminos_*.geojson          caminos del partido (tierra + rutas), de caminos_partido.py
  caminos_usuario.geojson    caminos marcados a mano en marcar.html
  caminos_catastro_*.geojson caminos rurales del catastro ARBA que no están en OSM
  red_rutas.json             rutas RN/RP de las 6 provincias, de procesar_osm.py
Los nodos se unen por coordenada (redondeada a 6 decimales, igual en las tres fuentes
porque salen del mismo OSM). Los extremos de los caminos marcados y del catastro se enganchan al nodo
más cercano. Cada punto pedido se engancha al nodo más cercano de la red y se busca el
camino más corto con A* (heurística = distancia en línea recta, admisible).

Se recarga solo si cambia alguno de los archivos.
"""

import glob
import heapq
import json
import math
import os
import re
import threading

DIR_SALIDA = os.environ.get("DIR_SALIDA", "/data/salida")
CELDA = 0.01            # grilla para buscar el nodo más cercano (~1 km)
MAX_ENGANCHE_KM = 30.0  # si el punto queda más lejos de la red, no se rutea
ENGANCHE_MARCADO_KM = 0.5
ENGANCHE_CATASTRO_KM = 0.2  # los caminos del catastro son el hueco entre parcelas (~40 m)
COSER_KM = 3.0         # puntas muertas de rutas a unir con otra componente
NO_TRANSITABLE = {"footway", "path", "cycleway", "steps", "pedestrian", "bridleway",
                  "proposed", "construction", "corridor", "platform", "elevator", "raceway",
                  "bus_stop", "abandoned", "razed"}
TIERRA = {"track", "unclassified"}
SIN_NOMBRE = {"tierra": "camino de tierra s/n", "calle": "calle s/n",
              "pavimento": "camino pavimentado s/n", "marcado": "camino marcado"}
# velocidades para estimar el tiempo de un camión (km/h)
VEL = {"tierra": 35.0, "pavimento": 70.0, "marcado": 30.0, "calle": 30.0}
# Los camiones salen de los campos por los caminos principales: al buscar el camino más corto
# cada km de camino secundario o vecinal "pesa" más (la distancia que se informa es la real).
# Categoría del catastro según el ancho de la franja (caminos_catastro.py); los caminos de
# tierra del OSM no traen categoría.
PESO = {"principal": 1.0, "secundario": 1.5, "vecinal": 3.0, "tierra_osm": 1.3}


def hav_km(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))


def _cargar_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa
        return None


def ref_ruta(ref):
    """'RP46;RN205' -> 'RP 46 / RN 205' (igual que en red_rutas.json); '' si no es RN/RP."""
    out = []
    for r in re.split(r"[;,]", ref or ""):
        m = re.match(r"^\s*(RN|RP)\s*-?\s*(\S+)", r.upper())
        if m:
            out.append(f"{m.group(1)} {m.group(2)}")
    return " / ".join(sorted(out))


def tipo_via(p):
    hw = p.get("highway", "")
    ref = (p.get("ref") or "").upper().replace(" ", "")
    if p.get("ruta") or ref.startswith(("RN", "RP")):
        return "pavimento"
    if hw in TIERRA:
        return "tierra"
    if hw in ("residential", "living_street", "service"):
        return "calle"
    return "pavimento"


class Red:
    def __init__(self):
        self.lonlat = []     # i -> (lon, lat)
        self.idx = {}        # (lon6, lat6) -> i
        self.ady = []        # i -> [(j, km, tipo, nombre, peso)]
        self.grilla = {}     # (cx, cy) -> [i]
        self.fuentes = {}

    def nodo(self, lon, lat):
        k = (round(lon, 6), round(lat, 6))
        i = self.idx.get(k)
        if i is None:
            i = len(self.lonlat)
            self.idx[k] = i
            self.lonlat.append(k)
            self.ady.append([])
        return i

    def arista(self, a, b, tipo, nombre, km=None, peso=1.0):
        if a == b:
            return
        if km is None:
            (lo1, la1), (lo2, la2) = self.lonlat[a], self.lonlat[b]
            km = hav_km(lo1, la1, lo2, la2)
        self.ady[a].append((b, km, tipo, nombre, peso))
        self.ady[b].append((a, km, tipo, nombre, peso))

    def linea(self, coords, tipo, nombre, peso=1.0):
        prev = None
        for lon, lat in coords:
            i = self.nodo(lon, lat)
            if prev is not None:
                self.arista(prev, i, tipo, nombre, peso=peso)
            prev = i

    def componentes(self):
        """comp[i] = id de la componente conexa de i; y el id de la más grande."""
        comp = [-1] * len(self.lonlat)
        mejor, mejor_n = -1, 0
        for i0 in range(len(self.lonlat)):
            if comp[i0] != -1 or not self.ady[i0]:
                continue
            comp[i0] = i0
            pila, n = [i0], 0
            while pila:
                u = pila.pop()
                n += 1
                for v, *_ in self.ady[u]:
                    if comp[v] == -1:
                        comp[v] = i0
                        pila.append(v)
            if n > mejor_n:
                mejor, mejor_n = i0, n
        return comp, mejor

    def coser(self, max_km=COSER_KM):
        """En el OSM las rutas RN/RP a veces quedan cortadas (cruzan un pueblo por calles
        sin ref, rotondas, puentes): cada punta muerta se une con el nodo más cercano de
        OTRA componente, si está a menos de max_km. Devuelve cuántas uniones hizo."""
        comp, _ = self.componentes()
        self.grilla = {}
        for i, (lon, lat) in enumerate(self.lonlat):
            if self.ady[i]:
                self.grilla.setdefault((int(math.floor(lon / CELDA)), int(math.floor(lat / CELDA))), []).append(i)
        n = 0
        for i in range(len(self.lonlat)):
            if len(self.ady[i]) != 1:
                continue
            lon, lat = self.lonlat[i]
            j, km = self.cercano(lon, lat, max_km, otra_comp=(comp, comp[i]))
            if j is not None:
                self.arista(i, j, "empalme", "", km)
                n += 1
        return n

    def componente_mayor(self):
        """Nodos de la componente conexa más grande (la red principal). Los pedazos sueltos
        (tramos de ruta cortados en el OSM, calles aisladas) no sirven para enganchar puntos:
        un punto enganchado ahí no llega a ningún lado."""
        comp, mejor = self.componentes()
        return [c == mejor for c in comp]

    def indexar(self):
        self.grilla = {}
        principal = self.componente_mayor()
        for i, (lon, lat) in enumerate(self.lonlat):
            if principal[i]:
                self.grilla.setdefault((int(math.floor(lon / CELDA)), int(math.floor(lat / CELDA))), []).append(i)

    def cercano(self, lon, lat, max_km=MAX_ENGANCHE_KM, otra_comp=None):
        """Nodo más cercano (i, km) buscando en anillos crecientes de la grilla."""
        cx, cy = int(math.floor(lon / CELDA)), int(math.floor(lat / CELDA))
        km_celda = CELDA * 111.0 * max(0.3, math.cos(math.radians(lat)))
        max_r = int(max_km / km_celda) + 2
        mejor, mejor_km = None, float("inf")
        for r in range(max_r + 1):
            if mejor is not None and (r - 1) * km_celda > mejor_km:
                break
            for x in range(cx - r, cx + r + 1):
                for y in range(cy - r, cy + r + 1):
                    if max(abs(x - cx), abs(y - cy)) != r:
                        continue
                    for i in self.grilla.get((x, y), ()):
                        if otra_comp is not None and otra_comp[0][i] == otra_comp[1]:
                            continue
                        lo, la = self.lonlat[i]
                        d = hav_km(lon, lat, lo, la)
                        if d < mejor_km:
                            mejor, mejor_km = i, d
        if mejor is None or mejor_km > max_km:
            return None, None
        return mejor, mejor_km

    def astar(self, s, t):
        lo_t, la_t = self.lonlat[t]
        h = lambda i: hav_km(self.lonlat[i][0], self.lonlat[i][1], lo_t, la_t) * 0.999  # noqa
        dist = {s: 0.0}
        prev = {}
        heap = [(h(s), 0.0, s)]
        cerrados = set()
        while heap:
            _, d, u = heapq.heappop(heap)
            if u in cerrados:
                continue
            if u == t:
                break
            cerrados.add(u)
            for v, km, tipo, nombre, peso in self.ady[u]:
                nd = d + km * peso
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = (u, km, tipo, nombre)
                    heapq.heappush(heap, (nd + h(v), nd, v))
        if t not in dist:
            return None
        tramos = []   # [(u, v, km, tipo, nombre)] de s a t
        x = t
        while x != s:
            u, km, tipo, nombre = prev[x]
            tramos.append((u, x, km, tipo, nombre))
            x = u
        tramos.reverse()
        return sum(tr[2] for tr in tramos), tramos


def _archivos():
    fs = sorted(set(glob.glob(os.path.join(DIR_SALIDA, "caminos_*.geojson")))
                - set(glob.glob(os.path.join(DIR_SALIDA, "caminos_*_limite.geojson"))))
    return fs + [os.path.join(DIR_SALIDA, "red_rutas.json")]


def _firma():
    out = []
    for p in _archivos():
        try:
            st = os.stat(p)
            out.append((p, st.st_mtime, st.st_size))
        except OSError:
            pass
    return tuple(out)


def construir():
    red = Red()
    sueltos = []   # (coords, tipo, nombre, km de enganche, peso): no comparten nodos con el OSM
    for p in _archivos():
        base = os.path.basename(p)
        if base == "red_rutas.json":
            continue
        gj = _cargar_json(p)
        if not gj:
            continue
        n = 0
        for f in gj.get("features", []):
            g = f.get("geometry") or {}
            pr = f.get("properties") or {}
            if g.get("type") != "LineString":
                continue
            if base == "caminos_usuario.geojson":
                sueltos.append((g["coordinates"], "marcado", pr.get("nombre") or "Camino marcado",
                                ENGANCHE_MARCADO_KM, 1.0))
                n += 1
                continue
            if "highway" not in pr:   # catastro ARBA (caminos_catastro.py): solo los que no están en OSM
                if pr.get("en_osm") in ("no", "sin_dato"):   # sin_dato: partido vecino sin OSM bajado
                    cu = f" (cuartel {pr['cuartel']})" if pr.get("cuartel") else ""
                    cat = pr.get("categoria") or "secundario"
                    sueltos.append((g["coordinates"], "tierra", f"camino {cat} de catastro" + cu,
                                    ENGANCHE_CATASTRO_KM, PESO.get(cat, 1.0)))
                    n += 1
                continue
            if pr.get("highway") in NO_TRANSITABLE:
                continue
            nombre = ref_ruta(pr.get("ref")) or pr.get("nombre") or pr.get("ref") or ""
            tv = tipo_via(pr)
            red.linea(g["coordinates"], tv, nombre, PESO["tierra_osm"] if tv == "tierra" else 1.0)
            n += 1
        red.fuentes[base] = n
    rr = _cargar_json(os.path.join(DIR_SALIDA, "red_rutas.json"))
    if rr:
        nodos = rr.get("nodos", {})
        n = 0
        for a, b, km, ruta in rr.get("aristas", []):
            pa, pb = nodos.get(str(a)), nodos.get(str(b))
            if not pa or not pb:
                continue
            ia, ib = red.nodo(pa[1], pa[0]), red.nodo(pb[1], pb[0])   # red_rutas: [lat, lon]
            red.arista(ia, ib, "pavimento", ruta.replace(";", " / "), km)
            n += 1
        red.fuentes["red_rutas.json"] = n
    red.fuentes["empalmes_cosidos"] = red.coser()
    red.indexar()
    # caminos del catastro y marcados a mano: se agregan y sus extremos se enganchan a la red
    for coords, tipo, nombre, max_km, peso in sueltos:
        if len(coords) < 2:
            continue
        nuevos_antes = len(red.lonlat)
        extremos = []
        for c in (coords[0], coords[-1]):
            extremos.append(red.cercano(c[0], c[1], max_km)[0])
        red.linea(coords, tipo, nombre, peso)
        propios = set(range(nuevos_antes, len(red.lonlat)))
        for j in propios:   # que el próximo camino suelto pueda engancharse a este
            lo, la = red.lonlat[j]
            red.grilla.setdefault((int(math.floor(lo / CELDA)), int(math.floor(la / CELDA))), []).append(j)
        for c, i in zip((coords[0], coords[-1]), extremos):
            if i is not None and i not in propios:
                red.arista(red.idx[(round(c[0], 6), round(c[1], 6))], i, tipo, nombre, peso=peso)
    red.indexar()
    return red


_RED, _FIRMA = None, None
_LOCK = threading.Lock()


def red_actual():
    global _RED, _FIRMA
    f = _firma()
    with _LOCK:
        if _RED is None or f != _FIRMA:
            _RED, _FIRMA = construir(), f
        return _RED


def _simplificar(coords, tol=0.00005):
    try:
        from shapely.geometry import LineString
        if len(coords) > 50:
            return [list(c) for c in LineString(coords).simplify(tol).coords]
    except Exception:  # noqa
        pass
    return coords


def ruta(puntos):
    """puntos: [[lat, lon], ...] (2 o más). Devuelve dict con km y geometría."""
    red = red_actual()
    if not red.grilla:
        return {"ok": False, "error": "todavía no hay red de caminos calculada en el servidor"}
    enganches = []
    for lat, lon in puntos:
        i, km = red.cercano(lon, lat)
        if i is None:
            return {"ok": False, "error": f"el punto {lat:.5f}, {lon:.5f} queda a más de "
                                          f"{MAX_ENGANCHE_KM:.0f} km de cualquier camino cargado"}
        enganches.append((i, km))
    piernas = []
    tot = {"km_total": 0.0, "km_acceso": 0.0, "horas": 0.0}
    por_tipo = {}
    for (lat1, lon1), (lat2, lon2), (s, ks), (t, kt) in zip(puntos, puntos[1:], enganches, enganches[1:]):
        r = red.astar(s, t)
        if r is None:
            return {"ok": False, "error": "no hay conexión por los caminos cargados entre esos puntos"}
        km_red, tramos = r
        coords = [[lon1, lat1], list(red.lonlat[s])]
        tipos = {}
        calles = []   # nombres de vías recorridas, en orden, sin repetir seguidas
        for u, v, km, tipo, nombre in tramos:
            coords.append(list(red.lonlat[v]))
            tipos[tipo] = tipos.get(tipo, 0.0) + km
            nombre = nombre or SIN_NOMBRE.get(tipo, tipo)
            if calles and calles[-1][0] == nombre:
                calles[-1][1] += km
            else:
                calles.append([nombre, km])
        coords.append([lon2, lat2])
        acceso = ks + kt
        tipos["acceso"] = acceso
        horas = sum(km / VEL.get(tp, 30.0) for tp, km in tipos.items())
        # sacar tramitos sueltos (< 200 m) para que la lista de vías sea legible
        resumen = []
        for n, k in calles:
            if k < 0.2:
                continue
            if resumen and resumen[-1]["via"] == n:
                resumen[-1]["km"] += k
            else:
                resumen.append({"via": n, "km": k})
        for v in resumen:
            v["km"] = round(v["km"], 2)
        piernas.append({
            "km": round(km_red + acceso, 2), "km_red": round(km_red, 2),
            "km_acceso_origen": round(ks, 3), "km_acceso_destino": round(kt, 3),
            "km_recta": round(hav_km(lon1, lat1, lon2, lat2), 2),
            "por_tipo": {k: round(v, 2) for k, v in tipos.items() if v > 0.005},
            "horas": round(horas, 2), "vias": resumen,
            "coords": [[round(c[1], 6), round(c[0], 6)] for c in _simplificar(coords)],
        })
        tot["km_total"] += km_red + acceso
        tot["km_acceso"] += acceso
        tot["horas"] += horas
        for k, v in tipos.items():
            por_tipo[k] = por_tipo.get(k, 0.0) + v
    return {"ok": True, "fuente": "red propia (OSM + catastro + caminos marcados)",
            "km_total": round(tot["km_total"], 2), "km_acceso": round(tot["km_acceso"], 3),
            "horas": round(tot["horas"], 2),
            "por_tipo": {k: round(v, 2) for k, v in por_tipo.items() if v > 0.005},
            "piernas": piernas}


# ------------------------------------------------------------- lugares para elegir
def _centroide(g):
    t = g.get("type")
    c = g.get("coordinates")
    if t == "Point":
        return c[0], c[1]
    if t == "Polygon":
        ring = c[0]
    elif t == "MultiPolygon":
        ring = c[0][0]
    elif t == "LineString":
        ring = c
    else:
        return None
    if not ring:
        return None
    return sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)


def _nombre(pr):
    for k in ("nombre", "name", "terminal", "titular", "propietario", "razon_social"):
        if pr.get(k):
            return str(pr[k])
    return ""


def lugares():
    """Lista unificada de puntos con nombre para elegir origen/destino."""
    fuentes = [
        ("campos.geojson", "campo"),
        ("campos_25_de_mayo.geojson", "campo"),   # campos del plano ubicados (resumen_campos.py)
        ("parcelas.geojson", "parcela"),
        ("plantas_sisa.geojson", "planta"),
        ("silos_puertos.geojson", None),
        ("silos_google.geojson", "acopio"),
    ]
    fuentes += [(os.path.basename(p), "localidad")
                for p in glob.glob(os.path.join(DIR_SALIDA, "localidades_*.geojson"))]
    vistos = {f for f, _ in fuentes}
    # capas que agreguen otros procesos (puertos_*.geojson, plantas_*.geojson, acopios_*.geojson)
    for pat, tipo in (("puertos*.geojson", "puerto"), ("plantas*.geojson", "planta"),
                      ("acopios*.geojson", "acopio")):
        for p in sorted(glob.glob(os.path.join(DIR_SALIDA, pat))):
            if os.path.basename(p) not in vistos:
                fuentes.append((os.path.basename(p), tipo))
                vistos.add(os.path.basename(p))
    out = []
    for archivo, tipo in fuentes:
        gj = _cargar_json(os.path.join(DIR_SALIDA, archivo))
        if not gj:
            continue
        for f in gj.get("features", []):
            c = _centroide(f.get("geometry") or {})
            if not c:
                continue
            pr = f.get("properties") or {}
            tp = tipo
            if tp is None:
                tp = "puerto" if pr.get("categoria") == "puerto" else "acopio"
            extra = pr.get("localidad") or pr.get("localidad_cercana") or pr.get("cuartel") or ""
            nombre = _nombre(pr)
            if not nombre:
                if tp == "acopio" and archivo == "silos_puertos.geojson":
                    continue   # miles de silos sueltos del OSM sin nombre: solo ensucian la lista
                nombre = f"{tp.capitalize()} {extra}".strip()
            if tp == "parcela" and pr.get("hectareas"):
                extra = f"{pr['hectareas']} ha" + (f" · cuartel {pr['cuartel']}" if pr.get("cuartel") else "")
            out.append({"tipo": tp, "nombre": nombre, "extra": str(extra),
                        "lat": round(c[1], 6), "lon": round(c[0], 6), "archivo": archivo})
    return out


def estado():
    red = red_actual()
    return {"nodos": len(red.lonlat), "fuentes": red.fuentes}


if __name__ == "__main__":
    import sys
    print(json.dumps(estado(), ensure_ascii=False))
    if len(sys.argv) >= 5:
        a = [float(x) for x in sys.argv[1:]]
        r = ruta([[a[i], a[i + 1]] for i in range(0, len(a) - 1, 2)])
        for p in r.get("piernas", []):
            p["coords"] = f"{len(p['coords'])} puntos"
        print(json.dumps(r, ensure_ascii=False, indent=1))


# ------------------------------------------------------------- zonas de competencia
# Para comparar destinos (puertos, plantas, acopios) se corre un Dijkstra desde cada destino
# a toda la red: así se sabe a cuántos km por camino queda cualquier punto de cada destino.
# Se guarda en memoria por destino (no depende de precios ni fletes, eso lo hace la página).
CELDA_ZONA = 0.05      # grilla del mapa de zonas (~5 km)
_DIJ = {}              # (firma, lon6, lat6) -> (km[], km_tierra[], enganche_km)
_DIJ_LOCK = threading.Lock()


def _dijkstra(red, s):
    from array import array
    inf = float("inf")
    km = array("d", [inf]) * len(red.lonlat)
    tierra = array("d", [0.0]) * len(red.lonlat)
    costo = array("d", [inf]) * len(red.lonlat)   # km ponderados por categoría de camino
    km[s] = costo[s] = 0.0
    heap = [(0.0, s)]
    ady = red.ady
    while heap:
        d, u = heapq.heappop(heap)
        if d > costo[u]:
            continue
        ku, tu = km[u], tierra[u]
        for v, k, tipo, _, peso in ady[u]:
            nd = d + k * peso
            if nd < costo[v]:
                costo[v] = nd
                km[v] = ku + k
                tierra[v] = tu + (k if tipo in ("tierra", "marcado") else 0.0)
                heapq.heappush(heap, (nd, v))
    return km, tierra


def _desde(red, lat, lon):
    """Distancias por camino desde un destino a todos los nodos (con caché)."""
    clave = (_FIRMA, round(lon, 6), round(lat, 6))
    with _DIJ_LOCK:
        if clave in _DIJ:
            return _DIJ[clave]
    s, eng = red.cercano(lon, lat)
    if s is None:
        return None
    km, tierra = _dijkstra(red, s)
    with _DIJ_LOCK:
        for k in [k for k in _DIJ if k[0] != _FIRMA]:   # la red cambió: tirar lo viejo
            del _DIJ[k]
        _DIJ[clave] = (km, tierra, eng)
    return _DIJ[clave]


def _puntos(archivos):
    out = []
    for archivo in archivos:
        gj = _cargar_json(os.path.join(DIR_SALIDA, archivo))
        for f in (gj or {}).get("features", []):
            c = _centroide(f.get("geometry") or {})
            if c:
                pr = f.get("properties") or {}
                out.append({"nombre": _nombre(pr) or "Campo", "lat": round(c[1], 6), "lon": round(c[0], 6),
                            "ha": pr.get("hectareas") or pr.get("ha")})
    return out


def zonas(destinos, celda=CELDA_ZONA):
    """destinos: [{"lat", "lon"}, ...]. Devuelve, para cada celda de la grilla con caminos y para
    cada campo cargado, los km por camino (y cuántos de tierra) a cada destino. Los precios y el
    flete los aplica la página, así se pueden cambiar sin recalcular."""
    red = red_actual()
    if not red.grilla:
        return {"ok": False, "error": "todavía no hay red de caminos calculada en el servidor"}
    tablas = []
    for d in destinos:
        t = _desde(red, d["lat"], d["lon"])
        if t is None:
            return {"ok": False, "error": f"el destino {d.get('nombre') or ''} queda a más de "
                                          f"{MAX_ENGANCHE_KM:.0f} km de cualquier camino cargado"}
        tablas.append(t)
    n = len(tablas)
    # celdas: para cada celda, el nodo de la red principal más cercano al centro de la celda
    mejor = {}   # (cx, cy) -> (dist2, i)
    for (gx, gy), nodos in red.grilla.items():
        cx, cy = int(math.floor(gx * CELDA / celda)), int(math.floor(gy * CELDA / celda))
        mx, my = (cx + 0.5) * celda, (cy + 0.5) * celda
        for i in nodos:
            lo, la = red.lonlat[i]
            d2 = (lo - mx) ** 2 + (la - my) ** 2
            if (cx, cy) not in mejor or d2 < mejor[(cx, cy)][0]:
                mejor[(cx, cy)] = (d2, i)
    # solo la zona con caminos rurales cargados (la red de rutas llega a 6 provincias)
    pts = _puntos_principales()
    if pts:
        lat0, lat1 = min(p["lat"] for p in pts) - 0.3, max(p["lat"] for p in pts) + 0.3
        lon0, lon1 = min(p["lon"] for p in pts) - 0.3, max(p["lon"] for p in pts) + 0.3
    else:
        lat0, lat1, lon0, lon1 = -90, 90, -180, 180
    celdas = []
    for (cx, cy), (_, i) in mejor.items():
        if not (lat0 <= (cy + 0.5) * celda <= lat1 and lon0 <= (cx + 0.5) * celda <= lon1):
            continue
        kms = [t[0][i] + t[2] for t in tablas]
        if any(k == float("inf") for k in kms):
            continue
        celdas.append([round((cy + 0.5) * celda, 4), round((cx + 0.5) * celda, 4),
                       [round(k, 1) for k in kms], [round(t[1][i], 1) for t in tablas]])
    def medir(puntos):
        out = []
        for c in puntos:
            i, eng = red.cercano(c["lon"], c["lat"])
            if i is None:
                continue
            kms = [t[0][i] + t[2] + eng for t in tablas]
            if any(k == float("inf") for k in kms):
                continue
            c["km"] = [round(k, 1) for k in kms]
            c["km_tierra"] = [round(t[1][i] + eng, 1) for t in tablas]
            out.append(c)
        return out
    campos = medir(_puntos(["campos.geojson", "campos_25_de_mayo.geojson"]))
    puntos = medir([dict(p) for p in _puntos_principales()])
    return {"ok": True, "celda": celda, "n": n, "enganche_km": [round(t[2], 2) for t in tablas],
            "celdas": celdas, "campos": campos, "puntos": puntos}


PASO_PUNTOS_KM = 10.0
_PUNTOS = (None, [])


def _puntos_principales():
    """Un punto cada 10 km sobre los caminos de tierra principales del catastro (franja de 30 m
    o más): son las salidas de los campos que JULIO quiere comparar."""
    global _PUNTOS
    if _PUNTOS[0] == _FIRMA:
        return _PUNTOS[1]
    out = []
    for p in sorted(glob.glob(os.path.join(DIR_SALIDA, "caminos_catastro_*.geojson"))):
        gj = _cargar_json(p)
        resto = PASO_PUNTOS_KM / 2   # km que faltan para el próximo punto (se arrastra entre tramos)
        for f in (gj or {}).get("features", []):
            pr = f.get("properties") or {}
            g = f.get("geometry") or {}
            if pr.get("categoria") != "principal" or g.get("type") != "LineString":
                continue
            c = g["coordinates"]
            for (lo1, la1), (lo2, la2) in zip(c, c[1:]):
                seg = hav_km(lo1, la1, lo2, la2)
                pos = 0.0
                while seg - pos >= resto:
                    pos += resto
                    t = pos / seg
                    out.append({"nombre": f"{pr.get('partido', '')}" + (f" (cuartel {pr['cuartel']})" if pr.get("cuartel") else ""),
                                "lat": round(la1 + (la2 - la1) * t, 5), "lon": round(lo1 + (lo2 - lo1) * t, 5)})
                    resto = PASO_PUNTOS_KM
                resto -= seg - pos
    _PUNTOS = (_FIRMA, out)
    return out
