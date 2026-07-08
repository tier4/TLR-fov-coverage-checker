"use strict";

const STATUS_COLOR = {
  covered: "#2ca02c",
  facing_away: "#ff7f0e",
  out_of_fov: "#d62728",
};
// Worst case drawn last (on top), matching visualizer.py's convention.
const STATUS_DRAW_ORDER = ["covered", "facing_away", "out_of_fov"];

const TYPE_COLOR = {
  vehicle: "gold",
  pedestrian: "#00bcd4",
  unknown: "silver",
};

// Fallback housing size [m] for the camera view's apparent-size boxes,
// used only when a light's own panel dimensions are missing from the map.
// The map normally carries the real per-light size (the `refers` panel
// way spans the housing width, its `height` tag gives the vertical size
// -- every signal on the bundled map has both), which the API delivers
// as panel_width/panel_height per candidate. These constants are the
// typical Japanese housings: horizontal 3-lamp vehicle (~1.25x0.45m) and
// vertical 2-lamp pedestrian (~0.45x0.9m).
const SIGNAL_HOUSING_M = {
  vehicle: { w: 1.25, h: 0.45 },
  pedestrian: { w: 0.45, h: 0.9 },
  unknown: { w: 0.45, h: 0.45 },
};

// Lens diameter [m] for drawing individual lamps in the camera view. The
// map gives each bulb's exact position and color/arrow tags but not the
// lens size; 0.3m is the standard Japanese 300mm lens. A stated guess,
// unlike the lamp positions themselves.
const LENS_DIAMETER_M = 0.3;
const LAMP_COLOR = {
  red: "#ff4136",
  yellow: "#ffdc00",
  green: "#2ecc71",
};

const mapCanvas = document.getElementById("map-canvas");
const mapCtx = mapCanvas.getContext("2d");
const frameCanvas = document.getElementById("frame-canvas");
const frameCtx = frameCanvas.getContext("2d");
const metaEl = document.getElementById("meta");
const pointInfoEl = document.getElementById("point-info");
const candidateTbody = document.querySelector("#candidate-table tbody");
const pointSizeInput = document.getElementById("point-size");
const pointSizeValueEl = document.getElementById("point-size-value");
const copyLinkBtn = document.getElementById("copy-link-btn");
const gmapLink = document.getElementById("gmap-link");
const streetviewLink = document.getElementById("streetview-link");
const loadSnapshotInput = document.getElementById("load-snapshot-input");
const loadMapInput = document.getElementById("load-map-input");
const dataStatusEl = document.getElementById("data-status");
const tabButtons = document.querySelectorAll(".tab-btn");
const tabCamera = document.getElementById("tab-camera");
const tabPatterns = document.getElementById("tab-patterns");
const patternFilterInput = document.getElementById("pattern-filter");
const patternSummaryEl = document.getElementById("pattern-summary");
const patternTbody = document.querySelector("#pattern-table tbody");
const colorModeSelect = document.getElementById("color-mode");

let points = [];
let trafficLights = [];
let selectedPointId = null;
let currentDetail = null;
let cameraSpec = null; // first camera of the rig (shared-frame reference)
let metaCameras = []; // every camera: name/fov/range/yaw_offset/pitch_offset

// per-camera accent colors for frustums and FOV rectangles; deliberately
// avoids the status palette (green/orange/red) so the two never collide
const CAMERA_COLORS = ["#1f77b4", "#e377c2", "#17becf", "#9467bd", "#8c564b"];
const cameraColor = (i) => CAMERA_COLORS[i % CAMERA_COLORS.length];
// {lat: [a,b,c], lon: [d,e,f]} affine fit from /api/meta, or null if the
// map's nodes carried no lat/lon attributes
let latlonTransform = null;
// target_tl_id -> status color, for whichever point is currently selected
let highlightedLights = new Map();
let pointSizeScale = 1.0;
// map dot coloring: "status" (coverage verdict) | "redundancy" (min visible heads)
let colorMode = "status";
// right-pane tab state + pattern catalog (fetched lazily on first open)
let activeTab = "camera";
let patternsData = null;
let selectedPatternSig = null;
let patternHighlightHeads = [];

function worldToLatLon(x, y) {
  if (!latlonTransform) return null;
  const { lat, lon } = latlonTransform;
  return [lat[0] * x + lat[1] * y + lat[2], lon[0] * x + lon[1] * y + lon[2]];
}

// ---- aerial photo underlay (GSI seamless photo tiles) ----
// GSI (Geospatial Information Authority of Japan) XYZ tiles: free to use
// with attribution, no API key -- unlike Google's imagery, whose terms
// require going through their SDK. Attribution is shown next to the toggle.
const aerialToggle = document.getElementById("aerial-toggle");
const aerialAttributionEl = document.getElementById("aerial-attribution");
let aerialEnabled = false;
const tileCache = new Map(); // "z/x/y" -> HTMLImageElement (may still be loading)
const TILE_SIZE = 256;
const tileUrl = (z, x, y) => `https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/${z}/${x}/${y}.jpg`;

