# Cruces de rutas - Provincia de Buenos Aires

Georreferencia todos los cruces de rutas nacionales/provinciales (RN/RP) de la
Provincia de Buenos Aires, y los accesos a pueblos que empalman con ellas,
usando datos de OpenStreetMap (API Overpass).

Salida: `cruces_rutas_bsas.csv` y `cruces_rutas_bsas.geojson` con tipo de cruce,
ruta, con qué se cruza, lat/lon, localidad cercana y link a Google Maps.

## Uso local
    pip install -r requirements.txt
    python cruces_rutas_bsas.py

## Railway
`run.sh` corre el script y luego sirve los resultados por HTTP en `$PORT`.

Datos © colaboradores de OpenStreetMap (ODbL).
