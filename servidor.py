"""
Servidor HTTP mínimo (stdlib) para publicar DIR_SALIDA y buscar en los CSV:
  /                      índice (index.html)
  /<archivo>             descarga directa
  /buscar?q=texto        filas de todos los CSV que contienen el texto (HTML)
  /buscar?q=texto&f=csv  lo mismo en CSV
  /api/lugares           campos, parcelas, plantas, acopios, puertos y pueblos (JSON)
  /api/ruta  (POST)      {"puntos": [[lat, lon], ...]} -> distancia por camino (ruteo.py)
"""

import csv
import glob
import html
import json
import os
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import ruteo

DIR_SALIDA = os.environ.get("DIR_SALIDA", "/data/salida")
PORT = int(os.environ.get("PORT", "8080"))
MAX_FILAS = 500

CAMPOS = os.path.join(DIR_SALIDA, "campos.geojson")
_LOCK = threading.Lock()


def cargar_campos():
    if os.path.exists(CAMPOS):
        try:
            with open(CAMPOS, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa
            pass
    return {"type": "FeatureCollection", "features": []}


def guardar_campo(nombre, lat, lon, nota=""):
    with _LOCK:
        fc = cargar_campos()
        nuevo_id = max([f.get("properties", {}).get("id", 0) for f in fc["features"]] + [0]) + 1
        feat = {"type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {"id": nuevo_id, "nombre": nombre, "nota": nota,
                               "creado": time.strftime("%Y-%m-%d %H:%M")}}
        fc["features"].append(feat)
        tmp = CAMPOS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(fc, f, ensure_ascii=False)
        os.replace(tmp, CAMPOS)
        return feat


def borrar_campo(cid):
    with _LOCK:
        fc = cargar_campos()
        antes = len(fc["features"])
        fc["features"] = [f for f in fc["features"] if f.get("properties", {}).get("id") != cid]
        tmp = CAMPOS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(fc, f, ensure_ascii=False)
        os.replace(tmp, CAMPOS)
        return antes - len(fc["features"])


CAMINOS_USU = os.path.join(DIR_SALIDA, "caminos_usuario.geojson")


def _cargar(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa
            pass
    return {"type": "FeatureCollection", "features": []}


def _hav_km(lon1, lat1, lon2, lat2):
    import math
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def guardar_camino(nombre, coords):
    largo = 0.0
    for (lo1, la1), (lo2, la2) in zip(coords, coords[1:]):
        largo += _hav_km(lo1, la1, lo2, la2)
    with _LOCK:
        fc = _cargar(CAMINOS_USU)
        nuevo_id = max([f.get("properties", {}).get("id", 0) for f in fc["features"]] + [0]) + 1
        feat = {"type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {"id": nuevo_id, "nombre": nombre, "km": round(largo, 2),
                               "creado": time.strftime("%Y-%m-%d %H:%M")}}
        fc["features"].append(feat)
        tmp = CAMINOS_USU + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(fc, f, ensure_ascii=False)
        os.replace(tmp, CAMINOS_USU)
        return feat


def borrar_camino(cid):
    with _LOCK:
        fc = _cargar(CAMINOS_USU)
        antes = len(fc["features"])
        fc["features"] = [f for f in fc["features"] if f.get("properties", {}).get("id") != cid]
        tmp = CAMINOS_USU + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(fc, f, ensure_ascii=False)
        os.replace(tmp, CAMINOS_USU)
        return antes - len(fc["features"])


PARCELAS = os.path.join(DIR_SALIDA, "parcelas.geojson")


def _guardar_fc(path, fc):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False)
    os.replace(tmp, path)


def guardar_parcela(propietario, hectareas, cuartel, ring):
    if ring[0] != ring[-1]:
        ring = ring + [ring[0]]
    with _LOCK:
        fc = _cargar(PARCELAS)
        nuevo_id = max([f.get("properties", {}).get("id", 0) for f in fc["features"]] + [0]) + 1
        feat = {"type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {"id": nuevo_id, "propietario": propietario,
                               "hectareas": hectareas, "cuartel": cuartel,
                               "creado": time.strftime("%Y-%m-%d %H:%M")}}
        fc["features"].append(feat)
        _guardar_fc(PARCELAS, fc)
        return feat


