"""
Servidor HTTP mínimo (stdlib) para publicar DIR_SALIDA y buscar en los CSV:
  /                      índice (index.html)
  /<archivo>             descarga directa
  /buscar?q=texto        filas de todos los CSV que contienen el texto (HTML)
  /buscar?q=texto&f=csv  lo mismo en CSV
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
    ThreadingHTTPServer(("0.0.0.0", PORT), Manejador).serve_forever()
