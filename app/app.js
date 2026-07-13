/* Ribat Watch — 日本のモスク × 政治的区割り の地図アプリ本体。

   build_districts.py が生成した正規化済み GeoJSON を読み込んで描画する:
   - data/mosques.geojson … 地点の属性: {layer, name, code, removed_from_gmaps, source_url}
   - data/districts_<muni|pref|hr|hc>.geojson … 区の属性: {level, code, name, mosque, prayer, planned, total}

   主な機能: 地点表示・点ヒートマップ・区ごとのコロプレス（塗り分け）・
   Local/Electoral/Both/Off の切替・凡例・ホバー表示・ポップアップ。
*/
'use strict';

// 区レベルごとの GeoJSON ファイル
const DISTRICT_FILES = {
  muni: 'data/districts_muni.geojson',   // 市区町村
  pref: 'data/districts_pref.geojson',   // 都道府県
  hr:   'data/districts_hr.geojson',     // 衆院小選挙区
  hc:   'data/districts_hc.geojson',     // 参院選挙区
};
// ポップアップ等に表示するレベル名
const LEVEL_LABEL = { muni:'市区町村', pref:'都道府県', hr:'衆院小選挙区', hc:'参院選挙区' };
// 地点種別ごとの色と円の半径
const POINT_STYLE = {
  mosque:      { color:'#d7301f', r:5 },   // モスク（赤）
  prayer_room: { color:'#2166ac', r:4 },   // 祈祷室（青）
  planned:     { color:'#238b45', r:5 },   // 建設予定地（緑）
};
// コロプレスの連続配色（薄黄→濃赤 / YlOrRd 系）
const RAMP = ['#ffffb2','#fed976','#feb24c','#fd8d3c','#f03b20','#bd0026'];
const ZERO_FILL = '#eeeeee';               // 件数0の区の塗り色（薄い灰）

// アプリの状態。UIの選択内容をここに集約し、描画関数がこれを参照する。
const state = {
  level:'local', localUnit:'muni', electoralUnit:'hr',   // 区レベルと下位単位
  metric:'mosque_prayer',                                // 塗り分けに使う集計指標
  points:{ mosque:true, prayer_room:true, planned:true }, heat:false,  // 地点・ヒートの表示
};

let map, canvasRenderer;
let mosquesGeo = null;
const DISTRICTS = {};          // レベルキー → GeoJSON（読込失敗時は null）
let filledLayer = null, outlineLayer = null;   // 塗り分けレイヤー／輪郭レイヤー
const pointGroups = {};        // 種別 → L.layerGroup（地点マーカー群）
let heatLayer = null;
let currentScale = null;       // 現在の配色スケール {grades:[...], color(v)}

// ---------- 補助関数 ----------
// GeoJSON を fetch で読む。失敗（404など）は null を返してアプリを止めない。
async function fetchJSON(url){
  try{ const r = await fetch(url); if(!r.ok) return null; return await r.json(); }
  catch(e){ return null; }
}
// 選択中の指標に応じて、その区の集計値を返す
function metricValue(p){
  if(state.metric==='mosque') return p.mosque||0;                     // モスクのみ
  if(state.metric==='mosque_prayer') return (p.mosque||0)+(p.prayer||0);  // ＋祈祷室
  return p.total||0;                                                  // 全て（予定地含む）
}
// 値の分布から分位ベースの配色スケールを作る（レベル・指標を変える度に再計算）
function quantileScale(values){
  const pos = values.filter(v=>v>0).sort((a,b)=>a-b);   // 正の値だけ昇順に
  if(pos.length===0) return { grades:[1], color:()=>ZERO_FILL };
  const max = pos[pos.length-1];
  // 1 から最大値までの間に最大6段階の区切りを作る
  const grades=[];
  const n=Math.min(6, Math.max(1, max));
  for(let i=0;i<n;i++){
    const q = pos[Math.min(pos.length-1, Math.floor((i/n)*pos.length))];  // i/n 分位点
    grades.push(Math.max(1, Math.round(q)));
  }
  // 重複を除いて昇順にし、最大値の段を必ず含める
  const uniq=[...new Set(grades)].sort((a,b)=>a-b);
  if(uniq[uniq.length-1] < max) uniq.push(max);
  // 値 → 色 を返す関数。値が入る段の番号を配色 RAMP にマッピングする
  const color = (v)=>{
    if(!v || v<=0) return ZERO_FILL;
    let idx=0;
    for(let i=0;i<uniq.length;i++){ if(v>=uniq[i]) idx=i; }
    const ci = Math.round(idx/(Math.max(1,uniq.length-1))*(RAMP.length-1));
    return RAMP[ci];
  };
  return { grades:uniq, color };
}

