#!/usr/bin/env python3
"""地理演算ユーティリティ（外部ライブラリ不要）。

このモジュールが提供する機能:
  - 点の多角形内包判定（レイキャスティング法）
  - バウンディングボックス（外接矩形）による高速な事前フィルタ
  - ダグラス・ポーカー法によるポリゴンの簡略化（頂点の間引き）
  - 座標の丸め（桁数削減によるファイルサイズ縮小）

座標はすべて GeoJSON 形式の [経度, 緯度]（[lon, lat]）で扱う。
簡略化では経度・緯度を平面座標として近似的に扱うが、全国規模の
ウェブ地図で用いる許容誤差では実用上問題にならない。
"""
from __future__ import annotations


# ---------- 点の多角形内包判定 ----------
def _point_in_ring(x: float, y: float, ring) -> bool:
    """レイキャスティング法による判定。ring = [[経度,緯度],...]（閉じていなくても可）。

    点 (x, y) から右方向に半直線を伸ばし、リングの辺と交差した回数が
    奇数なら内側、偶数なら外側と判定する古典的アルゴリズム。
    """
    inside = False
    n = len(ring)
    j = n - 1                       # j は「一つ前の頂点」の添字（初回は末尾）
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        # 辺 (i, j) が水平線 y をまたぐか判定
        if ((yi > y) != (yj > y)):
            # またぐ場合、その辺と水平線の交点の x 座標を求める
            # （分母に 1e-18 を足してゼロ除算を回避）
            xint = (xj - xi) * (y - yi) / (yj - yi + 1e-18) + xi
            if x < xint:            # 交点が点より右にあれば交差1回とカウント
                inside = not inside
        j = i
    return inside


def _point_in_polygon(x, y, polygon) -> bool:
    """polygon = [外周リング, 穴1, 穴2, ...]。外周の内側かつ全ての穴の外側なら True。"""
    if not polygon:
        return False
    if not _point_in_ring(x, y, polygon[0]):     # まず外周の内側にあるか
        return False
    for hole in polygon[1:]:                      # 穴（内周）の中に入っていたら除外
        if _point_in_ring(x, y, hole):
            return False
    return True


def point_in_geometry(x, y, geom) -> bool:
    """GeoJSON ジオメトリ（Polygon / MultiPolygon）に対する内包判定。"""
    t = geom.get("type")
    c = geom.get("coordinates")
    if t == "Polygon":
        return _point_in_polygon(x, y, c)
    if t == "MultiPolygon":
        # マルチポリゴンはいずれか一つのポリゴンに入っていれば内側
        return any(_point_in_polygon(x, y, poly) for poly in c)
    return False


# ---------- バウンディングボックス（外接矩形） ----------
def _walk_coords(c):
    """任意の入れ子座標配列から [経度, 緯度] のペアを順に取り出すジェネレータ。"""
    if not c:                                   # 空配列（簡略化で消えた微小島など）は無視
        return
    if isinstance(c[0], (int, float)):          # 末端＝1つの座標ペアに到達
        yield c
    else:
        for sub in c:                           # まだ入れ子なら再帰的に降りる
            yield from _walk_coords(sub)


def geom_bbox(geom):
    """ジオメトリの外接矩形 (経度min, 緯度min, 経度max, 緯度max) を返す。"""
    xs_min = ys_min = float("inf")
    xs_max = ys_max = float("-inf")
    for lon, lat in _walk_coords(geom["coordinates"]):
        if lon < xs_min: xs_min = lon
        if lon > xs_max: xs_max = lon
        if lat < ys_min: ys_min = lat
        if lat > ys_max: ys_max = lat
    return (xs_min, ys_min, xs_max, ys_max)


class BBoxIndex:
    """ポリゴン群に対する簡易な外接矩形インデックス。

    点を各ポリゴンに割り当てる際、まず外接矩形で大まかに絞り込み、
    矩形に入ったものだけ厳密な内包判定を行うことで高速化する。
    （311点 × 約1900自治体でも十分に速い）
    """
    def __init__(self, features):
        self.items = []  # (外接矩形, フィーチャ) のリスト
        for f in features:
            g = f.get("geometry")
            # ポリゴン系かつ座標を持つフィーチャのみ対象
            if not g or g.get("type") not in ("Polygon", "MultiPolygon") or not g.get("coordinates"):
                continue
            bb = geom_bbox(g)
            if bb[0] == float("inf"):   # 有効な座標が無い（空ジオメトリ）ものは除外
                continue
            self.items.append((bb, f))

    def query_point(self, x, y):
        """点 (x, y) を含む最初のフィーチャを返す。該当なしなら None。"""
        for (x0, y0, x1, y1), f in self.items:
            if x0 <= x <= x1 and y0 <= y <= y1:            # ①外接矩形で粗く判定
                if point_in_geometry(x, y, f["geometry"]):  # ②厳密な内包判定
                    return f
        return None


