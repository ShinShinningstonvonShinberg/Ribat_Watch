# データ更新パイプライン（自動化）

このリポジトリのデータは2系統あり、それぞれ **定期的に再取得 → 加工 → 差分だけを載せた
Pull Request を自動で開く** 仕組みを持つ。**自動マージはしない**。人間がPRをレビューして
はじめて公開される。

| データ | 出典 | 更新 Action | cadence |
|---|---|---|---|
| モスク地点（`app/data/mosques.geojson` ほか） | 第三者の公開マイマップ「全国モスクリスト」 | `.github/workflows/refresh-mosques.yml` | 週次 + 手動 |
| 議員（`app/data/reps_*.json`） | 日本語版 Wikipedia（知事/衆院/参院）＋ Claude+web_search（首長） | `.github/workflows/refresh-reps.yml` | 知事/衆院/参院 四半期・首長 月次・選挙時は手動 |

いずれも Python 標準ライブラリのみで動く（CIに追加の pip インストールは不要）。

---

## 1. モスク地点の更新

### 何をするか
1. 公開 mid（README/`scripts/mosque_source.json` に記載）から KML を取得。
2. `parse_kml.py` で解釈し、`scripts/scrub.py` で **スクラブ＋一般化**（下記）。
3. `mosques.geojson` / `mosques.csv` / `app/data/mosques.geojson` を書き出し。
4. `data/recount/*.geojson.gz`（フル解像度境界・同梱）で件数を再集計し、
   `app/data/districts_*.geojson` に件数だけ書き戻す（形状は不変）。
5. 追加・削除・件数の差分を PR にする。

### スクラブ＋一般化（個人情報対策）
公開版はモスクの説明文から **URLだけ** を残し、それ以外の自由記述（住所・メモ・氏名・
個人SNSハンドル・主観的論評）を全破棄する。denylist（既知の除去語）だけでなく自由記述を
まるごと落とすため、**まだ規則に載っていない新規の個人情報も自由記述としては漏れない**。
- 残す: モスク公式サイト・ニュース・Wikipedia・地図・画像等の**非SNSホスト**（既定で許可）と、
  **モスク名義**のSNS（既に公開中のデータにあるアカウント、または
  `scripts/mosque_social_allowlist.json` に登録したもの＝grandfather 集合に完全一致するもの）。
- 落とす: 未承認のSNS・非URLの自由記述すべて。承認済みSNSへのクエリ/フラグメント改ざんや、
  URL の userinfo（`user:pw@`）も除去する。
- 残る限界: 非SNSホストのURLは path/query をそのまま残す（`?cid=`・動画ID等の正当なクエリを
  壊さないため）。任意ドメインの path/query に情報を埋め込む改ざんは一般化では止まらないので、
  この経路は**人間によるPRレビュー**で担保する（差分に単独URLとして現れるため発見しやすい）。
- アプリ（`app/app.js`）はモスクのポップアップで `source_url` しか表示しないため、説明文を
  URLのみにしても表示上の欠落はない。公開ファイルからの露出だけが減る。

### 必須の手動設定（1回だけ）
- **GitHub Secrets に `SCRUB_RULES_JSON` を追加**：ローカルの `scripts/scrub_rules.json`
  （個人情報の除去語リスト・非公開）の中身をそのまま貼る。
  未設定だと Action は **意図的に失敗する**（生の個人情報を一般化なしで公開する事故を防ぐため）。
- Settings → Actions → General → Workflow permissions で
  **「Allow GitHub Actions to create and approve pull requests」** を有効化。

### ローカルでの実行・検証
```bash
# 出典から取得して更新（出典が不変なら差分ゼロ）
python3 scripts/refresh_mosques.py
# ローカルKMLで（オフライン確認）
python3 scripts/refresh_mosques.py --kml mosques_raw.kml --report /tmp/report.md
```
`data/recount/*.gz` を同梱しているので、`data/raw` の大容量境界（senkyoku2022.zip 約124MB）や
shapely 無しでも、公開中の件数と完全一致する再集計ができる。境界そのものが変わった（再区割り等）
場合のみ `data/recount/README.md` の手順で境界を作り直す。

