#!/usr/bin/env python3
"""Google マイマップの KML エクスポートを整形して GeoJSON と CSV に変換するスクリプト。

KML のフォルダ（レイヤー）と、このスクリプトが付ける内部種別の対応:
  モスク                -> mosque      （モスク本体）
  祈祷室                -> prayer_room （施設内の祈祷室・礼拝室）
  情報アリ（建築予定地） -> planned     （建設予定地の情報）

入力 : mosques_raw.kml（マイマップから取得した KML）
出力 : mosques.geojson（地図描画用）, mosques.csv（表計算・確認用）

スクラブ＋一般化（個人情報の除去）は scripts/scrub.py に集約している。ここで定義した
parse_kml()／write_geojson()／write_csv() は scripts/refresh_mosques.py からも再利用され、
KMLの解釈と公開向け整形を1か所に保つ。
"""
import os
import re
import csv
import json
import html
import sys
import xml.etree.ElementTree as ET
from collections import Counter

# スクラブ＋一般化の共有モジュール（scripts/scrub.py）を読み込む
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import scrub  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "mosques_raw.kml")
NS = {"k": "http://www.opengis.net/kml/2.2"}   # KML の名前空間
# 一般化スクラブで「モスク名義として承認済み」とみなすSNSの基準にする、既に公開中のデータ
PRIOR_PUBLIC = os.path.join(ROOT, "app", "data", "mosques.geojson")

# KML フォルダ名 → 内部種別 への対応表
LAYER_MAP = {
    "モスク": "mosque",
    "祈祷室": "prayer_room",
    "情報アリ（建築予定地）": "planned",
}

# 地点名の先頭に付く ID コードを抜き出す正規表現。
# 例: 「北海01 003 室蘭モスク」→ コード=北海01（漢字2〜3字＋数字2桁）, 連番=003
CODE_RE = re.compile(r"^\s*([一-鿿]{2,3}\d{2})\s+(\d{1,3})\b", re.S)

# GeoJSON / CSV の列順（両出力で共有）
CSV_COLS = ["layer", "code", "seq", "name", "removed_from_gmaps", "lat", "lon", "source_url", "description"]

first_url = scrub.first_url                 # 説明文の先頭URL（一般化後のURLから source_url を取る）


def clean_name(raw: str):
    """Placemark の <name> から (コード, 連番, 表示名) を取り出す。"""
    raw = raw.replace("\r", "")
    code = seq = None
    m = CODE_RE.match(raw)
    if m:                              # 先頭に ID コードがあれば分離
        code, seq = m.group(1), m.group(2)
        rest = raw[m.end():]
    else:
        rest = raw
    # 改行や連続空白を単一スペースに畳んで表示名を整える
    name = " ".join(rest.split())
    return code, seq, name


def parse_kml(src=SRC, scrubber=None, grandfather=None):
    """KML を読み、(rows, features) を返す。説明文はスクラブ＋一般化済み。

    src        : KMLファイルパス、KML文字列、または bytes。
    scrubber   : scrub.Scrubber（省略時は既定規則で新規作成）。
    grandfather: 承認済みSNSの正規化URL集合（省略時は PRIOR_PUBLIC から構築）。
    """
    if scrubber is None:
        scrubber = scrub.Scrubber()
    if grandfather is None:
        grandfather = scrub.load_grandfather_social(PRIOR_PUBLIC)

    # src がパスならファイルを、KML文字列/バイト列ならそれ自体を解析する
    if isinstance(src, (bytes, bytearray)):
        root = ET.fromstring(src)
    elif isinstance(src, str) and src.lstrip().startswith("<"):
        root = ET.fromstring(src)
    else:
        root = ET.parse(src).getroot()

    rows, features = [], []
    for folder in root.iter("{http://www.opengis.net/kml/2.2}Folder"):
        fname_el = folder.find("k:name", NS)
        fname = fname_el.text.strip() if fname_el is not None and fname_el.text else ""
        layer = LAYER_MAP.get(fname, fname)          # 種別に変換（未知名はそのまま）
        for pm in folder.findall("k:Placemark", NS):
            name_el = pm.find("k:name", NS)
            raw_name = name_el.text if name_el is not None and name_el.text else ""
            desc_el = pm.find("k:description", NS)
            desc = desc_el.text if desc_el is not None and desc_el.text else ""
            desc = html.unescape(desc or "").strip()  # HTML エスケープを解除
            desc = " ".join(desc.split())              # 改行・連続空白を単一スペースに
            desc = scrubber.clean(desc, grandfather=grandfather, generalize=True)  # 個人情報除去＋一般化
            coord_el = pm.find(".//k:coordinates", NS)
            if coord_el is None or not coord_el.text:  # 座標が無い地点は飛ばす
                continue
            parts = coord_el.text.strip().split(",")   # "経度,緯度,標高" 形式
            lon, lat = float(parts[0]), float(parts[1])

            code, seq, name = clean_name(raw_name)
            removed = "★" in name                      # ★ は「Google マップから削除済み」の印
            row = {
                "layer": layer, "code": code or "", "seq": seq or "", "name": name,
                "removed_from_gmaps": removed, "lat": lat, "lon": lon,
                "source_url": first_url(desc),         # スクラブ後の説明文から主要URLを取得
                "description": desc,
            }
            rows.append(row)
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                # 緯度経度は geometry 側に持たせるので properties からは除外
                "properties": {k: v for k, v in row.items() if k not in ("lat", "lon")},
            })
    return rows, features


def write_geojson(features, path):
    """フィーチャ配列を GeoJSON として書き出す（parse_kml は indent=1 の可読形式）。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f, ensure_ascii=False, indent=1)


def write_csv(rows, path):
    """行データを CSV として書き出す。"""
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        w.writeheader()
        w.writerows(rows)


def summarize(rows):
    """集計サマリ（種別内訳・都道府県接頭辞・座標範囲）を標準出力に表示する。"""
    by_layer = Counter(r["layer"] for r in rows)
    by_pref = Counter(r["code"][:2] for r in rows if r["code"])
    removed = sum(1 for r in rows if r["removed_from_gmaps"])
    with_code = sum(1 for r in rows if r["code"])
    print(f"Total placemarks parsed : {len(rows)}")
    print(f"With ID code            : {with_code}")
    print(f"Marked removed (★)      : {removed}")
    print("\nBy layer:")
    for k, v in by_layer.most_common():
        print(f"  {k:12s} {v}")
    print("\nTop region prefixes (first 2 chars of code):")
    for k, v in by_pref.most_common(12):
        print(f"  {k}  {v}")
    lons = [r["lon"] for r in rows]; lats = [r["lat"] for r in rows]
    print(f"\nLon range: {min(lons):.3f} .. {max(lons):.3f}")
    print(f"Lat range: {min(lats):.3f} .. {max(lats):.3f}")
    oob = [r for r in rows if not (122 < r["lon"] < 154 and 20 < r["lat"] < 46)]
    print(f"Out-of-Japan-bounds points: {len(oob)}")
    for r in oob[:10]:
        print(f"   ! {r['name']}  ({r['lat']}, {r['lon']})")


def main():
    rows, features = parse_kml(SRC)
    write_geojson(features, os.path.join(ROOT, "mosques.geojson"))
    write_csv(rows, os.path.join(ROOT, "mosques.csv"))
    summarize(rows)


if __name__ == "__main__":
    main()