def borrar_parcela(pid):
    with _LOCK:
        fc = _cargar(PARCELAS)
        antes = len(fc["features"])
        fc["features"] = [f for f in fc["features"] if f.get("properties", {}).get("id") != pid]
        _guardar_fc(PARCELAS, fc)
        return antes - len(fc["features"])


ASIGNACIONES = os.path.join(DIR_SALIDA, "asignaciones_campos.json")


def asignar_campo(campo_id, partidas):
    """Guarda qué parcelas ARBA forman un campo del plano y recalcula el resumen."""
    import subprocess
    import sys
    with _LOCK:
        asig = _cargar(ASIGNACIONES)
        if not isinstance(asig, dict) or "features" in asig:
            asig = {}
        if partidas:
            asig[campo_id] = partidas
        else:
            asig.pop(campo_id, None)
        _guardar_fc(ASIGNACIONES, asig)
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resumen_campos.py")
        subprocess.run([sys.executable, script], timeout=120, check=False)
    return len(asig)


def buscar(q):
    q = q.lower()
    for ruta in sorted(glob.glob(os.path.join(DIR_SALIDA, "*.csv"))
                       + glob.glob(os.path.join(DIR_SALIDA, "rutas", "*", "*.csv"))):
        nombre = os.path.relpath(ruta, DIR_SALIDA)
        with open(ruta, encoding="utf-8") as f:
            lector = csv.reader(f)
            hdr = next(lector, None)
            if not hdr:
                continue
            for fila in lector:
                if q in ",".join(fila).lower():
                    yield nombre, hdr, fila


