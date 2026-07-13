#!/usr/bin/env python3
"""境界データから、選挙区・行政区ごとの集計済み地図データ（コロプレス地図用）を生成するスクリプト。

各レベル（レイヤー）ごとの処理の流れ:
  生ポリゴンを読み込む
   → 属性を正規化 {level, code, name}（この時点では簡略化せず「フル解像度」の形状を保持）
   → 311 のモスク地点を「フル解像度の形状」で点在判定して各区に割り当てる（高精度）
   → ウェブ表示用に形状を簡略化＋座標丸め（shapely、トポロジー保持）
   → app/data/districts_<level>.geojson を出力（集計値 {mosque, prayer, planned, total} 付き）

レベルの内訳:
  muni : 市区町村（国土数値情報 N03）
  pref : 都道府県（市区町村をディゾルブして生成）
  hc   : 参議院選挙区（45）— 都道府県から合区2組を束ねて生成
  hr   : 衆議院小選挙区（289・現行）— build_hr.py が生成した hr.geojson を利用

参議院選挙区（45）は都道府県から導出する。合区2組
（鳥取＋島根、徳島＋高知）をまとめ、それぞれ件数を合算する。

実行方法（仮想環境の python を使う）:  ./.venv/bin/python scripts/build_districts.py
"""
from __future__ import annotations
import json, os, sys, shutil
from collections import defaultdict
from shapely.geometry import shape as shp_shape
from shapely.ops import unary_union
from shapely import set_precision, make_valid
import geo

# パスはすべて scripts/ の一つ上（プロジェクトルート）を基準に組み立てる
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")        # 生の境界データ
OUT = os.path.join(ROOT, "app", "data")        # アプリが読む出力先
MOSQUES = os.path.join(ROOT, "mosques.geojson")

# レイヤーごとの設定。code_fields/name_fields は「候補順」で最初に見つかった属性を使う。
SOURCES = {
    "muni": {"path": os.path.join(RAW, "muni.geojson"),
             "code_fields": ["N03_007"], "name_fields": ["N03_004"],
             "name_prefix_fields": ["N03_003"],   # 政令市・郡の接頭辞（例: 札幌市 中央区）
             "simplify": 0.0012, "precision": 1e-4},
    "hr":   {"path": os.path.join(RAW, "hr.geojson"),
             "code_fields": ["kucode"], "name_fields": ["kuname"],
             "simplify": 0.005, "precision": 1e-4},
}
PREF_SIMPLIFY, PREF_PRECISION = 0.004, 1e-4      # 都道府県・参院選挙区の簡略化パラメータ

# 参議院の合区。都道府県コード → (統合後コード, 統合後名称)
HC_MERGES = {"31": ("31_32", "鳥取県・島根県"), "32": ("31_32", "鳥取県・島根県"),
             "36": ("36_39", "徳島県・高知県"), "39": ("36_39", "徳島県・高知県")}

# モスクデータの種別 → 集計プロパティ名 の対応
LAYER_KEY = {"mosque": "mosque", "prayer_room": "prayer", "planned": "planned"}


def pick(props, fields):
    """候補の属性名を順に見て、最初に値が入っているものを返す（無ければ None）。"""
    for f in fields:
        if f in props and props[f] not in (None, ""):
            return props[f]
    return None


def norm_pref_code(v):
    """都道府県コードを2桁ゼロ埋め文字列に正規化する。

    '1'→'01'、5桁以上の市区町村コードなら先頭2桁を都道府県コードとみなす。
    """
    s = str(v).strip()
    d = "".join(c for c in s if c.isdigit())
    if len(d) >= 5:
        return d[:2]
    return d.zfill(2) if d else s


def load_features(path):
    """GeoJSON の FeatureCollection を読み、フィーチャ配列を返す。"""
    d = json.load(open(path, encoding="utf-8"))
    if d.get("type") != "FeatureCollection":
        sys.exit(f"ERROR: {path} not a FeatureCollection (topojson?)")
    return d["features"]


def load_points():
    """モスク地点を (経度, 緯度, 種別) のタプル配列として読み込む。"""
    feats = json.load(open(MOSQUES, encoding="utf-8"))["features"]
    return [(f["geometry"]["coordinates"][0], f["geometry"]["coordinates"][1],
             f["properties"].get("layer", "mosque")) for f in feats]


