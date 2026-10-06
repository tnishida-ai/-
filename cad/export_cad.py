"""
layout_realistic*.blend を CAD 用データに書き出すスクリプト。

使い方 (pip の bpy でも Blender 本体でも動きます):
    python export_cad.py 入力.blend 出力フォルダ [--stl]
    blender -b 入力.blend -P export_cad.py -- 入力.blend 出力フォルダ

必要なもの: bpy (Blender), ezdxf, shapely, cadquery-ocp (STEP 用。無ければ STEP は省略)

出力 (単位はすべて mm、Z 上向き、原点は Blender の原点):
    layout_plan.dxf       2D 平面図 (設備ごとに画層分け・名称文字付き) AutoCAD / BricsCAD / Fusion 等
    layout_plan_R12.dxf   同じ平面図を R12 形式・Shift-JIS で (Jw_cad などの古い DXF 読み込み用)
    layout_3d.obj/.mtl    3D 詳細モデル (オブジェクトごとのグループ・色付き) Rhino / Fusion / SketchUp 等
    layout_3d.stl         3D 詳細モデル (単一メッシュ。--stl を付けたときだけ。約150MB)
    layout_3d.glb         3D 詳細モデル (glTF バイナリ)
    layout_blocks.step    簡易ソリッドモデル (各設備を平面形状×高さで押し出した立体)
                          SolidWorks / Inventor / Fusion / FreeCAD などで編集可能
"""
import os
import re
import sys
from collections import defaultdict

import bpy
import numpy as np
from shapely import union_all
from shapely.geometry import MultiPolygon, Polygon

MM = 1000.0

argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else sys.argv[1:]
SRC, OUT = [a for a in argv if not a.startswith('--')][:2]
SRC, OUT = os.path.abspath(argv[0]), os.path.abspath(argv[1])
os.makedirs(OUT, exist_ok=True)

bpy.ops.wm.open_mainfile(filepath=SRC)
scene = bpy.context.scene

LABEL_COLL = '名称ラベル'
label_objs = {o.name for o in bpy.data.collections[LABEL_COLL].all_objects} \
    if LABEL_COLL in bpy.data.collections else set()

# ---------------------------------------------------------------------------
# 分類 (画層名・色)
# ---------------------------------------------------------------------------
LAYERS = {  # 画層名: (ACI 色, 説明)
    '壁': (8, 'コンテナ外周壁・仕切り壁'),
    '扉': (30, '扉'),
    '床': (9, '床・土間'),
    '設備': (5, '接種機・コンベヤ・作業台など'),
    '荷': (34, 'パレット上の荷・トレー・菌床'),
    '人': (3, '作業者'),
    '名称ラベル': (1, '元モデルの名称ラベル (文字形状)'),
    '名称': (7, '名称文字 (編集可能な TEXT)'),
    '寸法': (2, '全体寸法'),
}


def base_name(name):
    n = re.sub(r'\.\d+$', '', name)
    n = re.sub(r'_\d+$', '', n)
    for p in ('R_', '荷_'):
        if n.startswith(p):
            n = n[len(p):]
    return n


def classify(name):
    if name in label_objs:
        return '名称ラベル'
    n = name
    if re.match(r'^(R_(波板|レール|コーナーポスト|壁|仕切り壁|開口枠|取っ手)|Wall)', n):
        return '壁'
    if n.startswith(('R_コンテナ扉', 'R_片開き扉')):
        return '扉'
    if n.startswith(('R_床スラブ', 'R_鉄板床', 'R_土間', 'R_上がり框')):
        return '床'
    if n.startswith('作業者'):
        return '人'
    if n.startswith(('荷_', '菌棒_', '菌床トレー', '菌床袋')):
        return '荷'
    return '設備'


# 平面図に名称文字を入れる設備 (数が多い荷・人は入れない)
def display_name(name):
    b = base_name(name)
    return {'棚_ラック1': '棚 (菌床ラック)', 'CCP': 'CCP'}.get(b, b)


# ---------------------------------------------------------------------------
# 評価済みメッシュを最上位オブジェクトごとに集める
# ---------------------------------------------------------------------------
def root_of(obj):
    while obj.parent is not None:
        obj = obj.parent
    return obj


