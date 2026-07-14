# data/recount — 再集計用のフル解像度境界（同梱）

`scripts/refresh_mosques.py` がモスク地点の件数を選挙区・行政区へ再集計する際に使う、
**フル解像度**の境界ポリゴン（gzip 圧縮）。

- `muni.geojson.gz` … 市区町村（国土数値情報 N03 / smartnews-smri 経由・0.1%簡略化）。
  都道府県・参院選挙区の件数はこの市区町村コード（先頭2桁・合区規則）から導出する。
- `hr.geojson.gz` … 衆院小選挙区289（`scripts/build_hr.py` が CC0 の CSIS シェープファイルを
  ディゾルブして生成したもの）。

## なぜ同梱するのか
`app/data/districts_*.geojson` はウェブ表示用に簡略化されており、点在判定に使うと
海岸線付近で数地点が別区に割り当たる（＝公開中の件数と食い違い、毎回の差分にノイズが出る）。
一方 `build_districts.py` は **簡略化前のフル解像度**で件数を確定している。同じフル解像度形状を
同梱しておけば、GitHub Actions 上でも **外部の大容量ダウンロード（senkyoku2022.zip 約124MB）や
shapely 無しで**、公開中の件数と完全一致する再集計ができる（出典が不変なら差分ゼロ）。

## 再生成の手順（境界が変わったとき＝再区割り等のまれな場合のみ）
```
bash scripts/fetch_raw.sh          # data/raw/muni.geojson と senkyoku2022.zip を取得
./.venv/bin/python scripts/build_hr.py   # data/raw/hr.geojson を生成
gzip -c data/raw/muni.geojson > data/recount/muni.geojson.gz
gzip -c data/raw/hr.geojson  > data/recount/hr.geojson.gz
```
生の `data/raw/` は `.gitignore` 済み（大容量・再取得可能）。ここに置く gz だけをコミットする。
