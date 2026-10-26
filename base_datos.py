"""
Base de datos (Postgres + PostGIS en Railway) con los caminos rurales y la distancia por camino
de cada punto de origen a cada lugar de entrega.

  partidos      un renglón por partido: nombre, km de caminos, límite
  caminos       tramos del catastro ARBA (caminos_catastro.py): categoría, ancho, km, geometría
  destinos      lugares de entrega (puertos, plantas, acopios) con el precio que pagan (USD/t)
  puntos        orígenes: uno cada 10 km sobre los caminos de tierra principales y cada cruce
                ruta-ruta de Buenos Aires (cruces_buenos_aires.csv), sin la zona excluida
  distancias    km por camino de cada punto a cada destino (y cuántos de tierra)
  tarifa_flete  flete por tramos de 10 km (pesos por tonelada, km_hasta); parametros.dolar
  netos         vista: precio del destino menos el flete, por punto y destino
  mejor_destino vista: para cada punto, el destino que deja más neto

Las distancias se calculan una sola vez (un Dijkstra por destino sobre la red de ruteo.py) y se
guardan; si se agrega un destino se calcula solo esa columna. Si cambian los caminos (otra
firma de la red) se recalcula todo. Precio y flete se aplican al consultar.

Uso:
    DATABASE_URL=postgres://... python base_datos.py           # sincroniza todo
    DATABASE_URL=postgres://... python base_datos.py --estado  # cuenta lo que hay
"""

import csv
import glob
import hashlib
import io
import json
import math
import os
import sys
import threading
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
DIR_SALIDA = os.environ.get("DIR_SALIDA", "/data/salida")
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PRIVATE_URL")
_LOCK = threading.Lock()   # una sincronización a la vez

# lugares de entrega con precio (los que JULIO pasó para las zonas, USD/t)
DESTINOS_INICIALES = [
    ("Rosario", "puerto", 400, -32.98361, -60.61904),
    ("Bahía Blanca", "puerto", 390, -38.78900, -62.26780),
    ("Quequén", "puerto", 385, -38.57282, -58.70684),
    ("Trenque Lauquen", "planta", 370, -35.9706, -62.7316),
    ("Saladillo", "planta", 375, -35.6400, -59.7790),
]
# tarifa aproximada (FADEEAC marzo 2026) hasta que se cargue la CATAC vigente; igual que zonas.html
TARIFA_PUNTOS = [(10, 13617), (100, 25389), (500, 77700), (1500, 148417)]
DOLAR = 1450

ESQUEMA = """
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE IF NOT EXISTS partidos (
  slug text PRIMARY KEY, nombre text, codigo text, km real, tramos int,
  firma text, limite geometry(Geometry, 4326));
CREATE TABLE IF NOT EXISTS caminos (
  id bigserial PRIMARY KEY, partido text REFERENCES partidos(slug) ON DELETE CASCADE,
  categoria text, ancho_m real, en_osm text, km real, cuartel text,
  geom geometry(LineString, 4326));
CREATE INDEX IF NOT EXISTS caminos_geom ON caminos USING gist (geom);
CREATE INDEX IF NOT EXISTS caminos_partido ON caminos (partido);
CREATE TABLE IF NOT EXISTS destinos (
  id serial PRIMARY KEY, nombre text UNIQUE NOT NULL, tipo text, precio_usd_t real,
  geom geometry(Point, 4326) NOT NULL, creado timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS puntos (
  id serial PRIMARY KEY, clave text UNIQUE NOT NULL, tipo text, nombre text,
  geom geometry(Point, 4326) NOT NULL);
CREATE INDEX IF NOT EXISTS puntos_geom ON puntos USING gist (geom);
CREATE TABLE IF NOT EXISTS distancias (
  punto_id int REFERENCES puntos(id) ON DELETE CASCADE,
  destino_id int REFERENCES destinos(id) ON DELETE CASCADE,
  km real, km_tierra real, red text, PRIMARY KEY (punto_id, destino_id));
CREATE INDEX IF NOT EXISTS distancias_destino ON distancias (destino_id);
CREATE TABLE IF NOT EXISTS destinos_calculados (
  destino_id int PRIMARY KEY REFERENCES destinos(id) ON DELETE CASCADE, red text, puntos int,
  calculado timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS tarifa_flete (km_hasta int PRIMARY KEY, pesos_t real NOT NULL);
CREATE TABLE IF NOT EXISTS parametros (clave text PRIMARY KEY, valor text);
CREATE OR REPLACE VIEW netos AS
  SELECT d.punto_id, d.destino_id, p.tipo AS tipo_punto, p.nombre AS punto, de.nombre AS destino,
         d.km, d.km_tierra, de.precio_usd_t,
         f.pesos_t / NULLIF((SELECT valor::real FROM parametros WHERE clave = 'dolar'), 0) AS flete_usd_t,
         de.precio_usd_t - f.pesos_t / NULLIF((SELECT valor::real FROM parametros WHERE clave = 'dolar'), 0)
           AS neto_usd_t,
         p.geom
  FROM distancias d
  JOIN puntos p ON p.id = d.punto_id
  JOIN destinos de ON de.id = d.destino_id
  LEFT JOIN LATERAL (SELECT t.pesos_t FROM tarifa_flete t
                     WHERE t.km_hasta >= CEIL(d.km / 10.0) * 10 ORDER BY t.km_hasta LIMIT 1) f ON true
  WHERE de.precio_usd_t IS NOT NULL;
CREATE OR REPLACE VIEW mejor_destino AS
  SELECT DISTINCT ON (punto_id) * FROM netos ORDER BY punto_id, neto_usd_t DESC NULLS LAST;
"""