dg = bpy.context.evaluated_depsgraph_get()
groups = defaultdict(list)  # 最上位オブジェクト名 -> [(3,3) 三角形配列]
label_glyphs = defaultdict(list)  # 名称ラベルの文字部分だけの三角形
for inst in dg.object_instances:
    ob = inst.object
    if ob.type != 'MESH':
        continue
    top = root_of(inst.parent.original if inst.is_instance else ob.original)
    if not top.visible_get():
        continue
    me = ob.data
    me.calc_loop_triangles()
    n = len(me.loop_triangles)
    if n == 0:
        continue
    co = np.empty(len(me.vertices) * 3, dtype=np.float64)
    me.vertices.foreach_get('co', co)
    co = co.reshape(-1, 3)
    tri = np.empty(n * 3, dtype=np.int64)
    me.loop_triangles.foreach_get('vertices', tri)
    m = np.array(inst.matrix_world, dtype=np.float64)
    w = co @ m[:3, :3].T + m[:3, 3]
    tw = w[tri.reshape(-1, 3)] * MM
    groups[top.name].append(tw)
    if top.name in label_objs:
        mi = np.empty(n, dtype=np.int64)
        me.loop_triangles.foreach_get('material_index', mi)
        names = [s.material.name if s.material else '' for s in ob.material_slots]
        is_text = np.array(['文字' in names[i] if i < len(names) else False for i in mi], dtype=bool)
        label_glyphs[top.name].append(tw[is_text])

groups = {k: np.concatenate(v) for k, v in groups.items()}
label_glyphs = {k: np.concatenate(v) for k, v in label_glyphs.items()}
print(f'{len(groups)} グループ, 三角形 {sum(len(v) for v in groups.values()):,}')


# ---------------------------------------------------------------------------
# 上から見た外形 (三角形の XY 投影の和集合)
# ---------------------------------------------------------------------------
def footprint(tris, tol=1.0):
    xy = tris[:, :, :2]
    a = xy[:, 1] - xy[:, 0]
    b = xy[:, 2] - xy[:, 0]
    area = 0.5 * np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0])
    xy = xy[area > 0.5]  # 鉛直面 (面積ほぼ 0) は除く
    if len(xy) == 0:
        return None
    polys = [Polygon(t) for t in np.round(xy, 3)]
    g = union_all(polys).buffer(0.05).buffer(-0.05)
    return g.simplify(tol)


def polygons(g):
    if g is None or g.is_empty:
        return []
    if isinstance(g, Polygon):
        return [g]
    if isinstance(g, MultiPolygon):
        return list(g.geoms)
    return [p for p in getattr(g, 'geoms', []) if isinstance(p, Polygon)]


SIMPLIFY = {'荷': 10.0, '人': 5.0, '名称ラベル': 0.3}
plan = {}
glyphs = {k: footprint(v, tol=0.2) for k, v in label_glyphs.items()}
for name, tris in groups.items():
    fp = footprint(tris, tol=SIMPLIFY.get(classify(name), 1.0))
    z = tris[:, :, 2]
    plan[name] = (fp, float(z.min()), float(z.max()))
    print(f'  外形: {name}')

# ---------------------------------------------------------------------------
# DXF (2D 平面図)
# ---------------------------------------------------------------------------
import ezdxf
from ezdxf.enums import TextEntityAlignment


