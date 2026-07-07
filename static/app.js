"use strict";

const STATUS_COLOR = {
  covered: "#2ca02c",
  facing_away: "#ff7f0e",
  out_of_fov: "#d62728",
};
// Worst case drawn last (on top), matching visualizer.py's convention.
const STATUS_DRAW_ORDER = ["covered", "facing_away", "out_of_fov"];

const mapCanvas = document.getElementById("map-canvas");
const mapCtx = mapCanvas.getContext("2d");
const frameCanvas = document.getElementById("frame-canvas");
const frameCtx = frameCanvas.getContext("2d");
const metaEl = document.getElementById("meta");
const pointInfoEl = document.getElementById("point-info");
const candidateTbody = document.querySelector("#candidate-table tbody");

let points = [];
let trafficLights = [];
let selectedPointId = null;
let currentDetail = null;
let cameraSpec = null;
// target_tl_id -> status color, for whichever point is currently selected
let highlightedLights = new Map();

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

function drawStar(ctx, cx, cy, r, fillColor, strokeColor, strokeWidth) {
  ctx.beginPath();
  for (let i = 0; i < 5; i++) {
    const outerAngle = (Math.PI / 2) + (i * 2 * Math.PI) / 5;
    const innerAngle = outerAngle + Math.PI / 5;
    const ox = cx + r * Math.cos(outerAngle), oy = cy - r * Math.sin(outerAngle);
    const ix = cx + (r * 0.45) * Math.cos(innerAngle), iy = cy - (r * 0.45) * Math.sin(innerAngle);
    if (i === 0) ctx.moveTo(ox, oy); else ctx.lineTo(ox, oy);
    ctx.lineTo(ix, iy);
  }
  ctx.closePath();
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

  const byStatus = { covered: [], facing_away: [], out_of_fov: [] };
  for (const p of points) byStatus[p.status].push(p);

  const dotSize = Math.max(1, Math.min(2.2, view.scale * 0.15));
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

  const starR = Math.max(3, Math.min(9, view.scale * 3));
  const highlightR = starR * 1.8;

  // plain stars first, highlighted ones (candidates of the selected point)
  // drawn last/on top so they're never hidden by an overlapping neighbor.
  const highlighted = [];
  for (const tl of trafficLights) {
    if (highlightedLights.has(tl.id)) { highlighted.push(tl); continue; }
    const [sx, sy] = worldToScreen(tl.x, tl.y);
    drawStar(ctx, sx, sy, starR, "gold", "black", 0.6);
  }
  for (const tl of highlighted) {
    const [sx, sy] = worldToScreen(tl.x, tl.y);
    const color = highlightedLights.get(tl.id);
    drawStar(ctx, sx, sy, highlightR, color, "black", 2.2);
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

async function selectPoint(pointId) {
  selectedPointId = pointId;
  currentDetail = null;
  highlightedLights = new Map();
  frameView.zoom = 1;
  frameView.panX = 0;
  frameView.panY = 0;
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

  // auto-fit the plotted range to whatever is farthest off-axis, at least the FOV itself
  let maxAbs = 1.0;
  for (const c of detail.candidates) {
    maxAbs = Math.max(maxAbs, Math.abs(c.norm_x), Math.abs(c.norm_y));
  }
  const range = Math.max(1.3, maxAbs * 1.2);
  const scale = (Math.min(w, h) / 2 / range) * frameView.zoom;

  const toScreen = (nx, ny) => [cx + nx * scale, cy - ny * scale];

  // FOV rectangle (norm range -1..1 on both axes)
  const [rx0, ry0] = toScreen(-1, 1);
  const [rx1, ry1] = toScreen(1, -1);
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

  for (const c of detail.candidates) {
    const [sx, sy] = toScreen(c.norm_x, c.norm_y);
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

    metaEl.textContent =
      `${meta.lane_count} lanes | ${meta.traffic_light_count} traffic lights | ${meta.point_count} evaluated waypoints | ` +
      `camera: height=${meta.camera.height}m fov=${meta.camera.fov_h}x${meta.camera.fov_v}deg ` +
      `range=[${meta.camera.min_range},${meta.camera.max_range}]m facing_tolerance=${meta.camera.facing_tolerance_deg}deg`;

    resizeCanvases();
    fitViewToData();
    renderMap();
    setupMapInteraction();
    setupFrameInteraction();
    window.addEventListener("resize", () => { resizeCanvases(); renderMap(); });
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
