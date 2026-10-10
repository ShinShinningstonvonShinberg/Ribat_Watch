#!/usr/bin/env python3
"""「日本のモスク情報」（yumaki5555.github.io/mosque-map）から補足データを取り込む。

同サイトは本リポジトリと同じ出典マイマップ（全国モスクリスト）の273地点に、公開資料から
調べた区域区分・運営団体の種類・系統などを出典付きで加えている。データは各ページの HTML に
JSON として埋め込まれているので、それを取り出し、マイマップ由来の273地点について
**施設単位の事実（分類値・URL）だけ**を許可リスト方式で残して書き出す。

出力:
  data/yumaki/places.json   地点ごとの補足属性（enrich_mosques.py が mosques.geojson に結合する）

取り込むもの: 区域区分・法人の種類・施設の形態・系統・公式ホームページ（SNS以外）・遺体の洗浄施設の有無。
取り込まないもの（方針）:
  - 寄付・資金に関する情報（公開版に含めない）
  - 説明文・根拠メモ・詳細などの自由記述（同サイトのリポジトリにライセンス表示が無いため
    文章は転載せず、事実だけを使い、地点ごとに同サイトの該当ページへリンクする）
  - 会衆の国籍・宗派（README の個人情報方針で、会衆の国籍・民族の記述は公開しない）
  - 写真・住所・SNSアカウント・個人名
  - マイマップに無い地点（寄付の調査などから同サイトが加えた地点）

使い方:
  python3 scripts/fetch_yumaki.py                 # サイトから取得して書き出す
  python3 scripts/fetch_yumaki.py --from-dir DIR  # 取得済みの HTML（index/mosque/area.html）を使う
"""
from __future__ import annotations
import os
import re
import sys
import json
import argparse
import datetime
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scrub  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://yumaki5555.github.io/mosque-map/"
PAGES = ("index.html", "mosque.html", "area.html")
OUT_PLACES = os.path.join(ROOT, "data", "yumaki", "places.json")
UA = ("Ribat-Watch-refresh/1.1 (https://github.com/ShinShinningstonvonShinberg/Ribat_Watch; "
      "public mosque/district map)")

# 区域区分（同サイトの zk コード）→ 表示名
ZONE_LABEL = {
    "urban": "市街化区域",
    "control": "市街化調整区域",
    "nosen": "非線引き都市計画区域",
    "outside": "都市計画区域外／データ未整備",
}


def fetch(page: str, timeout: int = 60) -> str:
    """ページを取得して文字列で返す（urllib → 失敗時 curl。fetch_kml と同じ方針）。"""
    url = BASE + page
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8")
    except urllib.error.URLError:
        import shutil
        import subprocess
        if not shutil.which("curl"):
            raise
        return subprocess.run(["curl", "-sSL", "--fail", "-A", UA, "--max-time", str(timeout),
                               "--retry", "3", url], check=True, stdout=subprocess.PIPE).stdout.decode("utf-8")


def extract(html: str, name: str):
    """HTML 内の `const NAME = <JSON>` / `, NAME = <JSON>` を取り出す（無ければ None）。"""
    m = re.search(r"(?:\b(?:const|let|var)\s+|,\s*)" + name + r"\s*=\s*", html)
    if not m:
        return None
    obj, _ = json.JSONDecoder().raw_decode(html, m.end())
    return obj


def site_updated(index_html: str) -> str:
    """トップページの「最終更新 YYYY年M月D日 HH:MM」を ISO 形式（日本時間）で返す。"""
    m = re.search(r"最終更新\s*(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})", re.sub(r"<[^>]+>", " ", index_html))
    if not m:
        return ""
    y, mo, d, h, mi = map(int, m.groups())
    return f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}+09:00"


def homepage(links) -> str:
    """リンク一覧から公式ホームページ（SNS以外の http/https）を1つ選ぶ。"""
    for label, url in links or []:
        if label == "ホームページ" and url.startswith(("http://", "https://")) \
                and not scrub._is_social(scrub._host(url)):
            return scrub._emit_url(url)
    return ""


def reduce_places(mosques, places, nets):
    """マイマップ由来の273地点を、許可リストの属性だけに絞る。"""
    by_id = {p["id"]: p for p in places}
    out = []
    for m in mosques:
        p = by_id.get(m["id"], {})
        info = dict(m["info"])
        out.append({
            "yid": m["id"],
            "name": m["n"],
            "pref": m["p"],
            "city": m["ci"],
            "lat": m["la"],
            "lon": m["lo"],
            "gmaps": m.get("g") or "",               # マイマップ側との照合用（Google マップ cid）
            "removedFromGmaps": bool(m.get("rm")),
            "zone": p.get("zk") or m.get("zk") or None,
            "orgType": info.get("法人の種類") or None,
            "facility": info.get("施設の形態") or None,
            "network": [{"code": n, "name": nets.get(n, {}).get("name", n)} for n in m.get("net") or []],
            "homepage": homepage(m.get("links")),
            "washNow": bool(p.get("washNow")),       # 遺体の洗浄施設（既存）
            "washPlan": bool(p.get("washPlan")),     # 遺体の洗浄施設（計画）
            "lastChecked": p.get("lastChecked") or "",
        })
    return out


def main():
    ap = argparse.ArgumentParser(description="日本のモスク情報（yumaki5555）から補足データを取り込む")
    ap.add_argument("--from-dir", help="取得済み HTML のディレクトリ（index/mosque/area.html）")
    args = ap.parse_args()

    if args.from_dir:
        html = {p: open(os.path.join(args.from_dir, p), encoding="utf-8").read() for p in PAGES}
    else:
        html = {p: fetch(p) for p in PAGES}
    mosques = extract(html["mosque.html"], "DATA")
    nets = extract(html["mosque.html"], "NETS") or {}
    places = extract(html["area.html"], "PLACES") or []
    if not mosques:
        sys.exit("ERROR: 埋め込みデータが見つかりません（サイトの構造が変わった可能性）。既存の出力を保持します。")

    out = reduce_places(mosques, places, nets)
    os.makedirs(os.path.dirname(OUT_PLACES), exist_ok=True)
    with open(OUT_PLACES, "w", encoding="utf-8") as f:
        json.dump({
            "source": "日本のモスク情報",
            "sourceUrl": BASE,
            "siteUpdated": site_updated(html["index.html"]),
            "fetched": datetime.date.today().isoformat(),
            "note": "マイマップ由来の地点の事実（分類値・URL）のみを抜粋。",
            "zoneLabels": ZONE_LABEL,
            "places": out,
        }, f, ensure_ascii=False, indent=1)
    print(f"places: {len(out)} / site updated {site_updated(html['index.html'])}")


if __name__ == "__main__":
    main()
