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
// コロプレスの連続配色アンカー（薄黄→濃赤 / YlOrRd 系）。任意の段階数に補間して使う
const RAMP_ANCHORS = ['#ffffb2','#fed976','#feb24c','#fd8d3c','#f03b20','#bd0026'];
// 点ヒートマップの配色アンカー（冷→熱）。階調（段階数）に応じて段状に再構成して使う
const HEAT_ANCHORS = ['#2166ac','#41ab5d','#fdae61','#f03b20','#7f0000'];
const ZERO_FILL = '#eeeeee';               // 件数0の区の塗り色（薄い灰）

// アプリの状態。UIの選択内容をここに集約し、描画関数がこれを参照する。
const state = {
  level:'local', localUnit:'muni', electoralUnit:'hr',   // 区レベルと下位単位
  metric:'mosque_prayer',                                // 塗り分けに使う集計指標
  buckets:6, classify:'equal',                           // コロプレスの段階数と分類法（既定は等間隔＝最大値を最濃色に線形配色）
  points:{ mosque:true, prayer_room:true, planned:true }, heat:false,  // 地点・ヒートの表示
  heatOpts:{ radius:22, blur:18, intensity:0.8, steps:6 },  // 点ヒートの半径・ぼかし・強度・階調
  rank:{ col:'total', dir:'desc' },                      // ランキングの並び替え（列・方向）
};

let map, canvasRenderer;
let mosquesGeo = null;
const DISTRICTS = {};          // レベルキー → GeoJSON（読込失敗時は null）
let filledLayer = null, outlineLayer = null;   // 塗り分けレイヤー／輪郭レイヤー
const pointGroups = {};        // 種別 → L.layerGroup（地点マーカー群）
let heatLayer = null;
let currentScale = null;       // 現在の配色スケール {grades:[...], colors:[...], color(v)}
let currentFilledKey = null;   // 現在塗り分けているレベルキー（ランキング・再配色で参照）
const filledByCode = {};       // 区コード → 塗り分けレイヤー配列（ランキング行クリックのズーム用。参院の合区は同一コードで複数）

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
// --- 配色補間ユーティリティ（コロプレス／ヒートの任意段階数生成に使う） ---
function hexToRgb(h){ h=h.replace('#',''); return [parseInt(h.slice(0,2),16),parseInt(h.slice(2,4),16),parseInt(h.slice(4,6),16)]; }
function rgbToHex(r,g,b){ return '#'+[r,g,b].map(x=>Math.max(0,Math.min(255,Math.round(x))).toString(16).padStart(2,'0')).join(''); }
// 2色を t（0〜1）で線形補間
function lerpHex(a,b,t){ const [ar,ag,ab]=hexToRgb(a),[br,bg,bb]=hexToRgb(b); return rgbToHex(ar+(br-ar)*t, ag+(bg-ag)*t, ab+(bb-ab)*t); }
// アンカー配色列を t（0〜1）でサンプリングして1色を返す
function sampleAnchors(anchors,t){
  if(t<=0) return anchors[0]; if(t>=1) return anchors[anchors.length-1];
  const x=t*(anchors.length-1), i=Math.floor(x); return lerpHex(anchors[i], anchors[i+1], x-i);
}
// コロプレス用アンカーを n 段階に補間した配色列を返す
function rampColors(n){
  // 単一段階のときは最薄色を使う（最濃赤だと件数1を最大値のように誤認させ、多段階時の
  // 最下段＝最薄色とも不整合になるため）
  if(n<=1) return [RAMP_ANCHORS[0]];
  const out=[]; for(let i=0;i<n;i++) out.push(sampleAnchors(RAMP_ANCHORS, i/(n-1))); return out;
}

