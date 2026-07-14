#!/usr/bin/env python3
"""衆議院議員（小選挙区）スクレイパ。

出典: 日本語版 Wikipedia「第51回衆議院議員総選挙」（2026-02-08 投開票）の
「小選挙区当選者」節。この節は {{衆院小選挙区当選者}} テンプレートで、47都道府県
それぞれのパラメータ（|北海道= 等）に、その県の小選挙区当選者を「1区→2区→…」の
順で1人ずつ列挙している。各行は `色コード:[[人物]]` の形式で、行頭の3桁HEX色コードが
当選者の所属政党を表す（政党箱の色）。落選候補は載らないので、この節を読むだけで
「各選挙区で今 議席を持っているのは誰か」を過不足なく取れる（289小選挙区＝289行）。

役割は「今その職に就いているのは誰か」の検出。氏名と所属（色→政党で判定）だけを core
として返し、コードは選挙区名から引く。マグニチュード（289）・重複なし・全件マッピング
成功を検証してから返す。詳細な補完（推薦・初当選日など）は既存データ側に委ねる（_common参照）。
"""
from __future__ import annotations
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

LEVEL = "hr"
OFFICE = "衆議院議員（小選挙区）"
PAGE = "第51回衆議院議員総選挙"

# 47都道府県の並び（テンプレートのパラメータ順。これ以降の「○○増減」パラメータは対象外）
_PREFS = [
    "北海道", "青森", "岩手", "宮城", "秋田", "山形", "福島", "茨城", "栃木", "群馬",
    "埼玉", "千葉", "神奈川", "山梨", "東京", "新潟", "富山", "石川", "福井", "長野",
    "岐阜", "静岡", "愛知", "三重", "滋賀", "京都", "大阪", "兵庫", "奈良", "和歌山",
    "鳥取", "島根", "岡山", "広島", "山口", "徳島", "香川", "愛媛", "高知", "福岡",
    "佐賀", "長崎", "熊本", "大分", "宮崎", "鹿児島", "沖縄",
]

# 政党箱の色コード（3桁HEX）→ 正式党名。committed データ（app/data/reps_hr.json）と
# 全289件で照合し、各色が単一政党に一意対応することを確認済み（自民248/維新20/国民8/中道7/無所属5/減税1）。
_COLOR_PARTY = {
    "9e9": "自由民主党",
    "0c9": "日本維新の会",
    "9cf": "中道改革連合",
    "ffc": "国民民主党",
    "fff": "無所属",
    "9ad": "減税日本・ゆうこく連合",
}

# `色コード:[[記事]]` / `色コード:[[記事|表示名]]` を1件として拾う
_WINNER_RE = re.compile(r"([0-9A-Fa-f]{3}):\[\[([^\]]+)\]\]")
# テンプレート本体（{{衆院小選挙区当選者 … \n}}）を丸ごと取り出す
_TEMPLATE_RE = re.compile(r"\{\{衆院小選挙区当選者(.*?)\n\}\}", re.S)


def _norm_district(name):
    """選挙区名を照合用に正規化する。

    第・都・府・県 を除去し（道 は残す＝北海道は北海道のまま）、空白を落とす。
    委員会データ側の選挙区名（例「東京1区」「群馬1区」）と Wikipedia 側の表記
    （例「東京都第1区」「群馬県第1区」）を同じ形に寄せて突き合わせるための規則。
    ※両側に同じ規則を掛けるので、京都府→「京」のような潰れ方でも一意対応は保たれる。
    """
    for ch in "第都府県":
        name = name.replace(ch, "")
    return name.replace(" ", "").replace("　", "").strip()


def _display_name(link):
    """[[記事|表示名]] → 表示名、[[記事]] → 記事名。残った装飾は clean_cell で除く。"""
    return C.clean_cell(link.split("|")[-1])


