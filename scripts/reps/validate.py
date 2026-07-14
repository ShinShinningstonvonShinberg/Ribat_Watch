#!/usr/bin/env python3
"""議員データ（reps_*.json）の不変条件チェック。

議員データの更新パイプライン（scripts/reps/refresh.py）は、再収集した reps ファイルを
公開（PR化）する前に、必ずこの検証を通す。壊れたデータや取りこぼし（例: 参院ロスターの
サマリ取得で議員が静かに欠落する既知の事故）を、公開前に差し止めるのが目的。

チェック内容:
  - 構造: {level, office, asOf, records} が揃い、asOf が日付形式。
  - 件数: 47知事 / 289衆院小選挙区 / 45参院選挙区・148名 / 首長は妥当な範囲。
  - コード整合: reps のキーが対応する district GeoJSON のコード集合と一致（首長は部分集合）。
  - 必須フィールド: 氏名・出典URL・政党/会派・任期満了日 など（無首長の特例は除く）。
  - 参院: メンバー総数148、各選挙区の magnitude とメンバー数の一致、クラス（改選年）内訳。
  - 政党色: 参照される政党がすべて reps_parties.json に色定義を持つ。

使い方:
  python3 scripts/reps/validate.py                 # app/data の reps_*.json を全レベル検証
  python3 scripts/reps/validate.py --level hr
  python3 scripts/reps/validate.py --data-dir DIR  # 別ディレクトリを検証（再収集結果の事前検証）
戻り値: すべて合格なら 0、エラーがあれば 1。
"""
from __future__ import annotations
import os
import re
import sys
import json
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_DATA = os.path.join(ROOT, "app", "data")

DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")   # YYYY / YYYY-MM / YYYY-MM-DD（任期満了は粗くても可）
FULL_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")     # 完全な日付（YYYY-MM-DD）
LEVELS = ("pref", "hr", "hc", "muni")


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _district_codes(level, app_data=APP_DATA):
    d = _load(os.path.join(app_data, f"districts_{level}.geojson"))
    return {str(f["properties"]["code"]) for f in d["features"]}


def _base_checks(doc, expect_office, errors):
    for key in ("level", "office", "asOf", "records"):
        if key not in doc:
            errors.append(f"トップレベルに '{key}' がありません")
    if doc.get("asOf") and not DATE_RE.match(str(doc["asOf"])):
        errors.append(f"asOf の日付形式が不正: {doc.get('asOf')!r}")
    if expect_office and doc.get("office") and expect_office not in str(doc["office"]):
        errors.append(f"office が想定と異なります（'{expect_office}' を含むはず）: {doc.get('office')!r}")


# ---------------------------------------------------------------------------
# レベル別チェック
# ---------------------------------------------------------------------------
def check_pref(doc, app_data=APP_DATA):
    errors, warnings = [], []
    _base_checks(doc, "知事", errors)
    recs = doc.get("records", {})
    if len(recs) != 47:
        errors.append(f"知事は47人のはず: {len(recs)} 件")
    expect = {f"{i:02d}" for i in range(1, 48)}
    if set(recs) != expect:
        errors.append(f"都道府県コードが 01..47 と一致しません（差分 {sorted(set(recs) ^ expect)[:6]}…）")
    for code, r in recs.items():
        if not r.get("name"):
            errors.append(f"[{code}] 氏名なし")
        if not r.get("sourceUrl"):
            errors.append(f"[{code}] 出典URLなし")
        if not r.get("affiliation"):
            warnings.append(f"[{code}] 所属（affiliation）なし")
    return errors, warnings


def check_hr(doc, app_data=APP_DATA, parties=None):
    errors, warnings = [], []
    _base_checks(doc, "衆議院", errors)
    recs = doc.get("records", {})
    if len(recs) != 289:
        errors.append(f"衆院小選挙区は289のはず: {len(recs)} 件")
    dcodes = _district_codes("hr", app_data)
    if set(recs) != dcodes:
        errors.append(f"衆院コードが district_hr と一致しません（reps-districtsのみ {sorted(set(recs) - dcodes)[:6]}…）")
    for code, r in recs.items():
        if not r.get("name"):
            errors.append(f"[{code}] 氏名なし")
        if not r.get("party"):
            errors.append(f"[{code}] 政党なし")
        if not r.get("sourceUrl"):
            errors.append(f"[{code}] 出典URLなし")
        if parties and r.get("party") and r["party"] not in parties:
            warnings.append(f"[{code}] 政党 '{r['party']}' の色定義が reps_parties.json にありません")
    return errors, warnings


