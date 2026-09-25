/* Presentation layer.
 *
 * This file renders what the central server already decided. It does no confirmation,
 * clustering or aggregation of its own: if a feature is in a layer response, it is drawn.
 * Every threshold shown in the sidebar comes from /api/v1/ops/stats, not from here.
 *
 * The route filter is the one thing here that hides data, and it is a view filter in the
 * same category as a bounding box - it decides what is drawn, never what is true, and
 * the sidebar shows both counts so a filtered layer is never mistaken for an empty one.
 */

const API = "/api/v1";
const PUNE = [18.5204, 73.8567];

/* GeoJSON is [lon, lat]; Leaflet is [lat, lon]. Everything goes through here. */
const latlng = (feature) => [feature.geometry.coordinates[1], feature.geometry.coordinates[0]];

// ---------------------------------------------------------------- base map

/* The view lives in the URL hash (#zoom/lat/lon) so a demo view can be reloaded or
 * handed to someone else without hunting for the spot again. */
const fromHash = () => {
  const [z, lat, lon] = location.hash.slice(1).split("/").map(Number);
  return Number.isFinite(z) && Number.isFinite(lat) && Number.isFinite(lon)
    ? { center: [lat, lon], zoom: z } : { center: PUNE, zoom: 12 };
};

const map = L.map("map", { ...fromHash(), zoomControl: false, preferCanvas: true });
map.on("moveend", () => {
  const c = map.getCenter();
  history.replaceState(null, "", `#${map.getZoom()}/${c.lat.toFixed(5)}/${c.lng.toFixed(5)}`);
});
L.control.zoom({ position: "topright" }).addTo(map);
L.control.scale({ imperial: false, position: "bottomright" }).addTo(map);

const OSM_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
const ESRI_ATTR = "Tiles &copy; Esri";
const esri = (service, opts = {}) =>
  L.tileLayer(`https://server.arcgisonline.com/ArcGIS/rest/services/${service}/MapServer/tile/{z}/{y}/{x}`,
    { attribution: ESRI_ATTR, maxZoom: 19, ...opts });

/* Every tile source here is keyless. Carto's basemaps still answer 200 but now stamp
 * "API KEY REQUIRED" across the image, so they are not usable for an offline-ish demo. */
const BASEMAPS = {
  // Esri's "dark gray" canvas is really mid-grey (~#434345); dimming it in CSS gives the
  // heat layers something to stand out against on a projector.
  dark: esri("Canvas/World_Dark_Gray_Base", { className: "dim" }),
  light: esri("Canvas/World_Light_Gray_Base"),
  osm: L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    { attribution: OSM_ATTR, maxZoom: 19 }),
  satellite: esri("World_Imagery", { attribution: ESRI_ATTR + " · Maxar, Earthstar Geographics" }),
};
let base = BASEMAPS.dark.addTo(map);

document.getElementById("basemap").addEventListener("change", (e) => {
  map.removeLayer(base);
  base = BASEMAPS[e.target.value].addTo(map);
});

// ---------------------------------------------------------------- helpers

const el = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const num = (n, d = 1) => (n === null || n === undefined ? "—" : Number(n).toFixed(d));

const clock = (iso) => {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-IN", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false,
  });
};

const ago = (iso) => {
  const mins = (Date.now() - new Date(iso).getTime()) / 60000;
  if (mins < 0) return "just now";
  if (mins < 60) return `${Math.round(mins)} min ago`;
  if (mins < 48 * 60) return `${Math.round(mins / 60)} h ago`;
  return `${Math.round(mins / 1440)} d ago`;
};

const dur = (mins) => (mins < 90 ? `${mins} min` : `${Math.floor(mins / 60)} h ${mins % 60} min`);

