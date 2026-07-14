#!/usr/bin/env python3
"""個人情報スクラブ＋一般化（公開リポジトリ向けの共有モジュール）。

このモジュールは parse_kml.py（生KMLからの初回ビルド）と
scripts/refresh_mosques.py（定期的な再取得）の両方から使われる。説明文の処理を
1か所に集約し、公開データに個人情報が混入しないようにするのが目的。

処理は2段階:

  1) スクラブ（denylist） … scrub_rules.json に列挙した「除去語」と「除去URL片」を消す。
     除去語そのものが個人情報なので規則ファイルは公開しない（.gitignore 済み）。
     規則の読み込み元は次の優先順位:
        環境変数 SCRUB_RULES_JSON（JSON文字列そのもの。GitHub Actions の秘密情報用）
        > 環境変数 SCRUB_RULES_PATH（ファイルパス）
        > 既定 scripts/scrub_rules.json
     どこにも無ければスクラブは行わない（＝配布物は既に加工済みの前提）。

  2) 一般化（generalize） … 説明文から URL だけを取り出し、それ以外の自由記述
     （住所・メモ・氏名・SNSの個人ハンドル・主観的論評など）をすべて破棄する。
     denylist は「既知の語」しか消せないが、一般化は自由記述をまるごと落とすため、
     まだ規則に載っていない新規の個人情報も自由記述としては漏れない。
     - 非SNSホスト（モスク公式サイト・ニュース・Wikipedia・行政・地図・画像など）は
       既定で残す（＝SNSの denylist であり、ホストの allowlist ではない）。
     - SNSホスト（Facebook/Instagram/TikTok/X/YouTube 等）は、その正規化URLが
       「grandfather 集合」（＝既に公開中のデータに存在する＝モスク名義として確認済み）
       に完全一致する場合のみ残す。新規SNSや、承認済みURLへのクエリ/フラグメント改ざんは
       破棄し、PRの差分で人間が確認する。
     - 公開するURLは userinfo（user:pw@）とフラグメント（#…）を除いた形にする。
     - 残る限界: 非SNSホストのURLは path/query をそのまま保持するため、任意ドメインの
       path/query に個人情報を埋め込む改ざんは一般化では止まらない（cid= や動画ID等の
       正当なクエリを壊さないための設計）。この経路は「人間によるPRレビュー」で担保する。

  重要: アプリ（app/app.js）はモスクのポップアップで source_url しか表示せず、
  description 自由記述は一切描画しない。よって説明文をURLのみに切り詰めても
  UI上の欠落は無く、公開ファイルからの情報露出だけが減る。
"""
from __future__ import annotations
import os
import re
import json
from urllib.parse import urlsplit, urlunsplit

# 既定の規則ファイル（このファイルと同じ scripts/ ディレクトリ）
DEFAULT_RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrub_rules.json")
# モスク名義SNSの明示的な許可リスト（任意・公開可。人間が新規SNSを承認する際の追記先）
DEFAULT_SOCIAL_ALLOWLIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mosque_social_allowlist.json")

# 説明文中のURL抽出（<, >, ", ), 空白の手前まで）
URL_RE = re.compile(r'https?://[^\s<>"\')]+')
# URL末尾に紛れ込みやすい句読点・閉じ括弧を落とす
_URL_TRAILING = "。、，．,.）)」』】＞>…"

# SNS・リンク集約サービスのホスト（末尾一致で判定）。ここに該当するURLは
# grandfather 集合に無い限り破棄する（個人ハンドル流入を防ぐため）。
SOCIAL_HOSTS = (
    "facebook.com", "fb.com", "fb.watch", "instagram.com", "tiktok.com",
    "twitter.com", "x.com", "youtube.com", "youtu.be", "threads.net",
    "line.me", "lin.ee", "t.me", "wa.me", "whatsapp.com",
    "linktr.ee", "lit.link", "linkin.bio", "ameblo.jp", "note.com",
    "pinterest.com", "pinterest.jp", "snapchat.com", "mixi.jp",
)

# URL除去後に残るSNSラベルだけの断片（保険。一般化では自由記述を全破棄するため通常不要）
LABEL_ONLY_RE = re.compile(r"^\s*(tiktok|facebook|fb|instagram|インスタ|youtube|x|twitter)\s*[:：]?\s*$", re.I)