def write_dxf(path, version, encoding=None):
    doc = ezdxf.new(version, setup=True, units=4)  # 4 = mm
    if encoding:
        doc.encoding = encoding
        doc.header['$DWGCODEPAGE'] = 'ANSI_932'
    doc.header['$INSUNITS'] = 4
    doc.header['$MEASUREMENT'] = 1
    doc.styles.add('JP', font='msgothic.ttc')
    for lname, (col, desc) in LAYERS.items():
        lay = doc.layers.add(lname, color=col)
        if version != 'R12':
            lay.description = desc
    msp = doc.modelspace()
    r12 = version == 'R12'

    def add_ring(coords, layer):
        pts = [(round(x, 2), round(y, 2)) for x, y in coords[:-1]]
        if len(pts) < 2:
            return
        if r12:
            msp.add_polyline2d(pts, close=True, dxfattribs={'layer': layer})
        else:
            msp.add_lwpolyline(pts, close=True, dxfattribs={'layer': layer})

    order = ['床', '壁', '扉', '設備', '荷', '人', '名称ラベル']
    for name in sorted(plan, key=lambda n: order.index(classify(n))):
        fp, zmin, zmax = plan[name]
        layer = classify(name)
        for p in polygons(fp):
            add_ring(p.exterior.coords, layer)
            for hole in p.interiors:
                add_ring(hole.coords, layer)
        if layer == '名称ラベル':  # 板は外形線、文字は輪郭線 + 塗りつぶし
            for p in polygons(glyphs.get(name)):
                add_ring(p.exterior.coords, layer)
                for hole in p.interiors:
                    add_ring(hole.coords, layer)
                if not r12:
                    h = msp.add_hatch(color=1, dxfattribs={'layer': layer})
                    h.paths.add_polyline_path(list(p.exterior.coords)[:-1], is_closed=True)
                    for hole in p.interiors:
                        h.paths.add_polyline_path(list(hole.coords)[:-1], is_closed=True)
        # 名称ラベルが付いていない設備にだけ文字を入れる (ラベルと重ならないように)
        if layer == '設備' and not {f'ラベル_{name}', f'ラベル_荷_{name[2:]}'} & label_objs and fp is not None and not fp.is_empty:
            c = fp.representative_point() if not fp.centroid.within(fp) else fp.centroid
            t = msp.add_text(display_name(name), height=120,
                             dxfattribs={'layer': '名称', 'style': 'JP'})
            t.set_placement((c.x, c.y), align=TextEntityAlignment.MIDDLE_CENTER)

    # 全体寸法
    allg = union_all([plan[n][0] for n in plan if classify(n) == '壁' and plan[n][0] is not None])
    x0, y0, x1, y1 = allg.bounds
    da = {'layer': '寸法'}
    if r12:
        msp.add_line((x0, y0 - 600), (x1, y0 - 600), dxfattribs=da)
        msp.add_line((x0 - 600, y0), (x0 - 600, y1), dxfattribs=da)
        msp.add_text(f'{x1 - x0:.0f}', height=200, dxfattribs={**da, 'style': 'JP'}).set_placement(
            ((x0 + x1) / 2, y0 - 550), align=TextEntityAlignment.BOTTOM_CENTER)
        msp.add_text(f'{y1 - y0:.0f}', height=200, dxfattribs={**da, 'style': 'JP', 'rotation': 90}).set_placement(
            (x0 - 650, (y0 + y1) / 2), align=TextEntityAlignment.BOTTOM_CENTER)
    else:
        ds = doc.dimstyles.get('EZDXF')
        ds.dxf.dimtxt = 200
        ds.dxf.dimasz = 150
        ds.dxf.dimexe = 100
        ds.dxf.dimexo = 100
        ds.dxf.dimdec = 0
        ds.dxf.dimlfac = 1  # ezdxf 既定の EZDXF スタイルは m→cm 用に 100 倍になっている
        msp.add_linear_dim(base=(x0, y0 - 600), p1=(x0, y0), p2=(x1, y0), dimstyle='EZDXF',
                           dxfattribs=da).render()
        msp.add_linear_dim(base=(x0 - 600, y0), p1=(x0, y0), p2=(x0, y1), angle=90,
                           dimstyle='EZDXF', dxfattribs=da).render()
    doc.saveas(path)
    print('書き出し:', path)


write_dxf(os.path.join(OUT, 'layout_plan.dxf'), 'R2013')
write_dxf(os.path.join(OUT, 'layout_plan_R12.dxf'), 'R12', encoding='cp932')

