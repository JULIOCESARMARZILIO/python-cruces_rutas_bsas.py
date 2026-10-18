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
# capa de Google Places (si hay clave); reanuda si se cortó
if [ -n "$GOOGLE_PLACES_KEY" ] && [ "$FORZAR_GOOGLE" = "1" ]; then
  python silos_google.py || echo "silos_google.py terminó con error (se sigue igual)"
fi
# plantas del padrón SISA ubicadas con Google y enganchadas a la red de rutas
if [ -n "$GOOGLE_PLACES_KEY" ] && { [ "$FORZAR_PLANTAS" = "1" ] || [ ! -f "$DIR_SALIDA/plantas_sisa.csv" ]; }; then
  python plantas_sisa.py || echo "plantas_sisa.py terminó con error (se sigue igual)"
fi
python secuencias_rutas.py || true
cd "$DIR_SALIDA"
{
  echo '<!doctype html><meta charset="utf-8"><title>Cruces de rutas, silos y puertos</title>'
  echo '<body style="font-family:sans-serif;padding:2em"><h2>Cruces de rutas, silos y puertos</h2>'
  echo '<p>CSV para Excel; GeoJSON para QGIS / Google Earth. <a href="rutas/">Secuencia de cruces por ruta</a> · <a href="buscar">Buscar un nombre en todos los archivos</a>.</p><ul>'
  for f in plantas_sisa.csv plantas_sisa.geojson cruces_*.csv cruces_*.geojson tramos_*.csv red_rutas.json silos_puertos.csv silos_puertos.geojson silos_google.csv silos_google.geojson resumen.json; do
    [ -f "$f" ] && echo "<li><a href=\"$f\" download>$f</a></li>"
  done
  echo '</ul><p>Fuente: OpenStreetMap (ODbL), extracto de Geofabrik.</p></body>'
} > index.html
echo "Resultados publicados"
cd /app 2>/dev/null || cd "$(dirname "$0")"
exec python servidor.py
