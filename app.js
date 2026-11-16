"use strict";

const FALLBACK = [24.9537, 121.2257]; // 中壢火車站，定位失敗時使用
const LIST_SIZE = 10;
const COLORS = { paid: "#e8730c", free: "#1f9d55", unknown: "#7a8591", mine: "#8b5cf6", lot: "#0b7285" };
const DAY = ["", "一", "二", "三", "四", "五", "六", "日"];

const state = {
  me: null,          // [lat, lng]
  radius: 500,
  filters: { now: false, free: false, osm: true, lot: true },
  items: [],         // {id, kind, name, sub, geom: [[lat,lng],...], props, layer}
  selected: null,
};

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
};
Object.assign(state.filters, store.get("filters", {}));
state.radius = store.get("radius", state.radius);

// ---------- map ----------
const map = L.map("map", { zoomControl: false, attributionControl: true }).setView(FALLBACK, 16);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, attribution: "© OpenStreetMap",
}).addTo(map);
const layer = L.layerGroup().addTo(map);
let meMarker = null, radiusCircle = null, following = true;

// ---------- geometry ----------
function metersPerDeg(lat) { return [110540, 111320 * Math.cos(lat * Math.PI / 180)]; }
function nearestOnGeom(p, geom) {
  const [ky, kx] = metersPerDeg(p[0]);
  const P = [0, 0];
  const xy = q => [(q[1] - p[1]) * kx, (q[0] - p[0]) * ky];
  let best = { d: Infinity, pt: geom[0] };
  if (geom.length === 1) {
    const a = xy(geom[0]);
    return { d: Math.hypot(a[0], a[1]), pt: geom[0] };
  }
  for (let i = 0; i < geom.length - 1; i++) {
    const a = xy(geom[i]), b = xy(geom[i + 1]);
    const dx = b[0] - a[0], dy = b[1] - a[1];
    const len2 = dx * dx + dy * dy || 1;
    let t = ((P[0] - a[0]) * dx + (P[1] - a[1]) * dy) / len2;
    t = Math.max(0, Math.min(1, t));
    const x = a[0] + t * dx, y = a[1] + t * dy;
    const d = Math.hypot(x, y);
    if (d < best.d) best = { d, pt: [p[0] + y / ky, p[1] + x / kx] };
  }
  return best;
}
function fmtDist(m) { return m < 1000 ? `${Math.round(m / 10) * 10} m` : `${(m / 1000).toFixed(1)} km`; }

// ---------- fares ----------
function hm(s) { const [h, m] = s.split(":").map(Number); return h * 60 + m; }
function activeRule(rules, date = new Date()) {
  const dow = date.getDay() || 7;
  const t = date.getHours() * 60 + date.getMinutes();
  return rules.find(r => !r.free && r.days.includes(dow) && t >= hm(r.start) && t < hm(r.end)) || null;
}
function dayRange(days) {
  if (days.length === 7) return "每天";
  const s = days[0], e = days[days.length - 1];
  return days.length === 1 ? `週${DAY[s]}` : `週${DAY[s]}至週${DAY[e]}`;
}
function shortHours(r) { return `${r.start.slice(0, 2)}–${r.end.slice(0, 2)}`; }
function shortPrice(p) {
  return p.replace(/^第一.*$/, "累進").replace(/元\/次/, " 元／次").replace(/元\/30分鐘/, " 元／30 分");
}

// ---------- data ----------
function toLatLngs(g) {
  return g.type === "Point" ? [[g.coordinates[1], g.coordinates[0]]] : g.coordinates.map(c => [c[1], c[0]]);
}
async function loadJSON(url) {
  const r = await fetch(url, { cache: "no-cache" });
  if (!r.ok) throw new Error(url);
  return r.json();
}
function mineItems() {
  return store.get("mine", []).map(m => ({
    id: m.id, kind: "mine", name: m.name || "我的停車點", sub: "自己標記", geom: [[m.lat, m.lng]], props: m,
  }));
}
async function loadData() {
  const [seg, osm, lots] = await Promise.all([
    loadJSON("data/segments.geojson"),
    loadJSON("data/osm_moto.geojson").catch(() => ({ features: [] })),
    loadJSON("data/lots.geojson").catch(() => ({ features: [] })),
  ]);
  document.getElementById("updated").textContent = seg.updated ? `資料更新：${seg.updated}` : "";
  const items = seg.features.map(f => {
    const p = f.properties;
    return { id: p.id, kind: "paid", name: p.name, sub: `${p.town}｜${p.from}－${p.to}`, geom: toLatLngs(f.geometry), props: p };
  });
  for (const f of lots.features) {
    const p = f.properties;
    items.push({ id: `lot-${p.id}`, kind: p.free ? "free" : "lot", lot: true, name: p.name,
      sub: p.address || "", geom: toLatLngs(f.geometry), props: p });
  }
  for (const f of osm.features) {
    const p = f.properties;
    const kind = p.fee === "no" ? "free" : p.fee === "yes" ? "paid" : "unknown";
    items.push({ id: p.id, kind, osm: true, name: p.name || (p.lot ? "停車場（OSM）" : "機車停車場（OSM）"), sub: "OpenStreetMap 標記", geom: toLatLngs(f.geometry), props: p });
  }
  state.items = items.concat(mineItems());
}