const rows = (pairs) =>
  "<table>" +
  pairs.filter(([, v]) => v !== undefined && v !== null)
       .map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v}</td></tr>`).join("") +
  "</table>";

// ---------------------------------------------------------------- layer definitions

/* Each layer: how to fetch it, and how to turn the FeatureCollection into Leaflet
 * layers. `heat` layers get a weight-driven heatmap; defects also get invisible
 * circle markers on top, because a heat canvas cannot carry a popup and the popup
 * is what explains *why* the point was confirmed. */

/* Heat radius is specified in METRES, not pixels.
 *
 * Leaflet.heat only takes a pixel radius, so a fixed value is wrong at every zoom but
 * one: at city zoom a 26 px radius smears 791 hundred-metre congestion cells into a
 * single blob that reads as "the whole city is jammed". Each layer declares the ground
 * extent it represents and the radius is recomputed on zoom. */
const mPerPx = () =>
  (156543.03392 * Math.cos((map.getCenter().lat * Math.PI) / 180)) / 2 ** map.getZoom();

/* `maxZoom` is not a display limit here - Leaflet.heat multiplies EVERY point's
 * intensity by 1 / 2^(maxZoom - currentZoom). Left at the tile layers' 19 that is a
 * factor of 1/16 at zoom 15, so a cell whose density is 0.94 gets drawn at 0.06, falls
 * through to the minOpacity floor, and lands on the first gradient stop. Every road in
 * the city then renders the same yellow however congested it is, and the only thing
 * that still varies the colour is how many blobs happen to overlap a pixel - which is
 * a drawing artefact, not data. Pinning it to the current zoom makes the multiplier 1,
 * so the weight the server computed is the alpha that gets painted. */
const sizeHeat = (layer) => {
  const { metres, min, max } = layer._uip;
  const r = Math.min(max, Math.max(min, metres / mPerPx()));
  layer.setOptions({ radius: r, blur: r * 0.8, maxZoom: map.getZoom() });
};

/* Marker size also has to answer to zoom. A fixed 8 px dot is a 500 m blob over a
 * city view: it covers the very road it is reporting on. These stay small at city
 * zoom and grow to a clickable size once individual streets are visible. */
const dotSize = (small, big) => {
  const z = map.getZoom();
  return z <= 12 ? small : z >= 16 ? big : small + ((big - small) * (z - 12)) / 4;
};

const sizeDots = () => {
  map.eachLayer((l) => l._uipDot && l.setRadius(dotSize(...l._uipDot)));
  // incident markers are DOM divIcons, not canvas, so they size through CSS
  document.documentElement.style.setProperty("--inc", `${dotSize(9, 17).toFixed(1)}px`);
};

const dot = (ll, small, big, style) => {
  const m = L.circleMarker(ll, { ...style, radius: dotSize(small, big) });
  m._uipDot = [small, big];
  return m;
};

const heat = (points, gradient, metres, min, max, minOpacity = 0.3) => {
  const layer = L.heatLayer(points, { minOpacity, max: 1.0, gradient,
                                     maxZoom: map.getZoom() });
  layer._uip = { metres, min, max };
  sizeHeat(layer);
  return layer;
};

map.on("zoomend", () => {
  map.eachLayer((l) => l._uip && (sizeHeat(l), l.redraw()));
  sizeDots();
  // crossing a zoom band changes the aggregation the server should be doing
  if (congestionGrid() !== gridInUse) loadLayer("congestion");
});

/* Ask the server to aggregate at a cell size this zoom can actually draw.
 *
 * 100 m cells at city zoom land ~3 px apart, closer than the smallest usable heat
 * radius, so neighbouring cells bleed together and their alphas accumulate until an
 * ordinary road looks as hot as a real jam. Requesting a coarser grid instead keeps
 * the colour an honest average of that patch of road rather than a count of how many
 * cells overlapped a pixel. */
const congestionGrid = () => {
  const z = map.getZoom();
  return z <= 11 ? 800 : z === 12 ? 400 : z === 13 ? 300 : z === 14 ? 150 : 100;
};
let gridInUse = congestionGrid();

// ---------------------------------------------------------------- route filter

/* "Show me this corridor."
 *
 * The map holds the PMPML route network as a static GeoJSON - scripts/build_routes_geojson.py
 * copies it from the same OSM fetch that drives the simulated buses - draws whichever
 * route is selected, and keeps only the features lying within a corridor of that line.
 *
 * The filter is GEOMETRIC, not by bus_id, and that is the whole point. Several PMPML
 * services share the same tarmac through Pune, so selecting 333 also shows what buses
 * on 208, 43A and 100C reported along the stretch they share with it. Filtering by which
 * route a bus is assigned to would hide exactly the cross-route corroboration the
 * confirmation rule runs on.
 *
 * It is a viewport filter in the same category as bbox: it hides features, it never
 * changes anything the server decided. The counts in the sidebar show both numbers. */

const CORRIDOR_M = 60; // GPS error, plus the defect sits ahead of the reporting bus

let ROUTES = [];   // {id, ref, name, source, ll:[[lat,lon],...]}
let route = null;  // the selected route, or null for the whole city
let routeLine = null;

/* The route line goes in its own pane so it draws over the heat canvases, and does not
 * intercept clicks meant for the defect markers underneath it. */
map.createPane("routePane");
Object.assign(map.getPane("routePane").style, { zIndex: 450, pointerEvents: "none" });

/* Equirectangular metres about a local origin. Exact enough across one city, and it
 * turns every distance test below into flat 2-D arithmetic. */
const project = (lat, lon, o) => [
  (lon - o[1]) * 111320 * Math.cos((o[0] * Math.PI) / 180),
  (lat - o[0]) * 110540,
];

/* squared distance from a point to a segment, all in projected metres */
const segDist2 = (p, a, b) => {
  const vx = b[0] - a[0], vy = b[1] - a[1];
  const wx = p[0] - a[0], wy = p[1] - a[1];
  const len2 = vx * vx + vy * vy;
  const t = len2 ? Math.max(0, Math.min(1, (wx * vx + wy * vy) / len2)) : 0;
  const dx = wx - t * vx, dy = wy - t * vy;
  return dx * dx + dy * dy;
};

const withinCorridor = (ll, tol) => {
  const p = project(ll[0], ll[1], route.origin);
  const [x0, y0, x1, y1] = route.bbox;
  if (p[0] < x0 - tol || p[0] > x1 + tol || p[1] < y0 - tol || p[1] > y1 + tol) return false;
  const tol2 = tol * tol;
  for (let i = 1; i < route.xy.length; i++)
    if (segDist2(p, route.xy[i - 1], route.xy[i]) <= tol2) return true;
  return false;
};

const filterToRoute = (features, tol) =>
  route ? features.filter((f) => withinCorridor(latlng(f), tol)) : features;

function selectRoute(id) {
  if (routeLine) { map.removeLayer(routeLine); routeLine = null; }
  route = ROUTES.find((r) => r.id === id) || null;

  if (route) {
    // project once per selection, not once per feature test
    route.origin = route.ll[0];
    route.xy = route.ll.map(([lat, lon]) => project(lat, lon, route.origin));
    const xs = route.xy.map((p) => p[0]), ys = route.xy.map((p) => p[1]);
    route.bbox = [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];

    routeLine = L.layerGroup([
      L.polyline(route.ll, { pane: "routePane", color: "#0b0d10", weight: 9, opacity: 0.5 }),
      L.polyline(route.ll, { pane: "routePane", color: "#5eead4", weight: 3, opacity: 0.95 }),
    ]).addTo(map);
    map.fitBounds(L.latLngBounds(route.ll), { padding: [40, 40] });
  }

  el("route-note").innerHTML = route
    ? `<b>${esc(route.ref)}</b> — ${route.source === "osm"
        ? "a real PMPML relation, geometry from OpenStreetMap"
        : "a plausible corridor routed over the real road network with OSRM — not a documented service"}.
       <br>Showing everything within ${CORRIDOR_M} m of this line, <b>whichever bus reported it</b>.`
    : `All ${ROUTES.length} routes. Pick one to see only what the fleet reported along its corridor.`;

  renderAll();
}

async function loadRoutes() {
  const fc = await (await fetch("routes.geojson")).json();
  ROUTES = fc.features
    .map((f) => ({ ...f.properties,
                   ll: f.geometry.coordinates.map(([lon, lat]) => [lat, lon]) }))
    .sort((a, b) => a.ref.localeCompare(b.ref, undefined, { numeric: true }));

  const sel = el("route");
  for (const r of ROUTES) {
    // "Bus 333: Hinjawadi Maan Phase 3 => Pune Station" -> the terminus pair
    const where = r.name.replace(/^Bus\s*\S*\s*:\s*/, "");
    sel.insertAdjacentHTML("beforeend",
      `<option value="${esc(r.id)}">${esc(r.ref)} · ${esc(where)}</option>`);
  }
  sel.addEventListener("change", () => selectRoute(sel.value));
  selectRoute("");
}

const LAYERS = {
  congestion: {
    label: "Congestion", kind: "aggregated", colour: "var(--c-congestion)", on: true,
    /* Congestion features are grid cells, not observations: ST_SnapToGrid moves each
     * one to a cell corner up to grid/sqrt(2) away from the road that generated it. A
     * 60 m corridor would drop cells whose traffic was measured on the selected route,
     * so the tolerance grows with whatever grid this zoom asked for. */
    corridor: () => gridInUse * 0.75 + CORRIDOR_M,
    url() {
      gridInUse = congestionGrid();
      return `${API}/layers/congestion?minutes=${el("win-congestion").value}` +
             `&collapse=true&grid_m=${gridInUse}`;
    },
    build(fc) {
      const g = L.layerGroup();
      // One blob per cell, sized to the cell, so ordinary traffic stays yellow and
      // only a genuine hotspot climbs the ramp to orange and red.
      heat(fc.features.map((f) => [...latlng(f), f.properties.weight]),
           // The weight is the 90th percentile of density in the window, not the mean -
           // see the server's `stat` parameter. Against that, the stops sit where the
           // measured spread puts them rather than at round numbers: at a 400 m grid
           // over 24 h the ramp below lands 64% of cells green, 16% yellow, 11% orange
           // and 9% red. Free flow has to read as *green* rather than as a pale version
           // of "jammed" - a traffic map whose quiet roads are faint yellow is telling
           // you the city is mildly congested everywhere, which is not what it measured.
           { 0.10: "#31c46b", 0.22: "#ffe66d", 0.45: "#ff9f1c", 0.70: "#e63946" },
           /* About two thirds of the cell, not the whole of it. Leaflet.heat draws
            * each point out to
            * radius + blur and ACCUMULATES alpha where blobs overlap, so a blob wider
            * than the gap between cells makes a quiet road climb the ramp on its
            * neighbours' backs - the colour then reports how many cells landed near a
            * pixel rather than how congested the road is. Sized to the cell, adjacent
            * blobs only just overlap: the corridor still reads as a continuous line,
            * but a green cell stays green instead of being pushed up the ramp by its
            * neighbours. */
           gridInUse * 0.62, 5, 34, 0.15).addTo(g);
      for (const f of fc.features) {
        const p = f.properties;
        dot(latlng(f), 6, 9, { stroke: false, fillOpacity: 0 })
          .bindPopup(`<div class="pop">
            <h3><span class="tag" style="background:var(--c-congestion)">congestion</span>${gridInUse} m cell</h3>
            ${rows([
              ["density, busy period", num(p.p90_density_score, 2)],
              ["density, window mean", num(p.avg_density_score, 2)],
              ["density, worst sample", num(p.peak_density_score, 2)],
              ["vehicles seen", num(p.avg_vehicle_count, 1)],
              ["bus speed", `${num(p.avg_bus_speed_kmph, 1)} km/h`],
              ["samples", p.sample_count],
              ["reporting buses", p.reporting_buses],
              ["bucket", p.bucket_start ? clock(p.bucket_start) : "whole window"],
            ])}
            <div class="why">Aggregated, not confirmed — vehicle counting is high-confidence,
            so a single bus's reading is usable directly. The colour follows the busy period
            (90th percentile), because a mean over the window buries a short jam under hours
            of free flow.</div></div>`)
          .addTo(g);
      }
      return g;
    },
  },

  potholes: {
    label: "Potholes", kind: "confirmed", colour: "var(--c-pothole)", on: true,
    url: () => `${API}/layers/potholes?status=confirmed`,
    build(fc) {
      const g = L.layerGroup();
      // a pothole is a point defect; the halo is the 20 m confirmation radius
      heat(fc.features.map((f) => [...latlng(f), Math.max(0.45, f.properties.weight)]),
           { 0.2: "#e9d5ff", 0.5: "#c77dff", 1.0: "#7b2cbf" }, 60, 12, 45).addTo(g);
      for (const f of fc.features) {
        const p = f.properties;
        const why = p.distinct_buses >= 3
          ? `${p.distinct_buses} independent buses reported this spot.`
          : `Confirmed by the repeated-pass rule: ${p.report_count} separate passes over
             ${Math.round((new Date(p.last_seen_at) - new Date(p.first_seen_at)) / 3.6e6)} h
             by ${p.distinct_buses} bus${p.distinct_buses > 1 ? "es" : ""}.`;
        dot(latlng(f), 3.5, 9, {
          color: "#c77dff", weight: 2, fillColor: "#7b2cbf", fillOpacity: 0.35,
        }).bindPopup(`<div class="pop">
            <h3><span class="tag" style="background:var(--c-pothole)">pothole</span>${esc(p.severity)} severity</h3>
            ${rows([
              ["distinct buses", `<b>${p.distinct_buses}</b>`],
              ["reports", p.report_count],
              ["avg confidence", num(p.avg_confidence, 2)],
              ["first seen", clock(p.first_seen_at)],
              ["last seen", `${clock(p.last_seen_at)}`],
              ["status", esc(p.status)],
            ])}
            <div class="why">${why}</div></div>`).addTo(g);
      }
      return g;
    },
  },

  waterlogging: {
    label: "Waterlogging", kind: "confirmed", colour: "var(--c-water)", on: true,
    url: () => `${API}/layers/waterlogging?status=confirmed`,
    build(fc) {
      const g = L.layerGroup();
      heat(fc.features.map((f) => [...latlng(f), Math.max(0.45, f.properties.weight)]),
           { 0.2: "#bae6fd", 0.5: "#38bdf8", 1.0: "#0369a1" }, 60, 12, 45).addTo(g);
      for (const f of fc.features) {
        const p = f.properties;
        dot(latlng(f), 3.5, 9, {
          color: "#38bdf8", weight: 2, fillColor: "#0369a1", fillOpacity: 0.35,
        }).bindPopup(`<div class="pop">
            <h3><span class="tag" style="background:var(--c-water)">waterlogging</span>${num(p.avg_coverage_pct, 0)}% lane</h3>
            ${rows([
              ["distinct buses", `<b>${p.distinct_buses}</b>`],
              ["reports", p.report_count],
              ["avg confidence", num(p.avg_confidence, 2)],
              ["first seen", clock(p.first_seen_at)],
              ["last seen", `${clock(p.last_seen_at)} · ${ago(p.last_seen_at)}`],
              ["status", esc(p.status)],
            ])}
            <div class="why">Transient: confirmed on a 3 h window and expired 6 h after the
            last report, so yesterday's flood does not linger on the map.</div></div>`).addTo(g);
      }
      return g;
    },
  },

  incidents: {
    label: "Incidents", kind: "realtime", colour: "var(--c-accident)", on: true,
    url: () => `${API}/layers/incidents?hours=${el("win-incidents").value}`,
    build(fc) {
      const g = L.layerGroup();
      for (const f of fc.features) {
        const p = f.properties;
        const plates = (p.vehicles_involved || []).length
          ? p.vehicles_involved.map((v) =>
              `<div><span class="plate">${esc(v.plate_number)}</span>
               ${esc(v.vehicle_class)} · ${num(v.plate_confidence, 2)} · ${esc(v.camera_id)}</div>`).join("")
          : '<div style="color:var(--muted)">no plates read</div>';
        // `timestamp` is the bus clock, `received_at` is when the upload landed. They
        // differ by however long the bus was buffering - and by the whole simulated day
        // after a backfill, which dumps 24 h of events at once.
        const lag = Math.round((new Date(p.received_at) - new Date(p.timestamp)) / 60000);
        L.marker(latlng(f), {
          icon: L.divIcon({ className: "", html: `<div class="inc ${esc(p.subtype)}"></div>`,
                            iconSize: [15, 15], iconAnchor: [7, 7] }),
          zIndexOffset: 1000,
        }).bindPopup(`<div class="pop">
            <h3><span class="tag" style="background:${p.subtype === "accident" ? "var(--c-accident)" : "var(--c-rash)"}">${esc(p.subtype.replace("_", " "))}</span>${esc(p.bus_id)}</h3>
            ${rows([
              ["confidence", num(p.confidence, 2)],
              ["anomaly score", num(p.trajectory_anomaly_score, 2)],
              ["IMU impact", p.impact_signature ? `${num(p.impact_signature, 2)} g` : null],
              ["cameras", esc((p.contributing_camera_ids || []).join(", "))],
              ["detected", clock(p.timestamp)],
              ["received", `${clock(p.received_at)}${lag > 1 ? ` (+${dur(lag)})` : ""}`],
            ])}
            <div class="why" style="color:var(--text)"><b>ANPR — vehicles involved</b>${plates}</div>
            <div class="why">media_ref <span class="plate">${esc(p.media_ref || "—")}</span><br>
            Detected on the bus clock, received when the upload landed — the two differ
            whenever the bus was buffering offline. Sent on the realtime lane, bypassing
            the batch buffer, and rendered without confirmation: one high-confidence
            report is a case, not a density point.</div></div>`).addTo(g);
      }
      return g;
    },
  },

  pending: {
    label: "Pending potholes", kind: "ops", colour: "var(--c-pending)", on: false,
    url: () => `${API}/ops/pending/pothole?limit=2000`,
    build(fc) {
      const g = L.layerGroup();
      for (const f of fc.features) {
        const p = f.properties;
        dot(latlng(f), 2, 5, {
          color: "#8b98a5", weight: 1, dashArray: "2,2", fillOpacity: 0,
        }).bindPopup(`<div class="pop">
            <h3><span class="tag" style="background:var(--c-pending)">pending</span>uncorroborated</h3>
            ${rows([
              ["bus", esc(p.bus_id)],
              ["reported", clock(p.timestamp)],
              ["confidence", num(p.confidence, 2)],
              ["severity", esc(p.extra?.severity)],
              ["est. range", p.extra?.est_range_m ? `${num(p.extra.est_range_m, 1)} m` : null],
            ])}
            <div class="why">One bus's opinion. It stays off the public map until three
            distinct buses agree, or one bus reports it on four passes spanning six hours.</div></div>`)
          .addTo(g);
      }
      return g;
    },
  },
};

