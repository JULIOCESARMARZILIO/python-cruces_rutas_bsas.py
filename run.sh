#!/bin/sh
# Railway: 1) baja el mapa de Argentina al volumen /data (una sola vez)
#          2) procesa cruces + silos/puertos  3) publica los resultados por HTTP
set -e
export DIR_DATOS=/data
export DIR_SALIDA=/data/salida
mkdir -p "$DIR_SALIDA"
if [ "$FORZAR" = "1" ] || [ ! -f "$DIR_SALIDA/resumen.json" ]; then
  # NO borrar lo cargado por el usuario (campos, caminos marcados, parcelas)
  find "$DIR_SALIDA" -maxdepth 1 -type f \( -name '*.csv' -o -name '*.geojson' -o -name 'resumen.json' \) \
    ! -name campos.geojson ! -name caminos_usuario.geojson ! -name parcelas.geojson \
    ! -name asignaciones_campos.json -delete
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
# caminos rurales de un partido (si se pide por CALC_CAMINOS, p.ej. "25_de_mayo")
if [ -n "$CALC_CAMINOS" ]; then
  python caminos_partido.py "$CALC_CAMINOS" || echo "caminos_partido.py terminó con error (se sigue igual)"
fi
for H in mapa.html mapa_25_de_mayo.html marcar.html campos_25_de_mayo.html distancia.html; do
  cp "$(dirname "$0")/$H" "$DIR_SALIDA/" 2>/dev/null || cp "/app/$H" "$DIR_SALIDA/" 2>/dev/null || true
done
# catastro ARBA de 25 de Mayo (generado con caminos_catastro.py, viene en datos/)
for G in caminos_catastro_25_de_mayo.geojson parcelas_arba_25_de_mayo.geojson limite_arba_25_de_mayo.geojson; do
  cp "$(dirname "$0")/datos/$G" "$DIR_SALIDA/" 2>/dev/null || cp "/app/datos/$G" "$DIR_SALIDA/" 2>/dev/null || true
done
# archivo de campos cargados por el usuario (no pisar si ya existe)
[ -f "$DIR_SALIDA/campos.geojson" ] || echo '{"type":"FeatureCollection","features":[]}' > "$DIR_SALIDA/campos.geojson"
[ -f "$DIR_SALIDA/parcelas.geojson" ] || echo '{"type":"FeatureCollection","features":[]}' > "$DIR_SALIDA/parcelas.geojson"
[ -f "$DIR_SALIDA/caminos_usuario.geojson" ] || echo '{"type":"FeatureCollection","features":[]}' > "$DIR_SALIDA/caminos_usuario.geojson"
# campos del plano + parcelas ARBA: propietarios, hectáreas y promedios
python "$(dirname "$0")/resumen_campos.py" || echo "resumen_campos.py terminó con error (se sigue igual)"
cd "$DIR_SALIDA"
{
  echo '<!doctype html><meta charset="utf-8"><title>Cruces de rutas, silos y puertos</title>'
  echo '<body style="font-family:sans-serif;padding:2em"><h2>Cruces de rutas, silos y puertos</h2>'
  echo '<p><a href="distancia.html"><b>📏 Medir distancia por camino</b></a> · <a href="mapa.html"><b>Mapa interactivo</b></a> · <a href="mapa_25_de_mayo.html"><b>Campos y caminos rurales 25 de Mayo</b></a> · <a href="campos_25_de_mayo.html"><b>Propietarios 25 de Mayo</b></a> · <a href="rutas/">Secuencia de cruces por ruta</a> · <a href="buscar">Buscar un nombre</a>. CSV para Excel; GeoJSON para QGIS / Google Earth.</p><ul>'
  for f in plantas_sisa.csv plantas_sisa.geojson cruces_*.csv cruces_*.geojson tramos_*.csv red_rutas.json silos_puertos.csv silos_puertos.geojson silos_google.csv silos_google.geojson caminos_*.geojson aguas_*.geojson localidades_*.geojson cruces_25*.geojson parcelas.geojson campos_25_de_mayo.csv propietarios_25_de_mayo.csv campos_25_de_mayo.geojson parcelas_arba_*.geojson caminos_*_resumen.json resumen.json; do
    [ -f "$f" ] && echo "<li><a href=\"$f\" download>$f</a></li>"
  done
  echo '</ul><p>Fuente: OpenStreetMap (ODbL), extracto de Geofabrik.</p></body>'
} > index.html
echo "Resultados publicados"
cd /app 2>/dev/null || cd "$(dirname "$0")"
exec python servidor.py