// ---------- 地点（点）レイヤー ----------
// モスク地点を種別ごとの円マーカー群にまとめ、ポップアップを付ける
function buildPoints(){
  const cats = { mosque:[], prayer_room:[], planned:[] };
  mosquesGeo.features.forEach(f=>{
    const p=f.properties, [lon,lat]=f.geometry.coordinates;
    const cat = p.layer in POINT_STYLE ? p.layer : 'mosque';
    const st = POINT_STYLE[cat];
    // ★ 付き（Google マップ登録削除済み）は半透明で表示
    const removed = p.removed_from_gmaps===true || p.removed_from_gmaps==='True';
    const m = L.circleMarker([lat,lon], {
      renderer:canvasRenderer, radius:st.r, weight:1,
      color:'#fff', fillColor:st.color, fillOpacity:removed?0.35:0.9, opacity:removed?0.5:1,
    });
    // ポップアップ HTML（名称・種別・コード・出典リンク）
    let html = `<span class="pname">${removed?'<span class="removed">★ </span>':''}${p.name||'（名称不明）'}</span>`;
    html += `<div class="pcounts">${LEVEL_LABEL_POINT(cat)}${p.code?` · ${p.code}`:''}</div>`;
    if(p.source_url) html += `<a href="${p.source_url}" target="_blank" rel="noopener">出典 ↗</a>`;
    m.bindPopup(html);
    cats[cat].push(m);
  });
  for(const c of Object.keys(cats)) pointGroups[c] = L.layerGroup(cats[c]);
  // 凡例の件数表示を更新
  document.getElementById('n-mosque').textContent = cats.mosque.length;
  document.getElementById('n-prayer').textContent = cats.prayer_room.length;
  document.getElementById('n-planned').textContent = cats.planned.length;
}
// 地点ポップアップ用の種別ラベル
function LEVEL_LABEL_POINT(c){ return c==='mosque'?'モスク':c==='prayer_room'?'祈祷室':'予定地'; }

// 全地点からカーネル密度ヒートマップ用のレイヤーを作る（表示切替は renderPoints で）
function buildHeat(){
  const pts = mosquesGeo.features.map(f=>{
    const [lon,lat]=f.geometry.coordinates; return [lat,lon,0.8];   // [緯度,経度,重み]
  });
  heatLayer = L.heatLayer(pts, { radius:22, blur:18, maxZoom:11,
    gradient:{0.2:'#2166ac',0.45:'#41ab5d',0.65:'#fdae61',0.85:'#f03b20',1:'#7f0000'} });
}

// ---------- 区（ポリゴン）レイヤー ----------
// 塗り分けの各区のスタイル（集計値→色）
function styleFilled(f){
  const v = metricValue(f.properties);
  return { renderer:canvasRenderer, color:'#ffffff', weight:0.5, fillColor:currentScale.color(v),
           fillOpacity: v>0?0.78:0.35 };
}
// Both 表示時に上に重ねる選挙区の輪郭スタイル（塗りなし・破線の紫）
function styleOutline(){
  return { renderer:canvasRenderer, color:'#4a148c', weight:1.6, opacity:0.9, fill:false, dashArray:'4 3' };
}
// 区クリック時のポップアップ HTML（種別内訳の件数）
function districtPopup(p){
  const total=p.total||0;
  return `<span class="pname">${p.name||'（名称不明）'}</span>`+
    `<div class="pcounts">${LEVEL_LABEL[p.level]||p.level}${p.code?` · ${p.code}`:''}<br>`+
    `🕌 ${p.mosque||0} モスク · 🧎 ${p.prayer||0} 祈祷室 · 🏗️ ${p.planned||0} 予定地 · <b>合計 ${total}</b></div>`;
}
// 区レイヤーにホバー・クリックのイベントを付ける
function attachDistrictHandlers(layer, isFilled){
  layer.on('mouseover', e=>{
    const l=e.layer; if(isFilled){ l.setStyle({weight:2, color:'#222'}); l.bringToFront(); }  // ハイライト
    const p=l.feature.properties;
    // 地図下部の吹き出しと、パネル内の読み取り欄を更新
    const hb=document.getElementById('hoverbox'); hb.hidden=false;
    hb.innerHTML=`<b>${p.name}</b> — 🕌 ${p.mosque||0} · 計 ${metricValue(p)}`;
    document.getElementById('readout').innerHTML =
      `<b>${p.name}</b><br>🕌 ${p.mosque||0} モスク · 🧎 ${p.prayer||0} 祈祷室 · 🏗️ ${p.planned||0} 予定地`;
  });
  layer.on('mouseout', e=>{
    if(isFilled) filledLayer.resetStyle(e.layer);   // ハイライト解除
    document.getElementById('hoverbox').hidden=true;
  });
  layer.on('click', e=> e.layer.bindPopup(districtPopup(e.layer.feature.properties)).openPopup());
}