# ---------------------------------------------------------------------------
# 3D メッシュ (Blender 標準の書き出し機能)
# ---------------------------------------------------------------------------
# 名称ラベル・照明・カメラ・非表示のものを除いた状態で書き出す
for ob in list(bpy.data.objects):
    if ob.name in label_objs or ob.type in {'LIGHT', 'CAMERA', 'FONT'}:
        bpy.data.objects.remove(ob, do_unlink=True)

bpy.ops.object.select_all(action='DESELECT')
common = dict(apply_modifiers=True, global_scale=MM, forward_axis='Y', up_axis='Z')
bpy.ops.wm.obj_export(filepath=os.path.join(OUT, 'layout_3d.obj'), export_eval_mode='DAG_EVAL_RENDER',
                      export_materials=True, export_uv=False, export_normals=True,
                      export_object_groups=True, path_mode='STRIP', **common)
if '--stl' in argv:  # 150MB 近くになるので指定したときだけ
    bpy.ops.wm.stl_export(filepath=os.path.join(OUT, 'layout_3d.stl'), ascii_format=False, **common)
bpy.ops.export_scene.gltf(filepath=os.path.join(OUT, 'layout_3d.glb'), export_format='GLB',
                          use_visible=True, export_apply=True, export_cameras=False,
                          export_lights=False, export_image_format='NONE')

# ---------------------------------------------------------------------------
# STEP (簡易ソリッド: 平面形状 × 高さ)
# ---------------------------------------------------------------------------
try:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.BRep import BRep_Builder
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopoDS import TopoDS_Compound
    from OCP.XCAFDoc import XCAFDoc_ColorSurf, XCAFDoc_DocumentTool
    from OCP.Interface import Interface_Static
except ImportError:
    print('cadquery-ocp が無いため STEP は省略')
    sys.exit(0)

RGB = {'壁': (0.85, 0.85, 0.82), '扉': (0.95, 0.95, 0.95), '床': (0.6, 0.6, 0.6),
       '設備': (0.55, 0.65, 0.75), '荷': (0.55, 0.4, 0.25), '人': (0.95, 0.95, 0.95)}


def wire_of(coords, z):
    mp = BRepBuilderAPI_MakePolygon()
    for x, y in coords[:-1]:
        mp.Add(gp_Pnt(x, y, z))
    mp.Close()
    return mp.Wire()


def prism(poly, z0, z1):
    p = poly.simplify(5.0)
    if p.is_empty or p.area < 25 or not isinstance(p, Polygon):
        return None
    p = p.buffer(0)
    if not isinstance(p, Polygon):
        return None
    mf = BRepBuilderAPI_MakeFace(wire_of(list(p.exterior.coords), z0), True)
    for h in p.interiors:
        hw = wire_of(list(h.coords)[::-1], z0)
        mf.Add(hw)
    if not mf.IsDone():
        return None
    return BRepPrimAPI_MakePrism(mf.Face(), gp_Vec(0, 0, max(z1 - z0, 1.0))).Shape()


doc = TDocStd_Document(TCollection_ExtendedString('XmlOcaf'))
st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
count = 0
for name, (fp, zmin, zmax) in plan.items():
    layer = classify(name)
    if layer == '名称ラベル':
        continue
    comp = TopoDS_Compound()
    bb = BRep_Builder()
    bb.MakeCompound(comp)
    ok = False
    for p in polygons(fp):
        s = prism(p, zmin, zmax)
        if s is not None:
            bb.Add(comp, s)
            ok = True
    if not ok:
        continue
    lab = st.AddShape(comp, False)
    TDataStd_Name.Set_s(lab, TCollection_ExtendedString(f'{layer}_{name}', True))
    ct.SetColor(lab, Quantity_Color(*RGB[layer], Quantity_TOC_RGB), XCAFDoc_ColorSurf)
    count += 1

Interface_Static.SetCVal_s('write.step.unit', 'MM')
Interface_Static.SetCVal_s('write.step.schema', 'AP214IS')
w = STEPCAFControl_Writer()
w.SetNameMode(True)
w.SetColorMode(True)
w.Transfer(doc, STEPControl_AsIs)
path = os.path.join(OUT, 'layout_blocks.step')
assert w.Write(path) == IFSelect_RetDone
print(f'書き出し: {path} ({count} 部品)')