// ---------- filtering & rendering ----------
function visible(it) {
  if (it.osm && !state.filters.osm) return false;
  if (it.lot && !state.filters.lot) return false;
  if (state.filters.free && !(it.kind === "free" || it.kind === "mine")) return false;
  if (state.filters.now && !(it.props.rules && activeRule(it.props.rules))) return false;
  return true;
}
function describe(it) {
  if (it.props.rules) {
    const now = activeRule(it.props.rules);
    const r = now || it.props.rules[0];
    return `${shortPrice(r.price)}｜${shortHours(r)}${now ? "" : "｜現在免費"}`;
  }
  if (it.kind === "mine") return "自己標記";
  if (it.lot) return `停車場｜${it.props.temp.join("、") || "機車費率未註明"}${it.props.cap.length ? "｜有上限" : ""}`;
  const bits = [it.kind === "free" ? "免費" : it.kind === "paid" ? "收費" : "收費未標示"];
  if (it.props.capacity) bits.push(`${it.props.capacity} 格`);
  if (it.props.hours) bits.push(it.props.hours);
  return bits.join("｜");
}

function render() {
  layer.clearLayers();
  const center = state.me || [map.getCenter().lat, map.getCenter().lng];
  for (const it of state.items) {
    it.near = nearestOnGeom(center, it.geom);
    if (!visible(it)) continue;
    const color = COLORS[it.kind];
    const isSel = state.selected === it.id;
    let lyr;
    if (it.lot || it.props.lot) {
      lyr = L.marker(it.geom[0], { icon: L.divIcon({ className: "", iconSize: [22, 22],
        html: `<div class="lot-pin${isSel ? " sel" : ""}" style="background:${color}">P</div>` }) });
    } else if (it.geom.length > 1) {
      lyr = L.polyline(it.geom, { color, weight: isSel ? 9 : 6, opacity: .9, lineCap: "round" });
    } else {
      lyr = L.circleMarker(it.geom[0], { radius: isSel ? 10 : 7, color: "#fff", weight: 2, fillColor: color, fillOpacity: 1 });
    }
    lyr.on("click", e => { L.DomEvent.stop(e); select(it.id, false); });
    lyr.addTo(layer);
  }

  const near = state.items.filter(it => visible(it) && it.near.d <= state.radius)
    .sort((a, b) => a.near.d - b.near.d);
  const list = document.getElementById("list");
  list.innerHTML = "";
  for (const it of near.slice(0, LIST_SIZE)) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="dot ${it.kind}"></span>
      <div class="li-main"><div class="li-name"></div><div class="li-sub"></div></div>
      <span class="li-dist">${fmtDist(it.near.d)}</span>`;
    li.querySelector(".li-name").textContent = it.name;
    li.querySelector(".li-sub").textContent = describe(it);
    li.addEventListener("click", () => select(it.id, true));
    list.appendChild(li);
  }
  if (!near.length) {
    list.innerHTML = `<li class="empty">${fmtDist(state.radius)} 內沒有符合條件的停車點。試試放大搜尋半徑或關掉篩選。</li>`;
  }
  const where = state.me ? "你附近" : "地圖中心附近";
  document.getElementById("status").textContent =
    `${where} ${fmtDist(state.radius)} 內 ${near.length} 處${near.length > LIST_SIZE ? `，顯示最近 ${LIST_SIZE} 處` : ""}`;

  if (radiusCircle) radiusCircle.remove();
  radiusCircle = L.circle(center, { radius: state.radius, color: "#1f6feb", weight: 1, fillOpacity: .04, interactive: false }).addTo(map);
  if (state.selected) renderDetail();
}

function navUrl(pt) {
  return `https://www.google.com/maps/dir/?api=1&destination=${pt[0].toFixed(6)},${pt[1].toFixed(6)}&travelmode=two-wheeler`;
}
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