// 現在の state に基づいて区レイヤーを再描画する
function renderDistricts(){
  if(filledLayer){ map.removeLayer(filledLayer); filledLayer=null; }   // 既存を除去
  if(outlineLayer){ map.removeLayer(outlineLayer); outlineLayer=null; }

  // どのレベルを「塗り分け」／「輪郭」で出すか決める
  let filledKey=null, outlineKey=null;
  if(state.level==='local') filledKey=state.localUnit;
  else if(state.level==='electoral') filledKey=state.electoralUnit;
  else if(state.level==='both'){ filledKey=state.localUnit; outlineKey=state.electoralUnit; }  // 行政区=塗り, 選挙区=輪郭

  // 塗り分けレイヤーの値分布から配色スケールを計算して描画
  if(filledKey && DISTRICTS[filledKey]){
    const vals = DISTRICTS[filledKey].features.map(f=>metricValue(f.properties));
    currentScale = quantileScale(vals);
    filledLayer = L.geoJSON(DISTRICTS[filledKey], { style:styleFilled });
    attachDistrictHandlers(filledLayer, true);
    filledLayer.addTo(map);
  } else { currentScale=null; }

  // Both のときは選挙区を輪郭として重ねる
  if(outlineKey && DISTRICTS[outlineKey]){
    outlineLayer = L.geoJSON(DISTRICTS[outlineKey], { style:styleOutline });
    attachDistrictHandlers(outlineLayer, false);
    outlineLayer.addTo(map);
  }
  updateLegend();
}

// 凡例（配色スケールの各段）を更新する
function updateLegend(){
  const el=document.getElementById('legend');
  if(!currentScale){ el.innerHTML=''; return; }
  const g=currentScale.grades;
  let rows=`<div class="row"><span class="swatch" style="background:${ZERO_FILL}"></span> 0</div>`;
  for(let i=0;i<g.length;i++){
    const lo=g[i], hi=g[i+1];
    const label = hi ? `${lo}–${hi-1}` : `${lo}+`;   // 例: 「3–16」「17+」
    rows+=`<div class="row"><span class="swatch" style="background:${currentScale.color(lo)}"></span> ${label}</div>`;
  }
  el.innerHTML=rows;
}

// ---------- 地点・ヒートの表示切替 ----------
function renderPoints(){
  for(const c of Object.keys(pointGroups)){
    const on = state.points[c];
    if(on && !map.hasLayer(pointGroups[c])) pointGroups[c].addTo(map);      // 表示
    if(!on && map.hasLayer(pointGroups[c])) map.removeLayer(pointGroups[c]); // 非表示
  }
  if(heatLayer){
    if(state.heat && !map.hasLayer(heatLayer)) heatLayer.addTo(map);
    if(!state.heat && map.hasLayer(heatLayer)) map.removeLayer(heatLayer);
  }
}

