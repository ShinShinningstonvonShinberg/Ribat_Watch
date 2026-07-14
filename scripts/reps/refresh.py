#!/usr/bin/env python3
"""議員データ更新のオーケストレータ（収集 → 保守的マージ → 検証 → 差分 → 書き出し）。

各レベルのスクレイパ／収集を呼び、現職を検出して既存データと突き合わせ、
「氏名（＝在任者）が変わった」ところだけを差し替え候補にする（curation を壊さない）。
結果を validate.py で検証し、差分サマリを出す。GitHub Action がこれを --apply で走らせ、
差分だけを載せた Pull Request を開く（人間が承認。名簿は自動公開しない）。

レベルと収集元:
  pref … scrape_pref（Wikipedia 都道府県知事の一覧）        単任マージ
  hr   … scrape_hr  （Wikipedia 第51回衆院選 小選挙区当選者） 単任マージ
  hc   … scrape_hc  （Wikipedia 参議院議員一覧）              複数人区マージ
  muni … mayors/run （Claude + web_search・要APIキー・月次）  首長マージ

使い方:
  python3 scripts/reps/refresh.py --level pref                 # 候補を作り差分を表示（書き込まない）
  python3 scripts/reps/refresh.py --level pref --code 47       # 沖縄県知事だけ（2026-09-13 選挙）
  python3 scripts/reps/refresh.py --level pref --apply         # app/data に反映（CI用）
  python3 scripts/reps/refresh.py --level all --report r.md    # 知事/衆院/参院（muniは鍵があれば）
"""
from __future__ import annotations
import os
import sys
import json
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _common as C          # noqa: E402
import validate as V         # noqa: E402

TIER_OFFICE = {"pref": "知事", "hr": "衆議院議員（小選挙区）",
               "hc": "参議院議員（選挙区）", "muni": "首長"}


def _load_committed(level):
    with open(os.path.join(C.APP_DATA, f"reps_{level}.json"), encoding="utf-8") as f:
        return json.load(f)


def _collect(level, code=None):
    """レベルの現職を収集して (scraped_records, kind) を返す。kind ∈ {single, hc, muni}。"""
    if level == "pref":
        import scrape_pref
        return scrape_pref.collect(), "single"
    if level == "hr":
        import scrape_hr
        return scrape_hr.collect(), "single"
    if level == "hc":
        import scrape_hc
        return scrape_hc.collect(), "hc"
    if level == "muni":
        from mayors import run as mrun
        prior = _load_committed("muni")["records"]
        by_code, cities = mrun.collect(pref=code)     # muni の code は2桁の都道府県プレフィックス
        return mrun.assemble(by_code, cities, pref=code, prior=prior), "muni"
    raise ValueError(level)


def _restrict(scraped, kind, code):
    """--code 指定時、その district code だけに絞る（muni は前方一致で絞る）。"""
    if not code:
        return scraped
    if kind == "muni":
        return {k: v for k, v in scraped.items() if k.startswith(code)}
    return {k: v for k, v in scraped.items() if k == code}


def refresh_level(level, code=None, apply=False):
    """1レベルを更新して {changes, errors, warnings, wrote} を返す。"""
    committed = _load_committed(level)
    recs = committed["records"]
    scraped, kind = _collect(level, code=code)
    scraped = _restrict(scraped, kind, code)

    if kind == "hc":
        merged, changes = C.merge_hc(recs, scraped)
    elif kind == "muni":
        merged, changes = _merge_muni(recs, scraped)
    else:
        merged, changes = C.merge_single(recs, scraped)

    # asOf は「変化があったときだけ」更新する（無変化で差分を出さないため）
    as_of = C.today_iso() if changes else committed.get("asOf")
    out_doc = {"level": level, "office": committed.get("office", TIER_OFFICE[level]),
               "asOf": as_of, "records": merged}

    # 検証は「実際に書き出したファイル」を対象にする（--apply 時は本番ファイル、
    # ドライラン時は candidate。以前は常に本番ファイルを見ており、ドライランの検証が
    # 古い committed を見てしまう不具合があった）。
    cand_path = os.path.join(C.APP_DATA, f"reps_{level}.json" if apply else f"reps_{level}.candidate.json")
    with open(cand_path, "w", encoding="utf-8") as f:
        json.dump(out_doc, f, ensure_ascii=False, indent=1)
    errors, warnings = V.validate_level(level, data_dir=C.APP_DATA, fname=os.path.basename(cand_path))
    if errors and apply:
        # 検証NGなら本番書き込みは危険 → 直前の committed に戻す
        with open(cand_path, "w", encoding="utf-8") as f:
            json.dump(committed, f, ensure_ascii=False, indent=1)
    return {"changes": changes, "errors": errors, "warnings": warnings,
            "wrote": cand_path, "asOf": as_of}


