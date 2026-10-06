"""
layout_realistic*.blend を「部品単位のソリッド」の STEP に書き出すスクリプト。
Fusion / FreeCAD / SolidWorks などで、色付きのアセンブリとして開けます (レンダリング向け)。

使い方:
    python export_step_detailed.py 入力.blend 出力.step
必要なもの: bpy, trimesh, scipy, cadquery-ocp

作り方:
- 見た目用の面取り (Bevel モディファイア) は切って、元の形で評価する
- 各設備のメッシュを、つながった部品ごとに分ける
- 凸形状の部品 (箱・円柱など、ほとんどがこれ) → 凸包からソリッドを作る (形は元と同じ)
- 凸でない部品 (作業者・波板など) → メッシュをそのまま閉じた面にしてソリッドにする
- 同じ平面の三角形はまとめて 1 枚の面にする
- 部品はマテリアルごとに分けるので、色は元のマテリアルの色 (模様は単色になる)
- STEP の構成: 設備ごとのアセンブリ → 色ごとの部品
- 軽くするための簡略化:
  - パレットに積んだ菌棒は数が多すぎるので、外側のストレッチフィルムの外形だけにする
  - トレー・菌棒などの荷は、接している同じ色の部品を 1 つの塊にまとめる
  - 円柱などの曲面は角数を減らす (設備 20 角程度、荷 10 角程度)
  - 15mm より小さい部品 (ねじ頭など) は省く。作業者はポリゴンを間引く
"""
import os
import re
import sys
from collections import Counter, defaultdict

import bpy
import numpy as np
import trimesh

from OCP.BRep import BRep_Builder
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeFace,
                                BRepBuilderAPI_Sewing, BRepBuilderAPI_MakeSolid)
from OCP.gp import gp_Pnt
from OCP.IFSelect import IFSelect_RetDone
from OCP.Interface import Interface_Static
from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
from OCP.ShapeFix import ShapeFix_Solid
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_AsIs
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_SHELL
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Compound
from OCP.XCAFDoc import XCAFDoc_ColorSurf, XCAFDoc_DocumentTool

MM = 1000.0
MAX_FACETED_TRIS = 6000  # これより多い非凸部品は凸包で代用
MIN_SIZE = 15.0          # これより小さい部品 (ねじ頭など, mm) は省く
HULL_POINTS = 40         # 凸包の頂点数の上限 (円柱なら 20 角柱相当)
HULL_POINTS_LOAD = 20    # 荷 (トレー・菌棒など) の凸包の頂点数の上限
# 部品が多すぎるもの: 接している同じ色の部品をまとめて 1 つの塊にする
MERGE_TOUCHING = ('R_種トレー', '菌床トレー', '菌棒_', '荷_', 'R_パレット')
DECIMATE = {'作業者': 0.12}  # 人の形はポリゴンを間引く

argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else sys.argv[1:]
SRC, DST = os.path.abspath(argv[0]), os.path.abspath(argv[1])

bpy.ops.wm.open_mainfile(filepath=SRC)
for ob in bpy.data.objects:
    for m in ob.modifiers:
        if m.type == 'BEVEL':
            m.show_viewport = False
            m.show_render = False

for ob in bpy.data.objects:
    r = ob
    while r.parent is not None:
        r = r.parent
    for prefix, ratio in DECIMATE.items():
        if r.name.startswith(prefix) and ob.type == 'MESH':
            m = ob.modifiers.new('STEP用_間引き', 'DECIMATE')
            m.ratio = ratio

label_objs = {o.name for o in bpy.data.collections['名称ラベル'].all_objects} \
    if '名称ラベル' in bpy.data.collections else set()


def root_of(obj):
    while obj.parent is not None:
        obj = obj.parent
    return obj


def mat_color(mat):
    if mat is None:
        return (0.7, 0.7, 0.7)
    b = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None) \
        if mat.node_tree else None
    if b is None or b.inputs['Base Color'].is_linked:
        return tuple(mat.diffuse_color[:3])
    return tuple(b.inputs['Base Color'].default_value[:3])


def category(name):
    if name in label_objs:
        return '名称ラベル'
    if re.match(r'^(R_(波板|レール|コーナーポスト|壁|仕切り壁|開口枠|取っ手)|Wall)', name):
        return '壁'
    if name.startswith(('R_コンテナ扉', 'R_片開き扉')):
        return '扉'
    if name.startswith(('R_床スラブ', 'R_鉄板床', 'R_土間', 'R_上がり框')):
        return '床'
    if name.startswith('作業者'):
        return '人'
    if name.startswith(('荷_', '菌棒_', '菌床トレー', '菌床袋')):
        return '荷'
    return '設備'


