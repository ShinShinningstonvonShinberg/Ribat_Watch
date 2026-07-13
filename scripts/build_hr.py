#!/usr/bin/env python3
"""CC0 の「衆議院小選挙区2022」シェープファイル（東大CSIS・西澤明氏／119,706 の小地域
ポリゴン）を、小選挙区（kucode）ごとに結合（ディゾルブ）して、現行（2022年
「10増10減」改定後）の 289 小選挙区ポリゴンを生成するスクリプト。

出力 : data/raw/hr.geojson （属性: kucode, kuname, ken, ku）／座標系 WGS84
検証 : 東京（ken=13）が 30 区あることを確認して、確実に「改定後の地図」であることを保証する。
       （改定前の地図は東京が 25 区。区数だけでは新旧を区別できないため境界の内訳で判定する）
"""
import os, sys, json, time
from collections import defaultdict
import shapefile                              # pyshp：シェープファイル読み込み
from shapely.geometry import shape            # GeoJSON風dict → shapelyジオメトリ
from shapely.ops import unary_union           # 複数ポリゴンの結合（ディゾルブ）
from shapely import make_valid, set_precision # 不正ジオメトリ修復／座標グリッド丸め

# scripts/ の一つ上（プロジェクトルート）を基準にパスを組み立てる
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHP = os.path.join(ROOT, "data", "raw", "senkyoku2022", "senkyoku2022.shp")
OUT = os.path.join(ROOT, "data", "raw", "hr.geojson")
SIMPLIFY_TOL = 0.0012   # 約120m。全国地図で小選挙区の形が保てる程度の簡略化許容誤差


def main():
    t0 = time.time()
    # シェープファイルは Shift_JIS エンコード（区名など日本語を含む）
    r = shapefile.Reader(SHP, encoding="shift_jis")
    n = len(r)
    print(f"Reading {n:,} small-area polygons…")

    groups = defaultdict(list)       # kucode（小選挙区コード）→ shapelyジオメトリのリスト
    meta = {}                        # kucode → (区名, 都道府県コード, 区番号)
    bad = 0                          # 変換に失敗したジオメトリ数
    # 全ての小地域ポリゴンを1件ずつ読み、所属する小選挙区ごとに束ねる
    for i, sr in enumerate(r.iterShapeRecords()):
        rec = sr.record
        kucode = rec["kucode"]
        if kucode not in meta:
            meta[kucode] = (rec["kuname"], rec["ken"], rec["ku"])
        try:
            g = shape(sr.shape.__geo_interface__)
            if not g.is_valid:                # 自己交差などがあれば修復
                g = make_valid(g)
            groups[kucode].append(g)
        except Exception:
            bad += 1
        if (i + 1) % 20000 == 0:              # 進捗表示
            print(f"  …{i+1:,}/{n:,} read ({time.time()-t0:.0f}s)")
    print(f"Read done in {time.time()-t0:.0f}s. Districts (kucode groups): {len(groups)}. Bad geoms skipped: {bad}")

    features = []
    # 小選挙区ごとに、束ねた小地域ポリゴンを結合して1つの区ポリゴンにする
    for j, (kucode, geoms) in enumerate(sorted(groups.items())):
        merged = unary_union(geoms)           # ディゾルブ（内部境界を消して結合）
        if not merged.is_valid:
            merged = make_valid(merged)
        merged = merged.simplify(SIMPLIFY_TOL, preserve_topology=True)  # 簡略化
        merged = set_precision(merged, 1e-5)  # 座標を小数5桁グリッドに丸める
        kuname, ken, ku = meta[kucode]
        features.append({
            "type": "Feature",
            "geometry": merged.__geo_interface__,
            "properties": {"kucode": int(kucode), "kuname": kuname, "ken": int(ken), "ku": int(ku)},
        })
        if (j + 1) % 50 == 0:
            print(f"  dissolved {j+1}/{len(groups)} ({time.time()-t0:.0f}s)")

    # GeoJSON として書き出し（__geo_interface__ はタプルを返すが json が処理可能）
    fc = {"type": "FeatureCollection", "features": features}
    json.dump(fc, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    size = os.path.getsize(OUT)

    # ---- 版（新旧）の検証 ----
    # 各都道府県の区数を数え、改定で変わった代表県が「改定後」の値になっているか確認する
    from collections import Counter
    pc = Counter(f["properties"]["ken"] for f in features)
    tokyo = pc.get(13, 0)
    print(f"\nWrote {len(features)} districts -> {os.path.relpath(OUT, ROOT)} ({size//1024} KB) in {time.time()-t0:.0f}s")
    print(f"VINTAGE CHECK  Tokyo(13)={tokyo} [need 30]  Kanagawa(14)={pc.get(14)} [need 20]  "
          f"Miyagi(4)={pc.get(4)} [need 5]  Hiroshima(34)={pc.get(34)} [need 6]")
    # 289区・東京30・神奈川20・宮城5 が全て揃えば改定後の地図と断定できる
    ok = (len(features) == 289 and tokyo == 30 and pc.get(14) == 20 and pc.get(4) == 5)
    print("RESULT:", "✅ CURRENT post-2022 map confirmed" if ok else "❌ vintage/counts unexpected")
    if not ok:
        sys.exit(1)   # 想定外なら異常終了（誤った版を後段に流さない）


if __name__ == "__main__":
    main()