# ---------------------------------------------------------------------------
# 規則（denylist）の読み込み
# ---------------------------------------------------------------------------
def load_rules() -> dict:
    """除去規則を読み込む。優先順位は SCRUB_RULES_JSON > SCRUB_RULES_PATH > 既定パス。"""
    raw = os.environ.get("SCRUB_RULES_JSON")
    if raw:
        return json.loads(raw)
    path = os.environ.get("SCRUB_RULES_PATH", DEFAULT_RULES_PATH)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def rules_available() -> bool:
    """除去規則が読める状態か（CI で秘密情報の設定漏れを検知するのに使う）。"""
    if os.environ.get("SCRUB_RULES_JSON"):
        return True
    path = os.environ.get("SCRUB_RULES_PATH", DEFAULT_RULES_PATH)
    return os.path.exists(path)


class Scrubber:
    """規則を1度読み込み、denylist スクラブと一般化をまとめて提供する。"""

    def __init__(self, rules: dict | None = None):
        r = rules if rules is not None else load_rules()
        self.remove_url_substr = r.get("remove_url_substr", [])
        self.remove_phrases = r.get("remove_phrases", [])
        # 除去対象URL片を含むURL全体にマッチする正規表現（片が無ければ無効）
        self._blocked_url_re = (
            re.compile(r'https?://[^\s<>"]*(?:' + "|".join(re.escape(s) for s in self.remove_url_substr) + r')[^\s<>"]*')
            if self.remove_url_substr else None
        )

    # ---- 段階1: denylist スクラブ ----
    def scrub(self, desc: str) -> str:
        """scrub_rules.json の除去語・除去URL片を消す（従来の parse_kml と同一動作）。"""
        if not desc:
            return ""
        for ph in self.remove_phrases:
            desc = desc.replace(ph, "")
        segs = re.split(r"(?:<br\s*/?>)+", desc)      # <br> ごとに処理
        out = []
        for s in segs:
            if self._blocked_url_re:
                s = self._blocked_url_re.sub("", s)   # ブロック対象URLを除去
            s = " ".join(s.split())                    # 余分な空白を圧縮
            if not s:
                continue
            if LABEL_ONLY_RE.match(s):                 # ラベルのみの断片は捨てる
                continue
            out.append(s)
        return "<br>".join(out).strip()

    # ---- 段階2: 一般化（許可URLのみ残す） ----
    def generalize(self, desc: str, grandfather: set[str] | None = None) -> str:
        """説明文を「許可ホストのURLのみ」に切り詰める。非URLの自由記述は全破棄。"""
        if not desc:
            return ""
        gf = grandfather or set()
        kept, seen = [], set()
        for raw_url in URL_RE.findall(desc):
            u = raw_url.rstrip(_URL_TRAILING)
            host = _host(u)
            if not host:
                continue
            # 同定キーはクエリまで含める（承認済みURLに未承認のクエリ/フラグメントを付けた
            # 改ざんを弾き、別動画・別地点を1つに畳まないため）。SNSは grandfather 集合に
            # 完全一致するときだけ残す。
            key = norm_url(u)
            if _is_social(host) and key not in gf:
                continue                               # 未承認のSNS（改ざん含む）は破棄
            if key in seen:                            # 重複URLは1つに
                continue
            seen.add(key)
            kept.append(_emit_url(u))                   # userinfo・フラグメントを除去して公開
        return " ".join(kept)

    def clean(self, desc: str, grandfather: set[str] | None = None, generalize: bool = True) -> str:
        """denylist スクラブ → （任意で）一般化 をまとめて適用する。"""
        d = self.scrub(desc)
        if generalize:
            d = self.generalize(d, grandfather)
        return d


# ---------------------------------------------------------------------------
# URL ユーティリティ
# ---------------------------------------------------------------------------
def _host(url: str) -> str:
    """URL のホスト名を小文字・先頭 www. 除去で返す（失敗時は空文字）。

    urlsplit().hostname を使い、userinfo（user:pw@）とポートを除いた純粋なホストを返す。
    netloc をそのまま使うと 'user:pw@facebook.com' のような userinfo 付きURLで SNS 判定を
    すり抜け、未承認の個人アカウントが残ってしまうため。
    """
    try:
        h = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    if h.startswith("www."):
        h = h[4:]
    return h


def _is_social(host: str) -> bool:
    """ホストが SNS/リンク集約サービスか（末尾一致）。"""
    return any(host == s or host.endswith("." + s) for s in SOCIAL_HOSTS)