def check_hc(doc, app_data=APP_DATA, parties=None):
    errors, warnings = [], []
    _base_checks(doc, "参議院", errors)
    recs = doc.get("records", {})
    if len(recs) != 45:
        errors.append(f"参院選挙区は45のはず: {len(recs)} 件")
    dcodes = _district_codes("hc", app_data)
    if set(recs) != dcodes:
        errors.append(f"参院コードが district_hc と一致しません（差分 {sorted(set(recs) ^ dcodes)[:6]}…）")
    for gk in ("31_32", "36_39"):    # 合区コードの存在
        if gk not in recs:
            errors.append(f"合区コード {gk} がありません")
    total, cls = 0, {}
    for code, r in recs.items():
        members = r.get("members", [])
        total += len(members)
        if r.get("magnitude") is not None and r.get("magnitude") != len(members):
            errors.append(f"[{code}] magnitude({r.get('magnitude')}) とメンバー数({len(members)})が不一致")
        for m in members:
            cls[m.get("cls")] = cls.get(m.get("cls"), 0) + 1
            # 欠員（vacant）は議席として数えるが、氏名・政党・出典は無くてよい（辞職等・補選なし）
            required = ("cls",) if m.get("vacant") else ("name", "party", "termEnd", "sourceUrl")
            for fld in required:
                if not m.get(fld):
                    errors.append(f"[{code}] メンバー '{m.get('name') or '（欠員）'}' に {fld} なし")
            if m.get("termEnd") and not DATE_RE.match(str(m["termEnd"])):
                errors.append(f"[{code}] termEnd の形式が不正: {m.get('termEnd')!r}")
            elif m.get("termEnd") and not FULL_DATE_RE.match(str(m["termEnd"])):
                warnings.append(f"[{code}] {m.get('name','?')} の termEnd が年のみで粗い（YYYY-MM-DD 推奨）: {m['termEnd']!r}")
            if parties and not m.get("vacant") and m.get("party") and m["party"] not in parties:
                warnings.append(f"[{code}] 政党 '{m['party']}' の色定義なし")
    if total != 148:
        errors.append(f"参院選挙区の総議員数は148のはず: {total} 名")
    # クラス（改選年）内訳。選挙区は半数改選なので通常 74/74。
    if len(cls) == 2 and sorted(cls.values()) != [74, 74]:
        warnings.append(f"改選クラスの内訳が 74/74 ではありません: {cls}")
    tokyo = recs.get("13", {}).get("members", [])
    if len(tokyo) != 12:
        warnings.append(f"東京(13)は12名のはず: {len(tokyo)} 名")
    return errors, warnings


def check_muni(doc, app_data=APP_DATA):
    errors, warnings = [], []
    _base_checks(doc, "首長", errors)
    recs = doc.get("records", {})
    # 合併等で件数は変動しうるため、幅で判定（大きく外れたら異常）
    if not (1700 <= len(recs) <= 2000):
        errors.append(f"首長の件数が想定範囲(1700-2000)外: {len(recs)} 件")
    elif len(recs) != 1893:
        warnings.append(f"首長の件数が前回(1893)と異なります: {len(recs)} 件（合併・改称の可能性）")
    dcodes = _district_codes("muni", app_data)
    orphans = set(recs) - dcodes
    if orphans:
        errors.append(f"district_muni に無いコードの首長: {sorted(orphans)[:8]}…")
    for code, r in recs.items():
        if r.get("office") and r["office"] not in ("市長", "町長", "村長", "区長"):
            warnings.append(f"[{code}] 想定外の職名: {r.get('office')!r}")
        if r.get("inherited") and not r.get("parentCity"):
            errors.append(f"[{code}] inherited だが parentCity なし")
        if r.get("noHead"):
            continue                              # 無首長（北方領土6村など）は出典・任期の欠落を許容
        if not r.get("name"):
            errors.append(f"[{code}] 氏名なし")
        if not r.get("sourceUrl"):
            warnings.append(f"[{code}] 出典URLなし")
    return errors, warnings