def _merge_muni(committed, scraped):
    """首長マージ: 氏名が変わった（＝交代）コードだけ差し替え候補にする。"""
    merged = dict(committed)
    changes = []
    for code, rec in scraped.items():
        cur = committed.get(code)
        new_name = rec.get("name")
        if cur is None:
            merged[code] = rec
            changes.append({"code": code, "kind": "new_code", "old": None, "new": new_name})
        elif new_name and C.ident(new_name) != C.ident(cur.get("name", "")):
            merged[code] = rec
            changes.append({"code": code, "kind": "turnover", "old": cur.get("name"), "new": new_name})
    return merged, changes


def _fmt_changes(level, changes):
    if not changes:
        return f"- **{level}**: 変更なし"
    lines = [f"- **{level}**: {len(changes)} 件の変更候補"]
    for ch in changes[:40]:
        if ch["kind"] == "membership":
            old_id = {C.ident(n): n for n in ch["old"]}
            new_id = {C.ident(n): n for n in ch["new"]}
            added = [new_id[i] for i in new_id if i not in old_id]
            removed = [old_id[i] for i in old_id if i not in new_id]
            lines.append(f"    - [{ch['code']}] 増: {sorted(added)} / 減: {sorted(removed)}")
        else:
            lines.append(f"    - [{ch['code']}] {ch['kind']}: {ch['old']} → {ch['new']}")
    if len(changes) > 40:
        lines.append(f"    - …ほか {len(changes) - 40} 件")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="議員データ更新オーケストレータ")
    ap.add_argument("--level", choices=("pref", "hr", "hc", "muni", "all"), default="all")
    ap.add_argument("--code", help="district code を1つに絞る（muniは2桁の都道府県プレフィックス）")
    ap.add_argument("--apply", action="store_true", help="app/data/reps_*.json に反映（既定は .candidate.json）")
    ap.add_argument("--report", help="差分サマリの出力先（Markdown）")
    ap.add_argument("--skip-muni", action="store_true", help="首長（要APIキー・高コスト）を除外")
    args = ap.parse_args()

    levels = (["pref", "hr", "hc"] if args.level == "all" else [args.level])
    if args.level == "all" and not args.skip_muni and os.environ.get("ANTHROPIC_API_KEY"):
        levels.append("muni")   # 鍵があるときだけ muni も

    report_lines, total_err, total_changes = ["## 議員データ 更新 差分\n"], 0, 0
    for lvl in levels:
        try:
            r = refresh_level(lvl, code=args.code, apply=args.apply)
        except Exception as e:
            report_lines.append(f"- **{lvl}**: 収集失敗 — {type(e).__name__}: {e}")
            total_err += 1
            continue
        total_err += len(r["errors"])
        total_changes += len(r["changes"])
        report_lines.append(_fmt_changes(lvl, r["changes"]))
        for e in r["errors"]:
            report_lines.append(f"    - ✗ 検証エラー: {e}")
    report = "\n".join(report_lines)
    print(report)
    print(f"\n合計: 変更候補 {total_changes} 件 / 検証エラー {total_err} 件")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(report + "\n")
    sys.exit(1 if total_err else 0)


if __name__ == "__main__":
    main()