// ---------------------------------------------------------------- sidebar wiring

const box = el("layers");
for (const [key, def] of Object.entries(LAYERS)) {
  def.group = null;
  box.insertAdjacentHTML("beforeend", `
    <label class="layer" style="--swatch:${def.colour}">
      <input type="checkbox" data-key="${key}" ${def.on ? "checked" : ""}>
      <span class="box"></span>
      <span class="name">${def.label}</span>
      <span class="kind">${def.kind}</span>
      <span class="count" id="count-${key}">—</span>
    </label>`);
}

box.addEventListener("change", (e) => {
  const key = e.target.dataset.key;
  if (!key) return;
  LAYERS[key].on = e.target.checked;
  // re-render rather than add/remove the existing group: the route filter has to be
  // applied to it, and the cached response makes that free
  LAYERS[key].fc ? renderLayer(key) : refresh();
});

// ---------------------------------------------------------------- refresh

let busy = false;

function setStatus(text, isError = false) {
  el("status").className = isError ? "err" : "";
  el("status").innerHTML = `<span class="dot${isError ? "" : " on"}"></span>${esc(text)}`;
}

async function loadLayer(key) {
  const def = LAYERS[key];
  if (!def.on) { def.fc = null; return renderLayer(key); }
  def.fc = await (await fetch(def.url())).json();
  return renderLayer(key);
}

