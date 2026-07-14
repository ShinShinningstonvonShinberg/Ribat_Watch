# Ribat Watch の更新ガイド（日本語）

> 🌐 **English:** [UPDATING.en.md](UPDATING.en.md) ・ 概要版: [../REFRESH.md](../REFRESH.md)

これは Ribat Watch を最新に保つための**完全な運用リファレンス**です。モスク地点データ・議員
（政治的代表）データ・境界・アプリ本体それぞれについて、**どう更新されるか**・**手動での回し方**・
**レビューで見るべき点**・**壊れたときの復旧**をまとめています。

短く済ませたいときは [REFRESH.md](../REFRESH.md)（概要）を読んでください。本書はその**詳細・完全版**です。

---

## 目次

1. [思想と保証](#1-思想と保証)
2. [リポジトリの初回設定（1回だけ）](#2-リポジトリの初回設定1回だけ)
3. [モスクデータの更新](#3-モスクデータの更新)
4. [議員データの更新](#4-議員データの更新)
5. [検証と不変条件](#5-検証と不変条件)
6. [プライバシー・セキュリティ（常に守る不変条件）](#6-プライバシーセキュリティ常に守る不変条件)
7. [ローカル開発と検証](#7-ローカル開発と検証)
8. [トラブルシューティング](#8-トラブルシューティング)
9. [コード・境界・アプリの更新](#9-コード境界アプリの更新)
10. [ファイル・コマンド早見表](#10-ファイルコマンド早見表)

---

## 1. 思想と保証

Ribat Watch のデータは**2系統**あり、どちらも同じ形をとります。

> **定期的に再ビルド → 差分だけの Pull Request を開く → 人間がレビューして承認する。**
> **自動マージは一切しない。**

| データ | 出典 | スクリプト | ワークフロー | cadence | 必要な秘密情報 |
|---|---|---|---|---|---|
| モスク地点（`app/data/mosques.geojson` と `districts_*` の件数） | 第三者の公開 Google **マイマップ**「全国モスクリスト」 | `scripts/refresh_mosques.py` | `.github/workflows/refresh-mosques.yml` | 週次＋手動 | `SCRUB_RULES_JSON`（必須） |
| 議員（`app/data/reps_*.json`） | 日本語版 Wikipedia（知事/衆院/参院）＋ Claude+web_search（首長） | `scripts/reps/refresh.py` | `.github/workflows/refresh-reps.yml` | 知事/衆院/参院 四半期・首長 月次・選挙時 手動 | `ANTHROPIC_API_KEY`（首長のみ・任意） |

**なぜ「再ビルド→PR」で、ライブ取得にしないのか**

* モスクの出典はコミュニティが編集する地図で、自由記述に**個人情報**が混じりうる。公開データは
  意図的に**スクラブ＋一般化**している。ライブ取得は、その個人情報を再露出させるか、非公開の
  除去語リストをブラウザに配る羽目になるかのどちらかになる。
* 議員は**公職者（実名）**。スクレイプした変更を自動公開すると、実名データの未確認の変更が
  そのまま公開地図に載ってしまう。

そこで、変更は必ず**人間が先に読む PR**として着地させる。自動化の役割は、変化を*検出*し、
きれいな差分を*用意*することであって、公開そのものではない。

**フェイルセーフ（設計上）:**

* `refresh_mosques.py` は、スクラブ規則が読めなければ（`--allow-no-rules` が無い限り）**何も
  書き出す前に中断**する。CI の設定漏れは、生の個人情報を公開するのではなく、はっきり失敗する。
* 出典が取得不能・**0件**なら中断し、既存の公開スナップショットを保持する（アプリは壊れない）。
* `refresh.py` は議員データを**候補ファイルに書いて検証し、合格して初めて**本番に昇格させる。
  `--apply` で検証に失敗したら直前の committed に戻す。
* どちらも **Python 標準ライブラリのみ** — CI に `pip install` は不要。

---

## 2. リポジトリの初回設定（1回だけ）

以下を GitHub リポジトリで**1回だけ**設定するまで、Action は実質的に何もしない。

### 2.1 `SCRUB_RULES_JSON` 秘密情報（モスクパイプラインに必須）

個人情報の除去語リストは `scripts/scrub_rules.json` にある。除去語そのものが個人情報なので
**`.gitignore` 済み**。CI へは秘密情報として注入する。

1. ローカルの `scripts/scrub_rules.json` を開き、**中身全体**（JSON テキスト）をコピー。
2. リポジトリ → **Settings → Secrets and variables → Actions → New repository secret**。
3. 名前 `SCRUB_RULES_JSON`、値に JSON を貼り付けて保存。

この秘密情報が無いと、モスク Action は**意図的に失敗**する（ワークフローのガードと
`scrub.rules_available()`）。これは、除去語リストが黙って未適用のまま説明文を公開する事故を
防ぐための仕様。

> 一般化（URLのみ残す）段階は除去語リストが空でも動くが、除去語リストは第一の防御であり
> 必須。CI で `--allow-no-rules` を付けて失敗を「回避」してはいけない。

### 2.2 `ANTHROPIC_API_KEY` 秘密情報（任意・首長のみ）

**首長（市区町村長）の月次収集**を CI で回したい場合のみ必要。知事/衆院/参院は鍵不要
（Wikipedia をスクレイプするだけ）。

1. **Settings → Secrets and variables → Actions → New repository secret**。
2. 名前 `ANTHROPIC_API_KEY`、値に Anthropic の API キー。

未設定なら月次ジョブは「スキップ」と記録して正常終了し、知事/衆院/参院は四半期で更新される。
公開リポジトリの Action に鍵を置きたくなければ、首長は**ローカルで回して**（§4.10）ブランチを
手で push する運用でもよい。

### 2.3 ワークフロー権限（両方に必須）

どちらの Action も `peter-evans/create-pull-request@v6` で PR を開くため、次が必要:

* **Settings → Actions → General → Workflow permissions** →
  **「Allow GitHub Actions to create and approve pull requests」を有効化**。

### 2.4 設定の確認

各ワークフローを一度**手動実行**（Actions → 対象を選ぶ → Run workflow）する。出典が不変なら
緑で完了し、**PR は開かない**（差分ゼロ）。これで秘密情報・権限・ランナーが動くことを確認できる。

---

## 3. モスクデータの更新

### 3.1 パイプラインの流れ

`scripts/refresh_mosques.py`:

1. 公開マイマップから **KML を取得**（`mid` は `scripts/mosque_source.json`、または環境変数
   `MYMAP_MID`、または `--mid`）。`urllib` で取得し、ローカルに CA 証明書が無ければ `curl` に
   フォールバック。KMZ（ZIP）は自動展開。
2. `parse_kml.parse_kml()` → `scripts/scrub.py` で **解釈＋スクラブ＋一般化**:
   * **スクラブ**: 除去語・除去URL片（`scrub_rules.json`）を消す。
   * **一般化**: URL だけを残し、それ以外の自由記述を全破棄（§3.5）。
3. `mosques.geojson`・`mosques.csv`・`app/data/mosques.geojson`（現在311地点）を**書き出し**。
4. 同梱のフル解像度境界（`data/recount/{muni,hr}.geojson.gz`）で点在判定し、**件数を再集計**して
   `app/data/districts_*.geojson` に書き戻す（形状は不変）。都道府県・参院の件数は市区町村コード
   （先頭2桁＋合区規則）から導出し、`build_districts.py` と完全に一致する。
5. **差分サマリ**（追加・削除・移動、レベル別件数の増減）を標準出力と `--report` に出す。

出典が**不変なら、公開データを1バイトも違わず再現**する（0差分を検証済み）。

### 3.2 自動（週次）

cron `17 21 * * 0` — **毎週日曜 21:17 UTC ≒ 月曜 06:17 JST**。出典に変化があれば、差分サマリを
本文にした PR「【自動】モスクデータ更新…」がブランチ `auto/refresh-mosques` に開く。

### 3.3 手動 — GitHub の画面

**Actions → 「モスクデータ 定期更新（レビュー用PR）」 → Run workflow**（入力なし）。任意の
タイミングで最新の出典を取り込む。

### 3.4 手動 — ローカル実行

```bash
# ライブ: 現在の出典を取得・スクラブ・再集計し、差分を表示
python3 scripts/refresh_mosques.py

# オフライン: ローカルの KML で（ネット不要）、レポートを書き出す
python3 scripts/refresh_mosques.py --kml mosques_raw.kml --report /tmp/report.md

# 出典マイマップの mid を一時的に上書き
python3 scripts/refresh_mosques.py --mid <MAP_ID>
```

ローカルに `scripts/scrub_rules.json`（または `SCRUB_RULES_JSON` / `SCRUB_RULES_PATH` 環境変数）が
必要。フル解像度境界は `data/recount/*.gz` に同梱しているので、`data/raw/` や `shapely` 無しで
厳密な再集計ができる。ローカル実行後は `git diff` で変更を確認し、コミットして PR を開く
（`main` に直接 push しない）。

### 3.5 スクラブ＋一般化の方針

公開版の各モスクの説明文は **URL だけ**に切り詰め、それ以外の自由記述（住所・作業メモ・氏名・
個人SNSハンドル・主観的論評）を落とす。これが、除去語リストが知らない**新規の個人情報**を捕まえる
最後の砦。

* **既定で残す:** 非SNSホスト — 公式サイト・ニュース・Wikipedia・地図・画像。（これは SNS の
  *denylist* であって、ホストの *allowlist* ではない。）
* **SNSホスト**（Facebook / Instagram / TikTok / X / YouTube / LINE / linktr.ee / note.com …）:
  そのアカウントの正規化URLが **grandfather 集合**にある場合のみ残す。すなわち、既に公開中の
  データに存在するか、`scripts/mosque_social_allowlist.json` に登録済みの場合。
* **堅牢化（監査反映）:** 公開するURLは `user:pw@`（userinfo）と `#フラグメント`を除去。grandfather
  照合はクエリまで含めるので、承認済みアカウントURLに `?leak=…`/`#…` を付けた改ざんは拒否
  （その改ざんURLは公開せず破棄）。
* **残る限界:** **任意の非SNSホストの path/query** に埋め込んだ個人情報は除去されない（クエリを
  落とすと `?cid=` の地図リンク・`?v=` の動画・`?fife=` の画像など正当なリンクが壊れるため）。
  この経路は**人間のPRレビュー**で担保する — そうしたURLは差分に単独で現れて目立つ。

`app/app.js` はモスクのポップアップで **`source_url` のみ**を表示し説明文は一切描画しないため、
説明文をURLだけにしても**UI上の欠落はゼロ**。

### 3.6 モスク名義SNSの承認（grandfather 追加）

更新PRで、説明文から**新規SNS URL**が落とされていて、それがモスク公式アカウントだと確認できたら:

1. その正規化URLを `scripts/mosque_social_allowlist.json` に追記:
   ```json
   { "urls": [ "https://facebook.com/example.mosque" ] }
   ```
   （`host + path` の形で。スキーム・`www.`・クエリ・末尾スラッシュは照合時に正規化される。
   このファイルは公開されるので、**公式アカウントのみ**。個人アカウントは載せない。）
2. 更新を回し直すと、そのアカウントは残って差分に現れる。PR を承認する。

既に公開データにあるアカウントは自動で引き継がれるので、このファイルは初期値が空でよい。

### 3.7 除去語の追加

`scripts/scrub_rules.json`（`.gitignore` 済み・`SCRUB_RULES_JSON` 秘密情報と対応）は2つの配列を持つ:

```json
{ "remove_phrases": ["<消したい氏名など>"], "remove_url_substr": ["<消したいURL片>"] }
```

* ローカルで追記 → モスク更新を回す → 差分からその語が消えたことを確認。
* **リポジトリの `SCRUB_RULES_JSON` 秘密情報も同じ内容に更新する** — 秘密情報とローカルファイルは
  同期していないと、CI とローカルでスクラブ結果が食い違う。

### 3.8 出典マイマップの変更

出典の `mid` は公開値で `scripts/mosque_source.json` にある。別の地図を指すには、そこの `mid` を
編集するか、1回限りなら `MYMAP_MID` 環境変数 / `--mid` を使う。KML の URL テンプレート
（`https://www.google.com/maps/d/kml?mid={mid}&forcekml=1`）は Google のホストに固定。

### 3.9 区割り・境界の変更

件数の再集計は同梱のフル解像度境界を使う。**境界そのもの**が変わった（市町村合併・衆院の
区割り変更）場合は、それを作り直す（`data/recount/README.md` 参照）:

```bash
bash scripts/fetch_raw.sh                     # data/raw/muni.geojson と senkyoku2022.zip を取得
./.venv/bin/python scripts/build_hr.py        # ディゾルブ → data/raw/hr.geojson（289小選挙区）
gzip -c data/raw/muni.geojson > data/recount/muni.geojson.gz
gzip -c data/raw/hr.geojson  > data/recount/hr.geojson.gz
```

表示用の簡略化形状（新しい `districts_*.geojson`）を変える場合は、フル再ビルド
`scripts/build_districts.py`（shapely 入りの `.venv` が必要）を回す。コミットするのは
`data/recount/` の `.gz` だけ。`data/raw/` は `.gitignore` 済み（大容量・再取得可能）。

### 3.10 モスクデータ PR のレビュー — チェックリスト

* **追加・削除された地点**が妥当か（実在のモスクか、スクレイプノイズでないか）。
* 変更された説明文に**自由記述の個人情報が無い** — 説明文は **URL のみ**であること。非URLの文字列や
  `user:pw@` / `?note=<氏名・住所・電話>` のURLは**危険信号** → 却下。
* **新規SNS URL** は grandfather する前に公式アカウントか確認（§3.6）。
* レベル別の**件数増減**が地点の変化と整合しているか。
* 恒常的に未割当の2地点（お台場・ビッグサイトの祈祷室。簡略化海岸線の沖側）は想定内 —
  `muni/pref/hc = 地点数 − 2`。

---

## 4. 議員データの更新

### 4.1 パイプラインの流れ

`scripts/reps/refresh.py`（レベルごと）:

1. 現職を**収集**（スクレイパ、または首長コレクタ）。
2. committed データに対する**保守的マージ**: **在任者の氏名が変わったとき**だけレコードを差し替え
   候補にする。氏名が同じなら既存（手作業補完済み）のレコードをそのまま残す。これで推薦政党・
   初当選日・注記などの補完が壊れない。
3. 候補を `validate.py` で**検証**（ゲート）。
4. 変更候補の**差分レポート**。`--apply` なら `app/data/reps_*.json` に昇格。

committed の `reps_*.json` は**静的な手作業スナップショット**。スクレイパは変動しやすい部分
＝*今その職に就いているのは誰か*だけを検出する。

### 4.2 4つのレベル

| レベル | 件数 | 出典 | スクリプト | cadence |
|---|---|---|---|---|
| `pref`（知事） | 47 | Wikipedia「都道府県知事の一覧」 | `scrape_pref.py` | 四半期＋選挙 |
| `hr`（衆院小選挙区） | 289 | Wikipedia「第51回衆議院議員総選挙」小選挙区当選者 | `scrape_hr.py` | 四半期＋選挙 |
| `hc`（参院選挙区） | 45区 / 148名 | Wikipedia「参議院議員一覧」選挙区選出議員 | `scrape_hc.py` | 四半期＋選挙 |
| `muni`（首長） | 1,893 | Claude + web_search | `mayors/run.py` | 月次（要鍵）＋選挙 |

すべて district GeoJSON と**同じ `code`** をキーにする。参院は議員の配列で、合区（鳥取・島根
`31_32`、徳島・高知 `36_39`）は1コードを共有。

### 4.3 自動（四半期／月次）

* **四半期** cron `23 20 1 1,4,7,10 *`（1/4/7/10月の1日 20:23 UTC）: 知事＋衆院＋参院
  （`--level all --skip-muni`）。
* **月次** cron `23 20 5 * *`（毎月5日 20:23 UTC）: 首長のみ。`ANTHROPIC_API_KEY` があるときだけ
  実際に収集（無ければ記録して正常終了）。

いずれもブランチ `auto/refresh-reps` に PR「【自動】議員データ更新（現職の変化）」を開く。

### 4.4 手動 — GitHub の画面（選挙対応）

**Actions → 「議員データ 定期更新（レビュー用PR）」 → Run workflow**、入力:

* `level` = `pref` | `hr` | `hc` | `muni` | `all`（既定 `all`）
* `code` = 1つの district code に絞る（任意。`muni` は2桁の都道府県プレフィックス）

入力は検証され（level は許可リスト照合、code は数字と `_` のみに縮約）、環境変数として渡される
— シェルインジェクションに対して安全。

### 4.5 手動 — ローカル実行

```bash
python3 scripts/reps/validate.py                          # 現データの不変条件チェック
python3 scripts/reps/refresh.py --level pref              # ドライラン: 差分表示・候補のみ書く
python3 scripts/reps/refresh.py --level all --skip-muni   # 知事/衆院/参院（鍵不要）
python3 scripts/reps/refresh.py --level pref --code 47    # 沖縄県知事だけ
python3 scripts/reps/refresh.py --level pref --apply      # app/data/reps_pref.json に反映（CIはこれ）
```

* **`--apply` なし**ではマージ結果を `reps_<level>.candidate.json`（`.gitignore` 済み）に書き、
  そこを検証する安全なプレビュー。
* **`--apply` あり**では本番ファイルに書いて検証し、検証NGなら committed に戻す。
* 名簿が不変なら全レベルで **変更なし** と出る（ライブ Wikipedia で検証済み）。

### 4.6 選挙対応の手順（例）

**沖縄県知事選 2026‑09‑13**（都道府県コード `47`）。結果が確定したら:

*GitHub 経由:* Actions → refresh‑reps → Run workflow → `level=pref, code=47`。PR（前任 → 新任）を
確認し、出典を検めてマージ。

*ローカル:*
```bash
python3 scripts/reps/refresh.py --level pref --code 47            # 交代をプレビュー
python3 scripts/reps/refresh.py --level pref --code 47 --apply    # 反映 → git diff / PR
```
自動検出のレコードは最小（氏名＋出典）で、推薦・初当選日・正確な任期満了などの補完項目は
**未補完**である旨の注記が付く。マージ前後に手で埋める（§4.12）。

同様の契機: 知事の出直し・補選、衆院の総選挙（`scrape_hr.py` の出典記事名を更新し、色→政党対応を
再確認）、参院の通常選挙（2028年）。

### 4.7 保守的マージ

変更検出は `ident(name)`（§4.8）で比較し、レコード全体は見ない。帰結:

* コードの**氏名だけが変わる** → `turnover` として差し替え（最小スタブ＋注記）。
* **新しいコード**（新設区） → `new_code`。
* 氏名が同じ → **レコードをそのまま保持**（補完維持）。政党・任期・かな等の非氏名項目は
  **自動更新されない** — 出典が訂正したら手で直す。

### 4.8 表記揺れと異体字の畳み込み

出典が違えば同一人物の表記も違いうる。照合は `ident()` = `norm_name` ＋ 旧字体/異体字の畳み込み
表 ＋ 別名対応 を通す:

* **`scripts/reps/name_aliases.json`** — 「同一人物・別表記」のグループ。例
  `["塩村あやか", "塩村文夏"]`。**先頭が公開データで採用中の表記**。更新で**偽の交代**（実際は
  通称/かな↔正式漢字で同一人物の「A → B」）が出たら、その組をここに追記して回し直す。
* **`_common.py` の `_KANJI_FOLD`** — よくある異体字（壽↔寿, 邊↔辺, 髙↔高 …）を*比較のためだけ*に
  畳み込む。表示名は取得・補完したまま保持。新しい変種が揺れを起こしたら、畳み込み表に足すか
  （特定の人物なら）`name_aliases.json` に足す（後者推奨）。

### 4.9 欠員（vacancy）

現職不在の議席（辞職して補選なし）は、**第一級のメンバーエントリ**として持つ:

```json
{ "name": null, "vacant": true, "cls": "2028", "termEnd": "2028-07-25",
  "note": "…辞職…、2028年まで欠員", "sourceUrl": "https://…" }
```

これは**議席の不変条件に数える**（総148・改選74/74・東京12）。`validate.py` と `scrape_hc.py` は
欠員を氏名/政党の必須から除外し、`app.js` は「（欠員）」＋見出し「（N議席・M欠員）」で描画する。
新たな欠員を表すには、去った議員のオブジェクトを上の形に差し替える。

### 4.10 首長のエージェント収集

首長は数が多く出典も散在するため決定的スクレイパでは難しい。`scripts/reps/mayors/run.py` は
Claude（Messages API・モデル `claude-opus-4-8`・ツール `web_search_20260209`）に都道府県ごとに
「現職の首長は誰か」を調べさせ、決定的な部分（政令市の行政区は親市の市長を継承、北方領土6村は
`noHead`）を `targets.py` の分類から埋める。

```bash
python3 scripts/reps/mayors/targets.py                     # 対象分類を表示（1712+175+6=1893）
python3 scripts/reps/mayors/run.py --dry-run --pref 47     # プロンプト生成のみ（鍵不要）
python3 scripts/reps/mayors/run.py --pref 47 --out /tmp/okinawa.json   # 実収集（要鍵）
```

* `ANTHROPIC_API_KEY` が必要。**opt‑in かつ比較的高コスト**（全国で都道府県ごと＋政令市に1本ずつ
  のプロンプト）。モデル/web の出力は**無害化**（角括弧除去・URLスキーム検査）し、未確認の自治体は
  `name: null` として既存レコードを保持 — 決して推測で埋めない。
* CI に鍵を置きたくなければ、これをローカルで回し、`refresh.py` で `--apply` してブランチを手で
  push する。

### 4.11 議員データ PR のレビュー — チェックリスト

* **各変更が本当の在任者交代**か — 出典リンクで照合。
* 表記揺れによる**偽の交代**が無いか（あれば `name_aliases.json` へ — §4.8）。
* 交代スタブの補完項目（推薦・初当選日・任期満了）が埋まっているか。
* `validate.py` が緑か（CI がブロックする。ローカルでも確認）。
* 首長は `sourceUrl` と日付をいくつか抜き取り確認。`name: null` は「未確認・保持」扱い。

### 4.12 単一レコードの手修正

臨時の修正（誤字・出典訂正）は `app/data/reps_<level>.json` を直接編集し、**コミット前に
`validate.py` を回す**:

```bash
# レコードを編集 …
python3 scripts/reps/validate.py --level hr
```

近隣と同じ形（必須項目が揃い、`sourceUrl` は http(s)、`<`/`>` を含まない）に保つ。必要なら
ファイルの `asOf` を更新し、コミット＋PR。

---

## 5. 検証と不変条件

`scripts/reps/validate.py` は、議員データの全変更が通るゲート:

```bash
python3 scripts/reps/validate.py                 # 全レベル（exit 0=合格, 1=エラー）
python3 scripts/reps/validate.py --level hc
python3 scripts/reps/validate.py --data-dir DIR  # 候補ディレクトリを検証
```

チェック内容:

* **構造:** `{level, office, asOf, records}` が揃い、`asOf` が日付。
* **件数:** 知事47；衆院289（`districts_hr` とコード一致）；参院45区 / **総148名**、改選74/74、
  東京12、合区コード `31_32`/`36_39` の存在；首長は1700–2000。
* **コード整合:** reps のキーが district GeoJSON のコード集合と一致（首長は部分集合）。
* レベルごとの**必須フィールド**（欠員・無首長は適切に除外）。
* **政党色:** 参照される政党がすべて `reps_parties.json` に色定義を持つ（警告）。
* **注入スキャン（セキュリティ）:** レコード内の文字列に `<`/`>` を含むもの、`sourceUrl` の
  スキームが http/https でないものをエラーにする — これは（首長のエージェント収集データを想定し）
  HTML/スクリプトの断片が、`innerHTML` で描画するアプリに届くのを止める。

警告（例: 年のみの `termEnd`）はブロックしない。エラーは exit 1 でパイプラインを止める。

---

## 6. プライバシー・セキュリティ（常に守る不変条件）

**絶対にコミット・公開しない**（すべて `.gitignore` 済み。`git status --ignored` で確認）:

* `scripts/scrub_rules.json` — 個人情報の除去語リスト（それ自体が個人情報。ローカル＋秘密情報のみ）。
* `mosques_raw.kml` — 未加工の出典エクスポート。
* `data/raw/` — 大容量の境界元データ（再取得可能）。
* `Export_Mega/`, `funding_work/` — 非公開の調査。
* パイプラインの一時ファイル: `app/data/reps_*.candidate.json`, `*.scraped.json`,
  `scripts/reps/mayors/_prompts/`, `refresh_report.md`。

**データの不変条件:**

* 公開するモスクの説明文は **URL のみ** — 住所・氏名・メモ・個人SNSは無い。
* 議員データには**公職者（実名）のみ**が載る。**モスク関係者の氏名は一切載せない。**
* `app/app.js` はモスクの **`source_url` のみ**を描画し、説明文は描画しない。もし将来
  説明文を描画し始めるなら、スクラブ方針全体を見直すこと。

**アプリの堅牢化（維持する）:**

* `app.js` は、`innerHTML` に渡す前にデータ由来の全文字列を `esc()`（HTMLエスケープ）、全リンクを
  `safeUrl()`（http/https のみ）に通す。新しいポップアップ/表の項目も同様にすること — モスクの
  `name` は第三者由来、首長の項目は LLM 由来。

**同一性:** コミットは GitHub の noreply で行い、実名・メール・ローカルパスをリポジトリに残さない。

---

## 7. ローカル開発と検証

アプリは**静的** — ビルド工程は無い。

```bash
# アプリを起動
python3 -m http.server 8777      # → http://localhost:8777/app/
# macOS なら serve.command をダブルクリック

# 生KMLからモスクデータを再現し 0 差分を確認
python3 scripts/refresh_mosques.py --kml mosques_raw.kml
git diff --stat                  # 出典が不変なら空

# ライブ Wikipedia に対して議員データを再現し変更なしを確認
python3 scripts/reps/refresh.py --level all --skip-muni   # 変更なし を期待

# スクラブのロジックを自己テスト（改ざん耐性ケース込み）
python3 scripts/scrub.py
```

環境メモ: すべて **Python 標準ライブラリ**（CI は Python 3.12、ローカルは 3.14 でも動く）。macOS の
ローカル Python は CA 証明書が無いことがあり、HTTPS は `curl` にフォールバックする — 回避のために
**TLS 検証を無効化しない**こと。

---

## 8. トラブルシューティング

| 症状 | ありがちな原因 | 対処 |
|---|---|---|
| モスク Action が即 `SCRUB_RULES_JSON` エラーで失敗 | 秘密情報が未設定/空 | 秘密情報を設定（§2.1）。CI で `--allow-no-rules` を付けない。 |
| モスク実行が「地点が0件」で中断 | 出典が取得不能/空/非公開化 | フェイルセーフ（スナップショット保持）。後で再実行。地図が移動したら `mid` を更新（§3.8）。 |
| 再集計で `! 警告: … 書き戻せません` | 地点が簡略化形状にしか無い区に落ちた（境界変更） | `data/recount/*.gz` を作り直す（§3.9）か `build_districts.py` でフル再ビルド。 |
| モスク PR の説明文に想定外の自由記述 | 一般化をすり抜けた新規PII経路 | PR を却下。除去語を `scrub_rules.json` に追加（§3.7）して回し直す。 |
| スクレイパが `RuntimeError: …構造変化の可能性` | Wikipedia 記事の構造が変わった | パーサ（節見出し・テンプレート名・`_COLOR_PARTY`）を更新し、件数を再確認。 |
| 衆院スクレイパが「未知の政党色コード」 | 政党箱の色が `_COLOR_PARTY` に無い | `scrape_hr.py` に検証済みの `hex→政党` を追加。 |
| 議員 PR が**同一人物**の「交代」を出す | 表記/かな/漢字の揺れ | `name_aliases.json` に組を追加（§4.8）して回し直す。 |
| `validate.py` が議員数/改選内訳でエラー | 行の取りこぼし、または欠員のモデル化ミス | 収集集合をロスターと比較。欠員は §4.9 に従いモデル化。 |
| `validate.py` の「出典URLが http/https ではありません」/「HTMLになりうる文字」 | 不正なURLスキームや角括弧（多くは首長収集由来） | 該当レコードを修正。注入スキャンは意図的。 |
| 月次の議員ジョブが何もしない | `ANTHROPIC_API_KEY` 未設定 | 想定通り。鍵を設定（§2.2）するかローカルで首長を回す（§4.10）。 |
| 出典が不変なのに件数差分が出る | **簡略化**形状で再集計した（`.gz` が無い） | `data/recount/*.gz` がコミットされ存在することを確認。 |

---

## 9. コード・境界・アプリの更新

* **アプリ**（`app/`）は静的でバンドラは無い。`app/app.js` / `index.html` / `style.css` を直接
  編集する。入力は `data/*.geojson` と `reps_*.json` だけ。**公開** = `main` にマージし、`app/`
  ディレクトリを任意の静的ホストで配信（例: リポジトリを指す GitHub Pages — デプロイ用の
  ワークフローは不要・未設置）。
* **境界**は `scripts/build_hr.py`（CC0 の CSIS シェープファイルをディゾルブ → 289小選挙区）と
  `scripts/build_districts.py`（点在判定＋簡略化 → `app/data/*`）で作る。これらは `.venv`
  （shapely/pyshp）が必要。`scripts/geo.py` は依存無しのフォールバック。**境界**が変わったときだけ
  回す。日々の件数更新は `refresh_mosques.py` を通す。
* **区分レベルの追加**は、`app.js` の `DISTRICT_FILES`/`LEVEL_LABEL`、新しい
  `districts_<x>.geojson`、（代表者がいるなら）新しい `reps_<x>.json` ＋ `validate.py` のチェッカ
  ＋ スクレイパ に触れる。

---

## 10. ファイル・コマンド早見表

**更新パイプラインのファイル**

```
scripts/scrub.py                         個人情報スクラブ＋一般化（parse_kml/refresh_mosques 共有）
scripts/refresh_mosques.py               取得→スクラブ→再集計→差分（週次Action）
scripts/mosque_source.json               出典マイマップの mid（公開値）
scripts/mosque_social_allowlist.json     承認済みモスクSNS（公開・人間が編集）
scripts/scrub_rules.json                 個人情報の除去語リスト（ローカル/秘密のみ・非コミット）
data/recount/{muni,hr}.geojson.gz        厳密再集計用フル解像度境界（同梱）
scripts/reps/validate.py                 議員の不変条件＋注入チェック（ゲート）
scripts/reps/_common.py                  wiki取得・ident/マージ・異体字畳み込み・書き出し
scripts/reps/scrape_{pref,hr,hc}.py      知事/衆院/参院スクレイパ（Wikipedia）
scripts/reps/name_aliases.json           同一人物の表記橋渡し（人間が編集）
scripts/reps/refresh.py                  収集→マージ→検証→差分（オーケストレータ）
scripts/reps/mayors/{targets,run}.py     首長のエージェント収集（＋collect_prompt.md）
.github/workflows/refresh-*.yml          定期→PR の2つの Action
```

**コマンド早見**

```bash
# モスク
python3 scripts/refresh_mosques.py [--kml FILE] [--mid ID] [--report FILE] [--allow-no-rules]

# 議員
python3 scripts/reps/validate.py [--level pref|hr|hc|muni|all] [--data-dir DIR]
python3 scripts/reps/refresh.py --level LEVEL [--code CODE] [--apply] [--skip-muni] [--report FILE]
python3 scripts/reps/mayors/targets.py [--pref NN]
python3 scripts/reps/mayors/run.py [--dry-run] [--pref NN] [--out FILE]

# アプリ
python3 -m http.server 8777    # → http://localhost:8777/app/
```

---

*鉄則: 自動化は差分を用意し、公開するのは人間。迷ったらマージせず、PR に質問を残す。*
