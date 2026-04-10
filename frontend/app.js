/* ══════════════════════════════════════════
   GeoInsight AI — app.js
   ══════════════════════════════════════════ */

const API_BASE = window.location.hostname === "localhost"
  ? "http://localhost:8000"
  : "https://geoinsight-ai-21jl.onrender.com";

// ── State ────────────────────────────────────────────────────────────────────
let map, currentMarker, radarChart;
let heatLayer = null;
let heatVisible = false;
let analysisInFlight = false;

// ── Map Init ─────────────────────────────────────────────────────────────────
function initMap() {
  map = L.map("map", {
    center: [39.2, 35.4],   // Türkiye merkezi
    zoom: 6,
    zoomControl: false,
  });

  // Dark tile layer
  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
    attribution: '© <a href="https://carto.com/">CARTO</a>',
    subdomains: "abcd",
    maxZoom: 19,
  }).addTo(map);

  // Zoom control — sağ alt
  L.control.zoom({ position: "bottomright" }).addTo(map);

  // Click to analyze
  map.on("click", (e) => {
    if (analysisInFlight) return;
    analyzeLocation(e.latlng.lat, e.latlng.lng);
  });
}

// ── Marker ───────────────────────────────────────────────────────────────────
function placeMarker(lat, lng) {
  if (currentMarker) map.removeLayer(currentMarker);

  const icon = L.divIcon({
    html: '<div class="geo-marker"></div>',
    iconSize: [18, 18],
    iconAnchor: [9, 18],
    className: "",
  });

  currentMarker = L.marker([lat, lng], { icon }).addTo(map);
  map.panTo([lat, lng], { animate: true, duration: 0.5 });

  // Animasyonlu daire
  const circle = L.circle([lat, lng], {
    radius: 40000,
    color: "#58a6ff",
    fillColor: "#58a6ff",
    fillOpacity: 0.06,
    weight: 1,
    dashArray: "4 4",
  }).addTo(map);

  // 3 saniye sonra daire solar
  setTimeout(() => {
    let op = 0.06;
    const fade = setInterval(() => {
      op -= 0.005;
      if (op <= 0) { map.removeLayer(circle); clearInterval(fade); }
      else circle.setStyle({ fillOpacity: op, opacity: op * 4 });
    }, 60);
  }, 2000);
}

