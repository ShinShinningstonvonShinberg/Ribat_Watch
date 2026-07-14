#!/usr/bin/env python3
"""モスク地点データの定期再取得パイプライン（公開向け・スクラブ＋一般化＋再集計）。

目的: 出典の公開マイマップ（全国モスクリスト）から現在のピンを取り込み、公開版を最新に保つ。
このスクリプトは GitHub Actions（.github/workflows/refresh-mosques.yml）から定期実行され、
差分だけを載せた Pull Request を開く。人間がレビューして初めて公開される（自動公開しない）。

処理の流れ:
  1. KML を取得（既定は公開 mid のエンドポイント。--kml でローカルファイルも可）。
  2. parse_kml.parse_kml() で解釈し、説明文をスクラブ＋一般化（scripts/scrub.py）。
     - denylist（scrub_rules.json）は環境変数 SCRUB_RULES_JSON / SCRUB_RULES_PATH / 既定パスから。
     - 一般化により、まだ denylist に無い新規の個人情報（自由記述・新規SNS）も漏れない。
  3. mosques.geojson / mosques.csv（ルート）と app/data/mosques.geojson を書き出す。
  4. 4レベルの選挙区・行政区について、点在判定で件数を再集計し、
     既存の app/data/districts_*.geojson に件数だけ書き戻す（形状は不変）。
     - 高精度: data/raw/muni.geojson・data/raw/hr.geojson（フル解像度）を使う。
       都道府県・参院はコード先頭2桁と合区規則から導出（build_districts.py と同一結果）。
     - フォールバック: data/raw が無ければ、公開中の簡略化形状で近似再集計（警告を出す）。
  5. 差分サマリ（地点の増減・移動、レベル別件数の増減）を標準出力と --report に出力。

安全策:
  - denylist が読めない状態（CIで秘密情報の設定漏れ等）では既定で中断する（--allow-no-rules で続行）。
    生の個人情報を一般化なしで載せてしまう事故を防ぐ。
  - 出典が取得不能・空なら、既存の公開スナップショットを保持して非ゼロ終了（アプリは壊れない）。

使い方:
  python3 scripts/refresh_mosques.py                     # 出典から取得して更新
  python3 scripts/refresh_mosques.py --kml mosques_raw.kml   # ローカルKMLで更新（オフライン検証）
  python3 scripts/refresh_mosques.py --report /tmp/report.md # 差分サマリをファイルにも出力
"""
from __future__ import annotations
import os
import sys
import json
import gzip
import argparse
import urllib.request
import io
import zipfile
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)                       # parse_kml を import するため
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import parse_kml                               # noqa: E402
import scrub                                   # noqa: E402
import geo                                     # noqa: E402

APP_DATA = os.path.join(ROOT, "app", "data")
RAW = os.path.join(ROOT, "data", "raw")               # 生の境界（.gitignore・再DL可）
RECOUNT = os.path.join(ROOT, "data", "recount")       # 再集計用のフル解像度境界（gzで同梱）
SOURCE_CFG = os.path.join(ROOT, "scripts", "mosque_source.json")

LAYER_KEY = {"mosque": "mosque", "prayer_room": "prayer", "planned": "planned"}
# 参議院の合区: 都道府県コード -> 統合後コード（build_districts.py と一致させる）
HC_MERGES = {"31": "31_32", "32": "31_32", "36": "36_39", "39": "36_39"}


# ---------------------------------------------------------------------------
# KML 取得
# ---------------------------------------------------------------------------
def load_source_config():
    with open(SOURCE_CFG, encoding="utf-8") as f:
        return json.load(f)


