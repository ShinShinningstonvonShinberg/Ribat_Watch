#!/usr/bin/env python3
"""参議院議員（選挙区）スクレイパ。

出典: 日本語版 Wikipedia「参議院議員一覧」の「選挙区選出議員」表。
選挙区選出議員は全148名（都道府県ごとに、改選年 2028 と 2031 の2階級が並ぶ）。
役割は「今その選挙区に就いているのは誰か」という変動しやすい事実の検出。氏名・所属政党・
改選年（＝class）を拾い、任期満了日は class から決定的に導く。合区（鳥取県・島根県／
徳島県・高知県）は特別コード（31_32／36_39）にまとめる。詳細な補完（かな・推薦など）は
既存 reps_hc.json 側に委ねる（_common 参照）。

表の構造（重要）:
  - 各都道府県は「!rowspan=\"N\"|[[○○県選挙区|○○県]]」というヘッダセルで始まる。
    定数2/4 は rowspan=2、定数8/12 は rowspan=4。
  - 改選年セル「<small>2028年<br />（令和10年）</small>」で class が切り替わる。
    定数8/12 では年セルが rowspan=2 で複数の表行にまたがるため、class は状態として保持する。
  - 議員セルは「[[File:...]]<br/>[[氏名]]<br />（政党）」の形。File/ファイルリンクは写真。
"""
from __future__ import annotations
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

LEVEL = "hc"
OFFICE = "参議院議員（選挙区）"
PAGE = "参議院議員一覧"

# 合区（選挙区名 → 特別コード）
_MERGE = {"鳥取県・島根県": "31_32", "徳島県・高知県": "36_39"}

# class（改選年）→ 任期満了日（決定的）
_TERM_END = {"2028": "2028-07-25", "2031": "2031-07-28"}

# 都道府県ヘッダ: !rowspan="N"|[[○○選挙区|表示]] から選挙区名（○○）を取る
_HEADER_RE = re.compile(r'!\s*rowspan="\d+"\s*\|\s*\[\[\s*([^\]|]+?)選挙区')
# 改選年セル（class 判定）: 「2028年<br />（令和…」の形だけを class 境界とみなす
_CLASS_RE = re.compile(r"(2028|2031)\s*年\s*<br\s*/?>\s*（令和")
# 議員セル内の人物リンク（File/ファイルを除く）
_PERSON_RE = re.compile(r"\[\[(?![Ff]ile:|ファイル:)([^\]]+)\]\]")
# 政党（全角カッコ）
_PARTY_RE = re.compile(r"（([^（）]*)）")


def _section(wt):
    """「選挙区選出議員」節のウィキテキストだけを切り出す。"""
    m = re.search(r"==+\s*選挙区選出議員\s*==+", wt)
    if not m:
        raise RuntimeError("『選挙区選出議員』節が見つかりません（出典の構造変化の可能性）")
    start = m.end()
    n = re.search(r"\n==+\s*比例代表選出議員\s*==+", wt[start:])
    end = start + n.start() if n else len(wt)
    return wt[start:end]


def _row_cells(row):
    """表の1行を、行頭の | または ! で区切ってセル列に分解する。"""
    parts = re.split(r"\n\s*[|!]", "\n" + row.strip())
    return [p for p in parts if p is not None and p.strip()]


def _person_name(cell):
    """議員セルから氏名を取り出す（File以外の最初の人物リンクの表示名）。"""
    m = _PERSON_RE.search(cell)
    if not m:
        return ""
    link = m.group(1)
    # [[記事名|表示名]] は表示名を採る（曖昧さ回避付きの記事名を避ける）
    disp = link.split("|")[-1]
    return C.clean_cell("[[" + disp + "]]")


def _pick_party(text):
    """（…）群から政党らしいものを最後方から選ぶ（令和/定数の注記は除外）。"""
    for g in reversed(_PARTY_RE.findall(text)):
        g = C.clean_cell(g).strip()
        if not g or "令和" in g or "定数" in g:
            continue
        return g.split("／")[0].strip()   # 「無所属／会派」は「／」前を採る
    return ""


def _party(cell):
    """議員セルから所属政党を取り出す。

    通常はテンプレート除去後の「（政党）」を採る。欠員セル（{{Efn|…議員（政党）が辞職}}）
    では政党が注釈テンプレート内にあるため、除去前の生セルからも拾えるようフォールバックする。
    """
    txt = C._REF_RE.sub("", cell)
    stripped = re.sub(r"\{\{[^{}]*\}\}", "", txt)     # efn 等のテンプレートを除去
    return _pick_party(stripped) or _pick_party(txt)