# ---------------------------------------------------------------------------
# 評価済みメッシュを部品に分ける
# ---------------------------------------------------------------------------
dg = bpy.context.evaluated_depsgraph_get()
parts = defaultdict(list)  # 設備名 -> [(trimesh, color)]
for inst in dg.object_instances:
    ob = inst.object
    if ob.type != 'MESH':
        continue
    top = root_of(inst.parent.original if inst.is_instance else ob.original)
    if not top.visible_get() or top.name in label_objs:
        continue
    if top.name.startswith('荷_') and '菌棒' in ob.original.name:
        continue  # フィルムの中の菌棒は省略
    me = ob.data
    me.calc_loop_triangles()
    n = len(me.loop_triangles)
    if n == 0:
        continue
    co = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get('co', co)
    co = co.reshape(-1, 3)
    tri = np.empty(n * 3, dtype=np.int64)
    me.loop_triangles.foreach_get('vertices', tri)
    mi = np.empty(n, dtype=np.int64)
    me.loop_triangles.foreach_get('material_index', mi)
    m = np.array(inst.matrix_world)
    w = (co @ m[:3, :3].T + m[:3, 3]) * MM
    slots = [s.material for s in ob.material_slots]
    tri = tri.reshape(-1, 3)
    # マテリアルごとに分けてから、つながった部品に分ける (色が混ざらないように)
    for idx in np.unique(mi):
        mat = slots[idx] if idx < len(slots) else None
        mesh = trimesh.Trimesh(w, tri[mi == idx], process=True)
        for p in mesh.split(only_watertight=False):
            if np.linalg.norm(np.ptp(p.vertices, axis=0)) < MIN_SIZE:
                continue
            parts[top.name].append((p, mat_color(mat)))



def merge_touching(plist, tol=5.0):
    """同じ色で外接箱が接している部品をまとめる (まとめた部品は凸包になる)"""
    n = len(plist)
    lo = np.array([p.bounds[0] for p, _ in plist]) - tol
    hi = np.array([p.bounds[1] for p, _ in plist]) + tol
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(n):
        ov = np.all((lo[i] <= hi) & (lo <= hi[i]), axis=1)
        for j in np.nonzero(ov)[0]:
            if j > i and plist[i][1] == plist[j][1]:
                parent[find(i)] = find(j)
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    out = []
    for idx in groups.values():
        if len(idx) == 1:
            out.append(plist[idx[0]])
        else:
            v = np.vstack([plist[i][0].vertices for i in idx])
            out.append((trimesh.Trimesh(v, [], process=False), plist[idx[0]][1]))
    return out


for name in list(parts):
    if name.startswith(MERGE_TOUCHING):
        parts[name] = merge_touching(parts[name])

print(f'{len(parts)} 設備, 部品 {sum(len(v) for v in parts.values()):,}')


# ---------------------------------------------------------------------------
# 部品 → OCC ソリッド
# ---------------------------------------------------------------------------
def solid_from_triangles(verts, faces):
    sew = BRepBuilderAPI_Sewing(0.01)
    for f in faces:
        a, b, c = (verts[i] for i in f)
        if np.linalg.norm(np.cross(b - a, c - a)) < 1e-6:
            continue
        poly = BRepBuilderAPI_MakePolygon(gp_Pnt(*a), gp_Pnt(*b), gp_Pnt(*c), True)
        mf = BRepBuilderAPI_MakeFace(poly.Wire(), True)
        if mf.IsDone():
            sew.Add(mf.Face())
    sew.Perform()
    shape = sew.SewedShape()
    exp = TopExp_Explorer(shape, TopAbs_SHELL)
    if not exp.More():
        return None
    ms = BRepBuilderAPI_MakeSolid()
    while exp.More():
        ms.Add(TopoDS.Shell(exp.Current()))
        exp.Next()
    if not ms.IsDone():
        return None
    fix = ShapeFix_Solid(ms.Solid())
    fix.Perform()
    u = ShapeUpgrade_UnifySameDomain(fix.Solid(), True, True, True)
    u.Build()
    return u.Shape()


