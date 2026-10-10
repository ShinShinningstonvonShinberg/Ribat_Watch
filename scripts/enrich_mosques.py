#!/usr/bin/env python3
"""マイマップ由来の地点に「日本のモスク情報」の補足属性を結合し、同サイトだけにある地点を足す。

refresh_mosques.py が KML の解釈直後に呼ぶ（毎回の再生成でも結合が保たれる）。
入力は scripts/fetch_yumaki.py が作る data/yumaki/places.json と、人間が管理する
data/yumaki/match_overrides.json。places.json が無ければ何もしない（従来どおりの出力）。

地点の properties に足す属性:
  yid        同サイトの地点ID（アプリから同サイトの詳細ページへリンクする）
  zone       区域区分 urban / control / nosen / outside
  org_type   運営団体の法人の種類
  facility   施設の形態（分かる場合）
  network    系統（同じ団体が運営するモスクのグループ）の名前の配列
  homepage   公式ホームページ（SNS以外）
  wash       遺体の洗浄施設 now（既存）/ plan（計画）

照合: Google マップの cid が一致するもの → 半径 80m 以内で近い順に1対1。半径外でも同じ計画を
指すもの（ピンの精度が違う地点など）は match_overrides.json の same で対応づける。
places.json はマイマップ由来の地点だけを持つので、照合できずに残った地点はモスクとして
origin="yumaki" を付けて足す（寄付の調査など、マイマップに無い出所の地点は足さない）。
"""
from __future__ import annotations
import os
import re
import json
import math

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLACES = os.path.join(ROOT, "data", "yumaki", "places.json")
OVERRIDES = os.path.join(ROOT, "data", "yumaki", "match_overrides.json")
RADIUS_M = 80
ENRICH_KEYS = ("yid", "zone", "org_type", "facility", "network", "homepage", "wash")
CSV_KEYS = ("layer", "code", "seq", "name", "removed_from_gmaps", "lat", "lon", "source_url", "description", "origin")
_CID = re.compile(r"cid=(\d+)")


def _dist_m(lat1, lon1, lat2, lon2):
    """2点間の距離（m、球面近似）。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def load(places_path=PLACES, overrides_path=OVERRIDES):
    """(places, overrides) を返す。places.json が無ければ (None, {})。"""
    if not os.path.exists(places_path):
        return None, {}
    places = json.load(open(places_path, encoding="utf-8"))["places"]
    overrides = json.load(open(overrides_path, encoding="utf-8")) if os.path.exists(overrides_path) else {}
    return places, overrides


def _attrs(p):
    """同サイトの地点から、地点に足す属性（空の値は省く）を作る。"""
    a = {
        "yid": p["yid"], "zone": p.get("zone"), "org_type": p.get("orgType"), "facility": p.get("facility"),
        "network": [n["name"] for n in p.get("network") or []], "homepage": p.get("homepage"),
        "wash": "now" if p.get("washNow") else "plan" if p.get("washPlan") else None,
    }
    return {k: v for k, v in a.items() if v not in (None, "", [])}


def enrich(features, places=None, overrides=None):
    """features に属性を結合し、補足地点を足した配列と報告 dict を返す（入力は変更しない前提で複製して使う）。"""
    if places is None:
        places, overrides = load()
    if not places:
        return features, {"enabled": False}
    overrides = overrides or {}
    features = [f for f in features if f["properties"].get("origin") != "yumaki"]
    for f in features:                  # 何度実行しても同じ結果になるよう、前回の結合を消してから付け直す
        for k in ENRICH_KEYS:
            f["properties"].pop(k, None)

    pairs, used = {}, set()             # 地点の添字 → 同サイトの地点の添字
    by_cid = {}
    for j, p in enumerate(places):
        m = _CID.search(p.get("gmaps") or "")
        if m:
            by_cid.setdefault(m.group(1), []).append(j)
    for i, f in enumerate(features):    # 1) Google マップ cid の一致
        m = _CID.search(f["properties"].get("source_url") or "")
        for j in by_cid.get(m.group(1), []) if m else []:
            if j not in used:
                pairs[i] = j
                used.add(j)
                break
    yidx = {p["yid"]: j for j, p in enumerate(places)}
    for yid, name in (overrides.get("same") or {}).items():   # 2) 人間が対応づけた同一計画
        j = yidx.get(yid)
        cands = [i for i, f in enumerate(features) if f["properties"].get("name") == name and i not in pairs]
        if j is not None and j not in used and cands:
            pairs[cands[0]] = j
            used.add(j)
    near = []                           # 3) 半径内で近い順に1対1
    for i, f in enumerate(features):
        if i in pairs:
            continue
        lon, lat = f["geometry"]["coordinates"]
        for j, p in enumerate(places):
            if j not in used:
                d = _dist_m(lat, lon, p["lat"], p["lon"])
                if d <= RADIUS_M:
                    near.append((d, i, j))
    for d, i, j in sorted(near):
        if i not in pairs and j not in used:
            pairs[i] = j
            used.add(j)
    for i, j in pairs.items():
        features[i]["properties"].update(_attrs(places[j]))

    skip = set(overrides.get("skip") or {})
    added, not_added = [], []
    for j, p in enumerate(places):      # 4) 照合できなかったマイマップ由来の地点をモスクとして足す
        if j in used:
            continue
        if p["yid"] in skip:
            not_added.append(p)
            continue
        src = p.get("gmaps") or ""
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
            "properties": {"layer": "mosque", "code": "", "seq": "", "name": p["name"],
                           "removed_from_gmaps": bool(p.get("removedFromGmaps")),
                           "source_url": src, "description": src, "origin": "yumaki", **_attrs(p)},
        })
        added.append(("mosque", p))
    return features, {
        "enabled": True,
        "matched": len(pairs),
        "unmatched_points": len(features) - len(added) - len(pairs),
        "added": [f"{layer}: {p['name']}（{p['pref']}{p['city']}）" for layer, p in added],
        "not_added": [f"{p['name']}（match_overrides の skip）" for p in not_added],
    }


def csv_rows(features):
    """GeoJSON の地点から CSV 行（parse_kml.CSV_COLS の列）を作る。補足地点も含める。"""
    rows = []
    for f in features:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"]
        rows.append({k: (lat if k == "lat" else lon if k == "lon" else p.get(k, "")) for k in CSV_KEYS})
    return rows