def collect(app_data=C.APP_DATA):
    wt = C.wiki_wikitext(PAGE)
    sec = _section(wt)
    pref_code = C.pref_lookup(app_data)
    src = C.wiki_url(PAGE)

    records = {}
    cur_code = None
    cur_cls = None

    for row in re.split(r"\n\|-", sec):
        # 1) 都道府県ヘッダ（＝新しい選挙区の開始）
        mh = _HEADER_RE.search(row)
        if mh:
            pref = mh.group(1).strip()
            code = _MERGE.get(pref) or pref_code.get(pref)
            if not code:
                raise RuntimeError(f"選挙区名からコードを引けません: {pref!r}")
            cur_code = code
            records.setdefault(code, {"magnitude": 0, "members": []})

        # 2) class（改選年）の切り替え。年セルが無い継続行では前の class を保持する
        mc = _CLASS_RE.search(row)
        if mc:
            cur_cls = mc.group(1)

        # 3) 議員セルの抽出。
        #    ※「選挙区」等の文字列でのスキップは不可（写真のFile名に選挙区が入る例がある
        #      例: [[File:江島潔（…山口県第一選挙区支部長）.jpg]]）。人物リンクで判定する。
        for cell in _row_cells(row):
            if "欠員" in cell:
                # 欠員（辞職等で空席・補選なし）: 議席は残すが在任者は None。注釈から経緯を控える。
                # （注釈内の [[辞職者]] を議員として拾わないよう、人物抽出より前で処理して continue）
                if cur_code is None or cur_cls is None:
                    continue
                mefn = re.search(r"\{\{[Ee]fn\|([^{}]*)\}\}", cell)
                note = C.clean_cell(mefn.group(1)) if mefn else "欠員"
                records[cur_code]["members"].append({
                    "name": None, "vacant": True, "cls": cur_cls,
                    "termEnd": _TERM_END[cur_cls], "sourceUrl": src, "note": note})
                continue
            mp = _PERSON_RE.search(cell)
            if not mp:
                continue  # 年セル・空セル（人物リンク無し）
            if mp.group(1).split("|")[0].strip().endswith("選挙区"):
                continue  # 都道府県ヘッダセル（[[○○選挙区|○○]]）
            name = _person_name(cell)
            if not name:
                continue
            if cur_code is None or cur_cls is None:
                raise RuntimeError(f"選挙区/改選年が未確定のまま議員セルを検出: {name!r}")
            party = _party(cell)
            records[cur_code]["members"].append({
                "name": name,
                "kana": "",                     # この出典に振り仮名は無い（既存データ側で補完）
                "party": party,
                "cls": cur_cls,
                "termEnd": _TERM_END[cur_cls],
                "sourceUrl": src,
            })

    for rec in records.values():
        rec["magnitude"] = len(rec["members"])

    _assert_invariants(records)
    return records


def _assert_invariants(records):
    """必達不変条件を検証する（満たさなければ RuntimeError）。"""
    want = set(C.district_names(LEVEL).keys())
    got = set(records.keys())
    if got != want:
        raise RuntimeError(f"選挙区コードが一致しません。欠落={want - got} 余分={got - want}")
    if len(records) != 45:
        raise RuntimeError(f"選挙区数が45ではありません: {len(records)}")

    total = sum(len(r["members"]) for r in records.values())
    if total != 148:
        raise RuntimeError(f"議員総数が148ではありません: {total}（行の取りこぼしの可能性）")

    c28 = sum(1 for r in records.values() for m in r["members"] if m["cls"] == "2028")
    c31 = sum(1 for r in records.values() for m in r["members"] if m["cls"] == "2031")
    if c28 != 74 or c31 != 74:
        raise RuntimeError(f"改選階級の内訳が74-74ではありません: 2028={c28} 2031={c31}")

    if len(records["13"]["members"]) != 12:
        raise RuntimeError(f"東京都(13)の議員数が12ではありません: {len(records['13']['members'])}")

    for code, r in records.items():
        if r["magnitude"] != len(r["members"]):
            raise RuntimeError(f"{code}: magnitude と members 数が不一致")
        for m in r["members"]:
            # 欠員は氏名・政党が無くてよい（議席としては数える）
            req = ("cls", "termEnd", "sourceUrl") if m.get("vacant") else ("name", "party", "cls", "termEnd", "sourceUrl")
            for f in req:
                if not m.get(f):
                    raise RuntimeError(f"{code}: 必須フィールド {f} が空: {m}")


def _crosscheck(records):
    """委員会済み reps_hc.json と氏名集合を突き合わせ、一致率を出す。"""
    import json
    path = os.path.join(C.APP_DATA, "reps_hc.json")
    if not os.path.exists(path):
        return
    committed = json.load(open(path, encoding="utf-8"))["records"]
    tot = match = 0
    diffs = []
    for code in sorted(records):
        got = {C.norm_name(m["name"]) for m in records[code]["members"]}
        exp = {C.norm_name(m["name"]) for m in committed.get(code, {}).get("members", [])}
        inter = got & exp
        tot += len(exp)
        match += len(inter)
        if got != exp:
            diffs.append((code, sorted(exp - got), sorted(got - exp)))
    rate = (match / tot * 100) if tot else 0.0
    print(f"\n[cross-check vs committed reps_hc.json] 氏名一致率: {match}/{tot} = {rate:.1f}%")
    if diffs:
        print("  不一致の選挙区（committedのみ → scrapedのみ）:")
        for code, only_c, only_s in diffs:
            print(f"    {code}: -{only_c}  +{only_s}")
    else:
        print("  全選挙区で氏名集合が完全一致。")


def main():
    recs = collect()
    C.write_doc(recs, LEVEL, OFFICE, os.path.join(C.APP_DATA, "reps_hc.scraped.json"))
    total = sum(len(r["members"]) for r in recs.values())
    c28 = sum(1 for r in recs.values() for m in r["members"] if m["cls"] == "2028")
    c31 = sum(1 for r in recs.values() for m in r["members"] if m["cls"] == "2031")
    print(f"[hc] 選挙区数: {len(recs)} / 議員総数: {total} / 内訳 2028={c28} 2031={c31}"
          f" / 東京(13)={len(recs['13']['members'])}")
    for code in ("01", "11", "13", "31_32", "36_39", "47"):
        names = " ".join(f"{m['name'] or '（欠員）'}({m['cls']})" for m in recs[code]["members"])
        print(f"  {code} [{C.district_names(LEVEL)[code]}] x{recs[code]['magnitude']}: {names}")
    _crosscheck(recs)


if __name__ == "__main__":
    main()
