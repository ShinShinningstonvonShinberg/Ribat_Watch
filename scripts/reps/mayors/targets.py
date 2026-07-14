#!/usr/bin/env python3
"""市区町村長（首長）収集のターゲット表を作る（決定的な分類）。

首長データ（1893件）は3種類に分かれる。この分類は構造的な事実なので、エージェントに
問い合わせるのは「収集対象」だけにし、残りは決定的に埋める:

  collectable … 市町村長（1689）＋ 東京23特別区の区長（23）＝ 各自コードを持ち、
                現職を web で調べる対象。
  polei_cities … 政令指定都市（20市）の市長。政令市には単独コードが無く、配下の行政区
                （175コード）に同じ市長を inherited=true で書き込む（区長は任命職のため）。
                よって市長は「市名」で1回だけ調べ、配下コードへ複製する。
  nohead … 北方領土6村（01695–01700）。実効的な首長が存在しない → noHead で固定。

分類は既存の app/data/reps_muni.json（前回スナップショット）が持つ inherited / parentCity /
noHead / office を正とする。新設・合併で現れた未知コードは既定で collectable（市町村長）に回す。

使い方:
  python3 scripts/reps/mayors/targets.py            # 分類サマリを表示
  python3 scripts/reps/mayors/targets.py --pref 47  # 特定県のみ
  from targets import build_targets                 # {collectable, polei_cities, nohead}
"""
from __future__ import annotations
import os
import sys
import json
import argparse
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
APP_DATA = os.path.join(ROOT, "app", "data")


def _load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def build_targets(app_data=APP_DATA, pref=None):
    """{collectable, polei_cities, nohead} を返す。

    collectable  : {pref2: [(code, name), …]}  現職を調べる対象
    polei_cities : {parentCity: [childCode, …]} 政令市→配下行政区（市長を複製）
    nohead       : [code, …]
    pref を渡すとその2桁都道府県コードだけに絞る。
    """
    prior = _load(os.path.join(app_data, "reps_muni.json"))["records"]
    dist = {str(f["properties"]["code"]): f["properties"]["name"]
            for f in _load(os.path.join(app_data, "districts_muni.geojson"))["features"]
            if str(f["properties"]["code"])}      # 空コード（所属未定地）は除外

    collectable = defaultdict(list)
    polei_cities = defaultdict(list)
    nohead = []

    for code, name in sorted(dist.items()):
        if pref and code[:2] != pref:
            continue
        rec = prior.get(code)
        if rec and rec.get("noHead"):
            nohead.append(code)
        elif rec and rec.get("inherited"):
            polei_cities[rec.get("parentCity") or _city_of(name)].append(code)
        else:
            # 通常の市町村長・特別区長、または未知の新設コード
            collectable[code[:2]].append((code, name))

    return {"collectable": dict(collectable),
            "polei_cities": dict(polei_cities),
            "nohead": nohead}


def _city_of(muni_name):
    """行政区名から市名を推定する（例: '札幌市 中央区' → '札幌市'）。既存分類の補助のみ。"""
    return muni_name.split()[0] if muni_name else ""


def summarize(t):
    n_coll = sum(len(v) for v in t["collectable"].values())
    n_child = sum(len(v) for v in t["polei_cities"].values())
    print(f"collectable 市町村長・特別区長 : {n_coll} 件（{len(t['collectable'])} 都道府県）")
    print(f"政令市（市長を複製）          : {len(t['polei_cities'])} 市 → 配下 {n_child} 行政区")
    print(f"noHead（北方領土等）          : {len(t['nohead'])} 件 {t['nohead']}")
    print(f"合計対象コード                : {n_coll + n_child + len(t['nohead'])}")
    print("\n政令市の配下行政区数:")
    for city, kids in sorted(t["polei_cities"].items(), key=lambda kv: -len(kv[1])):
        print(f"  {city}: {len(kids)}")


def main():
    ap = argparse.ArgumentParser(description="首長収集のターゲット分類")
    ap.add_argument("--pref", help="2桁都道府県コードで絞り込み（例: 47）")
    args = ap.parse_args()
    t = build_targets(pref=args.pref)
    summarize(t)


if __name__ == "__main__":
    main()
