# Cruces de rutas, silos y puertos (OSM)

Georreferencia todos los cruces de rutas nacionales/provinciales (RN/RP) y los
accesos a pueblos que empalman con ellas, más plantas de silos/acopio y puertos,
en Buenos Aires (sin el GBA, hasta la RP 6 inclusive), Santa Fe, Entre Ríos,
Córdoba, La Pampa y Santiago del Estero. Procesa localmente el extracto de
Argentina de Geofabrik con pyosmium (`procesar_osm.py`).
`cruces_rutas_bsas_overpass.py` es la versión anterior vía API Overpass.

Salida: `cruces_<provincia>.csv/.geojson` y `silos_puertos.csv/.geojson`.

## Puertos de granos
`datos/puertos_granos.csv`: terminales portuarias de granos (Timbúes, San Martín,
San Lorenzo, Rosario, Ramallo, San Pedro, Quequén, Bahía Blanca, Entre Ríos,
Chaco) con nombre y empresa, ubicadas sobre imagen satelital. `puertos_granos.py`
calcula la distancia por ruta desde 25 de Mayo (OSRM; variable `ORIGEN_PUERTOS`
para otro origen) y, si hay `GOOGLE_PLACES_KEY`, cruza cada terminal con Google Maps.
Sale en `puertos_granos.csv/.geojson` y como capa en `mapa.html` y `mapa_25_de_mayo.html`.

## Uso local
    pip install -r requirements.txt
    python procesar_osm.py  # descarga el mapa (~300 MB) a /data; o pasar la ruta al .pbf

## Railway
`run.sh` usa el volumen `/data`, corre el procesador y sirve los resultados por HTTP en `$PORT`.
Poner la variable `FORZAR=1` para recalcular.

Datos © colaboradores de OpenStreetMap (ODbL).