def fetch_kml(mid=None, timeout=60):
    """公開 mid のエンドポイントから KML を取得して bytes で返す（KMZ なら解凍）。

    urllib で取得し、SSL/ネットワークで失敗したら curl（システムのCA証明書を使う）に
    フォールバックする。CI（Ubuntu）では urllib で成功し、一部のローカル環境
    （CA証明書未導入の macOS Python 等）でも curl 経由で取得できる。
    """
    cfg = load_source_config()
    mid = mid or os.environ.get("MYMAP_MID") or cfg["mid"]
    url = cfg["kml_url_template"].format(mid=mid)
    ua = "Ribat-Watch-refresh/1.0 (+https://github.com/)"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except urllib.error.URLError:
        import shutil
        import subprocess
        if not shutil.which("curl"):
            raise
        data = subprocess.run(["curl", "-sSL", "--fail", "-A", ua, "--max-time", str(timeout), url],
                              check=True, stdout=subprocess.PIPE).stdout
    if data[:2] == b"PK":                      # KMZ（ZIP）なら中の .kml を取り出す
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            kmls = [n for n in z.namelist() if n.lower().endswith(".kml")]
            if not kmls:
                raise ValueError("KMZ に .kml が含まれていません")
            data = z.read(sorted(kmls, key=lambda n: -z.getinfo(n).file_size)[0])
    return data


# ---------------------------------------------------------------------------
# 再集計（点在判定）
# ---------------------------------------------------------------------------
def _load_points(features):
    return [(f["geometry"]["coordinates"][0], f["geometry"]["coordinates"][1],
             f["properties"].get("layer", "mosque")) for f in features]


def _load_geojson(path):
    """GeoJSON を読み込む（.gz にも対応）。"""
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def _resolve_geom(name):
    """再集計用のフル解像度境界を解決する。data/raw を優先し、無ければ同梱の data/recount/*.gz。"""
    raw = os.path.join(RAW, f"{name}.geojson")
    if os.path.exists(raw):
        return raw
    gz = os.path.join(RECOUNT, f"{name}.geojson.gz")
    return gz if os.path.exists(gz) else None


def _count_against(path, code_field, points):
    """path のポリゴンに points を点在判定し、code -> [mosque,prayer,planned] を返す。"""
    feats = _load_geojson(path)["features"]
    for f in feats:
        f["properties"]["_c"] = str(f["properties"].get(code_field) or "")
        f["properties"]["_m"] = [0, 0, 0]
    idx = geo.BBoxIndex(feats)
    unassigned = 0
    for lon, lat, layer in points:
        f = idx.query_point(lon, lat)
        if f is None:
            unassigned += 1
            continue
        f["properties"]["_m"][{"mosque": 0, "prayer_room": 1, "planned": 2}.get(layer, 0)] += 1
    counts = defaultdict(lambda: [0, 0, 0])
    for f in feats:
        c = f["properties"]["_c"]; m = f["properties"]["_m"]
        a = counts[c]; a[0] += m[0]; a[1] += m[1]; a[2] += m[2]
    return counts, unassigned


def recount_fullres(points):
    """フル解像度の境界（data/raw または同梱の data/recount/*.gz）で4レベルの件数を求める。
    build_districts.py と同一のフル解像度形状を使うため、出典が不変なら件数も完全一致する。"""
    muni, un_m = _count_against(_resolve_geom("muni"), "N03_007", points)
    # 都道府県 = 市区町村コード先頭2桁で集約
    pref = defaultdict(lambda: [0, 0, 0])
    for c, a in muni.items():
        pc = c[:2] if len(c) >= 2 else c
        b = pref[pc]; b[0] += a[0]; b[1] += a[1]; b[2] += a[2]
    # 参院 = 都道府県を合区規則で集約
    hc = defaultdict(lambda: [0, 0, 0])
    for pc, a in pref.items():
        code = HC_MERGES.get(pc, pc); b = hc[code]; b[0] += a[0]; b[1] += a[1]; b[2] += a[2]
    hr, un_h = _count_against(_resolve_geom("hr"), "kucode", points)
    return {"muni": muni, "pref": pref, "hc": hc, "hr": hr}, {"muni": un_m, "hr": un_h}


def recount_simplified(points):
    """フォールバック: 公開中の簡略化形状で近似再集計（data/raw が無い場合）。"""
    counts = {}
    for level in ("muni", "pref", "hr", "hc"):
        c, _ = _count_against(os.path.join(APP_DATA, f"districts_{level}.geojson"), "code", points)
        counts[level] = c
    return counts


def has_fullres():
    return bool(_resolve_geom("muni")) and bool(_resolve_geom("hr"))