class Manejador(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=DIR_SALIDA, **k)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/lugares":
            return self._json({"ok": True, "lugares": ruteo.lugares()})
        if u.path == "/api/ruta/estado":
            return self._json({"ok": True, **ruteo.estado()})
        if u.path != "/buscar":
            return super().do_GET()
        qs = parse_qs(u.query)
        q = (qs.get("q") or [""])[0].strip()
        fmt = (qs.get("f") or ["html"])[0]
        res = []
        if len(q) >= 3:
            for r in buscar(q):
                res.append(r)
                if len(res) >= MAX_FILAS:
                    break
        if fmt == "csv":
            cuerpo = "archivo," + (",".join(res[0][1]) if res else "") + "\n"
            for nombre, hdr, fila in res:
                cuerpo += nombre + "," + ",".join('"' + c.replace('"', '""') + '"' for c in fila) + "\n"
            self._responder(cuerpo, "text/csv; charset=utf-8")
            return
        partes = ['<!doctype html><meta charset="utf-8"><title>Buscar</title>'
                  '<body style="font-family:sans-serif;padding:1em">'
                  '<form><input name="q" value="%s" size="30"> <button>Buscar</button></form>'
                  % html.escape(q)]
        if len(q) < 3:
            partes.append("<p>Escribí al menos 3 letras.</p>")
        else:
            partes.append(f"<p>{len(res)} resultados para <b>{html.escape(q)}</b>"
                          + (" (mostrando los primeros %d)" % MAX_FILAS if len(res) >= MAX_FILAS else "")
                          + "</p>")
            actual = None
            for nombre, hdr, fila in res:
                if nombre != actual:
                    if actual:
                        partes.append("</table>")
                    partes.append(f"<h3>{html.escape(nombre)}</h3><table border=1 cellpadding=3>"
                                  "<tr>" + "".join(f"<th>{html.escape(h)}</th>" for h in hdr) + "</tr>")
                    actual = nombre
                celdas = []
                for h, c in zip(hdr, fila):
                    if c.startswith("http"):
                        celdas.append(f'<td><a href="{html.escape(c)}">mapa</a></td>')
                    else:
                        celdas.append(f"<td>{html.escape(c)}</td>")
                partes.append("<tr>" + "".join(celdas) + "</tr>")
            if actual:
                partes.append("</table>")
        partes.append("</body>")
        self._responder("".join(partes), "text/html; charset=utf-8")

    def do_POST(self):
        u = urlparse(self.path)
        try:
            largo = int(self.headers.get("Content-Length", 0))
            cuerpo = json.loads(self.rfile.read(largo) or b"{}")
        except Exception:  # noqa
            cuerpo = {}
        if u.path == "/api/ruta":
            try:
                puntos = [[float(p[0]), float(p[1])] for p in cuerpo["puntos"]]
            except Exception:  # noqa
                return self._json({"ok": False, "error": "puntos inválidos"}, 400)
            if not 2 <= len(puntos) <= 20:
                return self._json({"ok": False, "error": "hacen falta entre 2 y 20 puntos"}, 400)
            try:
                return self._json(ruteo.ruta(puntos))
            except Exception as e:  # noqa
                return self._json({"ok": False, "error": f"error calculando la ruta: {e}"}, 500)
        if u.path == "/api/campo":
            try:
                lat = float(cuerpo["lat"]); lon = float(cuerpo["lon"])
            except Exception:  # noqa
                return self._json({"ok": False, "error": "lat/lon inválidos"}, 400)
            nombre = (str(cuerpo.get("nombre") or "")).strip()[:120] or "Campo sin nombre"
            nota = (str(cuerpo.get("nota") or "")).strip()[:500]
            feat = guardar_campo(nombre, lat, lon, nota)
            return self._json({"ok": True, "feature": feat})
        if u.path == "/api/campo/borrar":
            n = borrar_campo(cuerpo.get("id"))
            return self._json({"ok": True, "borrados": n})
        if u.path == "/api/camino":
            coords = cuerpo.get("coords")
            try:
                coords = [[float(c[0]), float(c[1])] for c in coords]
            except Exception:  # noqa
                return self._json({"ok": False, "error": "coords inválidas"}, 400)
            if len(coords) < 2:
                return self._json({"ok": False, "error": "mínimo 2 puntos"}, 400)
            nombre = (str(cuerpo.get("nombre") or "")).strip()[:120] or "Camino sin nombre"
            feat = guardar_camino(nombre, coords)
            return self._json({"ok": True, "feature": feat})
        if u.path == "/api/camino/borrar":
            n = borrar_camino(cuerpo.get("id"))
            return self._json({"ok": True, "borrados": n})
        if u.path == "/api/parcela":
            try:
                ring = [[float(c[0]), float(c[1])] for c in cuerpo["ring"]]
            except Exception:  # noqa
                return self._json({"ok": False, "error": "ring inválido"}, 400)
            if len(ring) < 3:
                return self._json({"ok": False, "error": "mínimo 3 puntos"}, 400)
            prop = (str(cuerpo.get("propietario") or "")).strip()[:120] or "Sin nombre"
            try:
                ha = float(cuerpo.get("hectareas")) if cuerpo.get("hectareas") not in (None, "") else None
            except Exception:  # noqa
                ha = None
            cuartel = (str(cuerpo.get("cuartel") or "")).strip()[:10]
            feat = guardar_parcela(prop, ha, cuartel, ring)
            tipo = (str(cuerpo.get("tipo") or "")).strip()[:40]
            if tipo:
                with _LOCK:
                    fc = _cargar(PARCELAS)
                    for f in fc["features"]:
                        if f["properties"].get("id") == feat["properties"]["id"]:
                            f["properties"]["tipo"] = tipo
                    _guardar_fc(PARCELAS, fc)
                feat["properties"]["tipo"] = tipo
            return self._json({"ok": True, "feature": feat})
        if u.path == "/api/parcela/borrar":
            return self._json({"ok": True, "borrados": borrar_parcela(cuerpo.get("id"))})
        if u.path == "/api/asignar":
            cid = (str(cuerpo.get("campo_id") or "")).strip()[:20]
            partidas = [str(p).strip()[:20] for p in (cuerpo.get("partidas") or []) if str(p).strip()]
            if not cid:
                return self._json({"ok": False, "error": "falta campo_id"}, 400)
            return self._json({"ok": True, "asignados": asignar_campo(cid, partidas[:50])})
        self._json({"ok": False, "error": "ruta no encontrada"}, 404)

    def _json(self, obj, code=200):
        datos = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def _responder(self, cuerpo, tipo):
        datos = cuerpo.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)


if __name__ == "__main__":
    print(f"Sirviendo {DIR_SALIDA} en el puerto {PORT}", flush=True)
    # armar la red de ruteo en segundo plano para que la primera consulta no espere
    threading.Thread(target=ruteo.red_actual, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Manejador).serve_forever()
