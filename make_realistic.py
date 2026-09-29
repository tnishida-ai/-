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

    # コンテナの塗装鋼板 (オフホワイト、下部の汚れ・点状のシミ・縦の色ムラ)
    mat, nt, b = new_material('R_コンテナ波板', (0.66, 0.67, 0.64), 0.45)
    geo = nt.nodes.new('ShaderNodeNewGeometry')
    pos = geo.outputs['Position']
    sep = nt.nodes.new('ShaderNodeSeparateXYZ')
    nt.links.new(pos, sep.inputs[0])
    streak = noise(nt, pos, 3.0, 4, 0.5, stretch=(6, 6, 0.4))
    base = ramp(nt, streak, [(0.3, (0.58, 0.59, 0.56)), (0.7, (0.72, 0.73, 0.69))])
    # 下ほど汚れる
    low = map_range(nt, sep.outputs['Z'], 0.0, 0.9, 1.0, 0.0)
    grime_n = noise(nt, pos, 8.0, 6, 0.65)
    grime = math_node(nt, 'MULTIPLY', low, map_range(nt, grime_n, 0.35, 0.65, 0.1, 0.8))
    mix1 = nt.nodes.new('ShaderNodeMix'); mix1.data_type = 'RGBA'
    nt.links.new(grime, mix1.inputs['Factor'])
    nt.links.new(base, mix1.inputs[6])
    mix1.inputs[7].default_value = (0.5, 0.48, 0.42, 1)
    # 点状のシミ (ボロノイの小さな点、下半分に多め)
    vor = nt.nodes.new('ShaderNodeTexVoronoi')
    vor.inputs['Scale'].default_value = 30.0
    nt.links.new(pos, vor.inputs['Vector'])
    dots = map_range(nt, vor.outputs['Distance'], 0.04, 0.09, 1.0, 0.0)
    dots_mask = map_range(nt, noise(nt, pos, 2.5, 3), 0.42, 0.55, 0.0, 1.0)
    dots = math_node(nt, 'MULTIPLY', dots, dots_mask)
    dots = math_node(nt, 'MULTIPLY', dots, map_range(nt, sep.outputs['Z'], 0.2, 1.0, 1.0, 0.25))
    mix2 = nt.nodes.new('ShaderNodeMix'); mix2.data_type = 'RGBA'
    nt.links.new(dots, mix2.inputs['Factor'])
    nt.links.new(mix1.outputs[2], mix2.inputs[6])
    mix2.inputs[7].default_value = (0.08, 0.08, 0.075, 1)
    nt.links.new(mix2.outputs[2], b.inputs['Base Color'])
    nt.links.new(map_range(nt, grime, 0.0, 0.8, 0.4, 0.7), b.inputs['Roughness'])
    # 塗膜の凹凸と板のゆがみ
    dent = noise(nt, pos, 1.2, 3, 0.5)
    fine = noise(nt, pos, 250, 2, 0.5)
    bump(nt, b, math_node(nt, 'ADD', dent, math_node(nt, 'MULTIPLY', fine, 0.15)), 0.08, 0.02)
    M['container'] = mat

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
    M['belt'] = new_material('R_ベルト', (0.03, 0.42, 0.14), 0.45)[0]

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
    # 接種機まわり
    M['acrylic_blue'] = new_material('R_青アクリル', (0.35, 0.68, 0.98), 0.01, transmission=1.0, ior=1.49)[0]
    M['sign_gray'] = new_material('R_看板_グレー', (0.72, 0.73, 0.74), 0.4, 0.3)[0]
    M['sign_light'] = new_material('R_看板_面', (0.78, 0.79, 0.8), 0.45)[0]
    M['sign_blue'] = new_material('R_看板_青', (0.05, 0.08, 0.32), 0.4)[0]
    M['sign_text'] = new_material('R_看板_文字', (0.04, 0.06, 0.25), 0.4)[0]
    M['warn'] = new_material('R_注意ラベル', (0.75, 0.25, 0.1), 0.5)[0]
    M['qr'] = new_material('R_QR', (0.15, 0.15, 0.15), 0.6)[0]
    M['paper'] = new_material('R_紙', (0.9, 0.9, 0.88), 0.8)[0]
    M['green_tape'] = new_material('R_養生テープ', (0.1, 0.65, 0.15), 0.6)[0]
    M['btn_green'] = new_material('R_押しボタン', (0.05, 0.45, 0.15), 0.3)[0]
    M['yellow'] = new_material('R_非常停止箱', (0.95, 0.65, 0.05), 0.4)[0]
    M['counter'] = new_material('R_カウンター', (0.02, 0.02, 0.02), 0.2, emission=((0.9, 0.95, 1.0), 4.0))[0]
    M['bag'] = new_material('R_菌床袋', (0.75, 0.62, 0.42), 0.15, transmission=0.3, sss=0.3)[0]
    # ベルトコンベヤまわり
    mat, nt, b = new_material('R_PVCベルト_緑', (0.02, 0.3, 0.17), 0.3, coat=0.2)
    v = tex_coord(nt)
    wear = noise(nt, v, 6, 5, 0.6)
    nt.links.new(ramp(nt, wear, [(0.4, (0.008, 0.16, 0.09)), (0.65, (0.014, 0.2, 0.115)), (0.9, (0.07, 0.24, 0.17))]), b.inputs['Base Color'])
    scr = noise(nt, v, 12, 4, 0.55)
    nt.links.new(map_range(nt, scr, 0.4, 0.7, 0.18, 0.42), b.inputs['Roughness'])
    bump(nt, b, noise(nt, v, 400, 2), 0.05)
    M['pvc_belt'] = mat
    M['brass'] = new_material('R_真鍮', (0.75, 0.55, 0.3), 0.35, 1.0)[0]
    M['orange_box'] = new_material('R_電源ボックス', (0.9, 0.3, 0.03), 0.4)[0]
    M['spray_red'] = new_material('R_スプレー缶', (0.75, 0.06, 0.05), 0.3, 0.4)[0]
    M['tape_measure'] = new_material('R_メジャー', (0.9, 0.75, 0.1), 0.35)[0]
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

    def torus(self, c, R, r, mat, segs=32, rsegs=6, tilt=0.0):
        """水平なリング (巻いたコード等)"""
        m = Matrix.Translation(c) @ Matrix.Rotation(tilt, 4, 'X')
        vs = []
        for i in range(segs):
            a = 2 * math.pi * i / segs
            for j in range(rsegs):
                b = 2 * math.pi * j / rsegs
                p = Vector(((R + r * math.cos(b)) * math.cos(a), (R + r * math.cos(b)) * math.sin(a), r * math.sin(b)))
                vs.append(self.bm.verts.new(m @ p))
        idx = self._mi(mat)
        for i in range(segs):
            for j in range(rsegs):
                q = (vs[i * rsegs + j], vs[((i + 1) % segs) * rsegs + j],
                     vs[((i + 1) % segs) * rsegs + (j + 1) % rsegs], vs[i * rsegs + (j + 1) % rsegs])
                f = self.bm.faces.new(q)
                f.material_index = idx
                f.smooth = True

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