def apply_counts(level, counts):
    """算出した件数を app/data/districts_<level>.geojson に書き戻す（形状は不変）。

    書き戻せなかった件数（＝簡略化で消えた区に落ちた地点）があれば返す（健全性チェック用）。
    """
    path = os.path.join(APP_DATA, f"districts_{level}.geojson")
    d = json.load(open(path, encoding="utf-8"))
    file_codes = set()
    for f in d["features"]:
        p = f["properties"]; c = str(p["code"]); file_codes.add(c)
        a = counts.get(c, [0, 0, 0])
        p["mosque"], p["prayer"], p["planned"] = a[0], a[1], a[2]
        p["total"] = a[0] + a[1] + a[2]
    json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    # 書き戻せなかった件数 = ファイルに存在しないコードに落ちた地点数（合区は同一コードが
    # 複数ポリゴンに現れるため、フィーチャ合計ではなく「コード集合の差」で判定する）。
    leftover = sum(sum(v) for c, v in counts.items() if c and c not in file_codes)
    return leftover    # >0 なら簡略化で消えた区に落ちた地点あり


# ---------------------------------------------------------------------------
# 差分サマリ
# ---------------------------------------------------------------------------
def _index_by_identity(features):
    """地点を安定な識別キー（種別・名称・完全精度の座標 ＋ 同一キーの出現連番）で索引化する。

    以前は (name, 小数4桁座標) をキーにしていたため、同名・同座標の重複ピンが互いを上書きし、
    人間がPRで承認に使う差分サマリから実在の追加・削除・移動が抜け落ちる恐れがあった。
    種別と完全精度座標を含め、なお衝突する完全重複には出現連番を付けて各ピンを個別に表す。"""
    out = {}
    counts = {}
    for f in features:
        lon, lat = f["geometry"]["coordinates"]
        p = f["properties"]
        base = (p.get("layer", ""), p.get("name", ""), lon, lat)
        n = counts.get(base, 0)
        counts[base] = n + 1
        out[base + (n,)] = p
    return out


def _disp(key):
    """識別キー (layer, name, lon, lat, occ) を表示用 (name, lat, lon) にする。"""
    _, nm, lo, la, _ = key
    return nm, round(la, 5), round(lo, 5)


def build_diff_report(old_feats, new_feats, old_counts, new_counts):
    lines = []
    oi, ni = _index_by_identity(old_feats), _index_by_identity(new_feats)
    added = [k for k in ni if k not in oi]
    removed = [k for k in oi if k not in ni]
    # 「移動」= 同名の地点が「追加」と「削除」の両方に現れるもの（＝座標だけ変わった）。
    # 単に旧データに同名が在るだけでは移動としない（generic な名称での過剰報告を避ける）。
    added_names = {k[1] for k in added}
    removed_names = {k[1] for k in removed}
    moved = added_names & removed_names
    lines.append(f"- 地点数: {len(old_feats)} → {len(new_feats)}")
    lines.append(f"- 追加: {len(added)} / 削除: {len(removed)}（うち移動の可能性: {len(moved)}）")
    if added:
        lines.append("  - 追加された地点:")
        for k in added[:25]:
            nm, la, lo = _disp(k)
            lines.append(f"    - {nm}  ({la}, {lo})")
        if len(added) > 25:
            lines.append(f"    - …ほか {len(added) - 25} 件")
    if removed:
        lines.append("  - 削除された地点:")
        for k in removed[:25]:
            nm, la, lo = _disp(k)
            lines.append(f"    - {nm}  ({la}, {lo})")
        if len(removed) > 25:
            lines.append(f"    - …ほか {len(removed) - 25} 件")
    # レベル別 合計件数の増減
    lines.append("- レベル別 合計件数:")
    for level in ("muni", "pref", "hc", "hr"):
        o = sum(sum(v) for v in old_counts.get(level, {}).values())
        n = sum(sum(v) for v in new_counts.get(level, {}).values())
        mark = "" if o == n else f"  ← {'+' if n >= o else ''}{n - o}"
        lines.append(f"    - {level}: {o} → {n}{mark}")
    return "\n".join(lines)