function renderDetail() {
  const it = state.items.find(x => x.id === state.selected);
  const box = document.getElementById("detail");
  if (!it) { box.hidden = true; return; }
  const target = state.me ? it.near.pt : it.geom[Math.floor(it.geom.length / 2)];
  let rows = "";
  if (it.props.rules) {
    const now = activeRule(it.props.rules);
    rows += `<div class="row"><b>起訖</b><span>${esc(it.props.from)} － ${esc(it.props.to)}</span></div>`;
    rows += `<div class="row"><b>收費</b><span>${it.props.rules.map(r =>
      `${esc(dayRange(r.days))} ${esc(r.start)}–${esc(r.end)}：${esc(r.free ? "免費" : r.price)}`).join("<br>")}</span></div>`;
    rows += `<div class="row"><b>現在</b><span>${now ? `收費中（${esc(now.price)}）` : "不收費"}</span></div>`;
    if (it.props.approx) rows += `<div class="row"><b>注意</b><span>位置為推估，請以現場為準</span></div>`;
  } else if (it.lot) {
    rows += `<div class="row"><b>類型</b><span>${esc(it.props.owner)}停車場</span></div>`;
    if (it.props.address) rows += `<div class="row"><b>地址</b><span>${esc(it.props.address)}</span></div>`;
    const p = it.props;
    rows += `<div class="row"><b>機車臨停</b><span>${p.temp.map(esc).join("<br>") || "資料未註明"}</span></div>`;
    rows += `<div class="row"><b>收費上限</b><span>${p.cap.length ? p.cap.map(esc).join("<br>")
      : p.free ? "免費" : p.temp.some(t => t.includes("小時")) ? "資料未註明上限，可能依時數累加"
      : "按次收費，資料未註明是否限當日"}</span></div>`;
    if (p.month.length) rows += `<div class="row"><b>機車月租</b><span>${p.month.map(esc).join("<br>")}</span></div>`;
    rows += `<div class="row"><b>原始說明</b><span class="muted">${esc(p.guide)}</span></div>`;
  } else if (it.kind === "mine") {
    rows += `<div class="row"><b>類型</b><span>你自己標記的停車點</span></div>`;
  } else {
    rows += `<div class="row"><b>收費</b><span>${it.kind === "free" ? "免費" : it.kind === "paid" ? "收費" : "OSM 沒有標示"}</span></div>`;
    if (it.props.charge) rows += `<div class="row"><b>價格</b><span>${esc(it.props.charge)}</span></div>`;
    if (it.props.capacity) rows += `<div class="row"><b>機車格數</b><span>${esc(it.props.capacity)}</span></div>`;
    if (it.props.hours) rows += `<div class="row"><b>時段</b><span>${esc(it.props.hours)}</span></div>`;
  }
  if (state.me) rows += `<div class="row"><b>距離</b><span>${fmtDist(it.near.d)}（直線）</span></div>`;
  const badge = it.props.rules ? (activeRule(it.props.rules) ? `<span class="badge on">收費中</span>` : `<span class="badge off">現在免費</span>`) : "";
  box.innerHTML = `<h2>${esc(it.name)}${badge}</h2>${rows}
    <div class="actions">
      <a class="btn primary" href="${navUrl(target)}" target="_blank" rel="noopener">導航</a>
      ${it.kind === "mine" ? `<button class="btn" id="del">刪除</button>` : ""}
      <button class="btn" id="close">關閉</button>
    </div>`;
  box.hidden = false;
  box.querySelector("#close").onclick = () => { state.selected = null; box.hidden = true; document.body.classList.remove("sheet-detail"); render(); };
  const del = box.querySelector("#del");
  if (del) del.onclick = () => {
    store.set("mine", store.get("mine", []).filter(m => m.id !== it.id));
    state.items = state.items.filter(x => x.id !== it.id);
    state.selected = null; box.hidden = true; document.body.classList.remove("sheet-detail"); render();
  };
}

