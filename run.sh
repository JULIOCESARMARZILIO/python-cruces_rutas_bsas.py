#!/bin/sh
# 1) calcula los cruces (si todavía no están)  2) publica los resultados por HTTP
set -e
mkdir -p salida
cd salida
if [ ! -f cruces_rutas_bsas.csv ]; then
  python ../cruces_rutas_bsas.py
fi
cat > index.html <<'HTML'
<!doctype html><meta charset="utf-8"><title>Cruces de rutas - Prov. de Buenos Aires</title>
<body style="font-family:sans-serif;padding:2em">
<h2>Cruces de rutas - Provincia de Buenos Aires</h2>
<ul>
<li><a href="cruces_rutas_bsas.csv" download>cruces_rutas_bsas.csv</a> (Excel / planilla)</li>
<li><a href="cruces_rutas_bsas.geojson" download>cruces_rutas_bsas.geojson</a> (QGIS / Google Earth)</li>
</ul>
<p>Fuente: OpenStreetMap (ODbL).</p>
</body>
HTML
echo "Resultados publicados"
exec python -m http.server ${PORT:-8080} --bind 0.0.0.0
