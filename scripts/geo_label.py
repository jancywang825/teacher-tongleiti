# -*- coding: utf-8 -*-
"""几何图顶点标签自动避线放置（Skill 共用模块，2026-10-14 新增）。

原则（用户硬性要求）：顶点字母放在顶点旁边，但**绝不压在任何棱线/线段上**。

算法（屏幕空间避线）：
1. 取当前视角 (azim, elev)，构造与 matplotlib 正交投影同一平面的屏幕基 (r, u)；
   （任何与该平面同基底的旋转/镜像都不改变"标签到棱线的距离"关系，故无需
    与 mpl 的内部约定完全一致。）
2. 把所有顶点投影到该 2D 平面；棱也变成 2D 线段。
3. 每个顶点的初始标签方向 = 其所有邻边单位向量的**反向和**（角的外侧平分方向）；
   无邻边的点（如棱中点 F）取屏幕上方，再进入避线搜索。
4. 避线搜索：从初始方向开始，按 ±15° 步进旋转（最多 ±180°），逐一检查
   "标签中心到所有棱线段的 2D 距离"，取第一个满足 ≥ line_pad 的方向；
   若全都不满足，取距离最大的方向。
5. 把选中的 2D 方向映射回 3D：g3 = gx*r + gy*u，标签 3D 位置 = 顶点 + g3*off。
   文字锚点用居中（ha/va='center'），因此字母恰好"贴着顶点旁、离开线"。
"""
import numpy as np


def _basis(azim, elev):
    a = np.deg2rad(azim); e = np.deg2rad(elev)
    d = np.array([np.cos(e)*np.cos(a), np.cos(e)*np.sin(a), np.sin(e)])
    r = np.array([-np.sin(a), np.cos(a), 0.0])
    u = np.cross(d, r)
    return r, u


def _seg_dist(p, a, b):
    a = np.asarray(a, float); b = np.asarray(b, float); p = np.asarray(p, float)
    ab = b - a; L2 = float(np.dot(ab, ab))
    if L2 < 1e-12:
        return float(np.linalg.norm(p - a))
    t = float(np.clip(np.dot(p - a, ab)/L2, 0.0, 1.0))
    return float(np.linalg.norm(p - (a + t*ab)))


def place_labels(ax, points, edges, off=0.22, line_pad=0.16, fontsize=12,
                 ink="#111111", text_kw=None, rot_steps=(0, 15, -15, 30, -30, 45, -45,
                                                          60, -60, 75, -75, 90, -90,
                                                          105, -105, 120, -120,
                                                          135, -135, 150, -150,
                                                          165, -180)):
    """在 3D 轴 `ax` 上为 `points` 中的每个顶点放置避线标签。

    参数
    ----
    points : {name: (x,y,z)} 全部需要标字母的点（含棱中点等非端点）。
    edges  : [(a,b),...] 图中画出的**所有**线段（含虚线隐藏棱、含点所落的线），
             用于避让判定。
    off    : 标签中心到顶点的距离（数据坐标，约 0.22）。
    line_pad : 标签中心到任一棱线的最小允许距离（小于它视为"压线"）。
    返回：{name: (pos3, min_dist)} 便于自检打印。
    """
    azim, elev = ax.azim, ax.elev
    r, u = _basis(azim, elev)
    pts = {k: np.asarray(v, float) for k, v in points.items()}
    P2 = {k: np.array([np.dot(v, r), np.dot(v, u)]) for k, v in pts.items()}
    E2 = [(P2[a], P2[b]) for (a, b) in edges]

    nbrs = {k: [] for k in pts}
    for a, b in edges:
        nbrs.setdefault(a, []).append(b)
        nbrs.setdefault(b, []).append(a)

    text_kw = dict(text_kw or {})
    placed = {}
    for name, p in pts.items():
        p2 = P2[name]
        dirs = []
        for nb in nbrs.get(name, []):
            v = P2[nb] - p2; nv = np.linalg.norm(v)
            if nv > 1e-9:
                dirs.append(v/nv)
        if dirs:
            g2 = -np.sum(dirs, axis=0)
            ng = np.linalg.norm(g2)
            g2 = g2/ng if ng > 1e-9 else np.array([0.0, 1.0])
        else:
            g2 = np.array([0.0, 1.0])   # 无邻边（中点）：先朝屏幕上方，再避线搜索

        best = None
        for deg in rot_steps:
            th = np.deg2rad(deg)
            c, s = np.cos(th), np.sin(th)
            g = np.array([c*g2[0] - s*g2[1], s*g2[0] + c*g2[1]])
            pos = p2 + g*off
            mind = min((_seg_dist(pos, a2, b2) for (a2, b2) in E2), default=9e9)
            if best is None or mind > best[0]:
                best = (mind, g)
            if mind >= line_pad:
                break
        mind, g = best
        g3 = g[0]*r + g[1]*u
        pos3 = p + g3*off
        kw = dict(fontsize=fontsize, color=ink, ha='center', va='center', zorder=7)
        kw.update(text_kw)
        ax.text(pos3[0], pos3[1], pos3[2], name, **kw)
        placed[name] = (pos3, mind)
    return placed