function latLonToMercPx(lat, lon, z) {
  const n = TILE_SIZE * Math.pow(2, z);
  const mx = ((lon + 180) / 360) * n;
  const rad = (lat * Math.PI) / 180;
  const my = ((1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2) * n;
  return [mx, my];
}

function getTile(z, x, y) {
  const key = `${z}/${x}/${y}`;
  let img = tileCache.get(key);
  if (!img) {
    if (tileCache.size > 400) tileCache.clear(); // crude but sufficient cap
    img = new Image();
    // No crossOrigin: the GSI server sends no CORS headers, so requesting
    // anonymously would make every tile fail outright. Drawing a
    // non-CORS image merely taints the canvas, which we never read back.
    img.src = tileUrl(z, x, y);
    img.onload = () => renderMap(); // repaint as tiles trickle in
    tileCache.set(key, img);
  }
  return img;
}

function drawAerial(ctx) {
  if (!aerialEnabled || !latlonTransform) return;
  const w = mapCanvas.width, h = mapCanvas.height;

  // pick the zoom whose ground resolution best matches the current view
  const [ccx, ccy] = screenToWorld(w / 2, h / 2);
  const centerLL = worldToLatLon(ccx, ccy);
  const metersPerScreenPx = 1 / view.scale;
  const groundResZ0 = 156543.03392 * Math.cos((centerLL[0] * Math.PI) / 180);
  let z = Math.round(Math.log2(groundResZ0 / metersPerScreenPx));
  z = Math.max(2, Math.min(18, z));

  // Affine mercator-pixel -> screen from three world-space correspondences.
  // The local frame can be slightly rotated relative to true north (the
  // affine lat/lon fit captures that), so tiles must be drawn through a
  // full 2x2 transform, not just scaled+translated. Mercator's remaining
  // nonlinearity across a city-scale view is far below a pixel.
  const worldRefs = [[ccx, ccy], [ccx + 100, ccy], [ccx, ccy + 100]];
  const mercRefs = worldRefs.map(([x, y]) => { const ll = worldToLatLon(x, y); return latLonToMercPx(ll[0], ll[1], z); });
  const screenRefs = worldRefs.map(([x, y]) => worldToScreen(x, y));
  const [m0, m1, m2] = mercRefs;
  const [s0, s1, s2] = screenRefs;
  const det = (m1[0] - m0[0]) * (m2[1] - m0[1]) - (m2[0] - m0[0]) * (m1[1] - m0[1]);
  if (Math.abs(det) < 1e-12) return;
  const a = ((s1[0] - s0[0]) * (m2[1] - m0[1]) - (s2[0] - s0[0]) * (m1[1] - m0[1])) / det;
  const c = ((s2[0] - s0[0]) * (m1[0] - m0[0]) - (s1[0] - s0[0]) * (m2[0] - m0[0])) / det;
  const b = ((s1[1] - s0[1]) * (m2[1] - m0[1]) - (s2[1] - s0[1]) * (m1[1] - m0[1])) / det;
  const d = ((s2[1] - s0[1]) * (m1[0] - m0[0]) - (s1[1] - s0[1]) * (m2[0] - m0[0])) / det;
  const e = s0[0] - a * m0[0] - c * m0[1];
  const f = s0[1] - b * m0[0] - d * m0[1];

  // visible tile range: canvas corners -> mercator px -> tile indices
  let minMx = Infinity, maxMx = -Infinity, minMy = Infinity, maxMy = -Infinity;
  for (const [sx, sy] of [[0, 0], [w, 0], [0, h], [w, h]]) {
    const [wx, wy] = screenToWorld(sx, sy);
    const ll = worldToLatLon(wx, wy);
    const [mx, my] = latLonToMercPx(ll[0], ll[1], z);
    minMx = Math.min(minMx, mx); maxMx = Math.max(maxMx, mx);
    minMy = Math.min(minMy, my); maxMy = Math.max(maxMy, my);
  }
  const maxTile = Math.pow(2, z) - 1;
  const tx0 = Math.max(0, Math.floor(minMx / TILE_SIZE)), tx1 = Math.min(maxTile, Math.floor(maxMx / TILE_SIZE));
  const ty0 = Math.max(0, Math.floor(minMy / TILE_SIZE)), ty1 = Math.min(maxTile, Math.floor(maxMy / TILE_SIZE));
  if ((tx1 - tx0 + 1) * (ty1 - ty0 + 1) > 150) return; // absurd range = bad transform, bail

  ctx.save();
  ctx.setTransform(a, b, c, d, e, f);
  for (let ty = ty0; ty <= ty1; ty++) {
    for (let tx = tx0; tx <= tx1; tx++) {
      const img = getTile(z, tx, ty);
      if (img.complete && img.naturalWidth > 0) {
        ctx.drawImage(img, tx * TILE_SIZE, ty * TILE_SIZE, TILE_SIZE, TILE_SIZE);
      }
    }
  }
  ctx.restore();

  // soften the photo so the coverage dots stay the dominant signal
  ctx.fillStyle = "rgba(255, 255, 255, 0.25)";
  ctx.fillRect(0, 0, w, h);
}

// Shade for a covered waypoint by its weakest group's visible-head
// fraction: ratio 1.0 = the standard covered green, lower = paler.
// Interpolation endpoints are easy to retune if the gradation needs a
// different emphasis (or swap this for a shape change instead).
function coveredShade(ratio) {
  const t = Math.max(0, Math.min(1, ratio));
  const lerp = (a, b) => Math.round(a + (b - a) * t);
  return `rgb(${lerp(186, 44)}, ${lerp(228, 160)}, ${lerp(179, 44)})`;
}

// Redundancy palette: the *absolute* min visible head count across a
// waypoint's groups. Distinct from the ratio shading above on purpose --
// 1 of 1 heads is 100% covered but has zero redundancy (one occluded or
// dirty head and the signal state is gone). Discrete colors because the
// quantity is discrete: 0=blind, 1=no margin, 2/3/4+ = real redundancy.
const REDUNDANCY_COLOR = ["#d62728", "#ff7f0e", "#a6d96a", "#1a9641", "#00584a"];
function redundancyColor(minVisible) {
  return REDUNDANCY_COLOR[Math.max(0, Math.min(REDUNDANCY_COLOR.length - 1, minVisible))];
}

// world <-> screen transform state for the map pane
const view = { scale: 1, offsetX: 0, offsetY: 0 };

// zoom/pan state for the camera-view (frame) pane, reset on each new point pick
const frameView = { zoom: 1, panX: 0, panY: 0 };

function resizeCanvases() {
  for (const c of [mapCanvas, frameCanvas]) {
    const rect = c.getBoundingClientRect();
    // a canvas inside a hidden tab measures 0x0 -- keep its previous
    // pixel buffer instead of wiping it, and fix it up on tab switch
    if (rect.width === 0 || rect.height === 0) continue;
    c.width = rect.width * devicePixelRatio;
    c.height = rect.height * devicePixelRatio;
  }
}

function fitViewToData() {
  // Plain loops, not Math.min(...bigArray): spreading 100k+ elements as call
  // arguments blows V8's call-stack/argument limit ("Maximum call stack size
  // exceeded"), silently aborting main() before anything ever renders.
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const p of points) {
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }
  for (const t of trafficLights) {
    if (t.x < minX) minX = t.x;
    if (t.x > maxX) maxX = t.x;
    if (t.y < minY) minY = t.y;
    if (t.y > maxY) maxY = t.y;
  }
  if (!Number.isFinite(minX)) { minX = maxX = minY = maxY = 0; }
  const w = mapCanvas.width, h = mapCanvas.height;
  const dataW = maxX - minX || 1, dataH = maxY - minY || 1;
  const pad = 0.05;
  const scale = Math.min(w / (dataW * (1 + pad)), h / (dataH * (1 + pad)));
  view.scale = scale;
  // world Y increases north/up, canvas Y increases down -> flip
  view.offsetX = w / 2 - scale * (minX + maxX) / 2;
  view.offsetY = h / 2 + scale * (minY + maxY) / 2;
}

