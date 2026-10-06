"""
layout_realistic*.blend をプレゼン用の 3D モデルに書き出すスクリプト。

使い方 (pip の bpy でも Blender 本体でも動きます):
    python export_presentation.py 入力.blend 出力フォルダ
    blender -b 入力.blend -P export_presentation.py -- 入力.blend 出力フォルダ

出力 (単位 m、色付き):
    layout_presentation.glb   PowerPoint の「挿入 → 3D モデル」、Keynote、Windows の 3D ビューアー、
                              Web ビューア、Twinmotion / Lumion / Unity など
    layout_presentation.fbx   3ds Max / SketchUp / Revit / Twinmotion / Lumion など
    layout_light.obj/.mtl     FreeCAD / Fusion 用の軽量メッシュ (単位 mm、Z 上向き、設備ごとに分割)

Blender のマテリアルはプロシージャル (ノードで模様を作る) なので、そのままでは他のソフトに
色が渡りません。書き出す前に、各マテリアルを「基本色・粗さ・金属・透明度」だけの単純な
マテリアルに置き換えます。また、パレット上の菌棒など三角形の多いものは間引いて軽くします。
"""
import os
import sys

import bpy

argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else sys.argv[1:]
SRC, OUT = os.path.abspath(argv[0]), os.path.abspath(argv[1])
os.makedirs(OUT, exist_ok=True)

bpy.ops.wm.open_mainfile(filepath=SRC)

# 間引く割合 (メッシュ名: 残す割合)。合計を 300 万 → 約 100 万三角形にする
DECIMATE = {'菌棒_積み': 0.35, '種菌カゴ': 0.3, '菌床トレー': 0.5, '菌棒': 0.5}

# 透けて見せたいもの (マテリアル名: 不透明度)
ALPHA = {'R_ガラス': 0.15, 'R_青アクリル': 0.45, 'R_ストレッチフィルム': 0.3}

# 色の調整 (diffuse_color が実際の見た目と違うもの)
COLOR = {}

# ---------------------------------------------------------------------------
# 不要なもの (照明・カメラ・非表示コレクション) を消す
# ---------------------------------------------------------------------------
for ob in list(bpy.data.objects):
    if ob.type in {'LIGHT', 'CAMERA', 'FONT'}:
        bpy.data.objects.remove(ob, do_unlink=True)
for cname in ('元レイアウト_箱モデル(非表示)', 'ラベル文字(非表示)'):
    c = bpy.data.collections.get(cname)
    if c:
        for ob in list(c.all_objects):
            bpy.data.objects.remove(ob, do_unlink=True)
        bpy.data.collections.remove(c)

# ---------------------------------------------------------------------------
# 重いメッシュを間引く
# ---------------------------------------------------------------------------
for name, ratio in DECIMATE.items():
    ob = bpy.data.objects.get(name)
    if ob is None or ob.type != 'MESH':
        continue
    m = ob.modifiers.new('プレゼン用_間引き', 'DECIMATE')
    m.decimate_type = 'COLLAPSE'
    m.ratio = ratio
    m.use_collapse_triangulate = True

# ---------------------------------------------------------------------------
# マテリアルを単純化 (基本色・粗さ・金属・透明度・発光)
# ---------------------------------------------------------------------------
def val(bsdf, name, default):
    s = bsdf.inputs.get(name)
    return s.default_value if s is not None else default


for mat in bpy.data.materials:
    if not mat.users or mat.node_tree is None:
        continue
    nt = mat.node_tree
    old = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if old is None:
        continue
    color = COLOR.get(mat.name)
    if color is None:
        if old.inputs['Base Color'].is_linked:
            color = tuple(mat.diffuse_color[:3])
        else:
            color = tuple(old.inputs['Base Color'].default_value[:3])
    rough = val(old, 'Roughness', 0.5) if not old.inputs['Roughness'].is_linked else 0.5
    metal = val(old, 'Metallic', 0.0)
    emis_col = tuple(val(old, 'Emission Color', (0, 0, 0, 1))[:3])
    emis_str = val(old, 'Emission Strength', 0.0)
    transmission = val(old, 'Transmission Weight', 0.0)

    nt.nodes.clear()
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    b = nt.nodes.new('ShaderNodeBsdfPrincipled')
    nt.links.new(b.outputs[0], out.inputs['Surface'])
    b.inputs['Base Color'].default_value = (*color, 1.0)
    b.inputs['Roughness'].default_value = rough
    b.inputs['Metallic'].default_value = metal
    if emis_str > 0 and max(emis_col) > 0:
        b.inputs['Emission Color'].default_value = (*emis_col, 1.0)
        b.inputs['Emission Strength'].default_value = min(emis_str, 1.0)
    alpha = ALPHA.get(mat.name)
    if alpha is None and transmission >= 0.9:
        alpha = 0.3
    if alpha is not None:
        b.inputs['Alpha'].default_value = alpha
        if hasattr(mat, 'surface_render_method'):
            mat.surface_render_method = 'BLENDED'
        else:
            mat.blend_method = 'BLEND'
    mat.diffuse_color = (*color, alpha if alpha is not None else 1.0)

# ---------------------------------------------------------------------------
# 書き出し
# ---------------------------------------------------------------------------
glb = os.path.join(OUT, 'layout_presentation.glb')
bpy.ops.export_scene.gltf(
    filepath=glb, export_format='GLB', use_visible=True, export_apply=True,
    export_cameras=False, export_lights=False, export_image_format='NONE',
    export_yup=True)
print('書き出し:', glb)

fbx = os.path.join(OUT, 'layout_presentation.fbx')
bpy.ops.export_scene.fbx(
    filepath=fbx, use_visible=True, use_mesh_modifiers=True, object_types={'MESH', 'EMPTY'},
    apply_scale_options='FBX_SCALE_ALL', axis_forward='-Z', axis_up='Y',
    path_mode='STRIP', bake_anim=False)
print('書き出し:', fbx)

# ---------------------------------------------------------------------------
# FreeCAD / Fusion 用の軽量 OBJ (単位 mm、Z 上向き、設備ごとのオブジェクト、色は MTL)
# ---------------------------------------------------------------------------
# 面取りを切り、フィルムの中の菌棒は省いて (フィルムは不透明にする)、さらに間引く
LIGHT_DECIMATE = {'種菌カゴ': 0.15, '菌床トレー': 0.3, '菌棒': 0.3}
for ob in bpy.data.objects:
    for m in ob.modifiers:
        if m.type == 'BEVEL':
            m.show_viewport = m.show_render = False
for name, ratio in LIGHT_DECIMATE.items():
    ob = bpy.data.objects.get(name)
    if ob is not None:
        m = ob.modifiers.get('プレゼン用_間引き') or ob.modifiers.new('プレゼン用_間引き', 'DECIMATE')
        m.ratio = ratio
ob = bpy.data.objects.get('菌棒_積み')
if ob is not None:
    bpy.data.objects.remove(ob, do_unlink=True)
film = bpy.data.materials.get('R_ストレッチフィルム')
if film is not None:
    b = next(n for n in film.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
    b.inputs['Alpha'].default_value = 1.0
    film.diffuse_color = (*film.diffuse_color[:3], 1.0)

obj = os.path.join(OUT, 'layout_light.obj')
bpy.ops.wm.obj_export(
    filepath=obj, export_eval_mode='DAG_EVAL_RENDER', apply_modifiers=True,
    global_scale=1000.0, forward_axis='Y', up_axis='Z',
    export_materials=True, export_uv=False, export_normals=False,
    export_object_groups=False, path_mode='STRIP')
print('書き出し:', obj)
