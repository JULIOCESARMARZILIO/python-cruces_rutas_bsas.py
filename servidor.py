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
import sqlite3
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DIR_SALIDA = os.environ.get("DIR_SALIDA", "/data/salida")
PORT = int(os.environ.get("PORT", "8080"))
MAX_FILAS = 500

# Lo que carga el usuario (campos y caminos) va a una base SQLite en el volumen de Railway (/data), fuera de
# DIR_SALIDA: así no lo borra un recálculo con FORZAR=1 (run.sh limpia los .geojson de la salida).
DB_USUARIO = os.environ.get("DB_USUARIO", os.path.join(os.environ.get("DIR_DATOS", "/data"), "usuario.db"))
_LOCK = threading.Lock()


def _db():
    con = sqlite3.connect(DB_USUARIO)
    con.row_factory = sqlite3.Row
    return con


def _ahora():
    return time.strftime("%Y-%m-%d %H:%M")


def _cargar(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa
            pass
    return {"type": "FeatureCollection", "features": []}


def iniciar_base():
    """Crea las tablas y, la primera vez, pasa a la base lo que había en los .geojson de la salida."""
    os.makedirs(os.path.dirname(DB_USUARIO) or ".", exist_ok=True)
    with _LOCK, _db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS campos (
              id INTEGER PRIMARY KEY, nombre TEXT NOT NULL, nota TEXT NOT NULL DEFAULT '',
              lat REAL NOT NULL, lon REAL NOT NULL, creado TEXT, modificado TEXT);
            -- clave_osm: el tramo de OpenStreetMap que este camino corrige (se oculta el original);
            -- borrado = 1 con clave_osm: el tramo de OSM se dio de baja.
            CREATE TABLE IF NOT EXISTS caminos (
              id INTEGER PRIMARY KEY, nombre TEXT NOT NULL, coords TEXT NOT NULL, km REAL NOT NULL,
              highway TEXT NOT NULL DEFAULT '', clave_osm TEXT, borrado INTEGER NOT NULL DEFAULT 0,
              creado TEXT, modificado TEXT);
        """)
        if not con.execute("SELECT 1 FROM campos LIMIT 1").fetchone():
            for f in _cargar(os.path.join(DIR_SALIDA, "campos.geojson"))["features"]:
                p = f.get("properties", {}); lon, lat = f["geometry"]["coordinates"][:2]
                con.execute("INSERT INTO campos (nombre, nota, lat, lon, creado) VALUES (?,?,?,?,?)",
                            (p.get("nombre") or "Campo sin nombre", p.get("nota") or "", lat, lon, p.get("creado")))
        if not con.execute("SELECT 1 FROM caminos LIMIT 1").fetchone():
            for f in _cargar(os.path.join(DIR_SALIDA, "caminos_usuario.geojson"))["features"]:
                p = f.get("properties", {}); coords = f["geometry"]["coordinates"]
                con.execute("INSERT INTO caminos (nombre, coords, km, creado) VALUES (?,?,?,?)",
                            (p.get("nombre") or "Camino sin nombre", json.dumps(coords), _largo(coords), p.get("creado")))


def campos_geojson():
    with _db() as con:
        filas = con.execute("SELECT * FROM campos ORDER BY id").fetchall()
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
         "properties": {"id": r["id"], "nombre": r["nombre"], "nota": r["nota"],
                        "creado": r["creado"], "modificado": r["modificado"]}} for r in filas]}


def guardar_campo(nombre, lat, lon, nota=""):
    with _LOCK, _db() as con:
        cid = con.execute("INSERT INTO campos (nombre, nota, lat, lon, creado) VALUES (?,?,?,?,?)",
                          (nombre, nota, lat, lon, _ahora())).lastrowid
    return {"id": cid, "nombre": nombre, "lat": lat, "lon": lon}


def editar_campo(cid, cuerpo):
    cambios, valores = [], []
    if "lat" in cuerpo or "lon" in cuerpo:
        cambios += ["lat = ?", "lon = ?"]; valores += [float(cuerpo["lat"]), float(cuerpo["lon"])]
    if "nombre" in cuerpo:
        cambios.append("nombre = ?"); valores.append(str(cuerpo["nombre"] or "").strip()[:120] or "Campo sin nombre")
    if "nota" in cuerpo:
        cambios.append("nota = ?"); valores.append(str(cuerpo["nota"] or "").strip()[:500])
    if not cambios:
        return 0
    with _LOCK, _db() as con:
        return con.execute(f"UPDATE campos SET {', '.join(cambios)}, modificado = ? WHERE id = ?",
                           valores + [_ahora(), cid]).rowcount


def borrar_campo(cid):
    with _LOCK, _db() as con:
        return con.execute("DELETE FROM campos WHERE id = ?", (cid,)).rowcount


def _hav_km(lon1, lat1, lon2, lat2):
    import math
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def _largo(coords):
    return round(sum(_hav_km(a[0], a[1], b[0], b[1]) for a, b in zip(coords, coords[1:])), 3)


def caminos_geojson():
    """Los caminos del usuario y, en «ocultar», las claves de los tramos de OSM corregidos o dados de baja."""
    with _db() as con:
        filas = con.execute("SELECT * FROM caminos ORDER BY id").fetchall()
    return {"type": "FeatureCollection",
            "ocultar": sorted({r["clave_osm"] for r in filas if r["clave_osm"]}),
            "features": [
                {"type": "Feature", "geometry": {"type": "LineString", "coordinates": json.loads(r["coords"])},
                 "properties": {"id": r["id"], "nombre": r["nombre"], "km": r["km"], "highway": r["highway"],
                                "clave_osm": r["clave_osm"], "creado": r["creado"], "modificado": r["modificado"]}}
                for r in filas if not r["borrado"]]}


def guardar_camino(nombre, coords, highway="", clave_osm=None):
    with _LOCK, _db() as con:
        if clave_osm:  # ya corregido antes: se reemplaza esa corrección
            fila = con.execute("SELECT id FROM caminos WHERE clave_osm = ?", (clave_osm,)).fetchone()
            if fila:
                con.execute("UPDATE caminos SET nombre = ?, coords = ?, km = ?, borrado = 0, modificado = ? WHERE id = ?",
                            (nombre, json.dumps(coords), _largo(coords), _ahora(), fila["id"]))
                return {"id": fila["id"], "km": _largo(coords)}
        cid = con.execute("INSERT INTO caminos (nombre, coords, km, highway, clave_osm, creado) VALUES (?,?,?,?,?,?)",
                          (nombre, json.dumps(coords), _largo(coords), highway, clave_osm, _ahora())).lastrowid
    return {"id": cid, "km": _largo(coords)}


def editar_camino(cid, coords=None, nombre=None):
    cambios, valores = [], []
    if coords is not None:
        cambios += ["coords = ?", "km = ?"]; valores += [json.dumps(coords), _largo(coords)]
    if nombre is not None:
        cambios.append("nombre = ?"); valores.append(nombre)
    if not cambios:
        return 0
    with _LOCK, _db() as con:
        return con.execute(f"UPDATE caminos SET {', '.join(cambios)}, modificado = ? WHERE id = ? AND borrado = 0",
                           valores + [_ahora(), cid]).rowcount


def borrar_camino(cid):
    """Un camino propio se borra; la corrección de un tramo de OSM queda como baja (sigue ocultando el original)."""
    with _LOCK, _db() as con:
        n = con.execute("UPDATE caminos SET borrado = 1, modificado = ? WHERE id = ? AND clave_osm IS NOT NULL",
                        (_ahora(), cid)).rowcount
        return n or con.execute("DELETE FROM caminos WHERE id = ?", (cid,)).rowcount


def borrar_tramo_osm(clave, nombre=""):
    with _LOCK, _db() as con:
        if con.execute("UPDATE caminos SET borrado = 1, modificado = ? WHERE clave_osm = ?", (_ahora(), clave)).rowcount:
            return 1
        con.execute("INSERT INTO caminos (nombre, coords, km, clave_osm, borrado, creado) VALUES (?,?,?,?,1,?)",
                    (nombre or "Tramo de OSM dado de baja", "[]", 0, clave, _ahora()))
        return 1


def _coords(valor):
    coords = [[float(c[0]), float(c[1])] for c in valor]
    if len(coords) < 2:
        raise ValueError("mínimo 2 puntos")
    if not all(-180 <= lo <= 180 and -90 <= la <= 90 for lo, la in coords):
        raise ValueError("coordenadas fuera de rango")
    return coords


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
        if u.path == "/campos.geojson":
            return self._json(campos_geojson())
        if u.path == "/caminos_usuario.geojson":
            return self._json(caminos_geojson())
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
        try:
            return self._post(u.path, cuerpo)
        except (KeyError, TypeError, ValueError) as e:
            return self._json({"ok": False, "error": f"datos inválidos: {e}"}, 400)
        except sqlite3.Error as e:
            return self._json({"ok": False, "error": f"no se pudo guardar en la base: {e}"}, 500)

    def _post(self, ruta, cuerpo):
        if ruta == "/api/campo":
            lat = float(cuerpo["lat"]); lon = float(cuerpo["lon"])
            nombre = (str(cuerpo.get("nombre") or "")).strip()[:120] or "Campo sin nombre"
            nota = (str(cuerpo.get("nota") or "")).strip()[:500]
            return self._json({"ok": True, "campo": guardar_campo(nombre, lat, lon, nota)})
        if ruta == "/api/campo/editar":
            n = editar_campo(int(cuerpo["id"]), cuerpo)
            return self._json({"ok": bool(n), "error": "" if n else "no existe ese campo"}, 200 if n else 404)
        if ruta == "/api/campo/borrar":
            return self._json({"ok": True, "borrados": borrar_campo(int(cuerpo["id"]))})
        if ruta == "/api/camino":
            coords = _coords(cuerpo.get("coords"))
            nombre = (str(cuerpo.get("nombre") or "")).strip()[:120] or "Camino sin nombre"
            clave = (str(cuerpo.get("clave_osm") or "")).strip()[:200] or None
            highway = (str(cuerpo.get("highway") or "")).strip()[:40]
            return self._json({"ok": True, "camino": guardar_camino(nombre, coords, highway, clave)})
        if ruta == "/api/camino/editar":
            coords = _coords(cuerpo["coords"]) if "coords" in cuerpo else None
            nombre = (str(cuerpo.get("nombre") or "")).strip()[:120] or "Camino sin nombre" if "nombre" in cuerpo else None
            n = editar_camino(int(cuerpo["id"]), coords, nombre)
            return self._json({"ok": bool(n), "error": "" if n else "no existe ese camino"}, 200 if n else 404)
        if ruta == "/api/camino/borrar":
            return self._json({"ok": True, "borrados": borrar_camino(int(cuerpo["id"]))})
        if ruta == "/api/osm/borrar":
            clave = str(cuerpo["clave_osm"]).strip()[:200]
            if not clave:
                raise ValueError("falta clave_osm")
            return self._json({"ok": True, "borrados": borrar_tramo_osm(clave, str(cuerpo.get("nombre") or "")[:120])})
        return self._json({"ok": False, "error": "ruta no encontrada"}, 404)

    def _json(self, obj, code=200):
        datos = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(datos)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(datos)

    def end_headers(self):
        # Los .geojson/.json/.html cambian (recálculos y lo que carga el usuario): que el navegador no muestre uno viejo.
        if self.path.split("?")[0].endswith((".geojson", ".json", ".html", ".js")) and not getattr(self, "_sin_cache", False):
            self._sin_cache = True
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _responder(self, cuerpo, tipo):
        datos = cuerpo.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)


if __name__ == "__main__":
    iniciar_base()
    print(f"Base de campos y caminos del usuario: {DB_USUARIO}", flush=True)
    print(f"Sirviendo {DIR_SALIDA} en el puerto {PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Manejador).serve_forever()