// 正の値の並びから n 段階の区切り（各段の下限）を求める。method: 'quantile'|'equal'
function classBreaks(pos, n, method){
  const min=pos[0], max=pos[pos.length-1];
  const raw=[];
  if(method==='equal'){
    // 等間隔: 1〜最大値を n 等分。最大値が最濃色になり、値に比例して段（＝温度）が決まる。
    // 最大値が小さい（例: 市区町村の 1〜4）と自然に段数が減り、スライダー未満になる。
    for(let i=0;i<n;i++) raw.push(min + (max-min)*i/n);
  } else {
    // 分位数: 値の種類が要求段階数以下なら各値をそのまま1段に割り当てる
    //（1 に集中するデータで「1 と 2+」の2段に潰れるのを防ぎ、粒度を最大化する）。
    const distinct=[...new Set(pos.map(v=>Math.max(1, Math.round(v))))].sort((a,b)=>a-b);
    if(distinct.length<=n) return distinct;
    for(let i=0;i<n;i++) raw.push(pos[Math.min(pos.length-1, Math.floor((i/n)*pos.length))]);
  }
  // 件数は整数なので四捨五入し、重複する段を畳んで昇順に（データが疎なら実段数は要求未満になる）
  return [...new Set(raw.map(b=>Math.max(1, Math.round(b))))].sort((a,b)=>a-b);
}