def build_corrugated(ob, coll):
    """コンテナの台形波板壁。壁の箱の範囲を、両面とも同じ波形を持つ鋼板で置き換える"""
    mn, mx = bounds(ob)
    along_x = (mx.x - mn.x) >= (mx.y - mn.y)
    u0, u1 = (mn.x, mx.x) if along_x else (mn.y, mx.y)
    t0, t1 = (mn.y, mx.y) if along_x else (mn.x, mx.x)
    z0, z1 = max(mn.z, 0.0) + 0.001, mx.z
    D = 0.036                       # 波の深さ
    seg = (0.10, 0.035, 0.108, 0.035)   # 山の平部 / 斜面 / 谷の平部 / 斜面 (ピッチ 278mm)
    prof = [(u0, 0.0)]
    u, k = u0, 0
    depth_at = (0.0, D, D, 0.0)
    while u < u1:
        u = min(u + seg[k % 4], u1)
        prof.append((u, depth_at[k % 4]))
        k += 1
    verts, faces = [], []

    def P(uu, tt, zz):
        return (uu, tt, zz) if along_x else (tt, uu, zz)
    for uu, c in prof:
        lo, hi = t0 + c, t1 - D + c
        verts += [P(uu, lo, z0), P(uu, hi, z0), P(uu, lo, z1), P(uu, hi, z1)]
    for i in range(len(prof) - 1):
        a, b = 4 * i, 4 * (i + 1)
        faces += [(a, b, b + 2, a + 2),          # 面 lo
                  (a + 1, a + 3, b + 3, b + 1),  # 面 hi
                  (a, a + 1, b + 1, b),          # 下
                  (a + 2, b + 2, b + 3, a + 3)]  # 上
    e = 4 * (len(prof) - 1)
    faces += [(0, 2, 3, 1), (e, e + 1, e + 3, e + 2)]
    me = bpy.data.meshes.new('R_波板_' + ob.name)
    me.from_pydata(verts, [], faces)
    me.materials.append(M['container'])
    me.validate()
    wo = bpy.data.objects.new('R_波板_' + ob.name, me)
    coll.objects.link(wo)
    bv = wo.modifiers.new('Bevel', 'BEVEL')
    bv.width = 0.004
    bv.segments = 2
    bv.limit_method = 'ANGLE'
    # 上下のレール (角パイプ)
    mb = MB()
    def rail(za, zb):
        if along_x:
            mb.box_mm((u0, t0 - 0.012, za), (u1, t1 + 0.012, zb), M['container'])
        else:
            mb.box_mm((t0 - 0.012, u0, za), (t1 + 0.012, u1, zb), M['container'])
    if z1 - z0 > 0.3:
        rail(z1 - 0.07, z1 + 0.004)
        if z0 < 0.05:
            rail(0.0005, 0.085)
    mb.build('R_レール_' + ob.name, coll, bevel=0.006)


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