function worldToScreen(x, y) {
  return [x * view.scale + view.offsetX, -y * view.scale + view.offsetY];
}

function screenToWorld(sx, sy) {
  return [(sx - view.offsetX) / view.scale, -(sy - view.offsetY) / view.scale];
}

// A traffic light marker: a triangle pointing in its facing_yaw direction
// (bearing from the bulb centroid toward its stop line, i.e. the
// direction the signal shines/points), or a plain circle when facing_yaw
// is unknown (no ref_line in the map -- normally a pedestrian signal).
// One solid filled shape rather than a separate thin arrow overlay: a
// thin line degrades badly at small sizes/low zoom (sub-pixel width,
// easy to lose against the background), while a filled wedge stays
// legible and never "collapses" the way a 1px line can.
//
// facing_yaw is a world-space bearing; converting it to a screen-space
// direction only needs negating the angle (screen y is flipped relative
// to world y, and there's no rotation between the two), so this is done
// directly rather than round-tripping through worldToScreen.
function drawLightMarker(ctx, sx, sy, r, facingYawDeg, fillColor, strokeColor, strokeWidth) {
  ctx.beginPath();
  if (facingYawDeg === null || facingYawDeg === undefined) {
    ctx.arc(sx, sy, r, 0, 2 * Math.PI);
  } else {
    const dirAngle = -(facingYawDeg * Math.PI) / 180;
    const tipLen = r * 1.7;
    const backLen = r * 1.05;
    const backSpread = (140 * Math.PI) / 180;
    const vertex = (angle, len) => [sx + len * Math.cos(angle), sy + len * Math.sin(angle)];
    const [tx, ty] = vertex(dirAngle, tipLen);
    const [lx, ly] = vertex(dirAngle + backSpread, backLen);
    const [rx, ry] = vertex(dirAngle - backSpread, backLen);
    ctx.moveTo(tx, ty);
    ctx.lineTo(lx, ly);
    ctx.lineTo(rx, ry);
    ctx.closePath();
  }
  ctx.fillStyle = fillColor;
  ctx.fill();
  ctx.strokeStyle = strokeColor;
  ctx.lineWidth = strokeWidth;
  ctx.stroke();
}

// hex "#rrggbb" -> "rgba(r,g,b,alpha)"
function withAlpha(hex, alpha) {
  const r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16), b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

// One camera's frustum: heading is the direction of travel, the camera's
// own axis adds its mounting yaw_offset.
function drawFrustum(ctx, point, headingDeg, cam, color) {
  const yaw = ((headingDeg + (cam.yaw_offset || 0)) * Math.PI) / 180;
  const half = (cam.fov_h / 2) * (Math.PI / 180);
  const a0 = yaw - half, a1 = yaw + half;

  const atAngle = (angle, dist) => worldToScreen(point.x + dist * Math.cos(angle), point.y + dist * Math.sin(angle));

  const steps = 24;
  const outerPts = [], innerPts = [];
  for (let i = 0; i <= steps; i++) {
    const a = a0 + ((a1 - a0) * i) / steps;
    outerPts.push(atAngle(a, cam.max_range));
    innerPts.push(atAngle(a, cam.min_range));
  }

  ctx.save();
  ctx.beginPath();
  ctx.moveTo(outerPts[0][0], outerPts[0][1]);
  for (const [x, y] of outerPts.slice(1)) ctx.lineTo(x, y);
  for (const [x, y] of innerPts.reverse()) ctx.lineTo(x, y);
  ctx.closePath();
  ctx.fillStyle = withAlpha(color, 0.12);
  ctx.fill();
  ctx.strokeStyle = withAlpha(color, 0.8);
  ctx.lineWidth = 1.5;
  ctx.stroke();

  // dashed axis line down the middle of the frustum
  const [sx, sy] = worldToScreen(point.x, point.y);
  const [hx, hy] = atAngle(yaw, cam.max_range);
  ctx.beginPath();
  ctx.moveTo(sx, sy);
  ctx.lineTo(hx, hy);
  ctx.setLineDash([5, 4]);
  ctx.strokeStyle = withAlpha(color, 0.9);
  ctx.lineWidth = 1;
  ctx.stroke();

  // camera name at the outer edge of the axis, so overlapping frustums
  // stay attributable
  ctx.setLineDash([]);
  ctx.fillStyle = withAlpha(color, 0.95);
  ctx.font = "11px sans-serif";
  ctx.fillText(cam.name, hx + 4, hy - 4);
  ctx.restore();
}

function renderMap() {
  const ctx = mapCtx;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, mapCanvas.width, mapCanvas.height);

  drawAerial(ctx);

  const dotSize = Math.max(1, Math.min(2.2, view.scale * 0.15)) * pointSizeScale;

  if (colorMode === "redundancy") {
    // discrete buckets by min visible heads, worst drawn last (on top)
    const buckets = [[], [], [], [], []];
    for (const p of points) {
      const n = Math.max(0, Math.min(4, p.min_heads_visible ?? 0));
      buckets[n].push(p);
    }
    for (let n = 4; n >= 0; n--) {
      if (!buckets[n].length) continue;
      ctx.fillStyle = redundancyColor(n);
      for (const p of buckets[n]) {
        const [sx, sy] = worldToScreen(p.x, p.y);
        ctx.fillRect(sx - dotSize / 2, sy - dotSize / 2, dotSize, dotSize);
      }
    }
    drawMapOverlays(ctx, dotSize);
    return;
  }

  const byStatus = { covered: [], facing_away: [], out_of_fov: [] };
  for (const p of points) byStatus[p.status].push(p);

  for (const status of STATUS_DRAW_ORDER) {
    if (status === "covered") {
      // graded green: a covered point's shade encodes its weakest group's
      // visible-head fraction (pale = 1 of many heads doing all the work,
      // saturated = every head visible). Bucketed to 5 shades so ~100k
      // points still draw in a handful of fillStyle changes.
      const buckets = [[], [], [], [], []];
      for (const p of byStatus.covered) {
        const ratio = p.heads_total > 0 ? p.heads_visible / p.heads_total : 1;
        buckets[Math.max(0, Math.min(4, Math.round(ratio * 4)))].push(p);
      }
      for (let b = 0; b < 5; b++) {
        if (!buckets[b].length) continue;
        ctx.fillStyle = coveredShade(b / 4);
        for (const p of buckets[b]) {
          const [sx, sy] = worldToScreen(p.x, p.y);
          ctx.fillRect(sx - dotSize / 2, sy - dotSize / 2, dotSize, dotSize);
        }
      }
      continue;
    }
    ctx.fillStyle = STATUS_COLOR[status];
    for (const p of byStatus[status]) {
      const [sx, sy] = worldToScreen(p.x, p.y);
      ctx.fillRect(sx - dotSize / 2, sy - dotSize / 2, dotSize, dotSize);
    }
  }

  drawMapOverlays(ctx, dotSize);
}