// 値の分布から段階配色スケールを作る（レベル・指標・段階数・分類法を変える度に再計算）
function makeScale(values, n, method){
  const pos = values.filter(v=>v>0).sort((a,b)=>a-b);   // 正の値だけ昇順に
  if(pos.length===0) return { grades:[1], colors:[ZERO_FILL], color:()=>ZERO_FILL, max:0 };
  const grades = classBreaks(pos, Math.max(1, n), method);
  const colors = rampColors(grades.length);
  // 値 → 色。値が属する段（下限以上で最大のもの）の番号を配色にマッピングする
  const color = (v)=>{
    if(!v || v<=0) return ZERO_FILL;
    let idx=0; for(let i=0;i<grades.length;i++){ if(v>=grades[i]) idx=i; }
    return colors[idx];
  };
  return { grades, colors, color, max: pos[pos.length-1] };   // max=この表示の最大件数（凡例の注記に使う）
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

// 強度（1点あたりの重み）を反映した点配列を作る
function heatPoints(){
  const w = state.heatOpts.intensity;
  return mosquesGeo.features.map(f=>{ const [lon,lat]=f.geometry.coordinates; return [lat,lon,w]; });  // [緯度,経度,重み]
}
// 階調（段階数）に応じた段状グラデーションを作る。段数が多いほど滑らか・少ないほど帯状
function heatGradient(steps){
  const grad={}, eps=0.0001;
  for(let i=0;i<steps;i++){
    const lo=i/steps, hi=(i+1)/steps, c=sampleAnchors(HEAT_ANCHORS, steps>1 ? i/(steps-1) : 1);
    grad[Math.max(0.01, +(lo+eps).toFixed(4))] = c;   // 段の始まり
    grad[Math.min(1,   +(hi-eps).toFixed(4))] = c;    // 段の終わり（同色にしてフラットな帯にする）
  }
  grad[1] = sampleAnchors(HEAT_ANCHORS, 1);
  return grad;
}
// 全地点からカーネル密度ヒートマップ用のレイヤーを作る（表示切替は renderPoints で）
function buildHeat(){
  const o=state.heatOpts;
  heatLayer = L.heatLayer(heatPoints(), { radius:o.radius, blur:o.blur, maxZoom:11, gradient:heatGradient(o.steps) });
}
// ヒートの各パラメータを反映する（which: 'radius-blur'|'intensity'|'steps'|'all'）
function updateHeat(which){
  if(!heatLayer) return;
  if(which==='intensity'||which==='all') heatLayer.setLatLngs(heatPoints());
  if(which==='radius-blur'||which==='all') heatLayer.setOptions({ radius:state.heatOpts.radius, blur:state.heatOpts.blur });
  if(which==='steps'||which==='all') heatLayer.setOptions({ gradient:heatGradient(state.heatOpts.steps) });
}
// 点ヒート設定を既定値に戻す
function resetHeat(){
  state.heatOpts={ radius:22, blur:18, intensity:0.8, steps:6 };
  const set=(id,v,fmt)=>{ const el=document.getElementById(id), lab=document.getElementById(id+'-val'); if(el)el.value=v; if(lab)lab.textContent=fmt?fmt(v):v; };
  set('heat-radius',22); set('heat-blur',18); set('heat-intensity',0.8,v=>Number(v).toFixed(1)); set('heat-steps',6);
  updateHeat('all');
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
// ---------- 議員（政治的代表）情報 ----------
// reps_*.json（{level, office, asOf, records:{code:…}}）を読み込んでポップアップに表示する。
// 公職者のみ（知事・衆院・参院議員）を扱う。個人（モスク関係者）の名は一切載せない。
const REPS = { pref:null, hr:null, hc:null };   // レベル → reps データ
let PARTIES = {};                                // 政党名 → {short, color, note}

// 政党名を色付きの小さなタグにする（色は reps_parties.json から引く）
function partyTag(name){
  if(!name) return '';
  const p = PARTIES[name] || {};
  return `<span class="ptag" style="background:${p.color||'#9aa0a6'}">${p.short||name}</span>`;
}
const shortParty = n => (PARTIES[n]||{}).short || n;

// 単一議員（知事・衆院）用の共通ラッパ
function repWrap(office, body, sourceUrl, asOf){
  const src = sourceUrl ? ` <a href="${sourceUrl}" target="_blank" rel="noopener">出典↗</a>` : '';
  return `<div class="reps"><div class="rep-office">${office}</div>`+
         `<div class="rep-name">${body}</div>`+
         `<div class="rep-src">${asOf||''}現在${src}</div></div>`;
}
// 区の議員情報ブロック（レベルに応じて 知事／衆院議員／参院議員一覧）
function repsBlock(p){
  if(p.level==='pref' && REPS.pref){
    const r=REPS.pref.records[p.code]; if(!r) return '';
    const win = r.winCount ? `（${r.winCount}期）` : '';
    const endorse = (r.endorsers && r.endorsers.length)
      ? `<span class="rep-endorse">${r.endorsers.map(shortParty).join('・')} 推薦</span>` : '';
    return repWrap(REPS.pref.office, `${r.name}${win} ${partyTag(r.affiliation)}${endorse}`, r.sourceUrl, REPS.pref.asOf);
  }
  if(p.level==='hr' && REPS.hr){
    const r=REPS.hr.records[p.code]; if(!r) return '';
    const win = r.winCount ? `（${r.winCount}期）` : '';
    const kaiha = (r.kaiha && r.kaiha!==r.party) ? `<span class="rep-endorse">会派: ${shortParty(r.kaiha)}</span>` : '';
    return repWrap(REPS.hr.office, `${r.name}${win} ${partyTag(r.party)}${kaiha}`, r.sourceUrl, REPS.hr.asOf);
  }
  if(p.level==='hc' && REPS.hc){
    const r=REPS.hc.records[p.code]; if(!r || !r.members) return '';
    const rows = r.members.map(m=>
      `<div class="rep-mem">${m.name} ${partyTag(m.party)}<span class="rep-cls">${m.cls?`任期~${m.cls}`:''}</span></div>`).join('');
    return `<div class="reps"><div class="rep-office">${REPS.hc.office} <span class="rep-mag">（${r.members.length}名）</span></div>`+
           `${rows}<div class="rep-src">${REPS.hc.asOf||''}現在</div></div>`;
  }
  return '';   // muni（首長）は Phase 3 で追加予定
}

// 区クリック時のポップアップ HTML（種別内訳の件数 ＋ 議員情報）
function districtPopup(p){
  const total=p.total||0;
  return `<span class="pname">${p.name||'（名称不明）'}</span>`+
    `<div class="pcounts">${LEVEL_LABEL[p.level]||p.level}${p.code?` · ${p.code}`:''}<br>`+
    `🕌 ${p.mosque||0} モスク · 🧎 ${p.prayer||0} 祈祷室 · 🏗️ ${p.planned||0} 予定地 · <b>合計 ${total}</b></div>`+
    repsBlock(p);
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
  currentFilledKey = filledKey;
  for(const k in filledByCode) delete filledByCode[k];   // 旧インデックスを破棄
  if(filledKey && DISTRICTS[filledKey]){
    const vals = DISTRICTS[filledKey].features.map(f=>metricValue(f.properties));
    currentScale = makeScale(vals, state.buckets, state.classify);
    filledLayer = L.geoJSON(DISTRICTS[filledKey], { style:styleFilled,
      onEachFeature:(feat,lyr)=>{ const c=feat.properties.code; (filledByCode[c]=filledByCode[c]||[]).push(lyr); } });   // コード→レイヤー索引（同一コードは配列に集約）
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
  renderRanking();
}

// 段階数・分類法だけ変えた時の再配色（レイヤー再構築は不要）
function recomputeScale(){
  if(!filledLayer || !currentFilledKey){ updateLegend(); return; }
  const vals = DISTRICTS[currentFilledKey].features.map(f=>metricValue(f.properties));
  currentScale = makeScale(vals, state.buckets, state.classify);
  filledLayer.setStyle(styleFilled);
  updateLegend();
}

// 凡例（配色スケールの各段）を更新する
function updateLegend(){
  const el=document.getElementById('legend');
  if(!currentScale){ el.innerHTML=''; return; }
  const g=currentScale.grades, cols=currentScale.colors;
  let rows=`<div class="row"><span class="swatch" style="background:${ZERO_FILL}"></span> 0</div>`;
  const hasData = !(cols.length===1 && cols[0]===ZERO_FILL);   // 正の値が無い時は0段のみ表示
  if(hasData) for(let i=0;i<g.length;i++){
    const lo=g[i], hi=g[i+1];
    const label = hi ? (hi-1>lo ? `${lo}–${hi-1}` : `${lo}`) : `${lo}+`;   // 例: 「3–16」「17+」
    rows+=`<div class="row"><span class="swatch" style="background:${cols[i]}"></span> ${label}</div>`;
  }
  // この表示の最大件数と実効段階数を注記（データが少ないと段階数はスライダー未満になる）
  if(hasData){
    const seg = g.length < state.buckets ? `${g.length}/${state.buckets} 段階` : `${g.length} 段階`;
    rows += `<div class="legend-note">最大 ${currentScale.max} 件 ・ ${seg}</div>`;
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

// ---------- エリアランキング ----------
// ランキング対象のレベルキー（塗り分け中の単位に追従。両方＝自治体側、非表示＝なし）
function rankingKey(){
  if(state.level==='local'||state.level==='both') return state.localUnit;
  if(state.level==='electoral') return state.electoralUnit;
  return null;   // 非表示のときは対象なし
}
// 列名 → その区の値（name は文字列）
function rankColVal(p, col){
  if(col==='mosque') return p.mosque||0;
  if(col==='prayer') return p.prayer||0;
  if(col==='total')  return p.total||0;
  return p.name||'';   // name
}
// 現在の並び順でランキング用のフィーチャ配列を返す（合計1件以上・同一コードは1件に集約）
function rankedRows(key){
  const seen=new Set();
  const feats = DISTRICTS[key].features.filter(f=>{
    const p=f.properties;
    if((p.total||0)<=0) return false;             // 0件の区は除外
    if(seen.has(p.code)) return false;            // 参院の合区など同一コードの重複を1件に
    seen.add(p.code); return true;
  });
  const { col, dir } = state.rank, sgn = dir==='asc'?1:-1;
  feats.sort((a,b)=>{
    const pa=a.properties, pb=b.properties;
    if(col==='name') return String(pa.name||'').localeCompare(String(pb.name||''),'ja')*sgn;
    const d = rankColVal(pa,col)-rankColVal(pb,col);
    if(d!==0) return d*sgn;
    const t=(pb.total||0)-(pa.total||0); if(t!==0) return t;                  // 同値は合計降順→名称で安定化
    return String(pa.name||'').localeCompare(String(pb.name||''),'ja');
  });
  return feats;
}
// ランキングパネルを再描画する
function renderRanking(){
  const key=rankingKey();
  const lbl=document.getElementById('rank-level');
  const body=document.getElementById('rank-body-inner');
  if(!body) return;
  if(!key || !DISTRICTS[key]){
    if(lbl) lbl.textContent='—';
    body.innerHTML='<div class="rank-empty">区分レベルを「非表示」以外にするとランキングが表示されます。</div>';
    return;
  }
  if(lbl) lbl.textContent = LEVEL_LABEL[key];
  const rows = rankedRows(key);
  const { col, dir } = state.rank;
  const caret = c => col===c ? (dir==='asc'?' ▲':' ▼') : '';
  let html = `<div class="rank-count">${rows.length} 件（合計1件以上）</div>`+
    `<table class="rank-table"><thead><tr>`+
    `<th class="rk-num">#</th>`+
    `<th class="rk-name sortable" data-col="name">エリア${caret('name')}</th>`+
    `<th class="rk-n sortable" data-col="mosque" title="モスク">🕌${caret('mosque')}</th>`+
    `<th class="rk-n sortable" data-col="prayer" title="祈祷室">🧎${caret('prayer')}</th>`+
    `<th class="rk-n sortable" data-col="total" title="合計">計${caret('total')}</th>`+
    `</tr></thead><tbody>`;
  if(rows.length===0){
    html += `<tr><td colspan="5" class="rank-empty">該当する区がありません。</td></tr>`;
  } else {
    rows.forEach((f,i)=>{
      const p=f.properties;
      html += `<tr data-code="${p.code}"><td class="rk-num">${i+1}</td>`+
        `<td class="rk-name" title="${p.name||''}">${p.name||''}</td>`+
        `<td class="rk-n">${p.mosque||0}</td>`+
        `<td class="rk-n">${p.prayer||0}</td>`+
        `<td class="rk-n rk-total">${p.total||0}</td></tr>`;
    });
  }
  body.innerHTML = html + `</tbody></table>`;
}
// 区にズームしてポップアップを開く（ランキング行クリック。合区は全ポリゴンを含む範囲へ）
function focusDistrict(code){
  const layers = filledByCode[code];
  if(!layers || !layers.length) return;
  let bounds = layers[0].getBounds();
  for(let i=1;i<layers.length;i++) bounds = bounds.extend(layers[i].getBounds());
  // animate:false で確実に移動する（初期の全国表示(z5)からのアニメーション付き fitBounds は
  // 反映されないことがあるため、行クリックでは常に即時に合わせる）
  map.fitBounds(bounds, { maxZoom:11, padding:[24,24], animate:false });
  layers[0].bindPopup(districtPopup(layers[0].feature.properties)).openPopup();
}

// ---------- UI 配線 ----------
// レベルに応じて下位単位のセクションを出し分ける
function syncSections(){
  document.getElementById('sec-local').style.display   = (state.level==='local'||state.level==='both')?'':'none';
  document.getElementById('sec-electoral').style.display= (state.level==='electoral'||state.level==='both')?'':'none';
  document.getElementById('both-hint').hidden = state.level!=='both';
}
// 点ヒートの詳細設定は、ヒート表示中のみ出す
function syncHeatSection(){
  document.getElementById('sec-heat').style.display = state.heat ? '' : 'none';
}
// 各コントロールの現在値ラベルを state に合わせて更新する
function refreshControlLabels(){
  document.getElementById('buckets-val').textContent = state.buckets;
  document.getElementById('heat-radius-val').textContent = state.heatOpts.radius;
  document.getElementById('heat-blur-val').textContent = state.heatOpts.blur;
  document.getElementById('heat-intensity-val').textContent = Number(state.heatOpts.intensity).toFixed(1);
  document.getElementById('heat-steps-val').textContent = state.heatOpts.steps;
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
  // コロプレスの段階数（スライダー）と分類法（分位/等間隔）
  document.getElementById('buckets').addEventListener('input',e=>{
    state.buckets=+e.target.value; document.getElementById('buckets-val').textContent=e.target.value; recomputeScale();
  });
  document.getElementById('classify').addEventListener('change',e=>{ state.classify=e.target.value; recomputeScale(); });
  document.getElementById('pt-mosque').addEventListener('change',e=>{state.points.mosque=e.target.checked;renderPoints();});
  document.getElementById('pt-prayer').addEventListener('change',e=>{state.points.prayer_room=e.target.checked;renderPoints();});
  document.getElementById('pt-planned').addEventListener('change',e=>{state.points.planned=e.target.checked;renderPoints();});
  document.getElementById('pt-heat').addEventListener('change',e=>{state.heat=e.target.checked;syncHeatSection();renderPoints();});
  // 点ヒート設定（半径・ぼかし・強度・階調）。共通ハンドラで state と現在値ラベルを更新
  const wireHeat=(id,key,which,fmt)=>{
    const el=document.getElementById(id), lab=document.getElementById(id+'-val');
    el.addEventListener('input',ev=>{ state.heatOpts[key]=+ev.target.value; if(lab)lab.textContent=fmt?fmt(+ev.target.value):ev.target.value; updateHeat(which); });
  };
  wireHeat('heat-radius','radius','radius-blur');
  wireHeat('heat-blur','blur','radius-blur');
  wireHeat('heat-intensity','intensity','intensity',v=>v.toFixed(1));
  wireHeat('heat-steps','steps','steps');
  document.getElementById('heat-reset').addEventListener('click',resetHeat);
  // ランキング：ヘッダクリックで並び替え、行クリックで該当区へズーム、行ホバーで区をハイライト
  const rankBody=document.getElementById('rank-body-inner');
  rankBody.addEventListener('click',e=>{
    const th=e.target.closest('th.sortable');
    if(th){
      const col=th.dataset.col;
      if(state.rank.col===col) state.rank.dir = state.rank.dir==='asc'?'desc':'asc';
      else state.rank = { col, dir: col==='name'?'asc':'desc' };   // 数値は降順・名称は昇順を既定に
      renderRanking(); return;
    }
    const tr=e.target.closest('tr[data-code]');
    if(tr) focusDistrict(tr.dataset.code);
  });
  rankBody.addEventListener('mouseover',e=>{
    const tr=e.target.closest('tr[data-code]'); if(!tr) return;
    const layers=filledByCode[tr.dataset.code]; if(!layers||!layers.length) return;
    layers.forEach(l=>{ l.setStyle({weight:2, color:'#222'}); l.bringToFront(); });   // 合区は複数ポリゴンを強調
    const p=layers[0].feature.properties;
    const hb=document.getElementById('hoverbox'); hb.hidden=false;
    hb.innerHTML=`<b>${p.name}</b> — 🕌 ${p.mosque||0} · 計 ${metricValue(p)}`;
  });
  rankBody.addEventListener('mouseout',e=>{
    const tr=e.target.closest('tr[data-code]'); if(!tr) return;
    const layers=filledByCode[tr.dataset.code]; if(layers && filledLayer) layers.forEach(l=>filledLayer.resetStyle(l));
    document.getElementById('hoverbox').hidden=true;
  });
  document.getElementById('rank-toggle').addEventListener('click',()=>{
    document.getElementById('rank-panel').classList.toggle('collapsed');   // ランキングパネル折りたたみ
  });
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
  state.buckets=+document.getElementById('buckets').value;
  state.classify=document.getElementById('classify').value;
  state.points.mosque=document.getElementById('pt-mosque').checked;
  state.points.prayer_room=document.getElementById('pt-prayer').checked;
  state.points.planned=document.getElementById('pt-planned').checked;
  state.heat=document.getElementById('pt-heat').checked;
  state.heatOpts.radius=+document.getElementById('heat-radius').value;
  state.heatOpts.blur=+document.getElementById('heat-blur').value;
  state.heatOpts.intensity=+document.getElementById('heat-intensity').value;
  state.heatOpts.steps=+document.getElementById('heat-steps').value;
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

  // 議員（政治的代表）データを読み込む（Phase 1&2: 知事・衆院・参院。首長は Phase 3）
  const [rp,rh,rc,pp] = await Promise.all([
    fetchJSON('data/reps_pref.json'), fetchJSON('data/reps_hr.json'),
    fetchJSON('data/reps_hc.json'),   fetchJSON('data/reps_parties.json'),
  ]);
  REPS.pref=rp; REPS.hr=rh; REPS.hc=rc; PARTIES=pp||{};
  const repsAsOf = (rp&&rp.asOf) || (rh&&rh.asOf) || '';
  const repEl=document.getElementById('src-reps');
  if(repEl) repEl.textContent = repsAsOf ? `${repsAsOf}現在` : '（未生成）';

  // UI 配線 → 初期状態を DOM から取得 → 現在値ラベルとヒートを同期 → 各レイヤーを初回描画
  wireUI(); readStateFromDOM(); refreshControlLabels(); syncSections(); syncHeatSection();
  updateHeat('all'); renderPoints(); renderDistricts();
}
init();
