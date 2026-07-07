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

let points = [];
let trafficLights = [];
let selectedPointId = null;
let currentDetail = null;
let cameraSpec = null;
// {lat: [a,b,c], lon: [d,e,f]} affine fit from /api/meta, or null if the
// map's nodes carried no lat/lon attributes
let latlonTransform = null;
// target_tl_id -> status color, for whichever point is currently selected
let highlightedLights = new Map();
let pointSizeScale = 1.0;

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

// world <-> screen transform state for the map pane
const view = { scale: 1, offsetX: 0, offsetY: 0 };

// zoom/pan state for the camera-view (frame) pane, reset on each new point pick
const frameView = { zoom: 1, panX: 0, panY: 0 };

function resizeCanvases() {
  for (const c of [mapCanvas, frameCanvas]) {
    const rect = c.getBoundingClientRect();
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

function drawFrustum(ctx, point, camYawDeg, fovHDeg, minRange, maxRange) {
  const yaw = (camYawDeg * Math.PI) / 180;
  const half = (fovHDeg / 2) * (Math.PI / 180);
  const a0 = yaw - half, a1 = yaw + half;

  const atAngle = (angle, dist) => worldToScreen(point.x + dist * Math.cos(angle), point.y + dist * Math.sin(angle));

  const steps = 24;
  const outerPts = [], innerPts = [];
  for (let i = 0; i <= steps; i++) {
    const a = a0 + ((a1 - a0) * i) / steps;
    outerPts.push(atAngle(a, maxRange));
    innerPts.push(atAngle(a, minRange));
  }

  ctx.save();
  ctx.beginPath();
  ctx.moveTo(outerPts[0][0], outerPts[0][1]);
  for (const [x, y] of outerPts.slice(1)) ctx.lineTo(x, y);
  for (const [x, y] of innerPts.reverse()) ctx.lineTo(x, y);
  ctx.closePath();
  ctx.fillStyle = "rgba(31, 119, 180, 0.15)";
  ctx.fill();
  ctx.strokeStyle = "rgba(31, 119, 180, 0.8)";
  ctx.lineWidth = 1.5;
  ctx.stroke();

  // dashed heading line down the middle of the frustum, the direction of travel
  const [sx, sy] = worldToScreen(point.x, point.y);
  const [hx, hy] = atAngle(yaw, maxRange);
  ctx.beginPath();
  ctx.moveTo(sx, sy);
  ctx.lineTo(hx, hy);
  ctx.setLineDash([5, 4]);
  ctx.strokeStyle = "rgba(31, 119, 180, 0.9)";
  ctx.lineWidth = 1;
  ctx.stroke();
  ctx.restore();
}

function renderMap() {
  const ctx = mapCtx;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, mapCanvas.width, mapCanvas.height);

  drawAerial(ctx);

  const byStatus = { covered: [], facing_away: [], out_of_fov: [] };
  for (const p of points) byStatus[p.status].push(p);

  const dotSize = Math.max(1, Math.min(2.2, view.scale * 0.15)) * pointSizeScale;
  for (const status of STATUS_DRAW_ORDER) {
    ctx.fillStyle = STATUS_COLOR[status];
    for (const p of byStatus[status]) {
      const [sx, sy] = worldToScreen(p.x, p.y);
      ctx.fillRect(sx - dotSize / 2, sy - dotSize / 2, dotSize, dotSize);
    }
  }

  if (selectedPointId !== null && currentDetail && cameraSpec) {
    drawFrustum(
      ctx,
      points[selectedPointId],
      currentDetail.cam_yaw,
      cameraSpec.fov_h,
      cameraSpec.min_range,
      cameraSpec.max_range
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
  pointInfoEl.textContent =
    `lane ${p.lane_id} @ (${p.x.toFixed(1)}, ${p.y.toFixed(1)})  |  ` +
    `cam_yaw=${detail.cam_yaw.toFixed(1)}deg  |  ` +
    `FOV ${detail.fov_h}x${detail.fov_v} deg  |  ${detail.candidates.length} candidate(s)  |  ` +
    `status=${detail.status}`;

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
    tr.appendChild(cell(c.signal_type));
    tr.appendChild(cell(c.distance_m.toFixed(1)));
    tr.appendChild(cell(c.yaw_diff.toFixed(1)));
    tr.appendChild(cell(c.pitch_diff.toFixed(1)));
    tr.appendChild(cell(c.in_fov ? "yes" : "no", c.in_fov ? "status-true" : "status-false"));
    tr.appendChild(cell(c.facing_camera ? "yes" : "no", c.facing_camera ? "status-true" : "status-false"));
    tr.appendChild(cell(c.is_covered ? "yes" : "no", c.is_covered ? "status-true" : "status-false"));
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
  const halfH = detail.fov_h / 2, halfV = detail.fov_v / 2;

  // Work in degrees with one shared px/deg scale on both axes, so the FOV
  // rectangle keeps its true aspect ratio (e.g. 30x17 renders wide, not
  // square the way the old +-1-normalized rendering did).
  let rangeX = halfH, rangeY = halfV;
  for (const c of detail.candidates) {
    rangeX = Math.max(rangeX, Math.abs(c.yaw_diff));
    rangeY = Math.max(rangeY, Math.abs(c.pitch_diff));
  }
  const pad = 1.2;
  const pxPerDeg = Math.min(w / (2 * rangeX * pad), h / (2 * rangeY * pad)) * frameView.zoom;

  // This is a view *through the camera*, out the windshield: yaw_diff is
  // CCW-positive (a target left of the heading has positive yaw_diff), so
  // positive must render toward the LEFT edge -- hence the minus sign.
  // Positive pitch_diff (above the horizon) renders up.
  const toScreen = (yawDeg, pitchDeg) => [cx - yawDeg * pxPerDeg, cy - pitchDeg * pxPerDeg];

  // horizon: the camera is level (pitch 0), so eye-height (z = ground +
  // cam height) maps to a full-width line through pitch_diff = 0.
  ctx.strokeStyle = "#3a5a8a";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, cy); ctx.lineTo(w, cy);
  ctx.stroke();
  ctx.fillStyle = "#6a8ab8";
  ctx.font = "10px sans-serif";
  ctx.fillText("horizon (pitch 0)", 6, cy - 4);

  // FOV rectangle: +-fov_h/2 wide, +-fov_v/2 tall
  const [rx0, ry0] = toScreen(halfH, halfV);
  const [rx1, ry1] = toScreen(-halfH, -halfV);
  ctx.strokeStyle = "#888";
  ctx.setLineDash([6, 4]);
  ctx.strokeRect(rx0, ry0, rx1 - rx0, ry1 - ry0);
  ctx.setLineDash([]);

  // crosshair at dead center
  ctx.strokeStyle = "#555";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(cx - 10, cy); ctx.lineTo(cx + 10, cy);
  ctx.moveTo(cx, cy - 10); ctx.lineTo(cx, cy + 10);
  ctx.stroke();

  ctx.fillStyle = "#aaa";
  ctx.font = "10px sans-serif";
  ctx.fillText("FOV edge", rx0 + 4, ry0 + 12);
  // orientation labels so left/right is never ambiguous
  ctx.fillText("L", rx0 + 4, cy - 6);
  ctx.fillText("R", rx1 - 12, cy - 6);

  for (const c of detail.candidates) {
    const [sx, sy] = toScreen(c.yaw_diff, c.pitch_diff);
    const color = c.is_covered ? STATUS_COLOR.covered : c.in_fov ? STATUS_COLOR.facing_away : STATUS_COLOR.out_of_fov;
    ctx.beginPath();
    ctx.arc(sx, sy, 6, 0, 2 * Math.PI);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.strokeStyle = "white";
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.fillStyle = "white";
    ctx.font = "10px sans-serif";
    ctx.fillText(`${c.target_tl_id} (${c.distance_m.toFixed(0)}m)`, sx + 8, sy - 8);
  }
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
    cameraSpec = meta.camera;
    latlonTransform = meta.latlon_transform;

    metaEl.textContent =
      `${meta.lane_count} lanes | ${meta.traffic_light_count} traffic lights | ${meta.point_count} evaluated waypoints | ` +
      `camera: height=${meta.camera.height}m fov=${meta.camera.fov_h}x${meta.camera.fov_v}deg ` +
      `range=[${meta.camera.min_range},${meta.camera.max_range}]m facing_tolerance=${meta.camera.facing_tolerance_deg}deg`;

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
    pointSizeInput.addEventListener("input", () => {
      pointSizeScale = parseFloat(pointSizeInput.value);
      pointSizeValueEl.textContent = `${pointSizeScale.toFixed(2)}x`;
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