// ── API Call ──────────────────────────────────────────────────────────────────
async function analyzeLocation(lat, lng) {
  if (analysisInFlight) return;
  analysisInFlight = true;

  placeMarker(lat, lng);
  showLoading(lat, lng);

  try {
    const res = await fetch(`${API_BASE}/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lat, lng, radius_km: 5 }),
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    showResult(data);
  } catch (err) {
    showError(err.message);
  } finally {
    analysisInFlight = false;
  }
}

// ── Heatmap ───────────────────────────────────────────────────────────────────
async function toggleHeatmap() {
  const btn = document.getElementById("btn-heatmap");

  if (heatVisible && heatLayer) {
    map.removeLayer(heatLayer);
    heatLayer = null;
    heatVisible = false;
    btn.classList.remove("active");
    return;
  }

  btn.classList.add("active");

  try {
    const res = await fetch(`${API_BASE}/heatmap-points`);
    const data = await res.json();

    const points = data.points.map(p => [p.lat, p.lng, p.intensity]);

    heatLayer = L.heatLayer(points, {
      radius: 35,
      blur: 20,
      maxZoom: 10,
      gradient: { 0.0: "#0d1117", 0.3: "#1e3a5f", 0.6: "#2563eb", 0.8: "#7c3aed", 1.0: "#ec4899" },
    }).addTo(map);

    heatVisible = true;
  } catch {
    btn.classList.remove("active");
    alert("Isı haritası yüklenemedi. Backend çalışıyor mu?");
  }
}

// ── Panel States ──────────────────────────────────────────────────────────────
function showPanel(id) {
  ["panel-idle", "panel-loading", "panel-result"].forEach(s => {
    document.getElementById(s).style.display = s === id ? "" : "none";
  });
}

function showLoading(lat, lng) {
  showPanel("panel-loading");
  document.getElementById("loading-coords").textContent =
    `${lat.toFixed(5)}, ${lng.toFixed(5)}`;
}

function showError(msg) {
  showPanel("panel-idle");
  const idle = document.getElementById("panel-idle");
  const existing = idle.querySelector(".error-msg");
  if (existing) existing.remove();

  const div = document.createElement("div");
  div.className = "error-msg";
  div.style.cssText = `
    color:#f78166; font-size:12px; background:rgba(247,129,102,.1);
    border:1px solid rgba(247,129,102,.3); border-radius:8px;
    padding:10px 14px; text-align:center; margin-top:8px;
  `;
  div.textContent = `⚠ Bağlantı hatası: ${msg}. Backend çalışıyor mu?`;
  idle.appendChild(div);
}

// ── Render Result ─────────────────────────────────────────────────────────────
function showResult(data) {
  showPanel("panel-result");

  // Header
  document.getElementById("result-coords").textContent =
    `${data.lat.toFixed(5)}°N, ${data.lng.toFixed(5)}°E`;

  // Overall badge
  const overall = data.overall_score;
  const obadge  = document.getElementById("overall-num");
  obadge.textContent = overall;
  obadge.style.color = scoreColor(overall);

  // Score cards (animated)
  setScore("env",  data.environmental_score);
  setScore("risk", data.risk_score);
  setScore("liv",  data.livability_score);

  // Radar chart
  buildRadar(data);

  // Detail tabs
  renderTab("tab-env",  data.environmental_details);
  renderTab("tab-risk", data.risk_details);
  renderTab("tab-liv",  data.livability_details);

  // AI summary
  document.getElementById("ai-summary").textContent       = data.summary;
  document.getElementById("ai-recommendation").textContent = "💡 " + data.recommendation;

  // Reset tab to env
  switchTab("env");
}

function setScore(key, value) {
  document.getElementById(`sc-${key}`).textContent = value;
  setTimeout(() => {
    document.getElementById(`bar-${key}`).style.width = `${value}%`;
  }, 50);
}

function scoreColor(v) {
  if (v >= 70) return "var(--good)";
  if (v >= 45) return "var(--moderate)";
  return "var(--poor)";
}

// ── Radar Chart ───────────────────────────────────────────────────────────────
function buildRadar(data) {
  const ctx = document.getElementById("radarChart");

  if (radarChart) { radarChart.destroy(); radarChart = null; }

  const env   = data.environmental_details.map(d => d.value);
  const risk  = data.risk_details.map(d => d.value);
  const liv   = data.livability_details.map(d => d.value);

  const labels = ["Hava", "Yeşil Alan", "Su", "Sessizlik",
                  "Sel Güv.", "Deprem", "Yangın", "Altyapı",
                  "Ulaşım", "Eğitim", "Sağlık", "Ekonomi"];

  const values = [...env, ...risk, ...liv];

  radarChart = new Chart(ctx, {
    type: "radar",
    data: {
      labels,
      datasets: [{
        label: "Skor",
        data: values,
        backgroundColor: "rgba(88,166,255,0.12)",
        borderColor: "#58a6ff",
        borderWidth: 1.5,
        pointBackgroundColor: values.map(v =>
          v >= 70 ? "var(--good)" : v >= 45 ? "var(--moderate)" : "var(--poor)"
        ),
        pointRadius: 3,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        r: {
          min: 0, max: 100,
          ticks: {
            display: false,
            stepSize: 25,
          },
          grid:        { color: "rgba(48,54,61,0.8)" },
          angleLines:  { color: "rgba(48,54,61,0.6)" },
          pointLabels: { color: "#8b949e", font: { size: 9 } },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "#161b22",
          borderColor: "#30363d",
          borderWidth: 1,
          titleColor: "#e6edf3",
          bodyColor: "#8b949e",
          callbacks: {
            label: ctx => ` ${ctx.raw} puan`,
          },
        },
      },
    },
  });
}

// ── Detail Tabs ───────────────────────────────────────────────────────────────
function renderTab(containerId, details) {
  const container = document.getElementById(containerId);
  container.innerHTML = "";

  details.forEach(d => {
    const row = document.createElement("div");
    row.className = "metric-row";
    row.innerHTML = `
      <span class="metric-label">${d.label}</span>
      <span class="metric-value" style="color:${metricColor(d.status)}">${d.value}</span>
      <span class="metric-unit">${d.unit}</span>
      <span class="metric-status ${d.status}">${statusLabel(d.status)}</span>
    `;
    container.appendChild(row);
  });
}

function metricColor(status) {
  return status === "good" ? "var(--good)" :
         status === "moderate" ? "var(--moderate)" : "var(--poor)";
}

function statusLabel(s) {
  return s === "good" ? "İyi" : s === "moderate" ? "Orta" : "Düşük";
}

function switchTab(key) {
  document.querySelectorAll(".tab").forEach(t => {
    t.classList.toggle("active", t.dataset.tab === key);
  });
  ["env", "risk", "liv"].forEach(k => {
    document.getElementById(`tab-${k}`).style.display = k === key ? "flex" : "none";
  });
}

// ── Reset ─────────────────────────────────────────────────────────────────────
function resetAll() {
  if (currentMarker) { map.removeLayer(currentMarker); currentMarker = null; }
  if (heatLayer)     { map.removeLayer(heatLayer);     heatLayer = null; heatVisible = false; }
  document.getElementById("btn-heatmap").classList.remove("active");
  showPanel("panel-idle");
  map.setView([39.2, 35.4], 6, { animate: true });

  // Bar sıfırla
  ["env", "risk", "liv"].forEach(k => {
    const bar = document.getElementById(`bar-${k}`);
    if (bar) bar.style.width = "0%";
  });
}

// ── Wire up events ────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  initMap();

  document.getElementById("btn-heatmap").addEventListener("click", toggleHeatmap);
  document.getElementById("btn-reset").addEventListener("click", resetAll);

  document.querySelectorAll(".tab").forEach(btn => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });
});