// everything drawn above the waypoint dots, shared by both color modes:
// frustum, light markers, pattern rings, selection ring
function drawMapOverlays(ctx, dotSize) {
  if (selectedPointId !== null && currentDetail && metaCameras.length) {
    metaCameras.forEach((cam, i) =>
      drawFrustum(ctx, points[selectedPointId], currentDetail.cam_yaw, cam, cameraColor(i))
    );
  }

  const markerR = Math.max(3, Math.min(9, view.scale * 3));
  const highlightR = markerR * 1.8;

  // plain markers first, highlighted ones (candidates of the selected
  // point) drawn last/on top so they're never hidden by an overlapping
  // neighbor. Color encodes signal_type normally; a highlighted marker's
  // color instead encodes that candidate's status, but keeps its shape
  // (triangle/circle) so facing direction stays visible either way.
  const highlighted = [];
  for (const tl of trafficLights) {
    if (highlightedLights.has(tl.id)) { highlighted.push(tl); continue; }
    const [sx, sy] = worldToScreen(tl.x, tl.y);
    const fillColor = TYPE_COLOR[tl.signal_type] || TYPE_COLOR.unknown;
    drawLightMarker(ctx, sx, sy, markerR, tl.facing_yaw, fillColor, "black", 0.6);
  }
  for (const tl of highlighted) {
    const [sx, sy] = worldToScreen(tl.x, tl.y);
    const color = highlightedLights.get(tl.id);
    drawLightMarker(ctx, sx, sy, highlightR, tl.facing_yaw, color, "black", 2.2);
  }

  // pattern-catalog highlight: magenta rings around every head matching
  // the selected pattern row, drawn only while the Patterns tab is open
  // so the two highlight vocabularies never mix on screen
  if (activeTab === "patterns" && patternHighlightHeads.length) {
    ctx.strokeStyle = "#d033ff";
    ctx.lineWidth = 2.5;
    const ringR = Math.max(7, markerR * 2);
    for (const head of patternHighlightHeads) {
      const [sx, sy] = worldToScreen(head.x, head.y);
      ctx.beginPath();
      ctx.arc(sx, sy, ringR, 0, 2 * Math.PI);
      ctx.stroke();
    }
  }

  if (selectedPointId !== null) {
    const p = points[selectedPointId];
    const [sx, sy] = worldToScreen(p.x, p.y);
    ctx.beginPath();
    ctx.arc(sx, sy, 7, 0, 2 * Math.PI);
    ctx.strokeStyle = "#1f77b4";
    ctx.lineWidth = 2;
    ctx.stroke();
  }
}

function findNearestPoint(worldX, worldY) {
  let best = null, bestDist = Infinity;
  for (const p of points) {
    const dx = p.x - worldX, dy = p.y - worldY;
    const d = dx * dx + dy * dy;
    if (d < bestDist) { bestDist = d; best = p; }
  }
  return best;
}

function updateUrlForPoint(point) {
  // Encoded by (lane_id, x, y) rather than the point's array index: that
  // index is just an insertion-order artifact of this particular run, so a
  // link built from it could point at the wrong waypoint after a rerun
  // with different filtering. The physical (lane, location) identity is
  // stable across any run of the same map/camera spec.
  const url = new URL(window.location.href);
  url.searchParams.set("lane", point.lane_id);
  url.searchParams.set("x", point.x);
  url.searchParams.set("y", point.y);
  history.replaceState(null, "", url);
  copyLinkBtn.disabled = false;
}

function findPointFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const lane = params.get("lane");
  const x = parseFloat(params.get("x"));
  const y = parseFloat(params.get("y"));
  if (lane === null || Number.isNaN(x) || Number.isNaN(y)) return null;

  let best = null, bestDist = Infinity;
  for (const p of points) {
    if (p.lane_id !== lane) continue;
    const d = (p.x - x) ** 2 + (p.y - y) ** 2;
    if (d < bestDist) { bestDist = d; best = p; }
  }
  // require a close match (1cm) -- a lane/coordinate typo shouldn't silently
  // jump to some unrelated point on the same lane.
  return bestDist < 0.01 ? best : null;
}

async function selectPoint(pointId) {
  selectedPointId = pointId;
  currentDetail = null;
  highlightedLights = new Map();
  frameView.zoom = 1;
  frameView.panX = 0;
  frameView.panY = 0;
  updateUrlForPoint(points[pointId]);
  renderMap(); // selection ring right away; frustum/highlights follow once detail arrives

  const res = await fetch(`/api/points/${pointId}/candidates`);
  if (!res.ok) return;
  const detail = await res.json();

  currentDetail = detail;
  // Colored by the light's *group* status, not just its own is_covered: a
  // redundant head that isn't itself visible still highlights green if a
  // sibling head sharing its stop line (group_id) is -- matching the
  // point's own group-aware status instead of implying you can see every
  // highlighted star directly.
  highlightedLights = new Map(
    detail.candidates.map((c) => [
      c.target_tl_id,
      c.group_covered ? STATUS_COLOR.covered : c.in_fov ? STATUS_COLOR.facing_away : STATUS_COLOR.out_of_fov,
    ])
  );

  renderMap();
  renderFrame(detail);
  renderPointInfo(detail);
  renderCandidateTable(detail);
}