def normalize(raw, cfg, level):
    """属性を {level, code, name, 集計値} に正規化する。形状は点在判定の精度確保のため
    フル解像度のまま保持する（簡略化は集計後に別途行う）。"""
    out = []
    for f in raw:
        g = f.get("geometry")
        if not g or g.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        p = f.get("properties", {})
        code = pick(p, cfg["code_fields"])
        name = pick(p, cfg["name_fields"])
        if cfg.get("name_prefix_fields"):          # 政令市・郡の接頭辞を名称に付ける
            pre = pick(p, cfg["name_prefix_fields"])
            if pre and pre != name:
                name = f"{pre} {name}"
        if level == "pref":
            code = norm_pref_code(code) if code is not None else None
        out.append({"type": "Feature", "geometry": g,
                    "properties": {"level": level, "code": str(code) if code is not None else "",
                                   "name": name or "", "mosque": 0, "prayer": 0, "planned": 0, "total": 0}})
    return out


def spatial_join(features, points):
    """各モスク地点を、それを含む区に割り当てて件数を集計する（点在判定）。

    どの区にも入らなかった地点数（unassigned）を返す。
    """
    idx = geo.BBoxIndex(features)     # 外接矩形インデックスで高速化
    unassigned = 0
    for lon, lat, layer in points:
        f = idx.query_point(lon, lat)
        if f is None:                 # どの区にも入らない（沖合・島など）
            unassigned += 1
            continue
        f["properties"][LAYER_KEY.get(layer, "mosque")] += 1
    # 種別ごとの件数から合計を計算
    for f in features:
        pr = f["properties"]
        pr["total"] = pr["mosque"] + pr["prayer"] + pr["planned"]
    return unassigned


def simplify_for_web(features, tol, precision):
    """ウェブ表示用に形状を簡略化＋座標丸めする。

    簡略化で空（微小な島など）に潰れたフィーチャは除外する。件数の集計は
    この処理の前にフル解像度の形状で済んでいるので、集計値には影響しない。
    """
    out = []
    dropped = 0
    for f in features:
        try:
            g = shp_shape(f["geometry"]).simplify(tol, preserve_topology=True)
            g = set_precision(g, precision)        # 座標をグリッドに丸める
            if g.is_empty:
                # 件数を持つ区は消さず、粗いフォールバック形状で残す
                if f["properties"].get("total", 0) > 0:
                    f["geometry"] = geo.round_geometry(f["geometry"], 4)
                    out.append(f); continue
                dropped += 1; continue             # 件数0で潰れたものは破棄
            gi = json.loads(json.dumps(g.__geo_interface__))
            if not gi.get("coordinates"):          # 座標が空になったものも破棄
                dropped += 1; continue
            f["geometry"] = gi
            out.append(f)
        except Exception:
            # shapely で失敗した場合は自前の簡略化にフォールバック
            fb = geo.simplify_geometry(f["geometry"], tol) or f["geometry"]
            f["geometry"] = geo.round_geometry(fb, 4)
            out.append(f)
    if dropped:
        print(f"   (simplify: dropped {dropped} empty micro-polygons with no mosques)")
    return out