# ---------- ダグラス・ポーカー法によるポリゴン簡略化 ----------
def _dp(points, tol):
    """折れ線 points を許容誤差 tol で簡略化する（再帰的ダグラス・ポーカー法）。

    始点と終点を結ぶ線分から最も離れた頂点を探し、その距離が tol を超えれば
    その点で分割して両側を再帰的に処理、超えなければ中間の頂点を全て捨てる。
    """
    if len(points) < 3:
        return points
    dmax, idx = 0.0, 0                # 最大距離と、その頂点の添字
    x0, y0 = points[0]
    xe, ye = points[-1]
    dx, dy = xe - x0, ye - y0
    seg2 = dx * dx + dy * dy          # 始点-終点線分の長さの2乗
    for i in range(1, len(points) - 1):
        xi, yi = points[i]
        if seg2 == 0:                # 始点と終点が同一 → 単純な点間距離
            d = ((xi - x0) ** 2 + (yi - y0) ** 2) ** 0.5
        else:
            # 頂点を線分に射影した位置 t（0..1にクランプ）を求め、垂線距離を計算
            t = ((xi - x0) * dx + (yi - y0) * dy) / seg2
            t = max(0.0, min(1.0, t))
            px, py = x0 + t * dx, y0 + t * dy
            d = ((xi - px) ** 2 + (yi - py) ** 2) ** 0.5
        if d > dmax:
            dmax, idx = d, i
    if dmax > tol:                   # 誤差が大きい → その点で分割して再帰
        left = _dp(points[:idx + 1], tol)
        right = _dp(points[idx:], tol)
        return left[:-1] + right     # 分割点の重複を避けて連結
    return [points[0], points[-1]]   # 誤差が小さい → 中間頂点を捨てる


def _simplify_ring(ring, tol, min_pts=4):
    """リング（多角形の1周）を簡略化。頂点が min_pts 未満に潰れたら None を返す。"""
    closed = ring[0] == ring[-1]              # 始点と終点が一致（閉じている）か
    pts = ring[:-1] if closed else ring[:]    # 閉じている場合は重複終点を一旦外す
    s = _dp(pts, tol)
    if closed:
        s = s + [s[0]]                        # 閉じ直す
    if len(s) < min_pts:                      # リングとして成立しないほど潰れた
        return None
    return s


def simplify_geometry(geom, tol):
    """Polygon / MultiPolygon 全体を簡略化する。潰れた場合は None を返す。"""
    t = geom["type"]
    if t == "Polygon":
        new = []
        for i, ring in enumerate(geom["coordinates"]):
            s = _simplify_ring(ring, tol)
            if s is None and i == 0:
                return None          # 外周が潰れたらポリゴンごと破棄
            if s is not None:
                new.append(s)        # 穴が潰れた場合はその穴だけ省く
        return {"type": "Polygon", "coordinates": new} if new else None
    if t == "MultiPolygon":
        polys = []
        for poly in geom["coordinates"]:
            # 各構成ポリゴンを Polygon として簡略化し、生き残ったものだけ集める
            sp = simplify_geometry({"type": "Polygon", "coordinates": poly}, tol)
            if sp:
                polys.append(sp["coordinates"])
        return {"type": "MultiPolygon", "coordinates": polys} if polys else None
    return geom


# ---------- 座標の丸め ----------
def round_coords(c, nd=5):
    """入れ子座標配列を小数 nd 桁に丸める（ファイルサイズ削減用）。"""
    if isinstance(c[0], (int, float)):
        return [round(c[0], nd), round(c[1], nd)]
    return [round_coords(sub, nd) for sub in c]


def round_geometry(geom, nd=5):
    """ジオメトリの全座標を小数 nd 桁に丸めた新しいジオメトリを返す。"""
    return {"type": geom["type"], "coordinates": round_coords(geom["coordinates"], nd)}
