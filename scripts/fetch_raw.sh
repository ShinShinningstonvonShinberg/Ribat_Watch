#!/bin/bash
# 境界データ再取得スクリプト。
#
# data/raw/ に入る「生の境界データ」を公開元からダウンロードする。
# これらはアプリの実行には不要（app/data/*.geojson が生成済みで完結する）。
# build_hr.py / build_districts.py で境界データを「作り直す」ときにだけ必要。
# サイズが大きく GitHub の100MB制限も超えるため、リポジトリには含めていない。

set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"   # プロジェクトルート
RAW="$ROOT/data/raw"
mkdir -p "$RAW"

# 1. 市区町村境界（国土数値情報 N03 / smartnews-smri 経由・0.1%簡略化）
#    → 市区町村・都道府県・参院選挙区の元データになる
MUNI="$RAW/muni.geojson"
if [ -s "$MUNI" ]; then
  echo "✓ muni.geojson は既に存在 — スキップ"
else
  echo "市区町村境界をダウンロード中…"
  curl -fSL "https://raw.githubusercontent.com/smartnews-smri/japan-topography/main/data/municipality/geojson/s0001/N03-21_210101.json" -o "$MUNI"
fi

# 2. 衆院小選挙区（2022年改定後・東大CSIS 西澤明・CC0）
ZIP="$RAW/senkyoku2022.zip"
if [ -s "$ZIP" ]; then
  echo "✓ senkyoku2022.zip は既に存在 — スキップ"
else
  echo "衆院小選挙区データをダウンロード中（約124MB）…"
  curl -fSL "https://gtfs-gis.jp/senkyoku2022/senkyoku2022.zip" -o "$ZIP"
fi

# ZIP を展開（.shp 一式を data/raw/senkyoku2022/ に取り出す）
if [ ! -f "$RAW/senkyoku2022/senkyoku2022.shp" ]; then
  echo "ZIP を展開中…"
  unzip -o "$ZIP" -d "$RAW/senkyoku2022" >/dev/null
fi

echo ""
echo "完了。境界データを再生成するには次を実行:"
echo "  ./.venv/bin/python scripts/build_hr.py         # 衆院小選挙区289を生成"
echo "  ./.venv/bin/python scripts/build_districts.py  # app/data/* を再生成"