def write_out(level, features):
    """正規化済みフィーチャを app/data/districts_<level>.geojson に書き出す。"""
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"districts_{level}.geojson")
    # separators でスペースを省きファイルサイズを削減
    json.dump({"type": "FeatureCollection", "features": features},
              open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    return path, os.path.getsize(path)


def report(level, features, unassigned):
    """集計結果の要約（件数上位8区など）を標準出力に表示する。"""
    with_m = sum(1 for f in features if f["properties"]["mosque"] > 0)
    top = sorted(features, key=lambda f: f["properties"]["total"], reverse=True)[:8]
    print(f"\n[{level}] {len(features)} districts | {with_m} with ≥1 mosque | {unassigned} pts unassigned")
    for f in top:
        p = f["properties"]
        print(f"   {p['total']:>3}  {p['name']}  (🕌{p['mosque']} 🧎{p['prayer']} 🏗️{p['planned']})  [{p['code']}]")


def derive_pref_from_muni(muni_raw):
    """市区町村ポリゴンを都道府県コード（N03_007 の先頭2桁）ごとに結合し、47都道府県を生成する。

    市区町村と同一出典（国土数値情報 N03）・同一境界になるため、Both表示時に
    行政区と選挙区の境界がきれいに重なる利点がある。
    """
    groups = defaultdict(list)
    names = {}
    skipped = 0
    for f in muni_raw:
        g = f.get("geometry"); p = f.get("properties", {})
        code = str(p.get("N03_007") or "")[:2]           # None でも落ちないように
        if not (len(code) == 2 and code.isdigit()):       # コード無し（係争中・無人島など）は除外
            skipped += 1
            continue
        if not g or g.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        names.setdefault(code, p.get("N03_001", ""))       # 都道府県名（N03_001）を記録
        try:
            geom = shp_shape(g)
            if not geom.is_valid:
                geom = make_valid(geom)
            groups[code].append(geom)
        except Exception:
            pass
    feats = []
    for code, geoms in sorted(groups.items()):
        merged = unary_union(geoms)                        # 県内の市区町村を結合
        if not merged.is_valid:
            merged = make_valid(merged)
        feats.append({"type": "Feature",
                      "geometry": json.loads(json.dumps(merged.__geo_interface__)),
                      "properties": {"level": "pref", "code": code, "name": names[code],
                                     "mosque": 0, "prayer": 0, "planned": 0, "total": 0}})
    if skipped:
        print(f"   (dissolve: skipped {skipped} uncoded muni polygons — disputed/uninhabited islands)")
    return feats


def derive_hc(pref_features, points):
    """都道府県から参議院選挙区（45）を導出する。

    合区（鳥取＋島根、徳島＋高知）を同一コード・同一名称にまとめる。ポリゴン自体は
    2つのまま残し、両者に「グループ合計の件数」を持たせて同じ色で塗られるようにする
    （地理的な結合はしないので shapely での union は不要）。
    """
    hc = []
    for f in pref_features:
        p = dict(f["properties"])
        code = norm_pref_code(p["code"])
        if code in HC_MERGES:                     # 合区対象なら統合後のコード・名称に置換
            p["code"], p["name"] = HC_MERGES[code]
        p["level"] = "hc"
        p.update(mosque=0, prayer=0, planned=0, total=0)
        hc.append({"type": "Feature", "geometry": f["geometry"], "properties": p})
    spatial_join(hc, points)                       # まずポリゴン単位で件数を数える
    # 同一コード（合区）のポリゴン同士で件数を合算する
    agg = defaultdict(lambda: [0, 0, 0])
    for f in hc:
        p = f["properties"]; a = agg[p["code"]]
        a[0] += p["mosque"]; a[1] += p["prayer"]; a[2] += p["planned"]
    for f in hc:                                    # 合算結果を各ポリゴンに書き戻す
        p = f["properties"]; m, pr, pl = agg[p["code"]]
        p["mosque"], p["prayer"], p["planned"], p["total"] = m, pr, pl, m + pr + pl
    return hc


def main():
    points = load_points()
    print(f"Loaded {len(points)} mosque points")

    # スクラブ済みのモスクデータをアプリ用にコピー（app/data/ を常に同期させる）
    os.makedirs(OUT, exist_ok=True)
    shutil.copyfile(MOSQUES, os.path.join(OUT, "mosques.geojson"))

    # --- 市区町村（都道府県・参院選挙区の元にもなる） ---
    muni_raw = load_features(SOURCES["muni"]["path"])
    muni = normalize(muni_raw, SOURCES["muni"], "muni")
    un = spatial_join(muni, points)
    muni = simplify_for_web(muni, SOURCES["muni"]["simplify"], SOURCES["muni"]["precision"])
    path, size = write_out("muni", muni); report("muni", muni, un)
    print(f"   -> {os.path.relpath(path, ROOT)} ({size//1024} KB)")

    # --- 都道府県：市区町村をディゾルブして生成（国土数値情報 N03 由来） ---
    print("\nDissolving municipalities -> 47 prefectures …")
    pref = derive_pref_from_muni(muni_raw)
    un = spatial_join(pref, points)                                   # フル解像度で点在判定
    # 参院選挙区の導出にはフル解像度の県形状が必要なので、簡略化前に控えを取る
    pref_fullres = [dict(type="Feature", geometry=f["geometry"], properties=dict(f["properties"])) for f in pref]
    pref = simplify_for_web(pref, PREF_SIMPLIFY, PREF_PRECISION)
    path, size = write_out("pref", pref); report("pref", pref, un)
    print(f"   -> {os.path.relpath(path, ROOT)} ({size//1024} KB)")

    # --- 参議院選挙区（45）：合区2組をまとめ、件数を合算 ---
    hc = derive_hc(pref_fullres, points)
    hc = simplify_for_web(hc, PREF_SIMPLIFY, PREF_PRECISION)
    path, size = write_out("hc", hc)
    uniq = {f["properties"]["code"]: f["properties"] for f in hc}
    print(f"\n[hc] {len(uniq)} districts from {len(hc)} polygons")
    for p in sorted(uniq.values(), key=lambda x: x["total"], reverse=True)[:8]:
        print(f"   {p['total']:>3}  {p['name']}  (🕌{p['mosque']})  [{p['code']}]")
    print(f"   -> {os.path.relpath(path, ROOT)} ({size//1024} KB)")

    # --- 衆議院小選挙区（289・現行の改定後地図） ---
    hr = normalize(load_features(SOURCES["hr"]["path"]), SOURCES["hr"], "hr")
    un = spatial_join(hr, points)
    hr = simplify_for_web(hr, SOURCES["hr"]["simplify"], SOURCES["hr"]["precision"])
    path, size = write_out("hr", hr); report("hr", hr, un)
    print(f"   -> {os.path.relpath(path, ROOT)} ({size//1024} KB)")


if __name__ == "__main__":
    main()