def log(m):
    print(f"[base] {m}", flush=True)


def conectar():
    import psycopg
    if not DATABASE_URL:
        raise RuntimeError("falta DATABASE_URL")
    return psycopg.connect(DATABASE_URL, autocommit=False)


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _wkt_linea(c):
    return "SRID=4326;LINESTRING(" + ",".join(f"{x} {y}" for x, y in c) + ")"


def esquema(con):
    with con.cursor() as cur:
        cur.execute(ESQUEMA)
        cur.execute("SELECT count(*) FROM tarifa_flete")
        if cur.fetchone()[0] == 0:
            filas = []
            for km in range(10, 1510, 10):
                i = 0
                while i < len(TARIFA_PUNTOS) - 2 and km > TARIFA_PUNTOS[i + 1][0]:
                    i += 1
                (k1, v1), (k2, v2) = TARIFA_PUNTOS[i], TARIFA_PUNTOS[i + 1]
                filas.append((km, round(v1 + (v2 - v1) * (km - k1) / (k2 - k1))))
            cur.executemany("INSERT INTO tarifa_flete VALUES (%s, %s)", filas)
        cur.execute("INSERT INTO parametros VALUES ('dolar', %s) ON CONFLICT DO NOTHING", (str(DOLAR),))
        cur.execute("SELECT count(*) FROM destinos")
        if cur.fetchone()[0] == 0:
            for nombre, tipo, precio, lat, lon in DESTINOS_INICIALES:
                cur.execute("INSERT INTO destinos (nombre, tipo, precio_usd_t, geom) "
                            "VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))",
                            (nombre, tipo, precio, lon, lat))
            # terminales portuarias verificadas (sin precio: se les pone desde la base o la API)
            p = os.path.join(AQUI, "datos", "puertos_granos.csv")
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        cur.execute("INSERT INTO destinos (nombre, tipo, geom) VALUES "
                                    "(%s, 'puerto', ST_SetSRID(ST_MakePoint(%s, %s), 4326)) "
                                    "ON CONFLICT (nombre) DO NOTHING",
                                    (r["terminal"], float(r["lon"]), float(r["lat"])))
    con.commit()


def cargar_caminos(con):
    """Partidos y tramos del catastro; solo recarga los partidos cuyo archivo cambió."""
    archivos = {os.path.basename(p)[len("caminos_catastro_"):-len(".geojson")]: p
                for p in glob.glob(os.path.join(DIR_SALIDA, "caminos_catastro_*.geojson"))}
    with con.cursor() as cur:
        cur.execute("SELECT slug, firma FROM partidos")
        actuales = dict(cur.fetchall())
        for slug in set(actuales) - set(archivos):
            cur.execute("DELETE FROM partidos WHERE slug = %s", (slug,))
        cambiados = 0
        for slug, p in sorted(archivos.items()):
            firma = _md5(p)
            if actuales.get(slug) == firma:
                continue
            with open(p, encoding="utf-8") as f:
                gj = json.load(f)
            r = gj.get("resumen") or {}
            limite = None
            lp = os.path.join(DIR_SALIDA, f"limite_arba_{slug}.geojson")
            if os.path.exists(lp):
                with open(lp, encoding="utf-8") as f:
                    limite = json.dumps(json.load(f)["features"][0]["geometry"])
            cur.execute("DELETE FROM partidos WHERE slug = %s", (slug,))
            cur.execute("INSERT INTO partidos VALUES (%s, %s, %s, %s, %s, %s, "
                        "ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))",
                        (slug, r.get("partido") or slug, r.get("codigo_arba"), r.get("km_caminos"),
                         r.get("tramos"), firma, limite))
            with cur.copy("COPY caminos (partido, categoria, ancho_m, en_osm, km, cuartel, geom) "
                          "FROM STDIN") as cp:
                for ft in gj["features"]:
                    pr, g = ft["properties"], ft["geometry"]
                    if g.get("type") != "LineString" or len(g["coordinates"]) < 2:
                        continue
                    cp.write_row((slug, pr.get("categoria"), pr.get("ancho_m"), pr.get("en_osm"),
                                  pr.get("km"), pr.get("cuartel") or None, _wkt_linea(g["coordinates"])))
            cambiados += 1
    con.commit()
    log(f"caminos: {len(archivos)} partidos, {cambiados} recargados")