def fps(v, k):
    """外側の点から順に k 点を均等に選ぶ (円柱の分割数を減らす)"""
    idx = [int(np.argmax(np.linalg.norm(v - v.mean(0), axis=1)))]
    d = np.linalg.norm(v - v[idx[0]], axis=1)
    for _ in range(k - 1):
        i = int(np.argmax(d))
        idx.append(i)
        d = np.minimum(d, np.linalg.norm(v - v[i], axis=1))
    return v[idx]


def hull_of(p, cap):
    v = p.vertices
    try:
        h = trimesh.convex.convex_hull(v)
        if len(h.vertices) > cap:
            h = trimesh.convex.convex_hull(fps(h.vertices, cap))
        if h.volume > 1e-3:
            return h.vertices, h.faces
    except Exception:
        pass
    # 平らな部品 (紙・ステッカーなど) は 0.5mm の厚みを付ける
    nrm = p.face_normals.mean(axis=0) if len(p.faces) else np.array([0, 0, 1.0])
    if np.linalg.norm(nrm) < 1e-9:
        nrm = np.array([0, 0, 1.0])
    nrm = nrm / np.linalg.norm(nrm) * 0.5
    try:
        h = trimesh.convex.convex_hull(np.vstack([v, v + nrm]))
        return h.vertices, h.faces
    except Exception:
        return None


stats = Counter()


def part_solid(p, cap):
    convex = False
    if len(p.faces) and p.is_watertight:
        try:
            hv = p.convex_hull.volume
            convex = hv > 0 and abs(p.volume) / hv > 0.97
        except Exception:
            convex = False
    if len(p.faces) and p.is_watertight and not convex and len(p.faces) <= MAX_FACETED_TRIS:
        s = solid_from_triangles(p.vertices, p.faces)
        if s is not None and BRepCheck_Analyzer(s).IsValid():
            return s
        stats['凸包で代用'] += 1
    h = hull_of(p, cap)
    if h is None:
        stats['省略'] += 1
        return None
    s = solid_from_triangles(*h)
    if s is None or not BRepCheck_Analyzer(s).IsValid():
        stats['省略'] += 1
        return None
    return s


# ---------------------------------------------------------------------------
# STEP (XCAF アセンブリ) に書き出す
# ---------------------------------------------------------------------------
doc = TDocStd_Document(TCollection_ExtendedString('XmlOcaf'))
st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
root = st.NewShape()
TDataStd_Name.Set_s(root, TCollection_ExtendedString('レイアウト', True))

nsolid = 0
for i, (name, plist) in enumerate(sorted(parts.items())):
    by_color = defaultdict(list)
    cap = HULL_POINTS_LOAD if category(name) == '荷' or name.startswith('R_種トレー') else HULL_POINTS
    for p, col in plist:
        s = part_solid(p, cap)
        if s is not None:
            by_color[tuple(round(c, 3) for c in col)].append(s)
    if not by_color:
        continue
    asm = st.NewShape()
    TDataStd_Name.Set_s(asm, TCollection_ExtendedString(f'{category(name)}_{name}', True))
    for j, (col, solids) in enumerate(by_color.items()):
        comp = TopoDS_Compound()
        bb = BRep_Builder()
        bb.MakeCompound(comp)
        for s in solids:
            bb.Add(comp, s)
        nsolid += len(solids)
        lab = st.AddShape(comp, False)
        TDataStd_Name.Set_s(lab, TCollection_ExtendedString(f'{name}_色{j + 1}', True))
        ct.SetColor(lab, Quantity_Color(*col, Quantity_TOC_RGB), XCAFDoc_ColorSurf)
        st.AddComponent(asm, lab, TopLoc_Location())
    st.AddComponent(root, asm, TopLoc_Location())
    print(f'  [{i + 1}/{len(parts)}] {name}: {sum(len(v) for v in by_color.values())} ソリッド')
st.UpdateAssemblies()

Interface_Static.SetCVal_s('write.step.unit', 'MM')
Interface_Static.SetCVal_s('write.step.schema', 'AP214IS')
w = STEPCAFControl_Writer()
Interface_Static.SetIVal_s('write.surfacecurve.mode', 0)  # 平面だけなので補助曲線 (pcurve) は不要
w.SetNameMode(True)
w.SetColorMode(True)
w.Transfer(doc, STEPControl_AsIs)
assert w.Write(DST) == IFSelect_RetDone
print(f'書き出し: {DST} ({nsolid:,} ソリッド)', dict(stats))
