// Editor de puntos de un camino (lo usan mapa.html y mapa_25_de_mayo.html).
//   editarCamino(mapa, puntos, { titulo, alGuardar }) abre la edición sobre el mapa:
//   - arrastrá un punto para moverlo; tocá un punto para borrarlo (quedan al menos 2);
//   - arrastrá o tocá un punto chico (entre dos) para agregar uno en el medio;
//   - tocá el mapa para agregar un punto al final;
//   - con op.puntosParaPegar(), un punto que cae cerca de otro camino se pega a ese punto (para unir tramos).
//   «Guardar» llama a alGuardar(coords) con [[lon, lat], ...]; «Cancelar» (o Esc) deja todo como estaba.
(function () {
  let activo = null;

  function barra(titulo) {
    const d = document.createElement('div');
    d.style.cssText = 'position:absolute;left:50%;bottom:24px;transform:translateX(-50%);z-index:1100;'
      + 'background:rgba(255,255,255,.97);color:#111;padding:10px 12px;border-radius:10px;'
      + 'box-shadow:0 2px 12px rgba(0,0,0,.35);font:13px system-ui,sans-serif;max-width:min(560px,calc(100vw - 32px));text-align:center';
    d.innerHTML = '<b></b><div style="margin:4px 0 8px">Arrastrá un punto para moverlo · tocá un punto para borrarlo · '
      + 'tocá un punto chico para agregar uno en el medio · tocá el mapa para agregar al final. Cerca de otro camino, el punto se pega a él.</div>'
      + '<span class="cuenta" style="margin-right:8px"></span>'
      + '<button class="g" style="padding:8px 14px;border:none;border-radius:8px;background:#0a7d2c;color:#fff;font-weight:600;cursor:pointer">✓ Guardar</button> '
      + '<button class="c" style="padding:8px 14px;border:none;border-radius:8px;background:#eee;color:#111;cursor:pointer">✕ Cancelar</button>';
    d.querySelector('b').textContent = titulo || 'Editando camino';
    L.DomEvent.disableClickPropagation(d);
    return d;
  }

  function icono(chico) {
    const t = chico ? 10 : 14;
    return L.divIcon({ className: '', iconSize: [t, t], iconAnchor: [t / 2, t / 2],
      html: '<div style="width:' + t + 'px;height:' + t + 'px;border-radius:50%;box-sizing:border-box;'
        + (chico ? 'background:#fff9;border:2px solid #d97706' : 'background:#fff;border:3px solid #d12') + '"></div>' });
  }

  window.editarCamino = function (mapa, puntos, op) {
    if (activo) activo.cancelar();
    let pts = puntos.map(p => L.latLng(p[0], p[1]));   // [lat, lng]
    // Imán: un punto que cae a menos de 14 px de un punto de otro camino se pega a él (así se unen los tramos sueltos).
    const pegar = ll => {
      const cerca = op.puntosParaPegar ? op.puntosParaPegar() : [];
      const a = mapa.latLngToContainerPoint(ll);
      let mejor = null, dist = 14;
      for (const c of cerca) { const d = a.distanceTo(mapa.latLngToContainerPoint(c)); if (d < dist) { dist = d; mejor = c; } }
      return mejor ? L.latLng(mejor.lat, mejor.lng) : ll;
    };
    const linea = L.polyline(pts, { color: '#d12', weight: 4, dashArray: '6 4', interactive: false }).addTo(mapa);
    const manijas = L.layerGroup().addTo(mapa);
    const ui = barra(op.titulo);
    mapa.getContainer().appendChild(ui);
    mapa.getContainer().style.cursor = 'crosshair';
    mapa.closePopup();

    function dibujar() {
      linea.setLatLngs(pts);
      manijas.clearLayers();
      ui.querySelector('.cuenta').textContent = pts.length + ' punto' + (pts.length === 1 ? '' : 's');
      pts.forEach((p, i) => {
        const m = L.marker(p, { icon: icono(false), draggable: true, zIndexOffset: 1000 });
        m.on('drag', e => { pts[i] = e.target.getLatLng(); linea.setLatLngs(pts); });
        m.on('dragend', e => { pts[i] = pegar(e.target.getLatLng()); dibujar(); });
        m.on('click', () => {
          if (pts.length <= 2) { alert('Un camino necesita al menos 2 puntos.'); return; }
          pts.splice(i, 1); dibujar();
        });
        manijas.addLayer(m);
      });
      for (let i = 0; i + 1 < pts.length; i++) {
        const medio = L.latLng((pts[i].lat + pts[i + 1].lat) / 2, (pts[i].lng + pts[i + 1].lng) / 2);
        const m = L.marker(medio, { icon: icono(true), draggable: true, zIndexOffset: 900 });
        let insertado = false;
        const insertar = () => { if (!insertado) { pts.splice(i + 1, 0, m.getLatLng()); insertado = true; } };
        m.on('dragstart', insertar);
        m.on('drag', e => { pts[i + 1] = e.target.getLatLng(); linea.setLatLngs(pts); });
        m.on('dragend', e => { pts[i] = pegar(e.target.getLatLng()); dibujar(); });
        m.on('click', () => { insertar(); dibujar(); });
        manijas.addLayer(m);
      }
    }
    const alClic = e => { pts.push(pegar(e.latlng)); dibujar(); };
    const alTecla = e => { if (e.key === 'Escape') cerrar(); };
    function cerrar() {
      mapa.off('click', alClic);
      document.removeEventListener('keydown', alTecla);
      mapa.removeLayer(linea); mapa.removeLayer(manijas);
      ui.remove(); mapa.getContainer().style.cursor = '';
      activo = null;
    }
    ui.querySelector('.c').onclick = cerrar;
    ui.querySelector('.g').onclick = async () => {
      if (pts.length < 2) { alert('Marcá al menos 2 puntos del camino.'); return; }
      const btn = ui.querySelector('.g'); btn.disabled = true; btn.textContent = 'Guardando…';
      try {
        await op.alGuardar(pts.map(p => [p.lng, p.lat]));
        cerrar();
      } catch (err) {
        alert('No se pudo guardar: ' + (err && err.message ? err.message : err));
        btn.disabled = false; btn.textContent = '✓ Guardar';
      }
    };
    // el clic que abrió la edición no tiene que sumar un punto
    setTimeout(() => mapa.on('click', alClic), 0);
    document.addEventListener('keydown', alTecla);
    dibujar();
    activo = { cancelar: cerrar };
    return activo;
  };

  // POST JSON al servidor; tira error con el mensaje del servidor si no salió.
  window.enviarJson = async function (ruta, cuerpo) {
    const r = await fetch(ruta, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cuerpo), cache: 'no-store' });
    let j = {};
    try { j = await r.json(); } catch (e) { /* sin cuerpo */ }
    if (!r.ok || !j.ok) throw new Error(j.error || ('el servidor respondió ' + r.status));
    return j;
  };
  // GET JSON sin caché (los caminos y campos cambian al guardar).
  window.leerJson = async function (ruta) {
    const r = await fetch(ruta, { cache: 'no-store' });
    if (!r.ok) throw new Error('el servidor respondió ' + r.status);
    return r.json();
  };
  // Clave estable de un tramo de OSM: primer y último punto (con 6 decimales) y cantidad de puntos.
  window.claveOsm = function (coords) {
    const f = c => c[0].toFixed(6) + ',' + c[1].toFixed(6);
    return f(coords[0]) + '|' + f(coords[coords.length - 1]) + '|' + coords.length;
  };
})();