CJK_FONTS = ('/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
             '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
             'C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/simhei.ttf',
             '/System/Library/Fonts/PingFang.ttc')


def text_mesh(body, size, mat, parent, loc, rot, align='LEFT', coll=None):
    """文字をメッシュ化して配置 (フォントファイルを .blend に同梱しなくて済む)"""
    import os
    path = next((p for p in CJK_FONTS if os.path.exists(p)), None)
    if path is None:
        return None
    font = bpy.data.fonts.load(path, check_existing=True)
    cu = bpy.data.curves.new('txt', 'FONT')
    cu.body = body
    cu.font = font
    cu.size = size
    cu.align_x = align
    cu.align_y = 'CENTER'
    tmp = bpy.data.objects.new('txt', cu)
    bpy.context.scene.collection.objects.link(tmp)
    dg = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(tmp.evaluated_get(dg))
    bpy.data.objects.remove(tmp)
    bpy.data.curves.remove(cu)
    me.materials.clear()
    me.materials.append(mat)
    ob = bpy.data.objects.new('R_看板文字', me)
    (coll or parent.users_collection[0]).objects.link(ob)
    ob.parent = parent
    ob.location = loc
    ob.rotation_euler = rot
    return ob


def build_inoculator(ob, coll, A):
    """接種機 (植菌機): 写真を元にしたステンレスフレーム + 青アクリル囲い + 供給コンベヤ

    元の箱の床面範囲 (幅 x 奥行き) に収め、高さは作業者と釣り合う実寸にする。
    供給コンベヤは -Y 側に伸び、操作盤も -Y 側を向く。
    """
    mn, mx = bounds(ob)
    W, D = mx.x - mn.x, mx.y - mn.y
    cx, cy = (mn.x + mx.x) / 2, (mn.y + mx.y) / 2
    y0, y1 = -D / 2, D / 2
    mb = MB()
    st, t = M['steel'], 0.04          # 角パイプ 40mm

    def tube_z(x, y, za, zb):
        mb.box((x, y, (za + zb) / 2), (t, t, zb - za), st)

    def tube_x(xa, xb, y, z):
        mb.box(((xa + xb) / 2, y, z), (xb - xa, t, t), st)

    def tube_y(x, ya, yb, z):
        mb.box((x, (ya + yb) / 2, z), (t, yb - ya, t), st)

    # ---------------- 本体フレーム ----------------
    bx0, bx1 = -0.6, 0.6               # 本体の幅
    by0, by1 = -0.05, y1 - 0.04        # 本体の奥行き
    ztop = 1.6
    posts = [(x, y) for x in (bx0, bx1) for y in (by0, by1, (by0 + by1) / 2)]
    for x, y in posts:
        tube_z(x, y, 0.13, ztop)
    for z in (0.14, 0.46, 0.8, ztop):
        for y in (by0, by1):
            tube_x(bx0, bx1, y, z)
        for x in (bx0, bx1):
            tube_y(x, by0, by1, z)
    # 下部の縦格子
    for x in (bx0, bx1):
        k = 0
        y = by0 + 0.35
        while y < by1 - 0.05:
            mb.box((x, y, 0.3), (0.012, 0.012, 0.32), st)
            y += 0.07
    # キャスター
    for x in (bx0, bx1):
        for y in (by0, by1):
            mb.box((x, y, 0.118), (0.1, 0.08, 0.006), st)
            mb.box((x, y, 0.085), (0.05, 0.035, 0.06), st)
            mb.cyl((x - 0.02, y, 0.05), (x + 0.02, y, 0.05), 0.05, M['boot'], 18)
    # 側面の閉じた箱 (下部ギア室、丸窓付き)
    mb.box((bx0 + 0.03, (by0 + by1) / 2 - 0.25, 0.64), (0.012, 0.5, 0.3), st)
    mb.cyl((bx0 + 0.02, (by0 + by1) / 2 - 0.25, 0.64), (bx0 + 0.04, (by0 + by1) / 2 - 0.25, 0.64), 0.09, M['black_plastic'], 24)
    # 作業デッキ (コンベヤ部分は開口)
    for xa, xb in ((bx0, -0.34), (0.34, bx1)):
        mb.box(((xa + xb) / 2, (by0 + by1) / 2, 0.83), (xb - xa, by1 - by0, 0.008), st)

    # ---------------- 搬送部 (V 字の桟 + 緑ベルト + チェーン) ----------------
    cz = 0.74                           # 搬送面の高さ
    fy0 = y0 + 0.03
    for x in (-0.36, 0.36):             # サイドフレーム (チャンネル)
        mb.box((x, (fy0 + by1) / 2, cz - 0.06), (0.035, by1 - fy0, 0.14), st)
    for x in (-0.3, 0.3):               # ローラーチェーン
        mb.box((x, (fy0 + by1) / 2, cz - 0.005), (0.018, by1 - fy0 - 0.04, 0.02), M['black_plastic'])
    for x in (-0.13, 0.13):             # 緑のベルト
        mb.box((x, (fy0 + by1) / 2, cz - 0.02), (0.07, by1 - fy0 - 0.04, 0.01), M['belt'])
    flights = []
    y = fy0 + 0.1
    while y < by1 - 0.08:
        # V 字トレー (2 枚の傾いた板)
        for s_ in (-1, 1):
            m = (Matrix.Translation((0, y + s_ * 0.03, cz + 0.035)) @ Matrix.Rotation(s_ * math.radians(-35), 4, 'X')
                 @ Matrix.Diagonal((0.56, 0.075, 0.003, 1)))
            r = bmesh.ops.create_cube(mb.bm, size=1.0, matrix=m)
            mb._assign(r['verts'], st)
        mb.box((0, y, cz + 0.004), (0.56, 0.012, 0.008), st)
        flights.append(y)
        y += 0.26
    # ガイドレール (丸棒) と黒ノブ
    for x in (-0.33, 0.33):
        mb.cyl((x, fy0 + 0.05, cz + 0.13), (x, by0 - 0.02, cz + 0.13), 0.006, st, 8)
        for yy in (fy0 + 0.2, (fy0 + by0) / 2, by0 - 0.1):
            mb.cyl((x, yy, cz + 0.02), (x, yy, cz + 0.13), 0.006, st, 8)
            mb.cyl((x + math.copysign(0.03, x), yy, cz + 0.06), (x + math.copysign(0.05, x), yy, cz + 0.06), 0.014, M['black_plastic'], 10)
            mb.sphere((x + math.copysign(0.055, x), yy, cz + 0.06), (0.012, 0.018, 0.018), M['black_plastic'], 10, 6)
    # 供給コンベヤの端のカバー箱と脚
    mb.box((0, fy0 + 0.13, cz - 0.2), (0.8, 0.26, 0.36), st)
    mb.box((0.41, (fy0 + by0) / 2 + 0.1, cz - 0.2), (0.012, by0 - fy0 - 0.3, 0.36), st)
    mb.box((0.416, fy0 + 0.13, cz - 0.18), (0.004, 0.12, 0.05), M['black_plastic'])
    for yy in (fy0 + 0.03, (fy0 + by0) / 2):
        for x in (-0.36, 0.36):
            tube_z(x, yy, 0.0, cz - 0.38)
            mb.cyl((x, yy, 0), (x, yy, 0.012), 0.03, M['rubber'], 10)
        tube_x(-0.36, 0.36, yy, 0.15)

    # ---------------- 上部: ヘッダー看板 + 青アクリル囲い ----------------
    hz0, hz1 = ztop + 0.02, ztop + 0.26
    mb.box((0, (by0 + by1) / 2, (hz0 + hz1) / 2), (bx1 - bx0 + 0.04, by1 - by0 + 0.04, hz1 - hz0), M['sign_gray'])
    mb.box((0, (by0 + by1) / 2, hz0 - 0.008), (bx1 - bx0 + 0.02, by1 - by0 + 0.02, 0.016), M['white_panel'])
    fy = by0 - 0.021                                        # 看板の前面
    mb.box((bx0 + 0.13, fy - 0.001, (hz0 + hz1) / 2), (0.22, 0.004, 0.2), M['sign_blue'])
    for k in range(3):                                      # 注意ラベル
        mb.box((bx0 + 0.29 + k * 0.05, fy - 0.001, hz0 + 0.04), (0.04, 0.003, 0.05), M['warn'])
    mb.box((bx1 - 0.08, fy - 0.001, hz0 + 0.06), (0.07, 0.003, 0.07), M['qr'])
    sx = bx1 + 0.021                                        # 看板の右側面
    mb.box((sx + 0.001, by0 + 0.2, (hz0 + hz1) / 2), (0.004, 0.36, 0.22), M['sign_blue'])
    mb.box((sx + 0.001, (by0 + by1) / 2 + 0.2, (hz0 + hz1) / 2), (0.003, by1 - by0 - 0.44, 0.22), M['sign_light'])
    # 屋根の青アクリル
    mb.box((0, (by0 + by1) / 2, hz1 + 0.006), (bx1 - bx0 + 0.06, by1 - by0 + 0.06, 0.008), M['acrylic_blue'])
    # 囲い (側面・背面)。ヒンジとトグルラッチ付き
    az0, az1 = 0.9, ztop
    for x in (bx0 - 0.028, bx1 + 0.028):
        mb.box((x, (by0 + by1) / 2 + 0.05, (az0 + az1) / 2), (0.006, by1 - by0 - 0.1, az1 - az0), M['acrylic_blue'])
        for yy in (by0 + 0.25, (by0 + by1) / 2, by1 - 0.2):
            mb.box((x + math.copysign(0.006, x), yy, az1 - 0.03), (0.01, 0.06, 0.035), st)
    mb.box((0, by1 + 0.028, (az0 + az1) / 2), (bx1 - bx0, 0.006, az1 - az0), M['acrylic_blue'])
    for x in (-0.3, 0.3):
        mb.box((x, fy - 0.006, hz0 + 0.02), (0.02, 0.012, 0.05), st)
        mb.box((x, fy - 0.01, hz0 + 0.05), (0.012, 0.01, 0.02), M['lamp_r'])
    # 天井の LED バー
    mb.box((bx1 - 0.08, (by0 + by1) / 2, ztop - 0.03), (0.02, by1 - by0 - 0.1, 0.008), M['led'])
    # カウンター表示
    mb.box((0.05, fy + 0.05, hz1 + 0.05), (0.26, 0.05, 0.075), M['black_plastic'])
    mb.box((0.07, fy + 0.024, hz1 + 0.05), (0.18, 0.002, 0.04), M['counter'])

    # ---------------- 前面の操作盤 ----------------
    px0, px1 = bx0 + 0.02, bx0 + 0.9
    py0, py1 = by0 - 0.3, by0
    pz0, pz1 = 0.95, ztop
    mb.box(((px0 + px1) / 2, (py0 + py1) / 2, (pz0 + pz1) / 2), (px1 - px0, py1 - py0, pz1 - pz0), st)
    pf = py0 - 0.002
    mid = px0 + 0.4
    mb.box((mid, pf, (pz0 + pz1) / 2), (0.006, 0.003, pz1 - pz0 - 0.02), M['black_plastic'])   # 扉の合わせ目
    for zz in (pz1 - 0.14, pz1 - 0.38):
        mb.box((px0 + 0.2, pf - 0.001, zz + 0.045), (0.3, 0.003, 0.03), M['sign_blue'])
        for k in range(4):
            x = px0 + 0.08 + k * 0.08
            mb.cyl((x, pf, zz), (x, pf - 0.02, zz), 0.022, st, 16)
            mb.cyl((x, pf - 0.02, zz), (x, pf - 0.028, zz), 0.017,
                   M['lamp_g'] if (zz > pz1 - 0.2 and k in (1, 2)) else M['btn_green'], 16)
    # タッチパネル
    hx = mid + 0.24
    mb.box((hx, pf - 0.012, pz1 - 0.2), (0.3, 0.024, 0.22), M['black_plastic'])
    mb.box((hx, pf - 0.025, pz1 - 0.2), (0.25, 0.002, 0.17), M['screen'])
    # 緑テープで貼った紙
    mb.box((hx, pf - 0.003, pz1 - 0.47), (0.3, 0.002, 0.26), M['paper'])
    for dz in (-0.13, 0.13):
        mb.box((hx, pf - 0.0045, pz1 - 0.47 + dz), (0.33, 0.002, 0.035), M['green_tape'])
    for dx in (-0.15, 0.15):
        mb.box((hx + dx, pf - 0.0045, pz1 - 0.47), (0.035, 0.002, 0.28), M['green_tape'])
    # 非常停止
    mb.box((bx1, by0 - 0.045, 1.35), (0.1, 0.05, 0.1), M['yellow'])
    mb.cyl((bx1, by0 - 0.07, 1.35), (bx1, by0 - 0.09, 1.35), 0.018, M['black_plastic'], 12)
    mb.cyl((bx1, by0 - 0.09, 1.35), (bx1, by0 - 0.11, 1.35), 0.032, M['lamp_r'], 20)
    # 前面の右側は透明アクリル
    mb.box(((px1 + bx1) / 2 + 0.02, by0 - 0.028, (az0 + az1) / 2), (bx1 - px1 - 0.06, 0.006, az1 - az0), M['acrylic_blue'])

    # ---------------- 内部機構 ----------------
    # 種菌ホッパー (四角錐台)
    hx_, hy_ = -0.08, (by0 + by1) / 2 - 0.05
    m = Matrix.Translation((hx_, hy_, 1.12)) @ Matrix.Rotation(math.pi / 4, 4, 'Z')
    r = bmesh.ops.create_cone(mb.bm, cap_ends=True, segments=4, radius1=0.1, radius2=0.3, depth=0.26, matrix=m)
    mb._assign(r['verts'], st)
    mb.box((hx_, hy_, 0.94), (0.14, 0.14, 0.1), st)
    for k in range(4):                                     # 上部のボルト列
        mb.cyl((hx_ + 0.1, hy_ - 0.15 + k * 0.1, 1.25), (hx_ + 0.1, hy_ - 0.15 + k * 0.1, 1.3), 0.012, st, 6)
    # 計量ボックス (ルーバー付き)
    bxa, bxb, bya, byb = 0.12, bx1 - 0.04, hy_ - 0.1, by1 - 0.05
    mb.box(((bxa + bxb) / 2, (bya + byb) / 2, 1.2), (bxb - bxa, byb - bya, 0.74), M['galv'])
    for k in range(8):
        mb.box((bxb - 0.1, bya - 0.004, 1.43 + k * 0.014), (0.12, 0.006, 0.006), st)
    # オイラーボトル
    mb.cyl((bxa - 0.06, bya + 0.2, 1.2), (bxa - 0.06, bya + 0.2, 1.34), 0.045, M['white_panel'], 20)
    mb.cyl((bxa - 0.06, bya + 0.2, 1.3), (bxa - 0.06, bya + 0.2, 1.31), 0.046, M['lamp_r'], 20)
    mb.cyl((bxa - 0.06, bya + 0.2, 1.2), (bxa - 0.06, bya + 0.2, 1.12), 0.01, M['white_panel'], 8)
    mb.cyl((bxa - 0.06, bya + 0.2, 1.12), (bxa - 0.2, bya + 0.3, 0.9), 0.004, M['white_panel'], 6)
    # モーター・スプロケット・チェーン
    mx_, my_ = -0.4, (by0 + by1) / 2 + 0.25
    mb.cyl((mx_, my_, 1.0), (mx_ + 0.22, my_, 1.0), 0.065, M['black_plastic'], 20)
    mb.box((mx_ + 0.27, my_, 1.0), (0.1, 0.13, 0.13), M['alu'])
    for (sy_, sz_, r_) in ((my_ - 0.12, 0.95, 0.055), (my_ + 0.12, 0.95, 0.045)):
        mb.cyl((-0.2, sy_, sz_), (-0.19, sy_, sz_), r_, st, 20)
        mb.cyl((-0.21, sy_, sz_), (-0.18, sy_, sz_), 0.012, st, 8)
    mb.box((-0.195, my_, 0.95 + 0.05), (0.008, 0.24, 0.01), M['black_plastic'])
    mb.box((-0.195, my_, 0.95 - 0.05), (0.008, 0.24, 0.01), M['black_plastic'])
    mb.cyl((-0.19, my_ - 0.12, 0.95), (0.05, my_ - 0.12, 0.95), 0.012, st, 8)
    # ガイドシャフト
    for (gx, gy) in ((0.05, hy_ - 0.2), (0.05, hy_ + 0.25), (-0.3, hy_ - 0.2)):
        mb.cyl((gx, gy, 0.83), (gx, gy, 1.28), 0.013, st, 10)
        mb.cyl((gx, gy, 1.28), (gx, gy, 1.31), 0.022, st, 6)
        mb.cyl((gx, gy, 0.86), (gx, gy, 0.92), 0.022, M['black_plastic'], 10)
    mb.box((-0.12, hy_ - 0.2, 1.05), (0.4, 0.05, 0.012), st)

    mach = mb.build('R_' + ob.name, coll, bevel=0.002, loc=(cx, cy, 0))

    # 看板の文字 (前面・右側面)
    text_mesh('金博洋', 0.055, M['white_panel'], mach, (bx0 + 0.13, fy - 0.004, (hz0 + hz1) / 2),
              (math.pi / 2, 0, 0), 'CENTER', coll)
    text_mesh('纯电动香菇固体接种机', 0.062, M['sign_text'], mach, (bx0 + 0.26, fy - 0.003, (hz0 + hz1) / 2 + 0.035),
              (math.pi / 2, 0, 0), 'LEFT', coll)
    text_mesh('金博洋', 0.1, M['white_panel'], mach, (sx + 0.004, by0 + 0.2, (hz0 + hz1) / 2),
              (math.pi / 2, 0, math.pi / 2), 'CENTER', coll)
    text_mesh('纯电动香菇固体接种机', 0.1, M['sign_text'], mach, (sx + 0.003, (by0 + by1) / 2 + 0.2, (hz0 + hz1) / 2),
              (math.pi / 2, 0, math.pi / 2), 'CENTER', coll)

    # 桟に載った菌床袋
    for k, yy in enumerate(flights):
        if yy < by0 - 0.1 and k % 3 != 2:
            b = MB()
            b.cyl((-0.24, 0, 0), (0.24, 0, 0), 0.052, M['bag'], 20)
            b.sphere((-0.24, 0, 0), (0.01, 0.05, 0.05), M['bag'], 12, 8)
            b.sphere((0.24, 0, 0), (0.01, 0.05, 0.05), M['bag'], 12, 8)
            bo = b.build('菌床袋', coll, loc=(cx, cy + yy, cz + 0.06))


