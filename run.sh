#!/bin/sh
# Railway: 1) baja el mapa de Argentina al volumen /data (una sola vez)
#          2) procesa cruces + silos/puertos  3) publica los resultados por HTTP
set -e
export DIR_DATOS=/data
export DIR_SALIDA=/data/salida
mkdir -p "$DIR_SALIDA"
if [ "$FORZAR" = "1" ] || [ ! -f "$DIR_SALIDA/resumen.json" ]; then
  rm -f "$DIR_SALIDA"/*.csv "$DIR_SALIDA"/*.geojson "$DIR_SALIDA"/resumen.json
  python procesar_osm.py
fi
cd "$DIR_SALIDA"
{
  echo '<!doctype html><meta charset="utf-8"><title>Cruces de rutas, silos y puertos</title>'
  echo '<body style="font-family:sans-serif;padding:2em"><h2>Cruces de rutas, silos y puertos</h2>'
  echo '<p>CSV para Excel; GeoJSON para QGIS / Google Earth.</p><ul>'
  for f in cruces_*.csv cruces_*.geojson silos_puertos.csv silos_puertos.geojson resumen.json; do
    [ -f "$f" ] && echo "<li><a href=\"$f\" download>$f</a></li>"
  done
  echo '</ul><p>Fuente: OpenStreetMap (ODbL), extracto de Geofabrik.</p></body>'
} > index.html
echo "Resultados publicados"
exec python -m http.server ${PORT:-8080} --bind 0.0.0.0