function renderPointInfo(detail) {
  const p = detail.point;
  const sel = selectedPointId !== null ? points[selectedPointId] : null;
  const headsNote = sel && sel.heads_total > 0
    ? `  |  weakest group: ${sel.heads_visible}/${sel.heads_total} heads visible  |  redundancy: ${sel.min_heads_visible ?? "?"}`
    : "";
  // per-camera visible-head totals across all candidates at this point
  let perCameraNote = "";
  if (metaCameras.length > 1) {
    const byCam = new Map(metaCameras.map((c) => [c.name, 0]));
    for (const c of detail.candidates) {
      if (byCam.has(c.camera_name)) byCam.set(c.camera_name, byCam.get(c.camera_name) + c.heads_visible);
    }
    perCameraNote = "  |  " + [...byCam.entries()].map(([name, n]) => `${name}: ${n}`).join("  ");
  }
  const lightCount = new Set(detail.candidates.map((c) => c.target_tl_id)).size;
  pointInfoEl.textContent =
    `lane ${p.lane_id} @ (${p.x.toFixed(1)}, ${p.y.toFixed(1)})  |  ` +
    `cam_yaw=${detail.cam_yaw.toFixed(1)}deg  |  ` +
    `${lightCount} candidate light(s)  |  ` +
    `status=${detail.status}${perCameraNote}${headsNote}`;

  const ll = worldToLatLon(p.x, p.y);
  if (ll) {
    const [lat, lon] = ll;
    gmapLink.href = `https://www.google.com/maps?q=${lat.toFixed(7)},${lon.toFixed(7)}`;
    streetviewLink.href =
      `https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=${lat.toFixed(7)},${lon.toFixed(7)}` +
      `&heading=${detail.cam_yaw !== null ? (90 - detail.cam_yaw).toFixed(1) : 0}`;
    gmapLink.hidden = false;
    streetviewLink.hidden = false;
  } else {
    gmapLink.hidden = true;
    streetviewLink.hidden = true;
  }
}

function renderCandidateTable(detail) {
  candidateTbody.innerHTML = "";
  for (const c of detail.candidates) {
    const tr = document.createElement("tr");
    const cell = (v, cls) => {
      const td = document.createElement("td");
      td.textContent = v;
      if (cls) td.className = cls;
      return td;
    };
    tr.appendChild(cell(c.target_tl_id));
    tr.appendChild(cell(c.camera_name || "-"));
    tr.appendChild(cell(c.signal_type));
    tr.appendChild(cell(c.distance_m.toFixed(1)));
    tr.appendChild(cell(c.yaw_diff.toFixed(1)));
    tr.appendChild(cell(c.pitch_diff.toFixed(1)));
    tr.appendChild(cell(c.in_fov ? "yes" : "no", c.in_fov ? "status-true" : "status-false"));
    tr.appendChild(cell(c.facing_camera ? "yes" : "no", c.facing_camera ? "status-true" : "status-false"));
    tr.appendChild(cell(c.is_covered ? "yes" : "no", c.is_covered ? "status-true" : "status-false"));
    tr.appendChild(
      cell(
        `${c.heads_visible}/${c.heads_total}`,
        c.heads_visible === c.heads_total ? "status-true" : c.heads_visible > 0 ? "" : "status-false"
      )
    );
    tr.appendChild(cell(c.group_covered ? "yes" : "no", c.group_covered ? "status-true" : "status-false"));
    candidateTbody.appendChild(tr);
  }
}

