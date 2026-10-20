# Cruces de rutas, silos y puertos (OSM)

Georreferencia todos los cruces de rutas nacionales/provinciales (RN/RP) y los
accesos a pueblos que empalman con ellas, más plantas de silos/acopio y puertos,
en Buenos Aires (sin el GBA, hasta la RP 6 inclusive), Santa Fe, Entre Ríos,
Córdoba, La Pampa y Santiago del Estero. Procesa localmente el extracto de
Argentina de Geofabrik con pyosmium (`procesar_osm.py`).
`cruces_rutas_bsas_overpass.py` es la versión anterior vía API Overpass.

Salida: `cruces_<provincia>.csv/.geojson` y `silos_puertos.csv/.geojson`.

## Uso local
    pip install -r requirements.txt
    python procesar_osm.py  # descarga el mapa (~300 MB) a /data; o pasar la ruta al .pbf

## Railway
`run.sh` usa el volumen `/data`, corre el procesador y sirve los resultados por HTTP en `$PORT`.
Los campos y caminos que se marcan o corrigen en los mapas se guardan en la base SQLite `/data/usuario.db`
(variable `DB_USUARIO` para cambiarla), fuera de la salida: un recálculo con `FORZAR=1` no los borra.
Poner la variable `FORZAR=1` para recalcular.

Datos © colaboradores de OpenStreetMap (ODbL).