def _counts_from_committed():
    """現在の app/data/districts_*.geojson から件数を読み出す（差分の基準）。"""
    out = {}
    for level in ("muni", "pref", "hc", "hr"):
        path = os.path.join(APP_DATA, f"districts_{level}.geojson")
        d = json.load(open(path, encoding="utf-8"))
        c = {}
        for f in d["features"]:
            p = f["properties"]; c[str(p["code"])] = [p.get("mosque", 0), p.get("prayer", 0), p.get("planned", 0)]
        out[level] = c
    return out


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="モスク地点の再取得・スクラブ・再集計")
    ap.add_argument("--kml", help="ローカルKMLファイル（省略時は出典から取得）")
    ap.add_argument("--report", help="差分サマリの出力先ファイル（Markdown）")
    ap.add_argument("--allow-no-rules", action="store_true",
                    help="denylist（scrub_rules）が無くても続行する（既定は中断）")
    ap.add_argument("--mid", help="出典マイマップの mid を上書き")
    args = ap.parse_args()

    # --- 安全策: denylist が読めない状態での実行を既定で拒否 ---
    if not scrub.rules_available() and not args.allow_no_rules:
        sys.exit("ERROR: スクラブ規則（SCRUB_RULES_JSON / SCRUB_RULES_PATH / scripts/scrub_rules.json）が見つかりません。\n"
                 "       生の個人情報を一般化なしで公開する事故を防ぐため中断します。\n"
                 "       CI では秘密情報 SCRUB_RULES_JSON を設定してください。\n"
                 "       規則ファイル無しで一般化のみ適用したい場合は --allow-no-rules を付けてください。")

    # --- 1. KML 取得 ---
    if args.kml:
        with open(args.kml, "rb") as f:
            kml = f.read()
        print(f"KML: ローカル {args.kml}（{len(kml)} bytes）")
    else:
        kml = fetch_kml(mid=args.mid)
        print(f"KML: 出典から取得（{len(kml)} bytes）")

    # --- 2. 解釈＋スクラブ＋一般化 ---
    scrubber = scrub.Scrubber()
    grandfather = scrub.load_grandfather_social(os.path.join(APP_DATA, "mosques.geojson"))
    rows, features = parse_kml.parse_kml(kml, scrubber=scrubber, grandfather=grandfather)
    if not features:
        sys.exit("ERROR: 地点が0件でした。出典が空か取得失敗の可能性。既存スナップショットを保持して中断します。")
    print(f"解釈: {len(features)} 地点（スクラブ＋一般化済み）")

    # 差分の基準として現在の公開データを控える
    old_feats = json.load(open(os.path.join(APP_DATA, "mosques.geojson"), encoding="utf-8"))["features"]
    old_counts = _counts_from_committed()

    # --- 3. 地点データを書き出し（ルート＋app/data、build_districts と同じく同内容） ---
    parse_kml.write_geojson(features, os.path.join(ROOT, "mosques.geojson"))
    parse_kml.write_csv(rows, os.path.join(ROOT, "mosques.csv"))
    parse_kml.write_geojson(features, os.path.join(APP_DATA, "mosques.geojson"))

    # --- 4. 再集計して district ファイルに件数を書き戻す ---
    points = _load_points(features)
    if has_fullres():
        new_counts, unassigned = recount_fullres(points)
        print(f"再集計: フル解像度（data/raw）| 未割当 muni={unassigned['muni']} hr={unassigned['hr']}")
    else:
        new_counts = recount_simplified(points)
        print("再集計: 簡略化形状での近似（data/raw が無いため）。厳密な件数は data/raw を用意して再実行してください。")
    for level in ("muni", "pref", "hc", "hr"):
        leftover = apply_counts(level, new_counts[level])
        if leftover:
            print(f"  ! 警告: {level} で {leftover} 地点が簡略化済みの区に割り当たり書き戻せません。"
                  f"境界変更の可能性 → build_districts.py での完全再ビルドを検討。")

    # --- 5. 差分サマリ ---
    report = build_diff_report(old_feats, features, old_counts, new_counts)
    print("\n=== 差分サマリ ===\n" + report)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write("## モスクデータ 再取得 差分\n\n" + report + "\n")
        print(f"\n差分サマリを書き出し: {args.report}")


if __name__ == "__main__":
    main()
