"""
レイアウト模型 (箱モデル) をリアルなシーンに変換するスクリプト。

使い方 (Blender 4.2 以降 / 5.x):
    blender -b 元ファイル.blend -P make_realistic.py -- 出力.blend
または Blender の Scripting タブで元ファイルを開いた状態で実行。

- 元の配置・寸法はそのまま使い、各設備をディテールのある形状に置き換える
- 元の箱オブジェクトとラベル文字は非表示コレクションに移動して残す
- マテリアルはすべてプロシージャル (外部画像テクスチャ不要)
"""
import bpy
import bmesh
import math
import random
import sys
from mathutils import Matrix, Vector

random.seed(7)

# ---------------------------------------------------------------------------
# 基本ユーティリティ
# ---------------------------------------------------------------------------

def set_input(node, names, value):
    """Blender のバージョン差を吸収してソケットに値を入れる"""
    if isinstance(names, str):
        names = [names]
    for n in names:
        if n in node.inputs:
            node.inputs[n].default_value = value
            return True
    return False


def new_material(name, color=(0.8, 0.8, 0.8), rough=0.5, metal=0.0, **kw):
    mat = bpy.data.materials.get(name)
    if mat:
        bpy.data.materials.remove(mat)
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = next(n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED')
    set_input(bsdf, 'Base Color', (*color, 1.0))
    set_input(bsdf, 'Roughness', rough)
    set_input(bsdf, 'Metallic', metal)
    table = {
        'coat': ['Coat Weight', 'Clearcoat'],
        'coat_rough': ['Coat Roughness', 'Clearcoat Roughness'],
        'transmission': ['Transmission Weight', 'Transmission'],
        'ior': ['IOR'],
        'aniso': ['Anisotropic'],
        'sheen': ['Sheen Weight', 'Sheen'],
        'sss': ['Subsurface Weight', 'Subsurface'],
        'spec': ['Specular IOR Level', 'Specular'],
        'alpha': ['Alpha'],
    }
    for k, v in kw.items():
        if k == 'emission':
            col, strength = v
            set_input(bsdf, ['Emission Color', 'Emission'], (*col, 1.0))
            set_input(bsdf, 'Emission Strength', strength)
        elif k in table:
            set_input(bsdf, table[k], v)
    mat.diffuse_color = (*color, 1.0)
    return mat, nt, bsdf


def tex_coord(nt, kind='Object'):
    tc = nt.nodes.new('ShaderNodeTexCoord')
    return tc.outputs[kind]


def noise(nt, vec, scale, detail=4.0, rough=0.5, stretch=None):
    n = nt.nodes.new('ShaderNodeTexNoise')
    n.inputs['Scale'].default_value = scale
    n.inputs['Detail'].default_value = detail
    n.inputs['Roughness'].default_value = rough
    if stretch is not None:
        mp = nt.nodes.new('ShaderNodeMapping')
        mp.inputs['Scale'].default_value = stretch
        nt.links.new(vec, mp.inputs['Vector'])
        vec = mp.outputs['Vector']
    nt.links.new(vec, n.inputs['Vector'])
    return n.outputs['Fac']


def ramp(nt, fac, stops):
    r = nt.nodes.new('ShaderNodeValToRGB')
    els = r.color_ramp.elements
    els[0].position, els[0].color = stops[0][0], (*stops[0][1], 1)
    els[1].position, els[1].color = stops[-1][0], (*stops[-1][1], 1)
    for pos, col in stops[1:-1]:
        e = els.new(pos)
        e.color = (*col, 1)
    nt.links.new(fac, r.inputs['Fac'])
    return r.outputs['Color']


def map_range(nt, val, a, b, c, d):
    m = nt.nodes.new('ShaderNodeMapRange')
    m.inputs['From Min'].default_value = a
    m.inputs['From Max'].default_value = b
    m.inputs['To Min'].default_value = c
    m.inputs['To Max'].default_value = d
    nt.links.new(val, m.inputs['Value'])
    return m.outputs['Result']


def bump(nt, bsdf, height, strength=0.1, dist=0.01):
    b = nt.nodes.new('ShaderNodeBump')
    b.inputs['Strength'].default_value = strength
    b.inputs['Distance'].default_value = dist
    nt.links.new(height, b.inputs['Height'])
    nt.links.new(b.outputs['Normal'], bsdf.inputs['Normal'])
    return b


def math_node(nt, op, a, b=None):
    m = nt.nodes.new('ShaderNodeMath')
    m.operation = op
    for i, v in enumerate((a, b)):
        if v is None:
            continue
        if isinstance(v, (int, float)):
            m.inputs[i].default_value = v
        else:
            nt.links.new(v, m.inputs[i])
    return m.outputs[0]


# ---------------------------------------------------------------------------
# マテリアル
# ---------------------------------------------------------------------------

M = {}


def build_materials():
    # エポキシ塗床 (クリーンルームでよく使われる明るいグレーグリーン)
    mat, nt, b = new_material('R_エポキシ床', rough=0.2, coat=0.6, coat_rough=0.08)
    v = tex_coord(nt)
    col = ramp(nt, noise(nt, v, 1.5, 6, 0.6), [(0.35, (0.50, 0.55, 0.54)), (0.65, (0.58, 0.63, 0.61))])
    speck = ramp(nt, noise(nt, v, 400, 2, 0.5), [(0.62, (0, 0, 0)), (0.66, (1, 1, 1))])
    mix = nt.nodes.new('ShaderNodeMix'); mix.data_type = 'RGBA'
    mix.inputs['Factor'].default_value = 0.12
    nt.links.new(col, mix.inputs[6]); nt.links.new(speck, mix.inputs[7])
    nt.links.new(mix.outputs[2], b.inputs['Base Color'])
    nt.links.new(map_range(nt, noise(nt, v, 0.8, 5, 0.6), 0.3, 0.7, 0.12, 0.38), b.inputs['Roughness'])
    bump(nt, b, noise(nt, v, 60, 3), 0.05)
    M['floor'] = mat

    # 前室の緑色長尺シート
    mat, nt, b = new_material('R_長尺シート_緑', (0.09, 0.30, 0.16), 0.45)
    v = tex_coord(nt)
    nt.links.new(ramp(nt, noise(nt, v, 4, 6), [(0.3, (0.08, 0.27, 0.14)), (0.7, (0.11, 0.34, 0.19))]), b.inputs['Base Color'])
    bump(nt, b, noise(nt, v, 300, 2), 0.08)
    M['vinyl'] = mat

    # クリーンルーム用サンドイッチパネル壁 (白、1200mm ピッチの目地)
    mat, nt, b = new_material('R_パネル壁', (0.86, 0.87, 0.86), 0.32)
    geo = nt.nodes.new('ShaderNodeNewGeometry')
    sep = nt.nodes.new('ShaderNodeSeparateXYZ')
    nt.links.new(geo.outputs['Position'], sep.inputs[0])
    masks = []
    for axis in ('X', 'Y'):
        f = math_node(nt, 'FRACT', math_node(nt, 'DIVIDE', sep.outputs[axis], 1.2))
        d = math_node(nt, 'MINIMUM', f, math_node(nt, 'SUBTRACT', 1.0, f))
        masks.append(map_range(nt, math_node(nt, 'MULTIPLY', d, 1.2), 0.0, 0.004, 1.0, 0.0))
    seam = math_node(nt, 'MAXIMUM', masks[0], masks[1])
    nt.links.new(ramp(nt, seam, [(0.0, (0.86, 0.87, 0.86)), (1.0, (0.45, 0.46, 0.46))]), b.inputs['Base Color'])
    bump(nt, b, math_node(nt, 'SUBTRACT', 1.0, seam), 0.3, 0.003)
    M['wall'] = mat

    # 扉 (焼付塗装鋼板)
    M['door'] = new_material('R_扉', (0.62, 0.66, 0.68), 0.35)[0]

    # ヘアラインステンレス
    mat, nt, b = new_material('R_ステンレス', (0.78, 0.78, 0.77), 0.25, 1.0, aniso=0.6)
    v = tex_coord(nt)
    hair = noise(nt, v, 3, 8, 0.7, stretch=(1, 1, 1))
    hair = noise(nt, v, 40, 4, 0.6, stretch=(1, 60, 1))
    nt.links.new(map_range(nt, hair, 0.3, 0.7, 0.18, 0.34), b.inputs['Roughness'])
    bump(nt, b, hair, 0.02)
    M['steel'] = mat

    # アルミフレーム
    M['alu'] = new_material('R_アルミフレーム', (0.85, 0.86, 0.87), 0.35, 1.0)[0]

    # 透明板 (ガラス / アクリル)
    M['glass'] = new_material('R_ガラス', (0.95, 0.97, 0.97), 0.02, transmission=1.0, ior=1.5)[0]
    M['acrylic'] = new_material('R_アクリル', (0.92, 0.95, 0.95), 0.05, transmission=1.0, ior=1.49)[0]

    # 青色樹脂パレット
    mat, nt, b = new_material('R_樹脂パレット', (0.03, 0.17, 0.52), 0.5)
    v = tex_coord(nt)
    nt.links.new(ramp(nt, noise(nt, v, 6, 4), [(0.3, (0.025, 0.14, 0.45)), (0.7, (0.04, 0.19, 0.56))]), b.inputs['Base Color'])
    bump(nt, b, noise(nt, v, 150, 2), 0.05)
    M['pallet'] = mat

    # 培養ビン用コンテナ (紺)
    M['crate'] = new_material('R_コンテナ', (0.03, 0.06, 0.16), 0.45)[0]
    # PP 培養ビン (半透明)
    M['bottle'] = new_material('R_培養ビン', (0.85, 0.83, 0.75), 0.25, sss=0.3, transmission=0.3)[0]
    # ビンのキャップ
    M['cap'] = new_material('R_キャップ', (0.92, 0.92, 0.9), 0.4)[0]
    # フィルター部 (キャップ中央)
    M['filter'] = new_material('R_フィルター', (0.75, 0.72, 0.62), 0.9)[0]

    # 種菌 (おが粉培地)
    mat, nt, b = new_material('R_種菌', (0.33, 0.2, 0.09), 0.95)
    v = tex_coord(nt)
    n = noise(nt, v, 120, 6, 0.7)
    nt.links.new(ramp(nt, n, [(0.3, (0.22, 0.12, 0.05)), (0.55, (0.38, 0.24, 0.11)), (0.8, (0.55, 0.42, 0.25))]), b.inputs['Base Color'])
    bump(nt, b, n, 0.6, 0.005)
    M['spawn'] = mat
    M['tray'] = new_material('R_トレー樹脂', (0.82, 0.8, 0.72), 0.35, sss=0.2, transmission=0.4)[0]

    # 段ボール
    mat, nt, b = new_material('R_段ボール', (0.45, 0.30, 0.17), 0.85)
    v = tex_coord(nt)
    nt.links.new(ramp(nt, noise(nt, v, 8, 5), [(0.3, (0.40, 0.26, 0.14)), (0.7, (0.50, 0.34, 0.19))]), b.inputs['Base Color'])
    bump(nt, b, noise(nt, v, 200, 2), 0.1)
    M['card'] = mat
    M['tape'] = new_material('R_OPPテープ', (0.6, 0.45, 0.25), 0.2)[0]

    # 塗装鋼板 (機械の差し色: 元モデルのティール色を継承)
    M['teal'] = new_material('R_塗装_ティール', (0.0, 0.33, 0.33), 0.35, coat=0.3)[0]
    M['gray_paint'] = new_material('R_塗装_グレー', (0.66, 0.68, 0.66), 0.4)[0]
    M['rack_blue'] = new_material('R_棚_支柱', (0.04, 0.12, 0.35), 0.4)[0]
    M['rack_orange'] = new_material('R_棚_ビーム', (0.85, 0.35, 0.05), 0.4)[0]
    M['galv'] = new_material('R_亜鉛メッキ', (0.7, 0.72, 0.72), 0.45, 1.0)[0]
    M['white_panel'] = new_material('R_白塗装', (0.9, 0.9, 0.88), 0.3)[0]
    M['rubber'] = new_material('R_ゴム', (0.02, 0.02, 0.02), 0.7)[0]
    M['black_plastic'] = new_material('R_黒樹脂', (0.015, 0.015, 0.018), 0.35)[0]
    M['belt'] = new_material('R_ベルト', (0.05, 0.25, 0.12), 0.55)[0]

    # 発光系
    M['screen'] = new_material('R_タッチパネル', (0.02, 0.02, 0.03), 0.1, emission=((0.35, 0.6, 1.0), 2.5))[0]
    M['lamp_g'] = new_material('R_表示灯_緑', (0.1, 0.8, 0.2), 0.2, emission=((0.1, 1.0, 0.25), 6.0), transmission=0.5)[0]
    M['lamp_y'] = new_material('R_表示灯_黄', (0.8, 0.6, 0.05), 0.2, transmission=0.5)[0]
    M['lamp_r'] = new_material('R_表示灯_赤', (0.7, 0.05, 0.03), 0.2, transmission=0.5)[0]
    M['led'] = new_material('R_LED', (1, 1, 1), 0.2, emission=((1.0, 0.97, 0.92), 8.0))[0]

    # 防塵服 (タイベック風)
    mat, nt, b = new_material('R_防塵服', (0.88, 0.89, 0.91), 0.75, sheen=0.4)
    v = tex_coord(nt)
    bump(nt, b, noise(nt, v, 90, 5, 0.65), 0.15)
    M['suit'] = mat
    M['mask'] = new_material('R_マスク', (0.55, 0.72, 0.88), 0.8)[0]
    M['goggle'] = new_material('R_ゴーグル', (0.02, 0.03, 0.04), 0.05, coat=1.0)[0]
    M['glove'] = new_material('R_ニトリル手袋', (0.12, 0.3, 0.8), 0.45)[0]
    M['boot'] = new_material('R_長靴', (0.8, 0.82, 0.83), 0.4)[0]

    # 屋外のコンクリート土間
    mat, nt, b = new_material('R_コンクリート', (0.4, 0.4, 0.38), 0.85)
    v = tex_coord(nt, 'Object')
    n1 = noise(nt, v, 0.4, 6, 0.6)
    nt.links.new(ramp(nt, n1, [(0.3, (0.30, 0.30, 0.29)), (0.7, (0.42, 0.42, 0.40))]), b.inputs['Base Color'])
    bump(nt, b, noise(nt, v, 50, 6, 0.6), 0.15)
    M['concrete'] = mat

    mat, nt, b = new_material('R_アスファルト', (0.06, 0.06, 0.06), 0.9)
    v = tex_coord(nt, 'Object')
    n1 = noise(nt, v, 300, 3, 0.5)
    nt.links.new(ramp(nt, n1, [(0.35, (0.035, 0.035, 0.035)), (0.65, (0.11, 0.11, 0.105))]), b.inputs['Base Color'])
    nt.links.new(ramp(nt, noise(nt, v, 0.15, 4), [(0.4, (0.85, 0.85, 0.85)), (0.6, (0.6, 0.6, 0.6))]), b.inputs['Roughness'])
    bump(nt, b, n1, 0.4, 0.005)
    M['asphalt'] = mat
    mat, nt, b = new_material('R_芝生', (0.12, 0.22, 0.05), 0.9, sheen=0.3)
    v = tex_coord(nt, 'Object')
    n1 = noise(nt, v, 0.08, 5, 0.6)
    n2 = noise(nt, v, 900, 2, 0.5)
    c1 = ramp(nt, n1, [(0.35, (0.07, 0.14, 0.03)), (0.55, (0.14, 0.24, 0.05)), (0.75, (0.22, 0.27, 0.09))])
    mix = nt.nodes.new('ShaderNodeMix'); mix.data_type = 'RGBA'; mix.blend_type = 'MULTIPLY'
    mix.inputs['Factor'].default_value = 0.5
    nt.links.new(c1, mix.inputs[6])
    nt.links.new(ramp(nt, n2, [(0.3, (0.5, 0.5, 0.5)), (0.7, (1, 1, 1))]), mix.inputs[7])
    nt.links.new(mix.outputs[2], b.inputs['Base Color'])
    bump(nt, b, n2, 0.6, 0.01)
    M['grass'] = mat
    M['line_paint'] = new_material('R_区画線', (0.8, 0.8, 0.78), 0.7)[0]


# ---------------------------------------------------------------------------
# メッシュビルダー (複数のパーツを 1 メッシュにまとめる)
# ---------------------------------------------------------------------------

class MB:
    def __init__(self):
        self.bm = bmesh.new()
        self.mats = []

    def _mi(self, mat):
        if mat not in self.mats:
            self.mats.append(mat)
        return self.mats.index(mat)

    def _assign(self, verts, mat, smooth=False):
        idx = self._mi(mat)
        faces = set()
        for v in verts:
            faces.update(v.link_faces)
        for f in faces:
            f.material_index = idx
            f.smooth = smooth

    def box(self, center, size, mat, rz=0.0):
        m = (Matrix.Translation(center) @ Matrix.Rotation(rz, 4, 'Z')
             @ Matrix.Diagonal((size[0], size[1], size[2], 1)))
        r = bmesh.ops.create_cube(self.bm, size=1.0, matrix=m)
        self._assign(r['verts'], mat)

    def box_mm(self, mn, mx, mat):
        """最小点・最大点で箱を作る"""
        c = [(a + b) / 2 for a, b in zip(mn, mx)]
        s = [abs(b - a) for a, b in zip(mn, mx)]
        self.box(c, s, mat)

    def cyl(self, p0, p1, r, mat, segs=16, r2=None, smooth=True):
        p0, p1 = Vector(p0), Vector(p1)
        d = p1 - p0
        rot = d.to_track_quat('Z', 'Y').to_matrix().to_4x4()
        m = Matrix.Translation((p0 + p1) / 2) @ rot
        res = bmesh.ops.create_cone(self.bm, cap_ends=True, segments=segs,
                                    radius1=r, radius2=r if r2 is None else r2,
                                    depth=d.length, matrix=m)
        self._assign(res['verts'], mat, smooth)

    def sphere(self, c, scale, mat, segs=16, rings=10):
        m = Matrix.Translation(c) @ Matrix.Diagonal((*scale, 1))
        res = bmesh.ops.create_uvsphere(self.bm, u_segments=segs, v_segments=rings,
                                        radius=1.0, matrix=m)
        self._assign(res['verts'], mat, True)

    def build(self, name, coll, bevel=0.0, loc=(0, 0, 0), rz=0.0):
        me = bpy.data.meshes.new(name)
        self.bm.to_mesh(me)
        self.bm.free()
        for m in self.mats:
            me.materials.append(m)
        ob = bpy.data.objects.new(name, me)
        ob.location = loc
        ob.rotation_euler[2] = rz
        coll.objects.link(ob)
        if bevel > 0:
            bv = ob.modifiers.new('Bevel', 'BEVEL')
            bv.width = bevel
            bv.segments = 2
            bv.limit_method = 'ANGLE'
            bv.harden_normals = False
        return ob


def shrink_mesh(ob, eps):
    """重なった箱同士の同一平面 (Cycles で黒く抜ける) を避けるため、各頂点を中心側へ eps だけ寄せる"""
    me = ob.data
    if not me.vertices:
        return
    c = sum((v.co for v in me.vertices), Vector()) / len(me.vertices)
    sc = ob.matrix_world.to_scale()
    for v in me.vertices:
        d = v.co - c
        v.co = Vector([v.co[i] - math.copysign(eps / max(abs(sc[i]), 1e-6), d[i]) if abs(d[i]) > 1e-5 else v.co[i]
                       for i in range(3)])


def instance(asset_coll, name, coll, loc, rz=0.0):
    e = bpy.data.objects.new(name, None)
    e.instance_type = 'COLLECTION'
    e.instance_collection = asset_coll
    e.location = loc
    e.rotation_euler[2] = rz
    e.empty_display_size = 0.2
    coll.objects.link(e)
    return e


def bounds(ob):
    ws = [ob.matrix_world @ Vector(c) for c in ob.bound_box]
    mn = Vector((min(w.x for w in ws), min(w.y for w in ws), min(w.z for w in ws)))
    mx = Vector((max(w.x for w in ws), max(w.y for w in ws), max(w.z for w in ws)))
    return mn, mx


# ---------------------------------------------------------------------------
# アセット (インスタンスで使い回すもの)
# ---------------------------------------------------------------------------

def make_asset_collection(name, root):
    c = bpy.data.collections.new(name)
    root.children.link(c)
    return c


CRATE = 0.36     # 培養ビンコンテナの一辺
CRATE_H = 0.19   # 段積みピッチ
PALLET_H = 0.15


def asset_crate(root):
    """16 本入り培養ビンコンテナ"""
    c = make_asset_collection('_A_培養ビンコンテナ', root)
    mb = MB()
    s, h, t = CRATE, 0.14, 0.012
    mb.box((0, 0, 0.006), (s - 0.01, s - 0.01, 0.012), M['crate'])
    for sx, sy, w, d in ((1, 0, t, s), (-1, 0, t, s), (0, 1, s, t), (0, -1, s, t)):
        x = sx * (s / 2 - t / 2)
        y = sy * (s / 2 - t / 2)
        mb.box((x, y, h / 2), (w, d, h), M['crate'])
        # 取っ手の窓
        if sx:
            mb.box((x * 1.001, y, h - 0.03), (t * 1.2, 0.1, 0.025), M['black_plastic'])
    step = 0.085
    for i in range(4):
        for j in range(4):
            x = (i - 1.5) * step
            y = (j - 1.5) * step
            mb.cyl((x, y, 0.012), (x, y, 0.15), 0.036, M['bottle'], 14)
            mb.cyl((x, y, 0.15), (x, y, 0.172), 0.039, M['cap'], 14)
            mb.cyl((x, y, 0.172), (x, y, 0.1735), 0.018, M['filter'], 10)
    mb.build('培養ビンコンテナ', c)
    return c


def asset_pallet(root):
    """1500 角の樹脂パレット"""
    c = make_asset_collection('_A_樹脂パレット', root)
    mb = MB()
    S = 1.5
    # 上面デッキ (桟)
    n = 9
    w = S / n * 0.78
    for i in range(n):
        x = -S / 2 + S / n * (i + 0.5)
        mb.box((x, 0, PALLET_H - 0.0125), (w, S, 0.025), M['pallet'])
    for y in (-S / 2 + 0.05, 0, S / 2 - 0.05):
        mb.box((0, y, PALLET_H - 0.035), (S, 0.1, 0.02), M['pallet'])
    # 桁 (ブロック)
    for x in (-S / 2 + 0.1, 0, S / 2 - 0.1):
        for y in (-S / 2 + 0.1, 0, S / 2 - 0.1):
            mb.box((x, y, 0.07), (0.2, 0.2, 0.1), M['pallet'])
    # 下面ランナー
    for x in (-S / 2 + 0.1, 0, S / 2 - 0.1):
        mb.box((x, 0, 0.01), (0.2, S, 0.02), M['pallet'])
    mb.build('樹脂パレット', c, bevel=0.006)
    return c


def asset_seed_stack(root):
    """種菌トレーの段積み (台車付き)"""
    c = make_asset_collection('_A_種菌トレー', root)
    mb = MB()
    W, D = 0.5, 0.4
    # 台車
    mb.box((0, 0, 0.07), (W, D, 0.025), M['steel'])
    for x in (-W / 2 + 0.05, W / 2 - 0.05):
        for y in (-D / 2 + 0.05, D / 2 - 0.05):
            mb.cyl((x, y, 0.0), (x, y, 0.058), 0.028, M['rubber'], 12)
            mb.box((x, y, 0.061), (0.04, 0.04, 0.01), M['steel'])
    z = 0.083
    th = 0.095
    for k in range(4):
        wall = 0.006
        mb.box((0, 0, z + 0.003), (W - 0.02, D - 0.02, 0.006), M['tray'])
        for sx, sy, w, d in ((1, 0, wall, D - 0.02), (-1, 0, wall, D - 0.02),
                             (0, 1, W - 0.02, wall), (0, -1, W - 0.02, wall)):
            mb.box((sx * (W / 2 - 0.013), sy * (D / 2 - 0.013), z + th / 2), (w, d, th), M['tray'])
        # 縁
        mb.box((0, D / 2 - 0.012, z + th - 0.004), (W - 0.01, 0.018, 0.008), M['tray'])
        mb.box((0, -D / 2 + 0.012, z + th - 0.004), (W - 0.01, 0.018, 0.008), M['tray'])
        # 中身
        fill = th * (0.75 if k < 3 else 0.6)
        mb.box((0, 0, z + 0.006 + fill / 2), (W - 0.045, D - 0.045, fill), M['spawn'])
        z += th + 0.006
    mb.build('種菌トレー', c, bevel=0.003)
    return c


def asset_box(root, name, size, mat=None):
    """テープ留めの段ボール箱"""
    c = make_asset_collection('_A_' + name, root)
    mb = MB()
    x, y, z = size
    mb.box((0, 0, z / 2), size, M['card'] if mat is None else mat)
    mb.box((0, 0, z + 0.0005), (x + 0.002, 0.05, 0.002), M['tape'])
    mb.box((0, y / 2 + 0.0005, z - 0.04), (x * 0.999, 0.002, 0.08), M['tape'])
    mb.box((0, -y / 2 - 0.0005, z - 0.04), (x * 0.999, 0.002, 0.08), M['tape'])
    mb.build(name, c, bevel=0.004)
    return c


# ---------------------------------------------------------------------------
# 設備ごとの置き換え
# ---------------------------------------------------------------------------

def build_pallet(ob, coll, A):
    (mn, mx) = bounds(ob)
    cx, cy = (mn.x + mx.x) / 2, (mn.y + mx.y) / 2
    rz = math.radians(random.uniform(-1.5, 1.5))
    instance(A['pallet'], 'R_' + ob.name, coll, (cx, cy, 0), rz)
    layers = random.choice((0, 1, 2, 2, 3, 3, 3))
    rot = Matrix.Rotation(rz, 4, 'Z')
    for L in range(layers):
        for i in range(4):
            for j in range(4):
                # 最上段は歯抜けにして手作業中の雰囲気を出す
                if L == layers - 1 and layers > 1 and random.random() < 0.18:
                    continue
                off = rot @ Vector(((i - 1.5) * (CRATE + 0.003), (j - 1.5) * (CRATE + 0.003), 0))
                jit = random.uniform(-0.006, 0.006)
                instance(A['crate'], 'コンテナ', coll,
                         (cx + off.x + jit, cy + off.y - jit, PALLET_H + L * CRATE_H),
                         rz + math.radians(random.uniform(-1, 1)))


def build_seed(ob, coll, A):
    mn, mx = bounds(ob)
    instance(A['seed'], 'R_' + ob.name, coll, ((mn.x + mx.x) / 2, (mn.y + mx.y) / 2, 0),
             math.radians(random.uniform(-2, 2)))


def build_table(ob, coll, A):
    """ステンレス作業台 (下段棚付き)"""
    mn, mx = bounds(ob)
    W, D, H = mx.x - mn.x, mx.y - mn.y, mx.z
    cx, cy = (mn.x + mx.x) / 2, (mn.y + mx.y) / 2
    mb = MB()
    top = 0.035
    mb.box((0, 0, H - top / 2), (W, D, top), M['steel'])
    # バックガード (長手方向の片側)
    leg = 0.038
    inset = 0.05
    xs = (-W / 2 + inset, W / 2 - inset)
    ny = max(2, int(D / 1.0) + 1)
    ys = [-D / 2 + inset + (D - 2 * inset) * k / (ny - 1) for k in range(ny)]
    for x in xs:
        for y in ys:
            mb.box((x, y, (H - top) / 2 + 0.02), (leg, leg, H - top - 0.04), M['steel'])
            mb.cyl((x, y, 0), (x, y, 0.02), 0.022, M['steel'], 12)
    # 下段棚
    mb.box((0, 0, 0.16), (W - 0.06, D - 0.06, 0.015), M['steel'])
    # 天板下の補強
    for x in xs:
        mb.box((x, 0, H - top - 0.03), (0.03, D - 0.1, 0.05), M['steel'])
    mb.build('R_' + ob.name, coll, bevel=0.004, loc=(cx, cy, 0))
    return (cx, cy, H, W, D)


def build_inoculator(ob, coll, A):
    """接種機: ステンレス架台 + アクリルカバー + 接種ヘッド + 操作盤"""
    mn, mx = bounds(ob)
    W, D, H = mx.x - mn.x, mx.y - mn.y, mx.z
    cx, cy = (mn.x + mx.x) / 2, (mn.y + mx.y) / 2
    mb = MB()
    base_h = 0.52
    # 下部キャビネット
    mb.box((0, 0, 0.06 + (base_h - 0.06) / 2), (W - 0.04, D - 0.04, base_h - 0.06), M['steel'])
    # パネル目地と扉の取っ手
    for k in range(3):
        y = -D / 2 + 0.02 + (D - 0.04) * (k + 0.5) / 3
        for sx in (-1, 1):
            mb.box((sx * (W / 2 - 0.018), y, 0.3), (0.006, (D - 0.04) / 3 - 0.02, base_h - 0.16), M['steel'])
            mb.box((sx * (W / 2 - 0.012), y + 0.25, 0.36), (0.012, 0.02, 0.12), M['black_plastic'])
    # アジャスタ脚
    for x in (-W / 2 + 0.08, W / 2 - 0.08):
        for y in (-D / 2 + 0.08, 0, D / 2 - 0.08):
            mb.cyl((x, y, 0), (x, y, 0.012), 0.035, M['rubber'], 12)
            mb.cyl((x, y, 0.012), (x, y, 0.06), 0.012, M['steel'], 8)
    # 天板
    mb.box((0, 0, base_h + 0.01), (W, D, 0.02), M['steel'])
    # アルミフレーム + アクリルカバー
    top = H
    fz = base_h + 0.02
    f = 0.03
    for x in (-W / 2 + f / 2, W / 2 - f / 2):
        for y in (-D / 2 + f / 2, D / 2 - f / 2):
            mb.box((x, y, (fz + top) / 2), (f, f, top - fz), M['alu'])
    for y in (-D / 2 + f / 2, D / 2 - f / 2):
        mb.box((0, y, top - f / 2), (W, f, f), M['alu'])
    for x in (-W / 2 + f / 2, W / 2 - f / 2):
        mb.box((x, 0, top - f / 2), (f, D, f), M['alu'])
    for x in (-W / 2 + f / 2, W / 2 - f / 2):
        mb.box((x, 0, (fz + top) / 2), (0.006, D - f, top - fz - f), M['acrylic'])
    for y in (-D / 2 + f / 2, D / 2 - f / 2):
        mb.box((0, y, (fz + top) / 2), (W - f, 0.006, top - fz - f), M['acrylic'])
    mb.box((0, 0, top - 0.003), (W - f, D - f, 0.006), M['acrylic'])
    # 内部コンベヤ
    mb.box((0, 0, fz + 0.06), (0.42, D - 0.1, 0.03), M['belt'])
    for x in (-0.23, 0.23):
        mb.box((x, 0, fz + 0.06), (0.03, D - 0.1, 0.07), M['steel'])
    # 接種ヘッド (門型)
    for x in (-0.45, 0.45):
        mb.box((x, 0.1, (fz + top - 0.05) / 2), (0.08, 0.18, top - 0.05 - fz), M['teal'])
    mb.box((0, 0.1, top - 0.1), (0.98, 0.2, 0.1), M['teal'])
    for k in range(4):
        x = (k - 1.5) * 0.085
        mb.cyl((x, 0.1, top - 0.15), (x, 0.1, top - 0.25), 0.018, M['steel'], 12)
        mb.cyl((x, 0.1, top - 0.25), (x, 0.1, top - 0.29), 0.008, M['steel'], 8, r2=0.004)
    # 種菌ホッパー
    mb.cyl((0.35, -0.7, top - 0.05), (0.35, -0.7, fz + 0.18), 0.17, M['steel'], 24, r2=0.05)
    # 操作盤 (タッチパネル)
    px, py = W / 2 + 0.02, -D / 2 + 0.25
    mb.cyl((px, py, base_h), (px, py, base_h + 0.25), 0.02, M['steel'], 10)
    mb.box((px + 0.02, py, base_h + 0.33), (0.06, 0.34, 0.24), M['gray_paint'])
    mb.box((px + 0.051, py, base_h + 0.34), (0.002, 0.29, 0.17), M['screen'])
    mb.box((px + 0.051, py - 0.12, base_h + 0.235), (0.006, 0.03, 0.03), M['lamp_r'])
    # 積層表示灯
    tx, ty = -W / 2 + 0.06, D / 2 - 0.06
    z = top
    mb.cyl((tx, ty, z), (tx, ty, z + 0.1), 0.01, M['steel'], 8)
    z += 0.1
    for m in (M['lamp_r'], M['lamp_y'], M['lamp_g']):
        mb.cyl((tx, ty, z), (tx, ty, z + 0.045), 0.03, m, 16)
        z += 0.047
    mb.cyl((tx, ty, z), (tx, ty, z + 0.01), 0.03, M['black_plastic'], 16)
    mb.build('R_' + ob.name, coll, bevel=0.003, loc=(cx, cy, 0))
    # 機内のコンテナ
    for k, y in enumerate((-0.8, -0.2, 0.55)):
        instance(A['crate'], 'コンテナ', coll, (cx, cy + y, fz + 0.075), 0)


def build_conveyor(ob, coll, A):
    """ローラーコンベヤ"""
    mn, mx = bounds(ob)
    W, D, H = mx.x - mn.x, mx.y - mn.y, mx.z
    cx, cy = (mn.x + mx.x) / 2, (mn.y + mx.y) / 2
    mb = MB()
    rw = 0.46  # 搬送幅
    for sx in (-1, 1):
        x = sx * (rw / 2 + 0.02)
        mb.box((x, 0, H - 0.04), (0.035, D, 0.08), M['steel'])
    n = int(D / 0.075)
    for k in range(n):
        y = -D / 2 + 0.04 + k * (D - 0.08) / (n - 1)
        mb.cyl((-rw / 2, y, H - 0.03), (rw / 2, y, H - 0.03), 0.024, M['galv'], 14)
    for y in (-D / 2 + 0.1, 0, D / 2 - 0.1):
        for sx in (-1, 1):
            x = sx * (rw / 2 + 0.02)
            mb.box((x, y, (H - 0.08) / 2), (0.04, 0.04, H - 0.08), M['steel'])
            mb.cyl((x, y, 0), (x, y, 0.015), 0.03, M['rubber'], 10)
        mb.box((0, y, 0.18), (rw + 0.04, 0.03, 0.03), M['steel'])
    # 制御ボックス
    mb.box((rw / 2 + 0.12, -D / 2 + 0.6, H - 0.2), (0.14, 0.24, 0.26), M['gray_paint'])
    mb.box((rw / 2 + 0.191, -D / 2 + 0.6, H - 0.16), (0.004, 0.03, 0.03), M['lamp_g'])
    mb.build('R_' + ob.name, coll, bevel=0.003, loc=(cx, cy, 0))
    for y in (-0.2, 0.35, 1.2):
        instance(A['crate'], 'コンテナ', coll, (cx, cy + y, H - 0.005), math.radians(random.uniform(-3, 3)))


def build_taper(ob, coll, A):
    """封函機 (コンベヤをまたぐ門型のテープ貼り機)"""
    mn, mx = bounds(ob)
    W, D, H = mx.x - mn.x, mx.y - mn.y, mx.z
    cx, cy = (mn.x + mx.x) / 2, (mn.y + mx.y) / 2
    mb = MB()
    for sx in (-1, 1):
        x = sx * (W / 2 - 0.04)
        mb.box((x, 0, H / 2), (0.08, D, H), M['white_panel'])
    mb.box((0, 0, H - 0.08), (W, D, 0.16), M['white_panel'])
    mb.box((0, -D / 2 - 0.002, H - 0.08), (W * 0.7, 0.004, 0.1), M['black_plastic'])
    # テープロール
    mb.cyl((0, 0.05, H + 0.02), (0.05, 0.05, H + 0.02), 0.09, M['tape'], 24)
    mb.cyl((-0.001, 0.05, H + 0.02), (0.051, 0.05, H + 0.02), 0.04, M['card'], 16)
    mb.box((W / 2 + 0.002, 0, H - 0.1), (0.004, 0.06, 0.06), M['lamp_g'])
    mb.build('R_' + ob.name, coll, bevel=0.004, loc=(cx, cy, 0))


def build_shelf(ob, coll, A):
    """中量ラック (3 段) + 保管物"""
    mn, mx = bounds(ob)
    W, D, H = mx.x - mn.x, mx.y - mn.y, mx.z
    cx, cy = (mn.x + mx.x) / 2, (mn.y + mx.y) / 2
    mb = MB()
    p = 0.05
    for x in (-W / 2 + p / 2, 0, W / 2 - p / 2):
        for y in (-D / 2 + p / 2, D / 2 - p / 2):
            mb.box((x, y, H / 2), (p, p, H), M['rack_blue'])
    levels = (0.1, 0.5, H - 0.02)
    for z in levels:
        for y in (-D / 2 + p / 2, D / 2 - p / 2):
            mb.box((0, y, z), (W, 0.04, 0.06), M['rack_orange'])
        mb.box((0, 0, z + 0.035), (W - 0.02, D - 0.02, 0.01), M['galv'])
    mb.build('R_' + ob.name, coll, bevel=0.004, loc=(cx, cy, 0))
    # 保管物
    for z in levels[:2]:
        x = -W / 2 + 0.1
        while x < W / 2 - 0.4:
            kind = random.choice(('box_a', 'box_b', 'seed'))
            if kind == 'seed' and z > 0.3:
                kind = 'box_a'
            sz = {'box_a': 0.45, 'box_b': 0.35, 'seed': 0.5}[kind]
            instance(A[kind], '保管物', coll, (cx + x + sz / 2, cy + random.uniform(-0.05, 0.05), z + 0.04),
                     math.radians(random.uniform(-4, 4)))
            x += sz + random.uniform(0.05, 0.2)
    top_z = levels[2] + 0.04
    x = -W / 2 + 0.2
    while x < W / 2 - 0.4:
        instance(A['box_b'], '保管物', coll, (cx + x + 0.2, cy, top_z), math.radians(random.uniform(-6, 6)))
        x += 0.5 + random.uniform(0.1, 0.4)


def build_hanger(ob, coll, A):
    """ハンガーラック + 吊るした防塵服"""
    mn, mx = bounds(ob)
    W, D, H = mx.x - mn.x, mx.y - mn.y, mx.z
    cx, cy = (mn.x + mx.x) / 2, (mn.y + mx.y) / 2
    mb = MB()
    for y in (-D / 2 + 0.02, D / 2 - 0.02):
        mb.cyl((0, y, 0.03), (0, y, H), 0.012, M['steel'], 10)
        mb.cyl((-W / 2, y, 0.03), (W / 2, y, 0.03), 0.012, M['steel'], 10)
        for x in (-W / 2, W / 2):
            mb.cyl((x, y, 0), (x, y, 0.03), 0.02, M['rubber'], 10)
    mb.cyl((0, -D / 2 + 0.02, H), (0, D / 2 - 0.02, H), 0.012, M['steel'], 10)
    n = max(2, int((D - 0.1) / 0.12))
    for k in range(n):
        y = -D / 2 + 0.08 + k * (D - 0.16) / max(1, n - 1)
        # ハンガー
        mb.cyl((0, y, H - 0.005), (0, y, H - 0.05), 0.003, M['black_plastic'], 6)
        mb.cyl((0, y - 0.0, H - 0.06), (0, y + 0.0, H - 0.06), 0.1, M['black_plastic'], 3)
        # 服 (肩～裾)
        mb.sphere((0, y, H - 0.1), (0.2, 0.035, 0.045), M['suit'])
        mb.box((0, y, H - 0.1 - 0.28), (0.36, 0.05, 0.52), M['suit'])
        mb.box((0, y, H - 0.1 - 0.28 - 0.26 - 0.08), (0.3, 0.045, 0.16), M['suit'])
    mb.build('R_' + ob.name, coll, bevel=0.01, loc=(cx, cy, 0))


def build_bench(ob, coll, A):
    """CCP: クリーンルーム用ベンチキャビネット"""
    mn, mx = bounds(ob)
    W, D, H = mx.x - mn.x, mx.y - mn.y, mx.z
    cx, cy = (mn.x + mx.x) / 2, (mn.y + mx.y) / 2
    mb = MB()
    mb.box((0, 0, H - 0.015), (W, D, 0.03), M['steel'])
    mb.box((0, 0, 0.05 + (H - 0.08) / 2), (W - 0.04, D - 0.06, H - 0.08), M['white_panel'])
    for k in range(3):
        x = -W / 2 + 0.02 + (W - 0.04) * (k + 0.5) / 3
        for sy in (-1, 1):
            mb.box((x, sy * (D / 2 - 0.029), 0.05 + (H - 0.08) / 2), ((W - 0.04) / 3 - 0.015, 0.004, H - 0.12), M['white_panel'])
            mb.box((x, sy * (D / 2 - 0.025), H - 0.08), (0.12, 0.01, 0.015), M['steel'])
    mb.box((0, 0, 0.025), (W - 0.06, D - 0.1, 0.05), M['black_plastic'])
    mb.build('R_' + ob.name, coll, bevel=0.004, loc=(cx, cy, 0))


def build_ffu(ob, coll, A):
    """FFU (ファンフィルタユニット) 壁付け"""
    mn, mx = bounds(ob)
    W, D, H = mx.x - mn.x, mx.y - mn.y, mx.z
    # 元の箱は壁に埋まっているので、背面を壁の室内側 (y=0.16) に合わせる
    cx, cy = (mn.x + mx.x) / 2, 0.165 + D / 2
    mb = MB()
    mb.box((0, 0, H / 2 + 0.2), (W, D, H), M['white_panel'])
    # 吹出しパンチング面 (格子で表現)
    fy = D / 2 + 0.002
    mb.box((0, fy - 0.003, H / 2 + 0.2), (W - 0.1, 0.004, H - 0.12), M['galv'])
    for k in range(9):
        z = 0.26 + k * (H - 0.12) / 9
        mb.box((0, fy, z), (W - 0.1, 0.006, 0.012), M['white_panel'])
    for k in range(11):
        x = -W / 2 + 0.05 + k * (W - 0.1) / 10
        mb.box((x, fy, H / 2 + 0.2), (0.012, 0.006, H - 0.12), M['white_panel'])
    mb.box((W / 2 - 0.08, fy + 0.004, H + 0.13), (0.06, 0.004, 0.03), M['lamp_g'])
    # 支持脚
    for x in (-W / 2 + 0.04, W / 2 - 0.04):
        mb.box((x, 0, 0.1), (0.04, D - 0.02, 0.2), M['steel'])
    mb.build('R_' + ob.name, coll, bevel=0.004, loc=(cx, cy, 0))


# ---------------------------------------------------------------------------
# 人物 (防塵服を着た作業者)
# ---------------------------------------------------------------------------

def build_worker(name, coll, loc, face_to, pose='work', height=1.62):
    s = height / 1.75
    # (位置, 半径X, 半径Y)  前方は -Y
    V = [
        ((0, 0, 0.95), 0.17, 0.12),   # 0 骨盤
        ((0, 0, 1.08), 0.16, 0.12),   # 1 腰
        ((0, 0.005, 1.30), 0.2, 0.135),  # 2 胸
        ((0, 0.01, 1.46), 0.075, 0.075),  # 3 首元
        ((0, 0.0, 1.52), 0.065, 0.065),  # 4 首
    ]
    E = [(0, 1), (1, 2), (2, 3), (3, 4)]
    if pose == 'work':
        arm = [((0.2, 0.01, 1.41), 0.078), ((0.25, -0.04, 1.14), 0.064),
               ((0.19, -0.3, 1.03), 0.052), ((0.15, -0.37, 1.0), 0.048)]
    else:
        arm = [((0.2, 0.01, 1.41), 0.078), ((0.25, 0.02, 1.13), 0.064),
               ((0.26, -0.03, 0.88), 0.052), ((0.26, -0.04, 0.81), 0.048)]
    leg = [((0.095, 0, 0.9), 0.1), ((0.1, -0.01, 0.5), 0.07),
           ((0.1, 0.01, 0.1), 0.06), ((0.1, -0.1, 0.05), 0.055)]
    hands = []
    for side in (1, -1):
        prev = 2
        for (p, r) in arm:
            V.append(((p[0] * side, p[1], p[2]), r, r))
            E.append((prev, len(V) - 1))
            prev = len(V) - 1
        hands.append(Vector((arm[-1][0][0] * side, arm[-1][0][1], arm[-1][0][2])))
        prev = 0
        for (p, r) in leg:
            V.append(((p[0] * side, p[1], p[2]), r, r))
            E.append((prev, len(V) - 1))
            prev = len(V) - 1

    me = bpy.data.meshes.new(name + '_body')
    me.from_pydata([Vector(p) * s for p, _, _ in V], E, [])
    ob = bpy.data.objects.new(name, me)
    coll.objects.link(ob)
    sk = ob.modifiers.new('Skin', 'SKIN')
    sk.branch_smoothing = 0.6
    sk.use_smooth_shade = True
    for i, (_, rx, ry) in enumerate(V):
        sv = me.skin_vertices[0].data[i]
        sv.radius = (rx * s, ry * s)
        sv.use_root = (i == 0)
    sub = ob.modifiers.new('Subsurf', 'SUBSURF')
    sub.levels = 2
    sub.render_levels = 2
    me.materials.append(M['suit'])

    # 頭部 (フード・ゴーグル・マスク)、手袋、長靴
    mb = MB()
    hz = 1.635 * s
    mb.sphere((0, 0.01 * s, hz), (0.1 * s, 0.112 * s, 0.125 * s), M['suit'], 24, 14)
    # フードの裾 (肩にかかるケープ部分)
    mb.cyl((0, 0.012 * s, 1.41 * s), (0, 0.012 * s, 1.56 * s), 0.14 * s, M['suit'], 24, r2=0.085 * s)
    mb.sphere((0, -0.098 * s, hz + 0.02 * s), (0.078 * s, 0.035 * s, 0.03 * s), M['goggle'], 20, 10)
    mb.sphere((0, -0.1 * s, hz - 0.045 * s), (0.06 * s, 0.035 * s, 0.04 * s), M['mask'], 16, 10)
    for h in hands:
        mb.sphere(h * s, (0.045 * s, 0.05 * s, 0.035 * s), M['glove'], 14, 8)
    for side in (1, -1):
        mb.box((0.1 * side * s, -0.03 * s, 0.1 * s), (0.11 * s, 0.26 * s, 0.2 * s), M['boot'])
    head = mb.build(name + '_装備', coll, bevel=0.02)
    head.parent = ob
    for m in head.modifiers:
        m.width = 0.02
    sub2 = head.modifiers.new('Subsurf', 'SUBSURF')
    sub2.levels = 1
    sub2.render_levels = 2

    d = Vector(face_to) - Vector(loc)
    ob.location = (loc[0], loc[1], 0)
    ob.rotation_euler[2] = math.atan2(d.x, -d.y)
    return ob


# ---------------------------------------------------------------------------
# 追加設備 (ラベルのみだった靴箱。建物の外側は元のまま変更しない)
# ---------------------------------------------------------------------------

def build_extras(coll):
    # 靴箱 (前室)
    mb = MB()
    W, D, H = 0.9, 0.32, 0.9
    mb.box((0, 0, H / 2), (W, D, H), M['white_panel'])
    for i in range(3):
        for j in range(4):
            x = -W / 2 + 0.03 + (W - 0.06) * (i + 0.5) / 3
            z = 0.05 + (H - 0.1) * (j + 0.5) / 4
            mb.box((x, -D / 2 + 0.14, z), ((W - 0.06) / 3 - 0.02, 0.3, (H - 0.1) / 4 - 0.02), M['black_plastic'])
            if random.random() < 0.6:
                mb.box((x - 0.05, -D / 2 + 0.1, z - 0.06), (0.09, 0.26, 0.07), M['boot'])
                mb.box((x + 0.05, -D / 2 + 0.1, z - 0.06), (0.09, 0.26, 0.07), M['boot'])
    mb.build('R_靴箱', coll, bevel=0.004, loc=(11.4, 1.24, 0), rz=math.pi)


# ---------------------------------------------------------------------------
# 照明・ワールド・レンダー設定
# ---------------------------------------------------------------------------

def setup_world_and_lights(scene, coll):
    world = scene.world or bpy.data.worlds.new('World')
    scene.world = world
    world.use_nodes = True
    nt = world.node_tree
    orig_bg_color, orig_bg_strength = (0.05, 0.05, 0.05, 1.0), 0.5
    for n in nt.nodes:
        if n.type == 'BACKGROUND':
            orig_bg_color = tuple(n.inputs['Color'].default_value)
            orig_bg_strength = n.inputs['Strength'].default_value
    nt.nodes.clear()
    out = nt.nodes.new('ShaderNodeOutputWorld')
    bg = nt.nodes.new('ShaderNodeBackground')
    # 空: 天頂の青 → 地平線の明るい霞のグラデーション (Sky Texture に依存しない)
    tc = nt.nodes.new('ShaderNodeTexCoord')
    sep = nt.nodes.new('ShaderNodeSeparateXYZ')
    nt.links.new(tc.outputs['Generated'], sep.inputs[0])
    sky_col = ramp(nt, sep.outputs['Z'], [(0.0, (0.55, 0.58, 0.6)), (0.5, (0.86, 0.9, 0.95)),
                                          (0.56, (0.62, 0.74, 0.9)), (1.0, (0.2, 0.38, 0.75))])
    sun_el, sun_rot = math.radians(38), math.radians(215)
    nt.links.new(sky_col, bg.inputs['Color'])
    bg.inputs["Strength"].default_value = 0.55
    # カメラに直接映る背景は元ファイルと同じダークグレー (周囲の空間は変更しない)
    bg_cam = nt.nodes.new('ShaderNodeBackground')
    bg_cam.inputs['Color'].default_value = orig_bg_color
    bg_cam.inputs['Strength'].default_value = orig_bg_strength
    lp = nt.nodes.new('ShaderNodeLightPath')
    mix = nt.nodes.new('ShaderNodeMixShader')
    nt.links.new(lp.outputs['Is Camera Ray'], mix.inputs['Fac'])
    nt.links.new(bg.outputs['Background'], mix.inputs[1])
    nt.links.new(bg_cam.outputs['Background'], mix.inputs[2])
    nt.links.new(mix.outputs['Shader'], out.inputs['Surface'])

    # 太陽 (空と方向をそろえる)
    sun = next((o for o in scene.objects if o.type == 'LIGHT' and o.data.type == 'SUN'), None)
    if sun is None:
        sun = bpy.data.objects.new('Sun', bpy.data.lights.new('Sun', 'SUN'))
        coll.objects.link(sun)
    sun.data.energy = 2.2
    sun.data.angle = math.radians(1.2)
    sun.data.color = (1.0, 0.95, 0.88)
    sun.rotation_euler = (math.pi / 2 - sun_el, 0, sun_rot + math.pi / 2)

    # 天井照明 (LED ベースライトを想定した面光源。カメラには映さない)
    lc = bpy.data.collections.new('R_照明')
    coll.children.link(lc)
    for i in range(4):
        for j in range(6):
            x = 1.5 + i * 3.0
            y = 1.3 + j * 2.7
            ld = bpy.data.lights.new('LED', 'AREA')
            ld.shape = 'RECTANGLE'
            ld.size, ld.size_y = 0.3, 1.2
            ld.energy = 70
            ld.color = (1.0, 0.97, 0.93)
            lo = bpy.data.objects.new('天井LED', ld)
            lo.location = (x, y, 2.6)
            lo.visible_camera = False
            lc.objects.link(lo)


def setup_render(scene):
    scene.render.engine = 'CYCLES'
    cy = scene.cycles
    cy.device = 'CPU'
    cy.samples = 256
    cy.use_adaptive_sampling = True
    cy.adaptive_threshold = 0.02
    cy.use_denoising = True
    try:
        cy.denoiser = 'OPENIMAGEDENOISE'
    except TypeError:
        pass
    cy.max_bounces = 8
    cy.transparent_max_bounces = 16
    cy.caustics_reflective = False
    cy.caustics_refractive = False
    cy.blur_glossy = 1.0
    scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
    scene.render.resolution_percentage = 100
    scene.view_settings.view_transform = 'AgX'
    for look in ('AgX - Medium High Contrast', 'Medium High Contrast'):
        try:
            scene.view_settings.look = look
            break
        except TypeError:
            pass
    scene.view_settings.exposure = -0.6


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main():
    scene = bpy.context.scene
    root = scene.collection
    build_materials()

    # 元オブジェクトの退避先 (非表示)
    old = bpy.data.collections.new('元レイアウト_箱モデル(非表示)')
    root.children.link(old)
    labels = bpy.data.collections.new('ラベル文字(非表示)')
    root.children.link(labels)
    real = bpy.data.collections.new('リアル化')
    root.children.link(real)
    assets = bpy.data.collections.new('_アセット')
    root.children.link(assets)

    def move(ob, dst):
        for c in list(ob.users_collection):
            c.objects.unlink(ob)
        dst.objects.link(ob)

    A = {
        'crate': asset_crate(assets),
        'pallet': asset_pallet(assets),
        'seed': asset_seed_stack(assets),
        'box_a': asset_box(assets, '段ボール_大', (0.45, 0.4, 0.32)),
        'box_b': asset_box(assets, '段ボール_小', (0.35, 0.3, 0.25)),
    }

    objs = list(bpy.data.objects)
    workers = []
    for ob in objs:
        n = ob.name
        base = n.split('.')[0]
        if ob.type == 'FONT':
            move(ob, labels)
            continue
        if ob.type != 'MESH':
            continue
        if base == 'パレット':
            build_pallet(ob, real, A); move(ob, old)
        elif base == '種トレー':
            build_seed(ob, real, A); move(ob, old)
        elif base == '台':
            build_table(ob, real, A); move(ob, old)
        elif base == '接種機':
            build_inoculator(ob, real, A); move(ob, old)
        elif base == 'コンベヤ':
            build_conveyor(ob, real, A); move(ob, old)
        elif base == 'テープ':
            build_taper(ob, real, A); move(ob, old)
        elif base == '棚':
            build_shelf(ob, real, A); move(ob, old)
        elif base == 'ハンガーラック':
            build_hanger(ob, real, A); move(ob, old)
        elif base == 'CCP':
            build_bench(ob, real, A); move(ob, old)
        elif base == 'FFU':
            build_ffu(ob, real, A); move(ob, old)
        elif base == 'Human_Body':
            mn, mx = bounds(ob)
            workers.append(((mn.x + mx.x) / 2, (mn.y + mx.y) / 2))
            move(ob, old)
        elif base == 'Human_Head':
            move(ob, old)
        elif n in ('Floor.002', 'Floor.004', 'Floor.005'):
            move(ob, old)   # 使われていない面
        elif n == 'Floor':
            ob.data.materials.clear(); ob.data.materials.append(M['floor'])
        elif n == 'Floor.001':
            ob.data.materials.clear(); ob.data.materials.append(M['vinyl'])
            ob.location.z += 0.002
        elif base == 'Wall':
            if n in ('Wall.013', 'Wall.014'):
                # 窓帯 → ガラス + サッシ
                mn, mx = bounds(ob)
                mb = MB()
                cy_ = (mn.y + mx.y) / 2
                mb.box_mm((mn.x, cy_ - 0.006, mn.z), (mx.x, cy_ + 0.006, mx.z), M['glass'])
                mb.box_mm((mn.x, mn.y, mn.z), (mx.x, mx.y, mn.z + 0.02), M['alu'])
                mb.box_mm((mn.x, mn.y, mx.z - 0.02), (mx.x, mx.y, mx.z), M['alu'])
                k = max(1, round((mx.x - mn.x) / 1.0))
                for i in range(k + 1):
                    x = mn.x + (mx.x - mn.x) * i / k
                    mb.box_mm((max(mn.x, x - 0.015), mn.y, mn.z), (min(mx.x, x + 0.015), mx.y, mx.z), M['alu'])
                mb.build('R_窓_' + n, real)
                move(ob, old)
                continue
            if n in ('Wall.005', 'Wall.022', 'Wall.018'):
                mat = M['door'] if n != 'Wall.018' else M['steel']
                ob.data.materials.clear(); ob.data.materials.append(mat)
                mn, mx = bounds(ob)
                mb = MB()
                for y in (mn.y - 0.02, mx.y + 0.02):
                    x = mx.x - 0.08
                    mb.box((x, y, 0.5), (0.1, 0.02, 0.02), M['steel'])
                mb.box_mm((mn.x + 0.1, mn.y - 0.002, 0.6), (mx.x - 0.1, mx.y + 0.002, 0.9), M['glass'])
                mb.build('R_取っ手_' + n, real)
            elif n in ('Wall.010', 'Wall.011'):
                ob.data.materials.clear(); ob.data.materials.append(M['steel'])
            else:
                ob.data.materials.clear(); ob.data.materials.append(M['wall'])
            shrink_mesh(ob, random.uniform(0.0006, 0.0025))
            if not any(m.type == 'BEVEL' for m in ob.modifiers):
                bv = ob.modifiers.new('Bevel', 'BEVEL')
                bv.width = 0.006
                bv.segments = 2
                bv.limit_method = 'ANGLE'

    # 作業者: 近くの作業対象の方を向かせる
    targets = [
        ((6.9, 12.35), (8.28, 12.4), 'work'),
        ((6.9, 13.35), (8.28, 13.3), 'work'),
        ((9.8, 13.35), (10.4, 13.2), 'work'),
        ((8.35, 9.97), (9.4, 9.7), 'work'),
        ((8.16, 5.84), (7.43, 5.6), 'work'),
        ((6.42, 7.65), (5.25, 7.4), 'stand'),
        ((6.45, 5.28), (7.43, 5.24), 'work'),
    ]
    for k, (pos, _) in enumerate(zip(workers, range(len(workers)))):
        best = min(targets, key=lambda t: (t[0][0] - pos[0]) ** 2 + (t[0][1] - pos[1]) ** 2)
        build_worker(f'作業者_{k + 1:02d}', real, pos, best[1], best[2],
                     height=random.uniform(1.58, 1.7))

    # 作業台の上の小物
    for n, off in (('台', (0, 0.6)), ('台.002', (0, -0.5)), ('台.003', (0, 0.3))):
        ob = bpy.data.objects.get(n)
        if ob:
            mn, mx = bounds(ob)
            instance(A['crate'], 'コンテナ', real,
                     ((mn.x + mx.x) / 2 + off[0], (mn.y + mx.y) / 2 + off[1], mx.z), math.radians(random.uniform(-5, 5)))

    build_extras(real)


    setup_world_and_lights(scene, real)
    setup_render(scene)

    # 非表示コレクションを除外
    vl = bpy.context.view_layer
    for c in (old, labels, assets):
        lc = vl.layer_collection.children.get(c.name)
        if lc:
            lc.exclude = True
        if c is not assets:
            c.hide_render = True
    # 空になった元コレクションは残す (カメラ・太陽が入っている)

    # 斜め俯瞰の追加カメラ
    cd = bpy.data.cameras.new('Camera_斜め俯瞰')
    cd.lens = 28
    cam2 = bpy.data.objects.new('Camera_斜め俯瞰', cd)
    cam2.location = (-5.5, -5.0, 7.5)
    real.objects.link(cam2)
    tgt = Vector((6.5, 8.0, 0.0))
    cam2.rotation_euler = (tgt - cam2.location).to_track_quat('-Z', 'Y').to_euler()


if __name__ == '__main__':
    main()
    argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
    if argv:
        bpy.ops.wm.save_as_mainfile(filepath=argv[0], compress=True)
        print('saved', argv[0])