function renderFrame(detail) {
  const ctx = frameCtx;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, frameCanvas.width, frameCanvas.height);

  const w = frameCanvas.width, h = frameCanvas.height;
  const cx = w / 2 + frameView.panX, cy = h / 2 + frameView.panY;

  // The shared frame is the *vehicle-heading* angle space: every camera's
  // FOV rectangle sits at its own mounting offset (center = yaw_offset/
  // pitch_offset), so overlaps and gaps between cameras are directly
  // visible, and every light plots exactly once.
  const rig = metaCameras.length ? metaCameras : [{ name: "camera", fov_h: detail.fov_h, fov_v: detail.fov_v, yaw_offset: 0, pitch_offset: 0 }];

  // one shared px/deg scale on both axes, so every FOV rectangle keeps
  // its true aspect ratio; auto-fit covers all rects plus all candidates
  let rangeX = 1, rangeY = 1;
  for (const cam of rig) {
    rangeX = Math.max(rangeX, Math.abs(cam.yaw_offset || 0) + cam.fov_h / 2);
    rangeY = Math.max(rangeY, Math.abs(cam.pitch_offset || 0) + cam.fov_v / 2);
  }
  for (const c of detail.candidates) {
    rangeX = Math.max(rangeX, Math.abs(c.yaw_diff));
    rangeY = Math.max(rangeY, Math.abs(c.pitch_diff));
  }
  const pad = 1.2;
  const pxPerDeg = Math.min(w / (2 * rangeX * pad), h / (2 * rangeY * pad)) * frameView.zoom;

  // This is a view *through the windshield*: yaw_diff is CCW-positive (a
  // target left of the heading has positive yaw_diff), so positive must
  // render toward the LEFT edge -- hence the minus sign. Positive
  // pitch_diff (above the horizon) renders up.
  const toScreen = (yawDeg, pitchDeg) => [cx - yawDeg * pxPerDeg, cy - pitchDeg * pxPerDeg];

  // horizon: pitch_diff = 0 (eye height, level) as a full-width line
  ctx.strokeStyle = "#3a5a8a";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, cy); ctx.lineTo(w, cy);
  ctx.stroke();
  ctx.fillStyle = "#6a8ab8";
  ctx.font = "10px sans-serif";
  ctx.fillText("horizon (pitch 0)", 6, cy - 4);

  // one FOV rectangle per camera, centered at its mounting offsets
  let leftEdgeX = Infinity, rightEdgeX = -Infinity;
  rig.forEach((cam, i) => {
    const yawOff = cam.yaw_offset || 0, pitchOff = cam.pitch_offset || 0;
    const [rx0, ry0] = toScreen(yawOff + cam.fov_h / 2, pitchOff + cam.fov_v / 2);
    const [rx1, ry1] = toScreen(yawOff - cam.fov_h / 2, pitchOff - cam.fov_v / 2);
    leftEdgeX = Math.min(leftEdgeX, rx0); rightEdgeX = Math.max(rightEdgeX, rx1);
    const color = metaCameras.length ? cameraColor(i) : "#888";
    ctx.strokeStyle = color;
    ctx.setLineDash([6, 4]);
    ctx.strokeRect(rx0, ry0, rx1 - rx0, ry1 - ry0);
    ctx.setLineDash([]);
    ctx.fillStyle = color;
    ctx.font = "10px sans-serif";
    ctx.fillText(cam.name, rx0 + 4, ry0 + 12);
  });

  // crosshair at dead ahead (vehicle heading), not any camera's axis
  ctx.strokeStyle = "#555";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(cx - 10, cy); ctx.lineTo(cx + 10, cy);
  ctx.moveTo(cx, cy - 10); ctx.lineTo(cx, cy + 10);
  ctx.stroke();

  ctx.fillStyle = "#aaa";
  ctx.font = "10px sans-serif";
  // orientation labels so left/right is never ambiguous
  ctx.fillText("L", leftEdgeX + 4, cy - 6);
  ctx.fillText("R", rightEdgeX - 12, cy - 6);

  // One drawing entry per light: its per-camera candidate rows merge, a
  // head drawing solid if ANY camera sees it, and the label totalling
  // observations across cameras. Farthest first, so when two lights
  // overlap the nearer (bigger-looking) one paints on top -- same
  // occlusion order a camera would see.
  const byLight = new Map();
  for (const c of detail.candidates) {
    let entry = byLight.get(c.target_tl_id);
    if (!entry) {
      entry = { ...c, heads: c.heads.map((hd) => ({ ...hd })), sum_visible: 0, any_covered: false, any_in_fov: false };
      byLight.set(c.target_tl_id, entry);
    } else {
      c.heads.forEach((hd, k) => { if (entry.heads[k]) entry.heads[k].visible = entry.heads[k].visible || hd.visible; });
    }
    entry.sum_visible += c.heads_visible;
    entry.any_covered = entry.any_covered || c.is_covered;
    entry.any_in_fov = entry.any_in_fov || c.in_fov;
  }
  const byDistanceDesc = [...byLight.values()].sort((p, q) => q.distance_m - p.distance_m);
  for (const c of byDistanceDesc) {
    const [sx, sy] = toScreen(c.yaw_diff, c.pitch_diff);
    const color = c.any_covered ? STATUS_COLOR.covered : c.any_in_fov ? STATUS_COLOR.facing_away : STATUS_COLOR.out_of_fov;

    // One box per physical housing, each at its own projected position
    // and its own mapped size -- not one box at the pooled centroid,
    // which can sit between housings where nothing physically exists.
    // Heads that are individually visible (in FOV + facing) draw solid;
    // the rest draw faint and dashed. Falls back to a single
    // centroid-box for data without per-head detail (old snapshots).
    const fallback = SIGNAL_HOUSING_M[c.signal_type] || SIGNAL_HOUSING_M.unknown;
    const headList = (c.heads && c.heads.length)
      ? c.heads
      : [{ yaw_diff: c.yaw_diff, pitch_diff: c.pitch_diff, panel_width: c.panel_width, panel_height: c.panel_height, visible: c.is_covered }];
    let labelRw = 0, labelRh = 0;
    for (const head of headList) {
      const wM = head.panel_width ?? fallback.w;
      const hM = head.panel_height ?? fallback.h;
      const wDeg = 2 * Math.atan2(wM / 2, c.distance_m) * (180 / Math.PI);
      const hDeg = 2 * Math.atan2(hM / 2, c.distance_m) * (180 / Math.PI);
      const rw = wDeg * pxPerDeg, rh = hDeg * pxPerDeg;
      labelRw = Math.max(labelRw, rw); labelRh = Math.max(labelRh, rh);
      const [hx, hy] = toScreen(head.yaw_diff, head.pitch_diff);
      ctx.fillStyle = color;
      ctx.globalAlpha = head.visible ? 0.4 : 0.15;
      ctx.fillRect(hx - rw / 2, hy - rh / 2, rw, rh);
      ctx.globalAlpha = 1.0;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.setLineDash(head.visible ? [] : [4, 3]);
      ctx.strokeRect(hx - rw / 2, hy - rh / 2, rw, rh);
      ctx.setLineDash([]);
    }
    const rw = labelRw, rh = labelRh;

    // Individual lamps at their true projected positions (each bulb is
    // mapped and projected separately, so an obliquely-viewed housing
    // shows its lamp row foreshortened, like a real camera image would).
    // Lens diameter is the one guessed quantity (LENS_DIAMETER_M).
    if (c.lamps && c.lamps.length) {
      const lensDeg = 2 * Math.atan2(LENS_DIAMETER_M / 2, c.distance_m) * (180 / Math.PI);
      const lensR = Math.max(1.2, (lensDeg * pxPerDeg) / 2);
      for (const lamp of c.lamps) {
        const [lx, ly] = toScreen(lamp.yaw_diff, lamp.pitch_diff);
        ctx.beginPath();
        ctx.arc(lx, ly, lensR, 0, 2 * Math.PI);
        ctx.fillStyle = LAMP_COLOR[lamp.color] || "#999";
        ctx.fill();
        ctx.strokeStyle = "#222";
        ctx.lineWidth = Math.max(0.5, lensR * 0.15);
        ctx.stroke();
        if (lamp.arrow) drawLampArrow(ctx, lx, ly, lensR, lamp.arrow);
      }
    } else {
      // no lamp detail (old snapshot): keep a small center dot visible
      ctx.beginPath();
      ctx.arc(sx, sy, 2.5, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();
    }

    ctx.fillStyle = "white";
    ctx.font = "10px sans-serif";
    // multi-camera: total observations across the rig; single camera:
    // the familiar k/n heads
    const headsNote = metaCameras.length > 1
      ? `, ${c.sum_visible} obs`
      : c.heads_total > 1 ? `, ${c.heads_visible}/${c.heads_total} heads` : "";
    ctx.fillText(
      `${c.target_tl_id} (${c.distance_m.toFixed(0)}m${headsNote})`,
      sx + Math.max(8, rw / 2 + 4),
      sy - Math.max(8, rh / 2 + 4)
    );
  }
}

