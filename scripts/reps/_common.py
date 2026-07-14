#!/usr/bin/env python3
"""議員データ収集の共通ユーティリティ（知事・衆院・参院のスクレイパが共有）。

方針（重要）: 既存の reps_*.json は手作業で補完された豊富な情報（推薦政党・初当選日・注記など）
を持つ。スクレイパはそれを全て再現できないため、ここでは「今その職に就いているのは誰か」という
**変動しやすい事実の検出**に徹する。収集した core フィールド（氏名＋数フィールド）を既存データと
突き合わせ、氏名が変わった選挙区（＝交代）だけを差し替え候補として挙げる。氏名が同じなら既存の
records をそのまま残す（＝手作業の補完を壊さない）。交代は差分レポートに出し、人間が詳細を補完する。

出典は日本語版 Wikipedia の API（JSON・標準ライブラリで解析可能・現職に追随して更新される）を
既定にする。取得は urllib、失敗時は curl（システムのCA証明書）にフォールバックする。
"""
from __future__ import annotations
import os
import re
import sys
import json
import datetime
import urllib.parse
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_DATA = os.path.join(ROOT, "app", "data")
UA = "Ribat-Watch-reps/1.0 (+https://github.com/; public mosque/district map)"


# ---------------------------------------------------------------------------
# HTTP / Wikipedia
# ---------------------------------------------------------------------------
def http_get(url, timeout=60):
    """URL を取得して bytes を返す（urllib→失敗時 curl フォールバック）。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.URLError:
        import shutil
        import subprocess
        if not shutil.which("curl"):
            raise
        return subprocess.run(["curl", "-sSL", "--fail", "-A", UA, "--max-time", str(timeout), url],
                              check=True, stdout=subprocess.PIPE).stdout


def wiki_wikitext(page, lang="ja"):
    """日本語版 Wikipedia の記事ウィキテキストを返す（action=parse&prop=wikitext）。"""
    url = (f"https://{lang}.wikipedia.org/w/api.php?action=parse"
           f"&page={urllib.parse.quote(page)}&prop=wikitext&format=json&formatversion=2&redirects=1")
    data = json.loads(http_get(url))
    if "error" in data:
        raise RuntimeError(f"Wikipedia API error for {page!r}: {data['error'].get('info')}")
    return data["parse"]["wikitext"]


def wiki_url(page, lang="ja"):
    """記事の恒久URL（出典として記録する）。"""
    return f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(page)}"


def today_iso():
    return datetime.date.today().isoformat()


# ---------------------------------------------------------------------------
# ウィキテキストの整形
# ---------------------------------------------------------------------------
_REF_RE = re.compile(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_LINK_RE = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]")   # [[A|B]]->B, [[A]]->A


def strip_links(s):
    """[[記事|表示]] を表示名に、[[記事]] を記事名に置換する。"""
    return _LINK_RE.sub(lambda m: m.group(1), s)


def clean_cell(s):
    """セル文字列から ref・HTMLタグ・装飾を除き、表示テキストだけにする。"""
    s = _REF_RE.sub("", s)
    s = strip_links(s)
    s = _TAG_RE.sub("", s)
    # {{Ruby|漢字|かな}} → 漢字（かな抽出は ruby_kana で別途）
    s = re.sub(r"\{\{[Rr]uby\|([^|}]+)\|[^}]*\}\}", r"\1", s)
    s = re.sub(r"\{\{[^}]*\}\}", "", s)          # 残りのテンプレートを除去
    s = s.replace("'''", "").replace("''", "")
    return " ".join(s.split()).strip(" |")


def ruby_kana(s):
    """{{Ruby|漢字|かな}} から (漢字, かな) を取り出す（無ければ (clean, '')）。

    入れ子リンク [[記事|表示]] は記事名（先頭）を採り、かなは仮名だけのセグメントを選ぶ。
    例: {{Ruby|[[玉城デニー|玉城康裕]]|たまき やすひろ}} → ('玉城デニー', 'たまき やすひろ')
    """
    m = re.search(r"\{\{[Rr]uby\|(.+?)\}\}", s, re.S)
    if not m:
        return clean_cell(s), ""
    inner = re.sub(r"\[\[([^\]|]+)\|[^\]]*\]\]", r"\1", m.group(1))   # [[X|Y]] -> X
    inner = inner.replace("[[", "").replace("]]", "")
    parts = [p.strip() for p in inner.split("|") if p.strip()]
    if not parts:
        return clean_cell(s), ""
    kanji = clean_cell(parts[0])
    kana = ""
    for p in parts[1:]:
        if re.fullmatch(r"[ぁ-んァ-ヶ゛゜ー\s]+", p):    # 仮名（＋長音）のみのセグメント
            kana = " ".join(p.split())
            break
    return kanji, kana


_DISAMBIG_RE = re.compile(r"\s*[（(][^）)]*[）)]\s*$")   # 末尾の曖昧さ回避「（政治家）」等


def strip_disambig(name):
    """Wikipedia 記事名の末尾に付く曖昧さ回避「(政治家)」「（曖昧さ回避）」等を落とす。
    日本人名に括弧は含まれないため末尾括弧の除去は安全。"""
    if not name:
        return name
    return _DISAMBIG_RE.sub("", name).strip()


# 異体字・旧字体 → 常用字体（氏名比較の揺れを吸収。出典間で 壽/寿・邊/辺 等が混在するため）。
# 表示名は各スクレイパが取得したまま保存し、この畳み込みは「比較」だけに使う。
_KANJI_FOLD = str.maketrans({
    "壽": "寿", "邊": "辺", "邉": "辺", "壯": "壮", "﨑": "崎", "嵜": "崎", "國": "国",
    "澤": "沢", "齋": "斎", "齊": "斎", "髙": "高", "眞": "真", "德": "徳", "櫻": "桜",
    "惠": "恵", "巖": "巌", "龍": "竜", "濱": "浜", "檜": "桧", "槇": "槙", "曾": "曽",
    "廣": "広", "萬": "万", "禮": "礼", "豐": "豊", "彌": "弥",
    "ヶ": "ケ", "ッ": "ツ", "ヵ": "カ",
})


def norm_name(name):
    """氏名を比較用に正規化する（曖昧さ回避・異体字・空白・全角空白・肩書の除去）。"""
    if not name:
        return ""
    n = strip_disambig(name).replace("　", "").replace(" ", "")
    n = re.sub(r"(知事|議員|氏)$", "", n)
    return n.translate(_KANJI_FOLD).strip()


# 通称↔正式名など「同一人物の別表記」を橋渡しする委員会承認済みの対応表（公開情報・非PII）。
# 例: 「塩村あやか（投票用の通称）」＝「塩村文夏（正式名）」。出典（go2senkyo等）と Wikipedia で
# 表記が食い違うと毎回の差分に偽の交代が出るため、確認済みの対応をここに1度だけ登録して黙らせる。
ALIASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "name_aliases.json")


def _load_alias_map():
    """{正規化名: 代表正規化名} を作る（同一グループは同じ代表に潰す）。"""
    amap = {}
    if os.path.exists(ALIASES_PATH):
        try:
            pairs = json.load(open(ALIASES_PATH, encoding="utf-8")).get("aliases", [])
        except (ValueError, OSError):
            pairs = []
        for group in pairs:
            keys = [norm_name(n) for n in group if n]
            if keys:
                rep = keys[0]
                for k in keys:
                    amap[k] = rep
    return amap


_ALIAS_MAP = _load_alias_map()


def ident(name):
    """同一人物判定用の識別子（正規化名を別表記対応で代表名に寄せる）。"""
    n = norm_name(name)
    return _ALIAS_MAP.get(n, n)


# ---------------------------------------------------------------------------
# コード対応表（既存の district GeoJSON から作る）
# ---------------------------------------------------------------------------
def district_names(level, app_data=APP_DATA):
    """district_<level>.geojson から {code: name} を作る。"""
    d = json.load(open(os.path.join(app_data, f"districts_{level}.geojson"), encoding="utf-8"))
    out = {}
    for f in d["features"]:
        p = f["properties"]
        out[str(p["code"])] = p["name"]
    return out


# 都道府県名（末尾の 都/道/府/県 込み）→ 2桁コード
def pref_name_to_code(app_data=APP_DATA):
    return {name: code for code, name in district_names("pref", app_data).items()}


# 都道府県名 → コード（「東京都」「東京」どちらでも引けるよう別名も張る）
def pref_lookup(app_data=APP_DATA):
    base = pref_name_to_code(app_data)
    out = dict(base)
    for name, code in base.items():
        short = re.sub(r"(都|道|府|県)$", "", name)
        out.setdefault(short, code)
    return out


# ---------------------------------------------------------------------------
# 保守的マージ（氏名が変わった＝交代のときだけ差し替え候補にする）
# ---------------------------------------------------------------------------
def merge_single(committed, scraped, volatile=("winCount",)):
    """単任（知事・衆院）用マージ。

    committed: {code: record}（既存の豊富なデータ）
    scraped  : {code: core}（{name, sourceUrl, ...} の最小情報）
    戻り値: (merged_records, changes)
      changes: [{code, kind, old, new}] kind ∈ {'turnover','new_code','name_missing'}
    氏名一致 → 既存recordを保持（curation維持）。氏名不一致 → 交代候補として core で置換し note を付す。
    """
    merged = dict(committed)
    changes = []
    for code, core in scraped.items():
        cur = committed.get(code)
        new_name = core.get("name", "")
        if cur is None:
            core = dict(core)
            core["note"] = (core.get("note", "") + " ｜自動検出の新規コード（要確認）").strip("｜ ")
            merged[code] = core
            changes.append({"code": code, "kind": "new_code", "old": None, "new": new_name})
            continue
        if not new_name:
            changes.append({"code": code, "kind": "name_missing", "old": cur.get("name"), "new": None})
            continue
        if ident(new_name) != ident(cur.get("name", "")):
            rec = dict(core)
            rec["note"] = (f"自動検出の交代候補: 前任「{cur.get('name','')}」→「{new_name}」。"
                           f"詳細（推薦・初当選日など）は要確認・未補完。")
            merged[code] = rec
            changes.append({"code": code, "kind": "turnover", "old": cur.get("name"), "new": new_name})
        # 氏名一致: 既存を保持（curation を壊さない）
    return merged, changes


def _kana_key(m):
    """議員の読み（かな）を比較キーに正規化する（空白・中黒除去）。無ければ空。"""
    k = (m.get("kana") or "").replace("　", "").replace(" ", "").replace("・", "")
    return k


def _same_member(a, b):
    """同一議員か: 識別子（別表記対応込み）が一致、または かな読みが一致。"""
    if ident(a.get("name", "")) == ident(b.get("name", "")):
        return True
    ka, kb = _kana_key(a), _kana_key(b)
    return bool(ka) and ka == kb


def merge_hc(committed, scraped):
    """参院（複数人区）用マージ。選挙区ごとに議員の顔ぶれを比べ、変化した区だけ差し替え候補にする。

    同一議員判定は氏名一致 or かな一致（出典間で「さや↔塩入清香」等の通称/正式名の差を吸収）。
    顔ぶれ一致 → 既存の members を保持（かな等の補完を壊さない）。
    変化あり → scraped を基に更新し、同一議員は既存レコードを引き継ぐ。
    戻り値: (merged_records, changes)  changes: [{code, kind:'membership', old:[…], new:[…]}]
    """
    merged = {k: v for k, v in committed.items()}
    changes = []
    for code, srec in scraped.items():
        crec = committed.get(code)
        s_members = srec.get("members", [])
        if crec is None:
            merged[code] = srec
            changes.append({"code": code, "kind": "new_code", "old": [],
                            "new": sorted(m.get("name", "") for m in s_members)})
            continue
        c_members = crec.get("members", [])
        # scraped 各員に一致する committed 員を探す。全員一致かつ員数同数なら変化なし。
        unmatched_c = list(c_members)
        new_members, all_matched = [], True
        for sm in s_members:
            hit = next((cm for cm in unmatched_c if _same_member(sm, cm)), None)
            if hit is not None:
                unmatched_c.remove(hit)
                new_members.append(hit)            # 既存レコードを引き継ぐ（curation維持）
            else:
                new_members.append(sm)
                all_matched = False
        if all_matched and not unmatched_c:
            continue                               # 顔ぶれ一致: 既存を保持
        merged[code] = {"magnitude": len(new_members), "members": new_members}
        changes.append({"code": code, "kind": "membership",
                        "old": sorted(m.get("name", "") for m in c_members),
                        "new": sorted(m.get("name", "") for m in s_members)})
    return merged, changes


def write_doc(records, level, office, path, asOf=None):
    """reps_<level>.json を既存と同じ体裁（indent=1）で書き出す。"""
    doc = {"level": level, "office": office, "asOf": asOf or today_iso(), "records": records}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    return path
