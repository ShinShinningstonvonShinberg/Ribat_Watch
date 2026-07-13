#!/usr/bin/env python3
"""Google マイマップの KML エクスポートを整形して GeoJSON と CSV に変換するスクリプト。

KML のフォルダ（レイヤー）と、このスクリプトが付ける内部種別の対応:
  モスク                -> mosque      （モスク本体）
  祈祷室                -> prayer_room （施設内の祈祷室・礼拝室）
  情報アリ（建築予定地） -> planned     （建設予定地の情報）

入力 : mosques_raw.kml（マイマップから取得した KML）
出力 : mosques.geojson（地図描画用）, mosques.csv（表計算・確認用）
"""
import os
import re
import csv
import json
import html
import xml.etree.ElementTree as ET

SRC = "mosques_raw.kml"
NS = {"k": "http://www.opengis.net/kml/2.2"}   # KML の名前空間

# KML フォルダ名 → 内部種別 への対応表
LAYER_MAP = {
    "モスク": "mosque",
    "祈祷室": "prayer_room",
    "情報アリ（建築予定地）": "planned",
}

# 地点名の先頭に付く ID コードを抜き出す正規表現。
# 例: 「北海01 003 室蘭モスク」→ コード=北海01（漢字2〜3字＋数字2桁）, 連番=003
CODE_RE = re.compile(r"^\s*([一-鿿]{2,3}\d{2})\s+(\d{1,3})\b", re.S)
URL_RE = re.compile(r"https?://[^\s<\"]+")       # 説明文中の最初の URL 抽出用


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


def first_url(desc: str):
    """説明文から最初に現れる URL を返す（無ければ空文字）。"""
    if not desc:
        return ""
    m = URL_RE.search(desc)
    return m.group(0) if m else ""


# ---- 個人情報スクラブ（公開リポジトリ向け） ----
# 公開ソース（マイマップ）由来だが、公開GitHubに載せる前に「個人レベルの情報」を除去する。
# モスク名・所在地・公式サイト・ニュース・Wikipedia・モスク名義の公式SNSアカウントは残す。
#
# 重要: 除去対象の具体的な語（個人の SNS ハンドル・個人名・国籍/民族の記述など）は、
# それ自体が個人情報なので、この公開スクリプトには書かない。公開しない別ファイル
# scripts/scrub_rules.json（.gitignore 済み）に置いて読み込む。
#   形式: {"remove_url_substr": ["…個人ハンドル片…"], "remove_phrases": ["…完全一致で削除する語…"]}
# ファイルが無い場合（クローンした人など）はスクラブを行わない＝配布物は既に加工済みの前提。
_RULES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "scrub_rules.json")
if os.path.exists(_RULES):
    _r = json.load(open(_RULES, encoding="utf-8"))
    REMOVE_URL_SUBSTR = _r.get("remove_url_substr", [])
    REMOVE_PHRASES = _r.get("remove_phrases", [])
else:
    REMOVE_URL_SUBSTR, REMOVE_PHRASES = [], []

# 単独で残るSNSラベルだけの断片（URL除去後に残るもの）を落とす
LABEL_ONLY_RE = re.compile(r"^\s*(tiktok|facebook|fb|instagram|インスタ|youtube)\s*[:：]?\s*$", re.I)
BLOCKED_URL_RE = (re.compile(r'https?://[^\s<>"]*(?:' + "|".join(re.escape(s) for s in REMOVE_URL_SUBSTR) + r')[^\s<>"]*')
                  if REMOVE_URL_SUBSTR else None)


def scrub_description(desc: str) -> str:
    """説明文から個人情報（個人SNS・個人名・国籍/民族の記述など）を除去する。
    除去語は scrub_rules.json から読み込む（無ければ何もしない）。"""
    for ph in REMOVE_PHRASES:
        desc = desc.replace(ph, "")
    segs = re.split(r"(?:<br\s*/?>)+", desc)   # <br> ごとに分割して処理
    out = []
    for s in segs:
        if BLOCKED_URL_RE:
            s = BLOCKED_URL_RE.sub("", s)      # ブロック対象URLを除去
        s = " ".join(s.split())                # 余分な空白を圧縮
        if not s:
            continue
        if LABEL_ONLY_RE.match(s):             # URL除去後に残ったラベルのみの断片は捨てる
            continue
        out.append(s)
    return "<br>".join(out).strip()


# KML を読み込み、ルート要素を取得
tree = ET.parse(SRC)
root = tree.getroot()

rows = []        # CSV 用の行データ
features = []     # GeoJSON 用のフィーチャ

# 全フォルダ（レイヤー）を走査
for folder in root.iter("{http://www.opengis.net/kml/2.2}Folder"):
    fname_el = folder.find("k:name", NS)
    fname = fname_el.text.strip() if fname_el is not None and fname_el.text else ""
    layer = LAYER_MAP.get(fname, fname)          # 種別に変換（未知名はそのまま）
    # フォルダ内の各地点（Placemark）を処理
    for pm in folder.findall("k:Placemark", NS):
        name_el = pm.find("k:name", NS)
        raw_name = name_el.text if name_el is not None and name_el.text else ""
        desc_el = pm.find("k:description", NS)
        desc = desc_el.text if desc_el is not None and desc_el.text else ""
        desc = html.unescape(desc or "").strip()  # HTML エスケープを解除
        desc = " ".join(desc.split())              # 改行・連続空白を単一スペースに
        desc = scrub_description(desc)             # 個人情報を除去（公開向け）
        coord_el = pm.find(".//k:coordinates", NS)
        if coord_el is None or not coord_el.text:  # 座標が無い地点は飛ばす
            continue
        parts = coord_el.text.strip().split(",")   # "経度,緯度,標高" 形式
        lon, lat = float(parts[0]), float(parts[1])

        code, seq, name = clean_name(raw_name)
        removed = "★" in name  # ★ は「Google マップから削除済み」の印
        row = {
            "layer": layer,
            "code": code or "",
            "seq": seq or "",
            "name": name,
            "removed_from_gmaps": removed,
            "lat": lat,
            "lon": lon,
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

# --- GeoJSON を書き出し ---
geojson = {"type": "FeatureCollection", "features": features}
with open("mosques.geojson", "w", encoding="utf-8") as f:
    json.dump(geojson, f, ensure_ascii=False, indent=1)

# --- CSV を書き出し ---
cols = ["layer", "code", "seq", "name", "removed_from_gmaps", "lat", "lon", "source_url", "description"]
with open("mosques.csv", "w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)

# --- 集計サマリを標準出力に表示（内容確認用） ---
from collections import Counter
by_layer = Counter(r["layer"] for r in rows)             # 種別ごとの件数
by_pref = Counter(r["code"][:2] for r in rows if r["code"])  # 都道府県接頭辞ごとの件数
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
# 座標が日本の範囲内（経度122〜154, 緯度20〜46）に収まっているかの健全性チェック
lons = [r["lon"] for r in rows]; lats = [r["lat"] for r in rows]
print(f"\nLon range: {min(lons):.3f} .. {max(lons):.3f}")
print(f"Lat range: {min(lats):.3f} .. {max(lats):.3f}")
oob = [r for r in rows if not (122 < r["lon"] < 154 and 20 < r["lat"] < 46)]
print(f"Out-of-Japan-bounds points: {len(oob)}")
for r in oob[:10]:
    print(f"   ! {r['name']}  ({r['lat']}, {r['lon']})")