// A small directional glyph inside an arrow lamp's lens. Arrow lamps are
// dark housings with a lit green arrow, so the lens circle itself is
// drawn in the lamp's color tag and this glyph shows which direction it
// points ("straight" is drawn as "up": both mean dead ahead).
function drawLampArrow(ctx, cx, cy, r, direction) {
  const angle = { up: -Math.PI / 2, straight: -Math.PI / 2, right: 0, left: Math.PI }[direction];
  if (angle === undefined) return;
  const len = r * 0.75;
  const tipX = cx + len * Math.cos(angle), tipY = cy + len * Math.sin(angle);
  const tailX = cx - len * Math.cos(angle), tailY = cy - len * Math.sin(angle);
  ctx.save();
  ctx.strokeStyle = "#111";
  ctx.lineWidth = Math.max(1, r * 0.22);
  ctx.beginPath();
  ctx.moveTo(tailX, tailY);
  ctx.lineTo(tipX, tipY);
  const headAngle = Math.PI / 5, headLen = r * 0.5;
  ctx.moveTo(tipX, tipY);
  ctx.lineTo(tipX - headLen * Math.cos(angle - headAngle), tipY - headLen * Math.sin(angle - headAngle));
  ctx.moveTo(tipX, tipY);
  ctx.lineTo(tipX - headLen * Math.cos(angle + headAngle), tipY - headLen * Math.sin(angle + headAngle));
  ctx.stroke();
  ctx.restore();
}

function setupMapInteraction() {
  let dragging = false, lastX = 0, lastY = 0, moved = false;

  mapCanvas.addEventListener("mousedown", (e) => {
    dragging = true; moved = false;
    lastX = e.clientX; lastY = e.clientY;
  });
  window.addEventListener("mouseup", () => { dragging = false; });
  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const dx = (e.clientX - lastX) * devicePixelRatio;
    const dy = (e.clientY - lastY) * devicePixelRatio;
    if (Math.abs(dx) > 1 || Math.abs(dy) > 1) moved = true;
    view.offsetX += dx;
    view.offsetY += dy;
    lastX = e.clientX; lastY = e.clientY;
    renderMap();
  });

  mapCanvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    const rect = mapCanvas.getBoundingClientRect();
    const mx = (e.clientX - rect.left) * devicePixelRatio;
    const my = (e.clientY - rect.top) * devicePixelRatio;
    const [wx, wy] = screenToWorld(mx, my);
    const factor = e.deltaY < 0 ? 1.2 : 1 / 1.2;
    view.scale *= factor;
    view.offsetX = mx - wx * view.scale;
    view.offsetY = my + wy * view.scale;
    renderMap();
  }, { passive: false });

  mapCanvas.addEventListener("click", (e) => {
    if (moved) return; // was a drag, not a click
    const rect = mapCanvas.getBoundingClientRect();
    const sx = (e.clientX - rect.left) * devicePixelRatio;
    const sy = (e.clientY - rect.top) * devicePixelRatio;
    const [wx, wy] = screenToWorld(sx, sy);
    const nearest = findNearestPoint(wx, wy);
    if (nearest) selectPoint(nearest.id);
  });
}

function setupFrameInteraction() {
  let dragging = false, lastX = 0, lastY = 0;

  frameCanvas.addEventListener("mousedown", (e) => {
    dragging = true;
    lastX = e.clientX; lastY = e.clientY;
  });
  window.addEventListener("mouseup", () => { dragging = false; });
  window.addEventListener("mousemove", (e) => {
    if (!dragging || !currentDetail) return;
    frameView.panX += (e.clientX - lastX) * devicePixelRatio;
    frameView.panY += (e.clientY - lastY) * devicePixelRatio;
    lastX = e.clientX; lastY = e.clientY;
    renderFrame(currentDetail);
  });

  frameCanvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    if (!currentDetail) return;
    const factor = e.deltaY < 0 ? 1.2 : 1 / 1.2;
    frameView.zoom = Math.max(0.1, Math.min(50, frameView.zoom * factor));
    renderFrame(currentDetail);
  }, { passive: false });

  // double-click resets zoom/pan back to the auto-fit view
  frameCanvas.addEventListener("dblclick", () => {
    if (!currentDetail) return;
    frameView.zoom = 1;
    frameView.panX = 0;
    frameView.panY = 0;
    renderFrame(currentDetail);
  });
}

const ARROW_CHAR = { left: "←", up: "↑", straight: "↑", right: "→" };
const LAMP_SWATCH_ORDER = { green: 0, yellow: 1, red: 2 };
const ARROW_SWATCH_ORDER = { left: 0, up: 1, straight: 2, right: 3 };

function setupTabs() {
  for (const btn of tabButtons) {
    btn.addEventListener("click", () => {
      activeTab = btn.dataset.tab;
      for (const b of tabButtons) b.classList.toggle("active", b === btn);
      tabCamera.hidden = activeTab !== "camera";
      tabPatterns.hidden = activeTab !== "patterns";
      if (activeTab === "patterns" && patternsData === null) loadPatterns();
      if (activeTab === "camera") {
        // canvas may have missed a window resize while its tab was hidden
        resizeCanvases();
        if (currentDetail) renderFrame(currentDetail);
      }
      renderMap(); // pattern highlight is drawn only while its tab is active
    });
  }
  patternFilterInput.addEventListener("input", () => renderPatternTable());
}

async function loadPatterns() {
  const res = await fetch("/api/patterns");
  if (!res.ok) {
    patternSummaryEl.textContent = `failed to load patterns (${res.status})`;
    return;
  }
  patternsData = await res.json();
  renderPatternTable();
}

function lampSwatchHtml(heads) {
  // representative head, lamps in canonical G/Y/R + arrow order (same
  // ordering the signature uses, so the swatch matches the text)
  const lamps = [...heads[0].lamps].sort((a, b) => {
    if (!!a.arrow !== !!b.arrow) return a.arrow ? 1 : -1;
    if (a.arrow && b.arrow) return (ARROW_SWATCH_ORDER[a.arrow] ?? 9) - (ARROW_SWATCH_ORDER[b.arrow] ?? 9);
    return (LAMP_SWATCH_ORDER[a.color] ?? 9) - (LAMP_SWATCH_ORDER[b.color] ?? 9);
  });
  return lamps
    .map((lamp) => {
      const color = LAMP_COLOR[lamp.color] || "#999";
      const glyph = lamp.arrow ? ARROW_CHAR[lamp.arrow] || "?" : "";
      return `<span class="lamp-dot" style="background:${color}">${glyph}</span>`;
    })
    .join("");
}