def build_conveyor(ob, coll, A):
    """ベルトコンベヤ (写真ベース): 緑の PVC 平ベルト + ステンレスフレーム + 駆動ボックス

    元の箱の床面範囲に収め、ベルト面の高さは接種機の搬送面とそろえて 0.75m。
    駆動ボックスは +Y 側の端。
    """
    mn, mx = bounds(ob)
    W, D = mx.x - mn.x, mx.y - mn.y
    cx, cy = (mn.x + mx.x) / 2, (mn.y + mx.y) / 2
    st = M['steel']
    mb = MB()
    bw = 0.62                   # ベルト幅
    L = D - 0.08                # フレーム長さ
    ya, yb = -L / 2, L / 2
    bz = 0.75                   # ベルト上面
    fx = bw / 2 + 0.025         # サイドフレーム中心
    # ベルト (上面・リターン・両端の巻き付き)
    mb.box((0, 0, bz - 0.002), (bw, L - 0.08, 0.004), M['pvc_belt'])
    mb.box((0, 0, bz - 0.084), (bw, L - 0.08, 0.004), M['pvc_belt'])
    for y in (ya + 0.04, yb - 0.04):
        mb.cyl((-bw / 2, y, bz - 0.043), (bw / 2, y, bz - 0.043), 0.043, M['pvc_belt'], 24)
        mb.cyl((-fx, y, bz - 0.043), (fx, y, bz - 0.043), 0.012, st, 10)
    # 受け板
    mb.box((0, 0, bz - 0.01), (bw - 0.02, L - 0.2, 0.008), M['galv'])
    # サイドフレーム (角パイプ、ベルト面より少し高い縁)
    for s in (-1, 1):
        mb.box((s * fx, 0, bz - 0.03), (0.045, L, 0.075), st)
        mb.box((s * (fx - 0.018), 0, bz + 0.012), (0.01, L, 0.012), st)
    # 脚 (角パイプ + 上部のテーパー金具 + アジャスタ)
    legs_y = (ya + 0.28, 0.0, yb - 0.45)
    for y in legs_y:
        for s in (-1, 1):
            x = s * fx
            mb.box((x, y, 0.37), (0.05, 0.05, 0.52), st)
            m = (Matrix.Translation((x, y, bz - 0.11)) @ Matrix.Rotation(math.pi / 4, 4, 'Z'))
            r = bmesh.ops.create_cone(mb.bm, cap_ends=True, segments=4, radius1=0.036, radius2=0.1,
                                      depth=0.08, matrix=m)
            mb._assign(r['verts'], st)
            mb.cyl((x, y, 0.02), (x, y, 0.11), 0.009, M['brass'], 8)       # ねじ棒
            mb.cyl((x, y, 0.07), (x, y, 0.09), 0.016, M['brass'], 6)       # ナット
            mb.cyl((x, y, 0.0), (x, y, 0.02), 0.04, M['rubber'], 16)      # ゴム足
        mb.box((0, y, 0.2), (2 * fx, 0.04, 0.04), st)                     # 横つなぎ
    for s in (-1, 1):                                                     # 縦つなぎ
        mb.box((s * fx, (legs_y[0] + legs_y[-1]) / 2, 0.2), (0.04, legs_y[-1] - legs_y[0], 0.04), st)
    # 駆動ボックス (+Y 端、ベルトの下に吊り下げ)
    mb.box((0.12, yb - 0.05, bz - 0.22), (0.3, 0.2, 0.34), st)
    mb.box((0.12, yb - 0.05, bz - 0.2), (0.3, 0.21, 0.004), st)
    mb.cyl((0.12 + 0.151, yb - 0.08, bz - 0.12), (0.12 + 0.16, yb - 0.08, bz - 0.12), 0.012, st, 8)
    mb.box((-0.2, yb - 0.05, bz - 0.2), (0.14, 0.14, 0.26), M['white_panel'])   # モーター
    # 切替スイッチ
    mb.cyl((fx + 0.023, yb - 0.25, bz - 0.02), (fx + 0.045, yb - 0.25, bz - 0.02), 0.014, st, 12)
    mb.box((fx + 0.052, yb - 0.25, bz - 0.02), (0.014, 0.008, 0.028), M['black_plastic'])
    # 床置きの電源ボックス (オレンジ) + 巻いたコード
    ox, oy = -0.05, 0.25
    mb.box((ox, oy, 0.12), (0.26, 0.4, 0.24), M['orange_box'])
    for k in range(5):
        mb.box((ox + 0.131, oy + 0.08, 0.06 + k * 0.025), (0.004, 0.14, 0.008), M['black_plastic'])
    mb.box((ox - 0.131, oy - 0.1, 0.12), (0.004, 0.06, 0.08), M['black_plastic'])
    for k, (r_, zz, dx, dy) in enumerate(((0.12, 0.245, 0.0, 0.02), (0.1, 0.255, 0.02, -0.03), (0.09, 0.265, -0.02, 0.0))):
        mb.torus((ox + dx, oy + dy, zz), r_, 0.004, M['white_panel'], tilt=0.12 * (k - 1))
    for k, pts in enumerate((((ox - 0.13, oy - 0.1, 0.02), (ox - 0.4, oy - 0.3, 0.008), (-0.6, -0.7, 0.008)),
                             ((ox + 0.1, oy + 0.2, 0.02), (0.3, oy + 0.5, 0.008), (0.45, yb - 0.3, 0.008)))):
        for p0, p1 in zip(pts[:-1], pts[1:]):
            mb.cyl(p0, p1, 0.007, M['rubber'], 8)
    mb.build('R_' + ob.name, coll, bevel=0.002, loc=(cx, cy, 0))


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
    perimeter_walls = []
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
            mn_, mx_ = bounds(ob)
            thin = min(mx_.x - mn_.x, mx_.y - mn_.y) < 0.2
            on_perimeter = (mx_.y < 0.25 or mn_.y > 15.75) if (mx_.x - mn_.x) > (mx_.y - mn_.y) \
                else (mx_.x < 0.25 or mn_.x > 11.75)
            if thin and on_perimeter and (mx_.z - mn_.z) > 1.0 and n not in ('Wall.022',):
                # 外周 = コンテナの壁 → 台形波板 (仕切り壁はクリーンルームパネルのまま)
                build_corrugated(ob, real)
                perimeter_walls.append(ob)
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

    # コンテナの四隅のコーナーポスト
    if perimeter_walls:
        mb = MB()
        for cx_, cy_ in ((0.075, 0.08), (11.925, 0.08), (0.075, 15.925), (11.925, 15.925)):
            mb.box((cx_, cy_, 0.6), (0.19, 0.19, 1.204), M['container'])
        mb.build('R_コーナーポスト', real, bevel=0.008)

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