/* Rebuild a layer from the response already in hand. Changing the route filter is a
 * client-side operation - the server sent the whole city and the filter only decides
 * what gets drawn - so it must not cost a round trip per layer. */
function renderLayer(key) {
  const def = LAYERS[key];
  const counter = el(`count-${key}`);
  if (def.group) { map.removeLayer(def.group); def.group = null; }
  if (!def.on || !def.fc) { counter.textContent = "—"; return 0; }

  const all = def.fc.features;
  const shown = filterToRoute(all, def.corridor ? def.corridor() : CORRIDOR_M);
  def.group = def.build({ ...def.fc, features: shown }).addTo(map);
  counter.textContent = route && shown.length !== all.length
    ? `${shown.length}/${all.length}` : shown.length;
  return shown.length;
}

const renderAll = () => Object.keys(LAYERS).forEach(renderLayer);

async function loadStats() {
  const s = await (await fetch(`${API}/ops/stats`)).json();
  const raw = s.raw_events || {};
  const total = Object.values(raw).reduce((a, r) => a + r.total, 0);
  const buses = Math.max(0, ...Object.values(raw).map((r) => r.reporting_buses || 0));
  const suppressed = Object.values(raw).reduce((a, r) => a + (r.cooldown_suppressed || 0), 0);
  const pot = s.confirmed_potholes || {};
  const wat = s.confirmed_waterlogging || {};
  const t = s.thresholds || {};

  el("stats").innerHTML = `
    ${[["Buses reporting", buses],
       ["Raw events ingested", total.toLocaleString()],
       ["Repeat reports suppressed", suppressed.toLocaleString()],
       ["Potholes confirmed", `${pot.confirmed || 0}${pot.resolved ? ` (+${pot.resolved} resolved)` : ""}`],
       ["Waterlogging confirmed", `${wat.confirmed || 0}${wat.expired ? ` (+${wat.expired} expired)` : ""}`],
      ].map(([k, v]) => `<div class="stat"><span>${k}</span><b>${v}</b></div>`).join("")}
    <div class="note">Confirmation: ${t.pothole?.min_confirmations ?? "?"} distinct buses within
    ${t.pothole?.radius_m ?? "?"} m for potholes, ${t.waterlogging?.min_confirmations ?? "?"} within
    ${t.waterlogging?.window_hours ?? "?"} h for waterlogging. ${total.toLocaleString()} raw events
    became ${(pot.confirmed || 0) + (wat.confirmed || 0)} confirmed defects.</div>`;
}

async function refresh() {
  if (busy) return;
  busy = true;
  setStatus("refreshing…");
  try {
    await Promise.all([...Object.keys(LAYERS).map(loadLayer), loadStats()]);
    setStatus(`updated ${new Date().toLocaleTimeString("en-IN", { hour12: false })}`);
  } catch (err) {
    setStatus(`server unreachable — ${err.message}`, true);
  } finally {
    busy = false;
  }
}

let timer = null;
function setAuto() {
  clearInterval(timer);
  const ms = Number(el("auto").value);
  if (ms) timer = setInterval(refresh, ms);
}

el("refresh").addEventListener("click", refresh);
el("auto").addEventListener("change", setAuto);
el("win-congestion").addEventListener("change", () => loadLayer("congestion"));
el("win-incidents").addEventListener("change", () => loadLayer("incidents"));

sizeDots();
loadRoutes().catch(() => {
  el("route-note").textContent =
    "routes.geojson is missing — run scripts/build_routes_geojson.py";
});
refresh();
setAuto();
