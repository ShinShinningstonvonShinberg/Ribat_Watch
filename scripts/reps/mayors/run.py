#!/usr/bin/env python3
"""市区町村長（首長）の再収集ランナー（エージェント支援・月次・任意）。

首長は数が多く（1893件）出典も散在するため、決定的スクレイパでは網羅・追随が難しい。
ここでは Claude（Messages API + web_search）に「現職の首長は誰か」を都道府県ごとに調べさせ、
決定的な部分（政令市の市長を配下行政区へ複製、北方領土の noHead）は targets.py の分類で埋める。
出力は reps_muni の records 辞書。orchestrator（refresh.py）がこれを検証し、差分PRにする。

安全性・再現性:
- プロンプトは committed（collect_prompt.md）。都道府県ごとに対象自治体を差し込む。
- 実収集には ANTHROPIC_API_KEY が要る。無ければ --dry-run（プロンプト生成のみ）で配線を確認できる。
  CI での月次実行は「鍵があるときだけ動く」opt-in。名簿は必ず人間がPRで承認する（自動公開しない）。
- 収集結果は推測で埋めない（未確認は name=null）。決定的部分は分類から埋める。

使い方:
  python3 scripts/reps/mayors/run.py --dry-run --pref 47      # 沖縄のプロンプトだけ生成（鍵不要）
  python3 scripts/reps/mayors/run.py --pref 47 --out /tmp/okinawa_muni.json  # 実収集（要APIキー）
  python3 scripts/reps/mayors/run.py --out app/data/reps_muni.candidate.json  # 全国（高コスト）
"""
from __future__ import annotations
import os
import re
import sys
import json
import time
import argparse
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))          # scripts/reps
import targets as T                                # noqa: E402
import _common as C                                # noqa: E402

PROMPT_PATH = os.path.join(HERE, "collect_prompt.md")
API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-opus-4-8"
API_VERSION = "2023-06-01"
TERM_END_NOHEAD = None


def load_template():
    with open(PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


def build_prompt(pref_name, items):
    """items = [(key, name), …] → プロンプト文字列。key は JISコードまたは市名。"""
    lines = "\n".join(f"  {k}: {n}" for k, n in items)
    return (load_template()
            .replace("{{PREF_NAME}}", pref_name)
            .replace("{{MUNI_LIST}}", lines))


# ---------------------------------------------------------------------------
# Anthropic Messages API（web_search で現職を調べ、JSONだけを返させる）
# ---------------------------------------------------------------------------
def _extract_json(text):
    """本文から最初の JSON オブジェクトを取り出す（前後に説明が付いても拾う）。"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("応答にJSONが見つかりません")
    return json.loads(m.group(0))


def call_api(prompt, timeout=300, max_tokens=8000):
    """Messages API を叩いて {code: record} を返す。ANTHROPIC_API_KEY が必要。"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY が未設定です（--dry-run なら鍵は不要）")
    body = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "thinking": {"type": "adaptive"},
        # 現職確認のため web 検索を許可（動的フィルタ版）
        "tools": [{"type": "web_search_20260209", "name": "web_search"}],
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read())
    # 最後のテキストブロックを結合して JSON を取り出す
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    return _extract_json(text).get("records", {})


# ---------------------------------------------------------------------------
# 収集 → 決定的な組み立て
# ---------------------------------------------------------------------------
def collect(pref=None, dry_run=False, prompts_dir=None, sleep=2.0):
    """収集を実行。(collected_by_code, collected_cities) を返す。dry_run はプロンプトのみ。"""
    t = T.build_targets(pref=pref)
    dist = {str(f["properties"]["code"]): f["properties"]["name"]
            for f in json.load(open(os.path.join(C.APP_DATA, "districts_muni.geojson"), encoding="utf-8"))["features"]
            if str(f["properties"]["code"])}
    pref_names = C.district_names("pref")           # {code2: 都道府県名}

    by_code, cities = {}, {}
    # (1) 市町村長・特別区長: 都道府県ごとに収集
    for pref2, items in sorted(t["collectable"].items()):
        pname = pref_names.get(pref2, pref2)
        prompt = build_prompt(pname, [(code, dist.get(code, name)) for code, name in items])
        if dry_run:
            _save_prompt(prompts_dir, f"muni_{pref2}", prompt)
            continue
        by_code.update(call_api(prompt))
        time.sleep(sleep)
    # (2) 政令市の市長: 市名で1回だけ収集し、配下行政区に複製する
    if t["polei_cities"]:
        city_items = [(city, city) for city in sorted(t["polei_cities"])]
        prompt = build_prompt("政令指定都市", city_items)
        if dry_run:
            _save_prompt(prompts_dir, "polei_cities", prompt)
        else:
            cities.update(call_api(prompt))
    return by_code, cities