def collect(app_data=C.APP_DATA):
    wt = C.wiki_wikitext(PAGE)
    src = C.wiki_url(PAGE)

    # 「小選挙区当選者」節に絞ってからテンプレート本体を取り出す
    if "===小選挙区当選者===" not in wt:
        raise RuntimeError("出典に『小選挙区当選者』節が見つかりません（記事構造の変化の可能性）")
    sec = wt.split("===小選挙区当選者===", 1)[1]
    mt = _TEMPLATE_RE.search(sec)
    if not mt:
        raise RuntimeError("{{衆院小選挙区当選者}} テンプレートを抽出できません（記事構造の変化の可能性）")
    body = mt.group(1)

    # 節冒頭〜テンプレート本体の手前までを走査して {{政党箱|党名}} 群から「登場する政党の
    # 集合」を得て、色対応表と整合を確認する（行番号に依存せずレイアウト変化に強くする）。
    header_parties = set(re.findall(r"\{\{政党箱\|([^}|]+)\}\}", sec[:mt.start()]))

    # |都道府県= 単位でパラメータに分割する
    parts = re.split(r"\n\|([^=\n]+)=", "\n" + body)
    it = iter(parts[1:])
    params = {}
    for name in it:
        params[name.strip()] = next(it)

    # 選挙区名 → kucode の対応表
    dn = C.district_names(LEVEL, app_data)
    name_to_code = {_norm_district(n): code for code, n in dn.items()}

    records = {}
    unmapped = []
    unknown_color = []
    for pref in _PREFS:
        if pref not in params:
            raise RuntimeError(f"出典に都道府県パラメータ |{pref}= がありません（記事構造の変化の可能性）")
        # その県の当選者を 1区→2区→… の順で拾う
        winners = _WINNER_RE.findall(params[pref])
        for idx, (color, link) in enumerate(winners, start=1):
            col = color.lower()
            party = _COLOR_PARTY.get(col)
            if party is None:
                unknown_color.append((pref, idx, color, link))
                continue
            wiki_name = f"{pref}第{idx}区"
            code = name_to_code.get(_norm_district(wiki_name))
            if code is None:
                unmapped.append(wiki_name)
                continue
            if code in records:
                # 同一選挙区に2人＝過剰カウント。黙って上書きせず失敗させる
                raise RuntimeError(f"選挙区コード {code}（{wiki_name}）が重複して割り当てられました（パース異常）")
            records[code] = {
                "name": _display_name(link),
                "party": party,
                "sourceUrl": src,
            }

    if unknown_color:
        raise RuntimeError(f"未知の政党色コードを検出（色→政党対応の更新が必要）: {unknown_color[:5]}")
    if unmapped:
        raise RuntimeError(f"kucode に対応づけできない選挙区名: {unmapped}")

    # 色対応表の党名集合が、節ヘッダの政党箱と食い違っていないか（構造変化の早期検知）
    if header_parties and not header_parties.issubset(set(_COLOR_PARTY.values())):
        extra = header_parties - set(_COLOR_PARTY.values())
        raise RuntimeError(f"ヘッダの政党箱に色対応表未登録の政党があります（要更新）: {extra}")

    # マグニチュード検証: 289小選挙区に過不足なく1対1で対応していること
    if set(records.keys()) != set(dn.keys()):
        missing = sorted(set(dn.keys()) - set(records.keys()))
        extra = sorted(set(records.keys()) - set(dn.keys()))
        raise RuntimeError(
            f"小選挙区の検出がコード集合と一致しません: 取得{len(records)}件 / "
            f"欠落={[dn.get(m, m) for m in missing]} / 余分={extra}"
        )
    if len(records) != 289:
        raise RuntimeError(f"小選挙区当選者の検出数が289ではありません: {len(records)}（出典の構造変化の可能性）")

    missing_field = [c for c, r in records.items() if not r.get("name") or not r.get("party")]
    if missing_field:
        raise RuntimeError(f"氏名または政党を取得できないコード: {missing_field}")

    return records


def main():
    recs = collect()
    C.write_doc(recs, LEVEL, OFFICE, os.path.join(C.APP_DATA, "reps_hr.scraped.json"))
    print(f"[hr] collected {len(recs)}")
    # 代表的な選挙区のサンプル出力（北海道1区・東京1区・大阪1区・沖縄4区）
    for code in ("101", "1301", "2701", "4704"):
        if code in recs:
            print(f"  {code}: {recs[code]}")


if __name__ == "__main__":
    main()