function select(id, fly) {
  state.selected = id;
  const it = state.items.find(x => x.id === id);
  document.body.classList.remove("sheet-open");
  document.body.classList.add("sheet-detail");
  if (fly && it) {
    following = false;
    const b = L.latLngBounds(it.geom);
    if (state.me) b.extend(state.me);
    map.flyToBounds(b, { maxZoom: 18, duration: .6,
      paddingTopLeft: [30, 110], paddingBottomRight: [30, window.innerHeight * 0.5 + 30] });
  }
  render();
  document.getElementById("detail").scrollIntoView({ block: "nearest" });
}

// ---------- location ----------
function setMe(lat, lng, acc) {
  state.me = [lat, lng];
  if (!meMarker) {
    meMarker = L.marker(state.me, { icon: L.divIcon({ className: "", html: '<div class="me"></div>', iconSize: [18, 18] }), interactive: false, zIndexOffset: 1000 }).addTo(map);
  } else meMarker.setLatLng(state.me);
  if (following) map.setView(state.me, Math.max(map.getZoom(), 16));
  render();
}
function locate() {
  if (!("geolocation" in navigator)) {
    document.getElementById("status").textContent = "這個瀏覽器不支援定位，顯示中壢火車站附近";
    render();
    return;
  }
  navigator.geolocation.watchPosition(
    p => setMe(p.coords.latitude, p.coords.longitude, p.coords.accuracy),
    err => {
      const msg = err.code === 1 ? "沒有定位權限，請在瀏覽器設定允許定位。先顯示中壢火車站附近" : "定位失敗，先顯示中壢火車站附近";
      render();
      document.getElementById("status").textContent = msg;
    },
    { enableHighAccuracy: true, maximumAge: 15000, timeout: 20000 },
  );
}

// ---------- UI wiring ----------
document.getElementById("locate").onclick = () => {
  following = true;
  if (state.me) map.setView(state.me, 17); else locate();
};
map.on("dragstart", () => { following = false; });
map.on("moveend", () => { if (!state.me) render(); });
map.on("click", () => document.body.classList.remove("sheet-open", "sheet-detail"));
map.on("contextmenu", e => {
  const name = prompt("幫這個停車點取個名字（例如：公司樓下）", "我的停車點");
  if (name === null) return;
  const m = { id: `mine-${Date.now()}`, name: name.trim() || "我的停車點", lat: e.latlng.lat, lng: e.latlng.lng };
  store.set("mine", store.get("mine", []).concat(m));
  state.items.push(...mineItems().filter(x => x.id === m.id));
  select(m.id, false);
});
document.getElementById("handle").onclick = () => document.body.classList.toggle("sheet-open");

function syncChips() {
  document.querySelectorAll("[data-filter]").forEach(b => b.setAttribute("aria-pressed", String(!!state.filters[b.dataset.filter])));
  document.querySelectorAll(".radius").forEach(b => b.setAttribute("aria-pressed", String(+b.dataset.r === state.radius)));
}
document.querySelectorAll("[data-filter]").forEach(b => b.onclick = () => {
  const k = b.dataset.filter;
  state.filters[k] = !state.filters[k];
  if (k === "now" && state.filters.now) state.filters.free = false;
  if (k === "free" && state.filters.free) state.filters.now = false;
  store.set("filters", state.filters); syncChips(); render();
});
document.querySelectorAll(".radius").forEach(b => b.onclick = () => {
  state.radius = +b.dataset.r; store.set("radius", state.radius); syncChips(); render();
});
syncChips();

// sheet drag (swipe up / down on the handle area)
(() => {
  const sheet = document.getElementById("sheet");
  let y0 = null;
  sheet.addEventListener("touchstart", e => {
    if (e.target.closest(".list, .detail") && document.body.classList.contains("sheet-open")) return;
    y0 = e.touches[0].clientY;
  }, { passive: true });
  sheet.addEventListener("touchend", e => {
    if (y0 === null) return;
    const dy = e.changedTouches[0].clientY - y0;
    if (dy < -30) document.body.classList.add("sheet-open");
    if (dy > 30) document.body.classList.remove("sheet-open");
    y0 = null;
  });
})();

setInterval(render, 60000); // 收費狀態跟著時間變

loadData().then(locate).catch(err => {
  document.getElementById("status").textContent = "資料載入失敗，請稍後重新整理";
  console.error(err);
});

if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