def _zona_excluida():
    p = os.path.join(AQUI, "datos", "zona_excluida.geojson")
    if not os.path.exists(p):
        return None
    from shapely.geometry import shape
    from shapely.ops import unary_union
    from shapely.prepared import prep
    with open(p, encoding="utf-8") as f:
        return prep(unary_union([shape(ft["geometry"]).buffer(0) for ft in json.load(f)["features"]]))


def _puntos_deseados():
    import ruteo
    ruteo.red_actual()   # _puntos_principales() cachea por firma de la red: que exista antes
    out = {}
    for pt in ruteo._puntos_principales():
        clave = f"c:{pt['lat']:.5f},{pt['lon']:.5f}"
        tipo = "ruta_provincia" if pt["nombre"].endswith("(ruta)") else "camino_principal"
        out[clave] = (tipo, pt["nombre"], pt["lat"], pt["lon"])
    p = os.path.join(DIR_SALIDA, "cruces_buenos_aires.csv")
    if os.path.exists(p):
        zona = _zona_excluida()
        from shapely.geometry import Point
        with open(p, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("tipo") != "ruta-ruta":
                    continue
                lat, lon = round(float(r["lat"]), 5), round(float(r["lon"]), 5)
                if zona is not None and zona.contains(Point(lon, lat)):
                    continue
                nombre = f"{r['ruta']} × {r['cruza_con']}" + (f" ({r['localidad_cercana']})"
                                                              if r.get("localidad_cercana") else "")
                out[f"r:{lat:.5f},{lon:.5f}"] = ("cruce_ruta", nombre, lat, lon)
    return out


def cargar_puntos(con):
    deseados = _puntos_deseados()
    with con.cursor() as cur:
        cur.execute("SELECT clave FROM puntos")
        hay = {r[0] for r in cur.fetchall()}
        sobran = list(hay - set(deseados))
        if sobran:
            cur.execute("DELETE FROM puntos WHERE clave = ANY(%s)", (sobran,))
        nuevos = [(k, *v) for k, v in deseados.items() if k not in hay]
        with cur.copy("COPY puntos (clave, tipo, nombre, geom) FROM STDIN") as cp:
            for k, tipo, nombre, lat, lon in nuevos:
                cp.write_row((k, tipo, nombre, f"SRID=4326;POINT({lon} {lat})"))
    con.commit()
    log(f"puntos: {len(deseados)} ({len(nuevos)} nuevos, {len(sobran)} sacados)")
    return len(nuevos) > 0 or len(sobran) > 0


def firma_red():
    """Huella de los archivos que arman la red (nombre y tamaño, no la fecha: run.sh los copia
    en cada arranque y la fecha cambia aunque el contenido sea el mismo)."""
    import ruteo
    h = hashlib.sha1()
    for p in ruteo._archivos():
        if os.path.exists(p):
            h.update(f"{os.path.basename(p)}:{os.path.getsize(p)};".encode())
    return h.hexdigest()


def calcular_distancias(con, solo_destino=None):
    """Para cada destino al que le faltan distancias con la red actual: un Dijkstra y el km de
    cada punto (por su nodo más cercano de la red, sumando el enganche)."""
    import ruteo
    red = ruteo.red_actual()
    firma = firma_red()
    with con.cursor() as cur:
        cur.execute("SELECT id, ST_Y(geom), ST_X(geom) FROM puntos ORDER BY id")
        puntos = cur.fetchall()
        # pendiente: sin cálculo con esta red, o con otra cantidad de puntos
        q = ("SELECT de.id, de.nombre, ST_Y(de.geom), ST_X(de.geom) FROM destinos de "
             "LEFT JOIN destinos_calculados c ON c.destino_id = de.id "
             "WHERE (c.red IS DISTINCT FROM %s OR c.puntos IS DISTINCT FROM %s)")
        cur.execute(q + (" AND de.id = %s" if solo_destino else ""),
                    (firma, len(puntos), solo_destino) if solo_destino else (firma, len(puntos)))
        pendientes = cur.fetchall()
    if not pendientes:
        log("distancias: al día")
        return 0
    nodos = [(pid,) + red.cercano(lon, lat) for pid, lat, lon in puntos]
    for did, nombre, lat, lon in pendientes:
        t0 = time.time()
        s, eng_d = red.cercano(lon, lat)
        if s is None:
            log(f"distancias: {nombre} queda lejos de la red, se saltea")
            continue
        km, tierra = ruteo._dijkstra(red, s)
        with con.cursor() as cur:
            cur.execute("DELETE FROM distancias WHERE destino_id = %s", (did,))
            with cur.copy("COPY distancias (punto_id, destino_id, km, km_tierra, red) FROM STDIN") as cp:
                for pid, i, eng in nodos:
                    if i is None or km[i] == math.inf:
                        continue
                    cp.write_row((pid, did, round(km[i] + eng + eng_d, 1), round(tierra[i] + eng, 1), firma))
            cur.execute("INSERT INTO destinos_calculados VALUES (%s, %s, %s, now()) ON CONFLICT (destino_id) "
                        "DO UPDATE SET red = EXCLUDED.red, puntos = EXCLUDED.puntos, calculado = now()",
                        (did, firma, len(puntos)))
        con.commit()
        log(f"distancias: {nombre} listo en {time.time() - t0:.0f} s")
    return len(pendientes)


def sincronizar():
    """Todo lo que haga falta para que la base refleje los archivos actuales."""
    if not DATABASE_URL:
        return
    with _LOCK:
        try:
            with conectar() as con:
                esquema(con)
                cargar_caminos(con)
                cargar_puntos(con)
                calcular_distancias(con)
            log("sincronizada")
        except Exception as e:  # noqa  la app sigue andando sin base
            log(f"error: {e}")


def agregar_destino(nombre, tipo, precio, lat, lon):
    """Alta (o actualización) de un lugar de entrega; sus distancias se calculan en segundo plano."""
    with conectar() as con, con.cursor() as cur:
        cur.execute("INSERT INTO destinos (nombre, tipo, precio_usd_t, geom) VALUES "
                    "(%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)) "
                    "ON CONFLICT (nombre) DO UPDATE SET tipo = EXCLUDED.tipo, "
                    "precio_usd_t = EXCLUDED.precio_usd_t, geom = EXCLUDED.geom RETURNING id, "
                    "(xmax = 0) AS nuevo", (nombre, tipo, precio, lon, lat))
        did, _ = cur.fetchone()
        cur.execute("DELETE FROM distancias WHERE destino_id = %s", (did,))   # pudo moverse
        cur.execute("DELETE FROM destinos_calculados WHERE destino_id = %s", (did,))
        con.commit()

    def _calc():
        with _LOCK, conectar() as c:
            calcular_distancias(c, solo_destino=did)
    threading.Thread(target=_calc, daemon=True).start()
    return did


def estado():
    if not DATABASE_URL:
        return {"ok": False, "error": "no hay base configurada (DATABASE_URL)"}
    with conectar() as con, con.cursor() as cur:
        out = {"ok": True}
        for t in ("partidos", "caminos", "destinos", "puntos", "distancias"):
            cur.execute(f"SELECT count(*) FROM {t}")
            out[t] = cur.fetchone()[0]
        cur.execute("SELECT round(sum(km)::numeric, 1) FROM caminos")
        out["km_caminos"] = float(cur.fetchone()[0] or 0)
        cur.execute("SELECT de.id, de.nombre, de.tipo, de.precio_usd_t, ST_Y(de.geom), ST_X(de.geom), "
                    "count(d.punto_id) FROM destinos de LEFT JOIN distancias d ON d.destino_id = de.id "
                    "GROUP BY de.id ORDER BY de.precio_usd_t DESC NULLS LAST, de.nombre")
        out["destinos_detalle"] = [{"id": r[0], "nombre": r[1], "tipo": r[2], "precio_usd_t": r[3],
                                    "lat": r[4], "lon": r[5], "puntos_con_distancia": r[6]}
                                   for r in cur.fetchall()]
        return out


def mejores_geojson():
    """Cada punto con el destino que más neto le deja (para el mapa o QGIS)."""
    with conectar() as con, con.cursor() as cur:
        cur.execute("SELECT punto_id, tipo_punto, punto, destino, km, round(flete_usd_t::numeric, 1), "
                    "round(neto_usd_t::numeric, 1), ST_X(geom), ST_Y(geom) FROM mejor_destino")
        feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [r[7], r[8]]},
                  "properties": {"punto_id": r[0], "tipo": r[1], "punto": r[2], "destino": r[3],
                                 "km": r[4], "flete_usd_t": float(r[5]) if r[5] is not None else None,
                                 "neto_usd_t": float(r[6]) if r[6] is not None else None}}
                 for r in cur.fetchall()]
    return {"type": "FeatureCollection", "features": feats}


if __name__ == "__main__":
    if "--estado" in sys.argv:
        print(json.dumps(estado(), ensure_ascii=False, indent=1))
    else:
        sincronizar()