CHECKERS = {"pref": check_pref, "hr": check_hr, "hc": check_hc, "muni": check_muni}

_URL_KEYS = ("sourceUrl", "source_url")
_HTTP_RE = re.compile(r"^https?://", re.I)


def injection_scan(doc):
    """公開前の安全チェック: レコード内の全文字列に < > が無いか（HTML/スクリプト注入の芽）、
    出典URL系フィールドが http/https スキームか、を検査する。首長データはエージェント＋web
    収集由来で完全には信頼できないため、アプリ（innerHTML描画）に渡る前にここで止める。"""
    errs = []

    def walk(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")
        elif isinstance(o, str):
            if "<" in o or ">" in o:
                errs.append(f"{path}: HTMLになりうる文字(< >)を含む: {o[:40]!r}")
            if path.rsplit(".", 1)[-1] in _URL_KEYS and o and not _HTTP_RE.match(o):
                errs.append(f"{path}: 出典URLが http/https ではありません: {o[:40]!r}")

    walk(doc.get("records", {}), "records")
    return errs


def validate_level(level, data_dir=APP_DATA, app_data=APP_DATA, parties=None, fname=None):
    """1レベルを検証して (errors, warnings) を返す。

    fname を渡すと data_dir 内のそのファイルを検証する（既定は reps_<level>.json）。
    再収集の候補（reps_<level>.candidate.json）を本番反映前に検証するのに使う。
    """
    path = os.path.join(data_dir, fname or f"reps_{level}.json")
    if not os.path.exists(path):
        return [f"ファイルがありません: {path}"], []
    doc = _load(path)
    if level in ("hr", "hc"):
        errors, warnings = CHECKERS[level](doc, app_data=app_data, parties=parties)
    else:
        errors, warnings = CHECKERS[level](doc, app_data=app_data)
    errors += injection_scan(doc)     # 公開前のHTML/スキーム注入チェック（全レベル共通）
    return errors, warnings


def validate_all(data_dir=APP_DATA, app_data=APP_DATA, levels=LEVELS):
    """指定レベルをすべて検証。{level: (errors, warnings)} を返す。"""
    parties = None
    ppath = os.path.join(data_dir, "reps_parties.json")
    if os.path.exists(ppath):
        parties = set(_load(ppath).keys())
    return {lvl: validate_level(lvl, data_dir, app_data, parties) for lvl in levels}


def main():
    ap = argparse.ArgumentParser(description="議員データ（reps_*.json）の不変条件チェック")
    ap.add_argument("--level", choices=LEVELS + ("all",), default="all")
    ap.add_argument("--data-dir", default=APP_DATA, help="検証対象の reps_*.json があるディレクトリ")
    ap.add_argument("--app-data", default=APP_DATA, help="district_*.geojson があるディレクトリ（コード整合の基準）")
    args = ap.parse_args()

    levels = LEVELS if args.level == "all" else (args.level,)
    results = validate_all(args.data_dir, args.app_data, levels)
    total_err = 0
    for lvl in levels:
        errors, warnings = results[lvl]
        total_err += len(errors)
        status = "OK" if not errors else f"NG（エラー{len(errors)}）"
        print(f"[{lvl}] {status}" + (f" / 警告{len(warnings)}" if warnings else ""))
        for e in errors:
            print(f"   ✗ {e}")
        for w in warnings[:20]:
            print(f"   ⚠ {w}")
        if len(warnings) > 20:
            print(f"   ⚠ …ほか {len(warnings) - 20} 件の警告")
    print(f"\n{'✅ 検証合格' if total_err == 0 else f'❌ 合計 {total_err} 件のエラー'}")
    sys.exit(1 if total_err else 0)


if __name__ == "__main__":
    main()
