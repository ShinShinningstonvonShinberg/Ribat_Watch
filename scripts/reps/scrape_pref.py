#!/usr/bin/env python3
"""知事（都道府県知事）スクレイパ。

出典: 日本語版 Wikipedia「都道府県知事の一覧」（現職47人の一覧表）。
「今その職に就いているのは誰か」を検出するのが役割。氏名・かな・所属・当選回数・
任期満了日・就任日を取れる範囲で拾い、コードは都道府県名から引く。マグニチュード（47）を
検証してから返す。詳細な補完（推薦政党・初当選日など）は既存データ側に委ねる（_common参照）。
"""
from __future__ import annotations
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

LEVEL = "pref"
OFFICE = "知事"
PAGE = "都道府県知事の一覧"

# 所属判定に使う語（この中の語を含むセルを所属とみなす）
_AFFIL_HINTS = ("無所属", "自由民主党", "立憲民主党", "公明党", "日本維新の会", "国民民主党",
                "日本共産党", "れいわ", "社会民主党", "参政党", "自民", "維新")
# 「YYYY年M月D日」を YYYY-MM-DD に
_DATE_RE = re.compile(r"(\d{4})年\s*(?:\{\{[^}]*\}\})?\s*(\d{1,2})月\s*(\d{1,2})日")


def _to_iso(text):
    m = _DATE_RE.search(text)
    if not m:
        return ""
    y, mo, d = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def _row_cells(row):
    """テーブル行を | 区切りのセル列に分解する（先頭の ! ヘッダは別扱い）。"""
    # 行内の "\n|" または "\n!" でセルを区切る
    parts = re.split(r"\n\s*[|!]", "\n" + row.strip())
    return [p for p in (parts[1:] if parts and not parts[0].strip() else parts) if p is not None]


def collect(app_data=C.APP_DATA):
    wt = C.wiki_wikitext(PAGE)
    pref_code = C.pref_lookup(app_data)
    src = C.wiki_url(PAGE)
    records = {}
    # 行を "|-" で分割し、都道府県の行だけ処理する
    for row in re.split(r"\n\|-", wt):
        # ヘッダセルに [[都道府県名]] があるか（{{Display none|NN/}} の直後）
        mh = re.search(r"\{\{Display ?none\|(\d+)/?\}\}\s*\[\[([^\]|]+)", row)
        if not mh:
            continue
        pref_name = mh.group(2).strip()
        code = pref_code.get(pref_name) or pref_code.get(re.sub(r"(都|道|府|県)$", "", pref_name))
        if not code:
            continue
        cells = _row_cells(row)
        name, kana = "", ""
        affiliation, win, term_end, term_start = "", "", "", ""
        for c in cells:
            if not name and "{{Ruby" in c.replace(" ", "").replace("｛", "{") or (not name and "Ruby" in c):
                name, kana = C.ruby_kana(c)
            cl = C.clean_cell(c)
            if not affiliation and any(h in cl for h in _AFFIL_HINTS) and "選挙" not in cl:
                affiliation = cl
            # 当選回数（1〜2桁）。セルに style 等の属性が付く場合に備え、末尾の数字も拾う
            if not win:
                mw = re.search(r"(?:^|\|)\s*(\d{1,2})\s*$", cl)
                if mw:
                    win = mw.group(1)
            if "選挙" in c and not term_start:          # 就任（直近の知事選）
                term_start = _to_iso(c)
            elif _DATE_RE.search(c) and "選挙" not in c and not term_end:
                term_end = _to_iso(c)
        if not name:
            # フォールバック: 行全体から最初の人物リンク
            m = re.search(r"\|\s*(?:style=[^|]*\|)?\{\{Ruby\|\[?\[?([^|\]}]+)", row)
            if m:
                name = m.group(1).strip()
        rec = {"name": C.strip_disambig(name), "sourceUrl": src}
        if kana:
            rec["kana"] = kana
        if affiliation:
            rec["affiliation"] = affiliation
        if win:
            rec["winCount"] = win
        if term_start:
            rec["currentTermStart"] = term_start
        if term_end:
            rec["termEnd"] = term_end
        records[code] = rec
    if len(records) != 47:
        raise RuntimeError(f"知事の検出数が47ではありません: {len(records)}（出典の構造変化の可能性）")
    missing = [c for c, r in records.items() if not r.get("name")]
    if missing:
        raise RuntimeError(f"氏名を取得できないコード: {missing}")
    return records


def main():
    recs = collect()
    C.write_doc(recs, LEVEL, OFFICE, os.path.join(C.APP_DATA, "reps_pref.scraped.json"))
    print(f"[pref] collected {len(recs)} governors")
    for code in ("01", "13", "27", "47"):
        print(f"  {code}: {recs[code]}")


if __name__ == "__main__":
    main()