---

## 2. 議員データの更新

### 方針
既存の `reps_*.json` は手作業で補完した豊富な情報（推薦政党・初当選日・注記）を持つ。
スクレイパはそれを全て再現しないので、「**今その職に就いているのは誰か**」という変動しやすい
事実だけを検出し、既存データと突き合わせる。**氏名（＝在任者）が変わった選挙区だけ** を
差し替え候補にし、氏名が同じなら既存 record をそのまま残す（curation を壊さない）。

### 収集元と cadence
| レベル | 収集 | 出典 | cadence |
|---|---|---|---|
| 知事 | `scripts/reps/scrape_pref.py` | Wikipedia「都道府県知事の一覧」 | 四半期 + 選挙時 |
| 衆院小選挙区 | `scripts/reps/scrape_hr.py` | Wikipedia「第51回衆議院議員総選挙」小選挙区当選者 | 四半期 + 選挙時 |
| 参院選挙区 | `scripts/reps/scrape_hc.py` | Wikipedia「参議院議員一覧」選挙区選出議員 | 四半期 + 選挙時 |
| 市区町村長 | `scripts/reps/mayors/run.py` | Claude + web_search（現職確認）| 月次（**要APIキー**）+ 選挙時 |

知事/衆院/参院は API キー不要（Wikipedia のみ）。首長は数が多く出典が散在するためエージェント
支援で、`ANTHROPIC_API_KEY` があるときだけ動く opt-in。

### 検証と揺れの吸収
- `scripts/reps/validate.py` が不変条件を確認：47知事 / 289衆院 / 45参院・148名（東京12・
  改選74/74）・合区コード・必須フィールド・district コードとの一致。検証NGなら本番へ書かない。
- 出典間の表記揺れは自動吸収：異体字/旧字体（壽↔寿・邊↔辺 等）と、通称↔正式名の対応
  （`scripts/reps/name_aliases.json`）。新たな食い違いを人間が確認したら name_aliases に追記する。

### 必須／任意の設定
- （任意）**首長を CI で回すなら Secrets に `ANTHROPIC_API_KEY` を追加**。公開リポジトリの
  Action に鍵を置くことに抵抗があれば、ローカルで月次実行してブランチを push する運用でもよい。
  鍵が無ければ知事/衆院/参院のみ自動更新される。
- Workflow permissions（上と同じ「Allow GitHub Actions to create and approve pull requests」）。

### ローカルでの実行・検証
```bash
python3 scripts/reps/validate.py                        # 現データの不変条件チェック
python3 scripts/reps/refresh.py --level pref            # 知事の差分を確認（書き込まない）
python3 scripts/reps/refresh.py --level all --skip-muni # 知事/衆院/参院（鍵不要）
python3 scripts/reps/refresh.py --level pref --code 47  # 沖縄県知事だけ（2026-09-13選挙）
python3 scripts/reps/mayors/run.py --dry-run --pref 47  # 首長プロンプトの生成のみ（鍵不要）
```
`--apply` を付けると `app/data/reps_*.json` に反映する（CIはこれを使い、差分をPRにする）。

### 直近の選挙対応
**沖縄県知事選 2026-09-13**（pref コード `47`）。投開票後に:
`Actions → 議員データ 定期更新 → Run workflow` で `level=pref, code=47` を手動実行するか、
`python3 scripts/reps/refresh.py --level pref --code 47 --apply` をローカルで回して PR を作る。

---

## 人間の承認フロー（両系統共通）
1. Action（定期 or 手動）が差分だけのブランチ `auto/refresh-*` を作り、PR を開く。
2. PR 本文に差分サマリ（追加/削除・件数増減、在任者の交代など）が載る。
3. 人間が内容を確認（新規の個人情報・誤検出・表記の是非）。
4. 問題なければマージ＝公開。合わない差分は取り込まない／ブランチ上で修正。

**名簿（議員）データもモスクデータも、レビューを経ずに公開されることはない。**