function renderPatternTable() {
  if (!patternsData) return;
  const filter = patternFilterInput.value.trim().toLowerCase();
  const rows = patternsData.patterns.filter((p) => !filter || p.signature.toLowerCase().includes(filter));

  patternTbody.innerHTML = "";
  let shownHeads = 0;
  for (const p of rows) {
    shownHeads += p.count;
    const tr = document.createElement("tr");
    if (p.signature === selectedPatternSig) tr.classList.add("selected");

    const swatchTd = document.createElement("td");
    swatchTd.innerHTML = lampSwatchHtml(p.heads);
    const sigTd = document.createElement("td");
    sigTd.textContent = p.signature;
    const countTd = document.createElement("td");
    countTd.textContent = p.count;
    tr.append(swatchTd, sigTd, countTd);

    tr.addEventListener("click", () => {
      if (selectedPatternSig === p.signature) {
        selectedPatternSig = null;
        patternHighlightHeads = [];
      } else {
        selectedPatternSig = p.signature;
        patternHighlightHeads = p.heads;
      }
      renderPatternTable();
      renderMap();
    });
    patternTbody.appendChild(tr);
  }

  const totalNote = filter ? `${shownHeads} of ${patternsData.total_heads} heads shown` : `${patternsData.total_heads} heads`;
  patternSummaryEl.textContent = `${rows.length} pattern(s) | ${totalNote}` +
    (selectedPatternSig ? " | highlighted on map" : "");
}

function setupDataControls() {
  // Both uploads send the file as the raw request body and reload the page
  // on success -- state on the server is fully replaced, so re-fetching
  // everything from scratch is both the simplest and the correct behavior.
  const upload = async (url, file, busyMsg) => {
    dataStatusEl.textContent = busyMsg;
    try {
      const res = await fetch(url, { method: "POST", body: file });
      if (!res.ok) throw new Error(`${res.status}: ${(await res.text()).slice(0, 200)}`);
      dataStatusEl.textContent = "done, reloading...";
      location.reload();
    } catch (err) {
      console.error(err);
      dataStatusEl.textContent = `failed: ${err.message}`;
    }
  };

  loadSnapshotInput.addEventListener("change", () => {
    const file = loadSnapshotInput.files[0];
    if (file) upload("/api/load_snapshot", file, "loading snapshot...");
  });
  loadMapInput.addEventListener("change", () => {
    const file = loadMapInput.files[0];
    if (file) upload("/api/load_map", file, "parsing map + running simulation (~30s)...");
  });
}

function setupCopyLinkButton() {
  copyLinkBtn.addEventListener("click", async () => {
    const original = "Copy link to this point";
    try {
      await navigator.clipboard.writeText(window.location.href);
      copyLinkBtn.textContent = "Copied!";
      copyLinkBtn.classList.add("copied");
    } catch (err) {
      console.error("clipboard write failed", err);
      copyLinkBtn.textContent = "Copy failed";
    }
    setTimeout(() => {
      copyLinkBtn.textContent = original;
      copyLinkBtn.classList.remove("copied");
    }, 1500);
  });
}

async function main() {
  try {
    const [metaRes, pointsRes, lightsRes] = await Promise.all([
      fetch("/api/meta"), fetch("/api/points"), fetch("/api/traffic_lights"),
    ]);
    if (!metaRes.ok || !pointsRes.ok || !lightsRes.ok) {
      throw new Error(`API request failed (meta=${metaRes.status}, points=${pointsRes.status}, lights=${lightsRes.status})`);
    }
    const meta = await metaRes.json();
    points = await pointsRes.json();
    trafficLights = await lightsRes.json();
    metaCameras = meta.cameras;
    cameraSpec = meta.cameras[0];
    latlonTransform = meta.latlon_transform;

    const camsText = meta.cameras
      .map((c) => {
        const offset = c.yaw_offset ? ` yaw${c.yaw_offset > 0 ? "+" : ""}${c.yaw_offset}deg` : "";
        return `${c.name}: ${c.fov_h}x${c.fov_v}deg [${c.min_range},${c.max_range}]m${offset}`;
      })
      .join(" | ");
    metaEl.textContent =
      `${meta.lane_count} lanes | ${meta.traffic_light_count} traffic lights | ${meta.point_count} evaluated waypoints | ` +
      camsText;

    resizeCanvases();
    fitViewToData();

    const restoredPoint = findPointFromUrl();
    if (restoredPoint) {
      view.offsetX = mapCanvas.width / 2 - Math.max(view.scale, 5) * restoredPoint.x;
      view.offsetY = mapCanvas.height / 2 + Math.max(view.scale, 5) * restoredPoint.y;
      view.scale = Math.max(view.scale, 5);
    }
    renderMap();
    setupMapInteraction();
    setupFrameInteraction();
    setupCopyLinkButton();
    setupDataControls();
    setupTabs();
    pointSizeInput.addEventListener("input", () => {
      pointSizeScale = parseFloat(pointSizeInput.value);
      pointSizeValueEl.textContent = `${pointSizeScale.toFixed(2)}x`;
      renderMap();
    });
    colorModeSelect.addEventListener("change", () => {
      colorMode = colorModeSelect.value;
      for (const el of document.querySelectorAll(".legend-status")) el.hidden = colorMode !== "status";
      for (const el of document.querySelectorAll(".legend-redundancy")) el.hidden = colorMode !== "redundancy";
      renderMap();
    });
    if (latlonTransform) {
      aerialToggle.addEventListener("change", () => {
        aerialEnabled = aerialToggle.checked;
        aerialAttributionEl.hidden = !aerialEnabled;
        renderMap();
      });
    } else {
      // no lat/lon in this map (or an old snapshot): nothing to georeference
      aerialToggle.disabled = true;
    }
    window.addEventListener("resize", () => { resizeCanvases(); renderMap(); });

    if (restoredPoint) {
      await selectPoint(restoredPoint.id);
    }
  } catch (err) {
    // Surface failures in the page itself -- a silently rejected promise
    // here (e.g. a fetch error, or a bug in rendering) used to leave the
    // whole UI blank with no clue why, visible only in the browser console.
    console.error(err);
    metaEl.textContent = `Failed to load: ${err.message}`;
    metaEl.style.color = "#d62728";
  }
}

main();