def _save_prompt(prompts_dir, name, prompt):
    d = prompts_dir or os.path.join(HERE, "_prompts")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{name}.md"), "w", encoding="utf-8") as f:
        f.write(prompt)


def assemble(by_code, cities, pref=None, prior=None):
    """収集結果＋決定的分類から reps_muni の records 辞書を作る。

    prior（既存 reps_muni.records）を渡すと、未確認（name=null）の自治体は既存を保持する。
    """
    t = T.build_targets(pref=pref)
    prior = prior or {}
    records = {}
    # 市町村長・特別区長
    for pref2, items in t["collectable"].items():
        for code, name in items:
            rec = by_code.get(code)
            if rec and rec.get("name"):
                records[code] = _clean_rec(rec)
            elif code in prior:
                records[code] = prior[code]          # 未確認は既存を保持
    # 政令市 → 配下行政区に市長を複製（区長は任命職）
    for city, kids in t["polei_cities"].items():
        crec = cities.get(city)
        for code in kids:
            if crec and crec.get("name"):
                records[code] = {"name": _san(C.strip_disambig(crec["name"])), "office": "市長",
                                 "inherited": True, "parentCity": city,
                                 "termEnd": _san(crec.get("termEnd", "")), "sourceUrl": _san_url(crec.get("sourceUrl", "")),
                                 "note": f"{city}長（政令市の行政区長は任命職のため市長を表示）"}
            elif code in prior:
                records[code] = prior[code]
    # noHead（北方領土等）
    for code in t["nohead"]:
        records[code] = prior.get(code, {"name": None, "office": "村長", "noHead": True,
                                         "note": "実効的な首長が存在しない地域"})
    return records


def _san(s):
    """モデル/web由来の文字列を無害化: 制御文字・角括弧（HTML/スクリプトの芽）を除去。"""
    if not s:
        return s
    return re.sub(r"[<>\x00-\x1f]", "", str(s)).strip()


def _san_url(u):
    """出典URLは http/https のみ許可（javascript: 等を弾く）。不許可なら空。"""
    u = _san(u)
    return u if re.match(r"^https?://", u, re.I) else ""


def _clean_rec(rec):
    # モデル出力（web_search 影響下）をそのまま公開データにしない。角括弧を除き、URLスキームを検査。
    out = {"name": _san(C.strip_disambig(rec.get("name", ""))), "office": _san(rec.get("office", "市長")) or "市長"}
    for k in ("kana", "termEnd", "note"):
        v = _san(rec.get(k))
        if v:
            out[k] = v
    su = _san_url(rec.get("sourceUrl", ""))
    if su:
        out["sourceUrl"] = su
    return out


def main():
    ap = argparse.ArgumentParser(description="市区町村長の再収集（エージェント支援）")
    ap.add_argument("--pref", help="2桁都道府県コードで絞り込み（例: 47）")
    ap.add_argument("--dry-run", action="store_true", help="プロンプト生成のみ（APIキー不要）")
    ap.add_argument("--prompts-dir", help="--dry-run のプロンプト出力先")
    ap.add_argument("--out", help="組み立て結果の reps_muni JSON 出力先")
    args = ap.parse_args()

    by_code, cities = collect(pref=args.pref, dry_run=args.dry_run, prompts_dir=args.prompts_dir)
    if args.dry_run:
        t = T.build_targets(pref=args.pref)
        n = len(t["collectable"]) + (1 if t["polei_cities"] else 0)
        print(f"[dry-run] {n} 本のプロンプトを生成しました（{args.prompts_dir or os.path.join(HERE, '_prompts')}）")
        return
    prior = json.load(open(os.path.join(C.APP_DATA, "reps_muni.json"), encoding="utf-8"))["records"]
    records = assemble(by_code, cities, pref=args.pref, prior=prior)
    print(f"収集: 市町村長 {len(by_code)} / 政令市 {len(cities)} → 組み立て {len(records)} 件")
    if args.out:
        C.write_doc(records, "muni", "首長", args.out)
        print(f"書き出し: {args.out}")


if __name__ == "__main__":
    main()