// ---------- UI 配線 ----------
// レベルに応じて下位単位のセクションを出し分ける
function syncSections(){
  document.getElementById('sec-local').style.display   = (state.level==='local'||state.level==='both')?'':'none';
  document.getElementById('sec-electoral').style.display= (state.level==='electoral'||state.level==='both')?'':'none';
  document.getElementById('both-hint').hidden = state.level!=='both';
}
// 各コントロールに変更イベントを結び付ける
function wireUI(){
  document.querySelectorAll('input[name="level"]').forEach(r=>r.addEventListener('change',e=>{
    state.level=e.target.value; syncSections(); renderDistricts();
  }));
  document.querySelectorAll('input[name="localUnit"]').forEach(r=>r.addEventListener('change',e=>{
    state.localUnit=e.target.value; renderDistricts();
  }));
  document.querySelectorAll('input[name="electoralUnit"]').forEach(r=>r.addEventListener('change',e=>{
    state.electoralUnit=e.target.value; renderDistricts();
  }));
  document.getElementById('metric').addEventListener('change',e=>{ state.metric=e.target.value; renderDistricts(); });
  document.getElementById('pt-mosque').addEventListener('change',e=>{state.points.mosque=e.target.checked;renderPoints();});
  document.getElementById('pt-prayer').addEventListener('change',e=>{state.points.prayer_room=e.target.checked;renderPoints();});
  document.getElementById('pt-planned').addEventListener('change',e=>{state.points.planned=e.target.checked;renderPoints();});
  document.getElementById('pt-heat').addEventListener('change',e=>{state.heat=e.target.checked;renderPoints();});
  document.getElementById('panel-toggle').addEventListener('click',()=>{
    document.getElementById('panel').classList.toggle('collapsed');   // パネル折りたたみ
  });
  // 読み込めなかったレベルのコントロールは無効化してグレーアウトする
  for(const key of Object.keys(DISTRICT_FILES)){
    if(!DISTRICTS[key]){
      const sel = key==='muni'?'localUnit':key==='pref'?'localUnit':'electoralUnit';
      const input=document.querySelector(`input[name="${sel}"][value="${key}"]`);
      if(input){ input.disabled=true; input.parentElement.style.opacity=.4; input.parentElement.title='境界ファイルが未生成です'; }
    }
  }
}

// 初期状態を DOM の値から読み取る。
// （Chrome の再読込時のフォーム状態復元と、JS 側の state がズレないようにするため）
function readStateFromDOM(){
  const g=n=>document.querySelector(`input[name="${n}"]:checked`);
  if(g('level')) state.level=g('level').value;
  if(g('localUnit') && !g('localUnit').disabled) state.localUnit=g('localUnit').value;
  if(g('electoralUnit') && !g('electoralUnit').disabled) state.electoralUnit=g('electoralUnit').value;
  state.metric=document.getElementById('metric').value;
  state.points.mosque=document.getElementById('pt-mosque').checked;
  state.points.prayer_room=document.getElementById('pt-prayer').checked;
  state.points.planned=document.getElementById('pt-planned').checked;
  state.heat=document.getElementById('pt-heat').checked;
}

// ---------- 初期化 ----------
async function init(){
  // 地図を生成（日本中心・多数ポリゴン対策で Canvas レンダラを既定に）
  map = L.map('map', { preferCanvas:true, zoomControl:true }).setView([37.6,137.5], 5);
  canvasRenderer = L.canvas({ padding:0.5 });
  // 背景タイル（CARTO Positron。表示にはネット接続が必要）
  L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
    attribution:'© OpenStreetMap contributors © CARTO', subdomains:'abcd', maxZoom:19,
  }).addTo(map);

  // モスク地点を読み込む（無ければ空で続行し、注意書きを出す）
  mosquesGeo = await fetchJSON('data/mosques.geojson');
  if(!mosquesGeo){
    document.getElementById('readout').innerHTML='<b style="color:#b00">mosques.geojson が見つかりません</b> — 先にビルドを実行してください。';
    mosquesGeo={type:'FeatureCollection',features:[]};
  }
  buildPoints(); buildHeat();

  // 4レベルの区データを並行で読み込む
  const loaded = await Promise.all(Object.entries(DISTRICT_FILES).map(async([k,url])=>[k, await fetchJSON(url)]));
  loaded.forEach(([k,g])=>{ DISTRICTS[k]=g; });
  const srcEl=document.getElementById('src-boundaries');
  srcEl.textContent = Object.values(DISTRICTS).some(Boolean) ? '読込済み' : '（未生成）';

  // UI 配線 → 初期状態を DOM から取得 → 各レイヤーを初回描画
  wireUI(); readStateFromDOM(); syncSections(); renderPoints(); renderDistricts();
}
init();