def norm_url(url: str) -> str:
    """アカウント同定用の正規化: スキーム除去・ホスト小文字（userinfo/ポート除去）・
    フラグメント除去・末尾スラッシュ除去。クエリは同定に含める。

    クエリを含めるのは、(1) 承認済みSNSアカウントURLに未承認のクエリ（?で始まる自由記述の
    個人情報など）を付けた改ざんを grandfather 一致から弾くため、(2) ?cid= や ?v= のように
    クエリが実体を表すURL（Googleマップ地点・YouTube動画）を別物として区別し、重複畳み込みで
    取りこぼさないため。"""
    u = url.rstrip(_URL_TRAILING)
    try:
        p = urlsplit(u)
    except ValueError:
        return u.lower()
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    key = f"{host}{p.path.rstrip('/')}"
    if p.query:
        key += "?" + p.query
    return key.lower()


def _emit_url(url: str) -> str:
    """公開用にURLを整形する: userinfo（user:pw@）とフラグメント（#…）を除去し、
    スキーム・ホスト・パス・クエリだけを残す。クエリは実体を表すことがあるため保持する
    （?cid=・?v=・画像の?fife= 等）。userinfo とフラグメントは公開URLに一切現れず、
    自由記述の個人情報を紛れ込ませる余地になるため落とす。"""
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    netloc = p.hostname or ""
    if p.port:
        netloc += f":{p.port}"
    return urlunsplit((p.scheme, netloc, p.path, p.query, ""))


def first_url(desc: str) -> str:
    """説明文から最初に現れる URL を返す（無ければ空文字）。末尾句読点は落とす。"""
    if not desc:
        return ""
    m = URL_RE.search(desc)
    return m.group(0).rstrip(_URL_TRAILING) if m else ""


def load_grandfather_social(prior_geojson_path: str | None = None,
                            allowlist_path: str | None = DEFAULT_SOCIAL_ALLOWLIST) -> set[str]:
    """モスク名義として承認済みとみなす SNS アカウントURLの集合を作る。

    出所:
      (a) 既に公開中の mosques.geojson の説明文に含まれる SNS URL
          （＝過去に人間の確認を経て公開されたもの）
      (b) scripts/mosque_social_allowlist.json の urls 配列（人間が明示追加する場所）
    正規化キー（host+path）で保持する。
    """
    gf: set[str] = set()
    if prior_geojson_path and os.path.exists(prior_geojson_path):
        try:
            data = json.load(open(prior_geojson_path, encoding="utf-8"))
            for f in data.get("features", []):
                desc = (f.get("properties", {}) or {}).get("description", "") or ""
                for u in URL_RE.findall(desc):
                    if _is_social(_host(u)):
                        gf.add(norm_url(u))
        except (ValueError, OSError):
            pass
    if allowlist_path and os.path.exists(allowlist_path):
        try:
            al = json.load(open(allowlist_path, encoding="utf-8"))
            for u in al.get("urls", []):
                gf.add(norm_url(u))
        except (ValueError, OSError):
            pass
    return gf


# 簡易自己テスト: `python3 scripts/scrub.py`
if __name__ == "__main__":
    s = Scrubber(rules={})   # denylist 無しで一般化のみ検証
    gf = {"facebook.com/sapporomasjid"}
    samples = [
        ("北海道旭川市3条通2丁目1011-1<br>公式HP：https://asahikawamasjid.com/<br>インスタ：https://instagram.com/personal_handle",
         "非SNS公式のみ残る／個人インスタは破棄"),
        ("宗教法人<br>https://facebook.com/sapporomasjid<br>謎のメモ", "承認済みfacebookは残る"),
        ("住所だけで<br>URLなし<br><img src=\"\" />", "空になる"),
        ("https://en.wikipedia.org/wiki/Foo と https://x.com/random_person", "wiki残る・x破棄"),
        # --- 改ざん耐性（監査で確認した経路） ---
        ("http://user:pw@facebook.com/personal_handle", "userinfoすり抜け無し→破棄"),
        ("https://facebook.com/sapporomasjid?leak=山田太郎", "承認済みへのクエリ改ざん→破棄"),
        ("https://facebook.com/sapporomasjid#山田太郎", "フラグメント改ざん→承認済みは残しフラグメントは除去"),
    ]
    for desc, note in samples:
        print(f"IN : {desc}")
        print(f"OUT: {s.generalize(desc, gf)!r}   # {note}\n")