// Capas de caminos con edición (mapa.html y mapa_25_de_mayo.html):
//   capasDeCaminos(mapa, { osm: 'caminos_25_de_mayo.geojson' | null, verOsm (false: arranca apagada), estiloOsm(f), alCambiar() })
//   - los tramos de OSM (si se pasa osm), menos los que el usuario corrigió o dio de baja;
//   - los caminos guardados por el usuario (caminos_usuario.geojson, desde la base del servidor), en verde.
//   Cada tramo tiene en su globo «mover puntos», «renombrar» y «borrar». Devuelve { recargar, nuevo, mostrarOsm, mostrarUsuario }.
(function () {
  const esc = s => (s == null ? '' : String(s)).replace(/[<>&"]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c]));
  const fmt = n => (Math.round(n * 10) / 10).toLocaleString('es-AR');
  const ESTILO_USU = { color: '#0a9e4a', weight: 5, opacity: .95 };

  window.capasDeCaminos = function (mapa, op) {
    let capaOsm = null, capaUsu = null, verOsm = Boolean(op.osm) && op.verOsm !== false, verUsu = true, osmCache = null, usu = null;
    const acciones = {};   // id de acción -> función (los globos llaman a accionCamino(id))
    let nAccion = 0;
    window.accionCamino = k => { const f = acciones[k]; if (f) f(); };
    const accion = (texto, f) => { const k = 'a' + (++nAccion); acciones[k] = f; return '<a href="#" onclick="accionCamino(\'' + k + '\');return false">' + texto + '</a>'; };

    // Para el imán del editor: los puntos de los tramos visibles cerca de la vista.
    function puntosParaPegar() {
      const b = mapa.getBounds().pad(0.2), out = [];
      for (const capa of [capaOsm, capaUsu]) {
        if (!capa || !mapa.hasLayer(capa)) continue;
        capa.eachLayer(l => { for (const ll of l.getLatLngs().flat(2)) if (b.contains(ll)) out.push(ll); });
      }
      return out;
    }
    const editar = (coords, titulo, guardar) => window.editarCamino(mapa, coords.map(c => [c[1], c[0]]),
      { titulo, puntosParaPegar, alGuardar: async nuevas => { await guardar(nuevas); await recargar(); } });

    function popupOsm(f, l) {
      const p = f.properties || {}, coords = f.geometry.coordinates, clave = window.claveOsm(coords);
      const nombre = p.nombre || p.ref || 'Camino sin nombre';
      const div = () => '<div class="popup"><b>' + esc(nombre) + '</b>' + (p.ref ? ' (' + esc(p.ref) + ')' : '') + ' — ' + fmt(p.km) + ' km<br>'
        + '<small>OpenStreetMap · ' + esc(p.highway || '') + '</small><br>'
        + accion('✏ mover puntos', () => editar(coords, 'Corrigiendo ' + nombre, nuevas =>
            window.enviarJson('api/camino', { nombre, coords: nuevas, clave_osm: clave, highway: p.highway || '' }))) + ' · '
        + accion('borrar', async () => {
            if (!confirm('¿Dar de baja este tramo de OpenStreetMap? Deja de verse en el mapa.')) return;
            try { await window.enviarJson('api/osm/borrar', { clave_osm: clave, nombre }); mapa.closePopup(); await recargar(); }
            catch (e) { alert('No se pudo borrar: ' + e.message); }
          }) + '</div>';
      l.bindPopup(div);
    }
    function popupUsu(f, l) {
      const p = f.properties || {}, coords = f.geometry.coordinates;
      l.bindPopup(() => '<div class="popup"><b>' + esc(p.nombre || 'Camino') + '</b> — ' + fmt(p.km || 0) + ' km<br>'
        + '<small>' + (p.clave_osm ? 'Tramo de OpenStreetMap corregido' : 'Marcado por vos') + (p.modificado ? ' · cambiado ' + esc(p.modificado) : p.creado ? ' · ' + esc(p.creado) : '') + '</small><br>'
        + accion('✏ mover puntos', () => editar(coords, 'Editando ' + (p.nombre || 'camino'), nuevas =>
            window.enviarJson('api/camino/editar', { id: p.id, coords: nuevas }))) + ' · '
        + accion('renombrar', async () => {
            const n = prompt('Nombre del camino:', p.nombre || ''); if (n === null) return;
            try { await window.enviarJson('api/camino/editar', { id: p.id, nombre: n }); mapa.closePopup(); await recargar(); }
            catch (e) { alert('No se pudo renombrar: ' + e.message); }
          }) + ' · '
        + accion('borrar', async () => {
            if (!confirm(p.clave_osm ? '¿Dar de baja este tramo? (el original de OpenStreetMap tampoco se va a ver)' : '¿Borrar este camino?')) return;
            try { await window.enviarJson('api/camino/borrar', { id: p.id }); mapa.closePopup(); await recargar(); }
            catch (e) { alert('No se pudo borrar: ' + e.message); }
          }) + '</div>');
    }

    async function recargar() {
      for (const k in acciones) delete acciones[k];
      try { usu = await window.leerJson('caminos_usuario.geojson'); }
      catch (e) { usu = usu || { features: [], ocultar: [] }; console.warn('caminos del usuario', e); }
      const ocultar = new Set(usu.ocultar || []);
      if (capaUsu) mapa.removeLayer(capaUsu);
      capaUsu = L.geoJSON(usu, { style: ESTILO_USU, onEachFeature: popupUsu });
      if (verUsu) capaUsu.addTo(mapa);
      if (op.osm && (verOsm || osmCache)) {
        if (!osmCache) osmCache = await window.leerJson(op.osm);
        if (capaOsm) mapa.removeLayer(capaOsm);
        capaOsm = L.geoJSON(osmCache, { filter: f => f.geometry && !ocultar.has(window.claveOsm(f.geometry.coordinates)),
          style: op.estiloOsm, onEachFeature: popupOsm });
        if (verOsm) { capaOsm.addTo(mapa); capaUsu.bringToFront(); }
      }
      if (op.alCambiar) op.alCambiar({ usuario: usu.features, ocultos: ocultar.size });
    }
    function nuevo() {
      verUsu = true; if (capaUsu && !mapa.hasLayer(capaUsu)) capaUsu.addTo(mapa);
      window.editarCamino(mapa, [], { titulo: 'Camino nuevo: tocá el mapa en cada punto donde dobla', puntosParaPegar,
        alGuardar: async coords => {
          const nombre = prompt('Nombre del camino (opcional):') || '';
          await window.enviarJson('api/camino', { nombre, coords });
          await recargar();
        } });
    }
    const mostrar = (capa, v) => { if (!capa) return; if (v) capa.addTo(mapa); else mapa.removeLayer(capa); };
    return {
      recargar, nuevo,
      mostrarOsm: async v => { verOsm = v; if (v && !capaOsm) await recargar(); mostrar(capaOsm, v); if (capaUsu && mapa.hasLayer(capaUsu)) capaUsu.bringToFront(); },
      mostrarUsuario: v => { verUsu = v; mostrar(capaUsu, v); },
      osmCargado: () => Boolean(osmCache),
    };
  };
})();
