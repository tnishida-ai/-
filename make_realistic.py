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

WALL_SCALE = 2.0   # 壁の高さの倍率 (元モデル 1.2m → 2.4m)
DOOR_WALL = 'Wall.001'   # コンテナ扉を付ける壁 (北側外周、作業室の棚の後ろ)
CONTAINER_H = 2.65       # コンテナ (外周の波板壁) の高さ
DOOR_X = (9.25, 11.55)   # 観音開き扉の開口の X 範囲 (幅 2.3m、右寄り)
DOOR_H = 2.5             # 観音開き扉の開口の高さ
SINGLE_DOOR_X = (7.15, 8.05)   # 片開き扉の開口の X 範囲 (観音開き扉の左側)
SINGLE_DOOR_TOP = 1.95         # 片開き扉の開口上端
AIR_SHOWER_XY = (10.64, 11.84, 2.595, 3.595)   # エアシャワー外形 (W1200 x D1000)
AIR_SHOWER_BACK_WALL = 'Wall.009'             # エアシャワー出口側の壁 (作業場との境)
HANGER_MOVE = {                                # ハンガーラックの移動先 (エアシャワーのある部屋の西側)
    'ハンガーラック.001': (9.4, 2.45),
}
SHOE_BOX_XY = (11.685, 0.53)                   # 靴箱 (入口側の小部屋、入って右 = 東の壁に付ける)
SHOE_BOX_RZ = -math.pi / 2                     # 棚の口を部屋の内側 (西) に向ける
DOMA = (9.15, 11.48, 0.16, 0.55)               # 土間 (入口の小部屋、入ってすぐ) x0, x1, y0, y1
DOMA_DEPTH = 0.15                              # 土間の下がり
STEEL_FLOOR_ROOMS = {                          # 鉄板の床にする部屋 (x0, x1, y0, y1)
    '前室': (9.15, 11.86, 0.16, 3.6),
    '作業室': (6.15, 11.86, 11.0, 15.85),
}


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

    # 黒色樹脂パレット
    mat, nt, b = new_material('R_樹脂パレット', (0.012, 0.012, 0.013), 0.6, spec=0.3)
    v = tex_coord(nt)
    nt.links.new(ramp(nt, noise(nt, v, 6, 4), [(0.3, (0.008, 0.008, 0.009)), (0.7, (0.018, 0.018, 0.02))]), b.inputs['Base Color'])
    nt.links.new(map_range(nt, noise(nt, v, 3, 4), 0.35, 0.65, 0.5, 0.72), b.inputs['Roughness'])
    vor = nt.nodes.new('ShaderNodeTexVoronoi')
    vor.inputs['Scale'].default_value = 120.0
    nt.links.new(v, vor.inputs['Vector'])
    bump(nt, b, map_range(nt, vor.outputs['Distance'], 0.0, 0.3, 1.0, 0.0), 0.25, 0.002)
    M['pallet'] = mat

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
    build_kinbou_materials()
    build_spawn_bag_materials()
    build_stretch_film_material()
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
    # テープ貼り機まわり
    mat, nt, b = new_material('R_ステンレス_使用感', (0.72, 0.72, 0.7), 0.3, 1.0)
    v = tex_coord(nt)
    patch = map_range(nt, noise(nt, v, 9, 6, 0.7), 0.45, 0.62, 0.0, 1.0)
    nt.links.new(ramp(nt, patch, [(0.0, (0.76, 0.76, 0.75)), (1.0, (0.5, 0.47, 0.42))]), b.inputs['Base Color'])
    nt.links.new(map_range(nt, patch, 0.0, 1.0, 0.22, 0.65), b.inputs['Roughness'])
    bump(nt, b, noise(nt, v, 60, 4, 0.6, stretch=(1, 30, 1)), 0.03)
    M['steel_worn'] = mat
    mat, nt, b = new_material('R_グリップベルト', (0.02, 0.3, 0.13), 0.65)
    v = tex_coord(nt)
    bump(nt, b, noise(nt, v, 500, 2, 0.5), 0.5, 0.003)
    M['grip_belt'] = mat
    M['motor_gray'] = new_material('R_モーター塗装', (0.35, 0.38, 0.4), 0.4)[0]
    M['tape_roll'] = new_material('R_テープロール', (0.88, 0.85, 0.75), 0.25, sss=0.3)[0]
    # 菌床ラックまわり
    M['tray_black'] = new_material('R_菌床トレー', (0.04, 0.043, 0.048), 0.4, spec=0.5)[0]
    mat, nt, b = new_material('R_亜鉛メッキ角パイプ', (0.66, 0.68, 0.68), 0.4, 1.0)
    v = tex_coord(nt)
    sp = noise(nt, v, 25, 3, 0.5)
    nt.links.new(ramp(nt, sp, [(0.35, (0.58, 0.6, 0.6)), (0.65, (0.72, 0.74, 0.74))]), b.inputs['Base Color'])
    nt.links.new(map_range(nt, sp, 0.3, 0.7, 0.3, 0.55), b.inputs['Roughness'])
    M['galv_tube'] = mat
    M['red_tape'] = new_material('R_赤テープ', (0.75, 0.03, 0.04), 0.5)[0]
    # コンテナ扉まわり
    mat, nt, b = new_material('R_注意ステッカー', (0.9, 0.7, 0.02), 0.5)
    wv = nt.nodes.new('ShaderNodeTexWave')
    wv.bands_direction = 'DIAGONAL'
    wv.inputs['Scale'].default_value = 1.2
    wv.inputs['Distortion'].default_value = 0.0
    nt.links.new(tex_coord(nt, 'Object'), wv.inputs['Vector'])
    r_ = nt.nodes.new('ShaderNodeValToRGB')
    r_.color_ramp.interpolation = 'CONSTANT'
    r_.color_ramp.elements[0].color = (0.9, 0.68, 0.02, 1)
    r_.color_ramp.elements[1].position = 0.5
    r_.color_ramp.elements[1].color = (0.02, 0.02, 0.02, 1)
    nt.links.new(wv.outputs['Fac'], r_.inputs['Fac'])
    nt.links.new(r_.outputs['Color'], b.inputs['Base Color'])
    M['hazard'] = mat
    M['bolt_head'] = new_material('R_ボルト頭', (0.62, 0.55, 0.42), 0.6)[0]
    M['chrome'] = new_material('R_クローム', (0.9, 0.9, 0.9), 0.08, 1.0)[0]
    M['knob_beige'] = new_material('R_内開放ノブ', (0.78, 0.7, 0.52), 0.4)[0]
    M['as_panel'] = new_material('R_エアシャワー外装', (0.84, 0.85, 0.85), 0.35)[0]
    M['nozzle'] = new_material('R_ノズル', (0.35, 0.3, 0.75), 0.35)[0]
    # 鉄板の床 (1200 x 2400 割付の目地、色ムラ・擦り傷・汚れ)
    mat, nt, b = new_material('R_鉄板床', (0.4, 0.41, 0.42), 0.4, 0.9)
    geo = nt.nodes.new('ShaderNodeNewGeometry')
    pos = geo.outputs['Position']
    sep = nt.nodes.new('ShaderNodeSeparateXYZ')
    nt.links.new(pos, sep.inputs[0])
    masks = []
    for axis, pitch in (('X', 1.2), ('Y', 2.4)):
        f = math_node(nt, 'FRACT', math_node(nt, 'DIVIDE', sep.outputs[axis], pitch))
        d = math_node(nt, 'MINIMUM', f, math_node(nt, 'SUBTRACT', 1.0, f))
        masks.append(map_range(nt, math_node(nt, 'MULTIPLY', d, pitch), 0.0, 0.003, 1.0, 0.0))
    seam = math_node(nt, 'MAXIMUM', masks[0], masks[1])
    mottle = noise(nt, pos, 1.5, 6, 0.6)
    base = ramp(nt, mottle, [(0.3, (0.3, 0.31, 0.32)), (0.55, (0.42, 0.43, 0.44)), (0.8, (0.5, 0.49, 0.47))])
    dirt = map_range(nt, noise(nt, pos, 4, 5, 0.65), 0.5, 0.7, 0.0, 0.7)
    mix1 = nt.nodes.new('ShaderNodeMix'); mix1.data_type = 'RGBA'
    nt.links.new(dirt, mix1.inputs['Factor'])
    nt.links.new(base, mix1.inputs[6])
    mix1.inputs[7].default_value = (0.18, 0.17, 0.15, 1)
    mix2 = nt.nodes.new('ShaderNodeMix'); mix2.data_type = 'RGBA'
    nt.links.new(seam, mix2.inputs['Factor'])
    nt.links.new(mix1.outputs[2], mix2.inputs[6])
    mix2.inputs[7].default_value = (0.05, 0.05, 0.05, 1)
    nt.links.new(mix2.outputs[2], b.inputs['Base Color'])
    scratch = noise(nt, pos, 25, 4, 0.6, stretch=(1, 12, 1))
    nt.links.new(map_range(nt, math_node(nt, 'ADD', scratch, dirt), 0.3, 1.2, 0.28, 0.65), b.inputs['Roughness'])
    bump(nt, b, math_node(nt, 'SUBTRACT', math_node(nt, 'MULTIPLY', scratch, 0.2), seam), 0.3, 0.002)
    M['floor_steel'] = mat
    # 土間のタイル (300 角、グレー、目地)
    mat, nt, b = new_material('R_土間タイル', (0.42, 0.41, 0.39), 0.6)
    geo = nt.nodes.new('ShaderNodeNewGeometry')
    sep = nt.nodes.new('ShaderNodeSeparateXYZ')
    nt.links.new(geo.outputs['Position'], sep.inputs[0])
    masks = []
    for axis in ('X', 'Y'):
        f = math_node(nt, 'FRACT', math_node(nt, 'DIVIDE', sep.outputs[axis], 0.3))
        d = math_node(nt, 'MINIMUM', f, math_node(nt, 'SUBTRACT', 1.0, f))
        masks.append(map_range(nt, math_node(nt, 'MULTIPLY', d, 0.3), 0.0, 0.004, 1.0, 0.0))
    grout = math_node(nt, 'MAXIMUM', masks[0], masks[1])
    speck = ramp(nt, noise(nt, geo.outputs['Position'], 60, 4, 0.6), [(0.35, (0.36, 0.35, 0.33)), (0.65, (0.46, 0.45, 0.43))])
    mix = nt.nodes.new('ShaderNodeMix'); mix.data_type = 'RGBA'
    nt.links.new(grout, mix.inputs['Factor'])
    nt.links.new(speck, mix.inputs[6])
    mix.inputs[7].default_value = (0.2, 0.2, 0.19, 1)
    nt.links.new(mix.outputs[2], b.inputs['Base Color'])
    bump(nt, b, math_node(nt, 'SUBTRACT', 1.0, grout), 0.3, 0.003)
    M['doma_tile'] = mat
    # 上がり框 (木)
    mat, nt, b = new_material('R_上がり框', (0.45, 0.28, 0.14), 0.4, coat=0.4)
    v = tex_coord(nt)
    grain = noise(nt, v, 6, 6, 0.6, stretch=(0.08, 1, 1))
    nt.links.new(ramp(nt, grain, [(0.3, (0.32, 0.19, 0.09)), (0.7, (0.55, 0.36, 0.19))]), b.inputs['Base Color'])
    bump(nt, b, grain, 0.1)
    M['wood_kamachi'] = mat
    M['plate'] = new_material('R_銘板', (0.45, 0.42, 0.38), 0.4, 0.8)[0]
    M['line_paint'] = new_material('R_区画線', (0.8, 0.8, 0.78), 0.7)[0]


def substrate_material(name, base_stops, speck_stops, fine_thr, flake_thr):
    """袋越しに見えるおが粉培地: 大きなムラの地色に、細かい粒と大きめのかけらが散る"""
    mat, nt, b = new_material(name, base_stops[1][1], 0.8, sss=0.05)
    tc = nt.nodes.new('ShaderNodeTexCoord')
    oi = nt.nodes.new('ShaderNodeObjectInfo')
    off = nt.nodes.new('ShaderNodeVectorMath'); off.operation = 'MULTIPLY_ADD'
    off.inputs[1].default_value = (1.0, 1.0, 1.0)
    comb = nt.nodes.new('ShaderNodeCombineXYZ')
    for i, k in enumerate((17.0, 11.0, 5.0)):
        nt.links.new(math_node(nt, 'MULTIPLY', oi.outputs['Random'], k), comb.inputs[i])
    nt.links.new(tc.outputs['Object'], off.inputs[0])
    nt.links.new(comb.outputs[0], off.inputs[2])
    v = off.outputs[0]
    base = ramp(nt, noise(nt, v, 7, 5, 0.65), base_stops)
    fine = ramp(nt, noise(nt, v, 220, 3, 0.65), [(fine_thr, (0, 0, 0)), (fine_thr + 0.05, (1, 1, 1))])
    flake = ramp(nt, noise(nt, v, 55, 4, 0.7), [(flake_thr, (0, 0, 0)), (flake_thr + 0.06, (1, 1, 1))])
    dots = math_node(nt, 'MAXIMUM', fine, flake)
    speck = ramp(nt, noise(nt, v, 60, 3, 0.6), speck_stops)
    mix = nt.nodes.new('ShaderNodeMix'); mix.data_type = 'RGBA'
    nt.links.new(dots, mix.inputs['Factor'])
    nt.links.new(base, mix.inputs[6]); nt.links.new(speck, mix.inputs[7])
    nt.links.new(mix.outputs[2], b.inputs['Base Color'])
    bump(nt, b, math_node(nt, 'ADD', noise(nt, v, 80, 4, 0.6), math_node(nt, 'MULTIPLY', dots, 0.5)), 0.35, 0.003)
    return mat


def build_kinbou_materials():
    """菌棒 (袋入りのおが粉培地に菌糸がまわったもの) 用のマテリアル"""
    # 中身: 白い菌糸に、おが粉の茶色い粒と、菌糸の薄い灰色のムラが透ける
    M['kinbou'] = substrate_material(
        'R_菌棒', [(0.38, (0.3, 0.32, 0.35)), (0.5, (0.46, 0.47, 0.48)), (0.62, (0.6, 0.6, 0.58))],
        [(0.3, (0.1, 0.055, 0.04)), (0.7, (0.24, 0.13, 0.08))], 0.58, 0.62)
    # パレット積みの菌棒 (接種前): 青みがかった灰色に、黒っぽいおが粉の粒が密に見える
    M['kinbou_raw'] = substrate_material(
        'R_菌棒_接種前', [(0.35, (0.18, 0.2, 0.24)), (0.5, (0.29, 0.31, 0.36)), (0.65, (0.44, 0.45, 0.49))],
        [(0.3, (0.04, 0.035, 0.04)), (0.7, (0.14, 0.1, 0.09))], 0.55, 0.6)

    # 袋のフィルム: 透明 + 映り込み、細かいシワ
    mat, nt, b = new_material('R_菌棒フィルム', (0.2, 0.2, 0.2), 0.1, spec=0.8)
    out = next(n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL')
    tr = nt.nodes.new('ShaderNodeBsdfTransparent')
    lw = nt.nodes.new('ShaderNodeLayerWeight'); lw.inputs['Blend'].default_value = 0.3
    ms = nt.nodes.new('ShaderNodeMixShader')
    nt.links.new(map_range(nt, lw.outputs['Fresnel'], 0.0, 1.0, 0.12, 0.75), ms.inputs['Fac'])
    nt.links.new(tr.outputs[0], ms.inputs[1])
    nt.links.new(b.outputs[0], ms.inputs[2])
    nt.links.new(ms.outputs[0], out.inputs['Surface'])
    v = tex_coord(nt)
    bump(nt, b, noise(nt, v, 14, 4, 0.6, stretch=(0.35, 1.0, 1.0)), 0.25, 0.004)
    M['film'] = mat

    # 接種穴の種菌 (粒の粗い茶色のおが粉)
    mat, nt, b = new_material('R_菌棒_種菌', (0.3, 0.17, 0.08), 0.95)
    v = tex_coord(nt)
    n = noise(nt, v, 260, 4, 0.75)
    nt.links.new(ramp(nt, n, [(0.3, (0.07, 0.035, 0.018)), (0.55, (0.2, 0.1, 0.045)), (0.78, (0.38, 0.22, 0.1))]),
                 b.inputs['Base Color'])
    bump(nt, b, n, 0.9, 0.003)
    M['kinbou_spawn'] = mat

    # マスキングテープ (紙・クレープ)
    mat, nt, b = new_material('R_マスキングテープ', (0.86, 0.85, 0.74), 0.85)
    v = tex_coord(nt)
    bump(nt, b, noise(nt, v, 300, 3, 0.6, stretch=(3.0, 0.4, 1.0)), 0.15, 0.002)
    M['masking'] = mat
    M['tie'] = new_material('R_結束', (0.88, 0.88, 0.86), 0.45, sss=0.15, transmission=0.2)[0]


def build_stretch_film_material():
    """パレット巻きのストレッチフィルム: 透明 + 映り込み、横方向の伸びじわと白い曇り"""
    mat, nt, b = new_material('R_ストレッチフィルム', (0.85, 0.85, 0.85), 0.08, spec=0.9)
    out = next(n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL')
    tr = nt.nodes.new('ShaderNodeBsdfTransparent')
    lw = nt.nodes.new('ShaderNodeLayerWeight'); lw.inputs['Blend'].default_value = 0.35
    v = tex_coord(nt)
    haze = noise(nt, v, 3, 4, 0.6, stretch=(1.0, 1.0, 4.0))
    fac = math_node(nt, 'ADD', map_range(nt, lw.outputs['Fresnel'], 0.0, 1.0, 0.04, 0.6),
                    map_range(nt, haze, 0.5, 0.75, 0.0, 0.25))
    ms = nt.nodes.new('ShaderNodeMixShader')
    nt.links.new(math_node(nt, 'MINIMUM', fac, 0.85), ms.inputs['Fac'])
    nt.links.new(tr.outputs[0], ms.inputs[1])
    nt.links.new(b.outputs[0], ms.inputs[2])
    nt.links.new(ms.outputs[0], out.inputs['Surface'])
    bump(nt, b, noise(nt, v, 9, 5, 0.65, stretch=(0.25, 0.25, 6.0)), 0.5, 0.006)
    M['stretch'] = mat


def build_spawn_bag_materials():
    # 袋入りの種菌: 茶色のおが粉に、白い菌糸のかたまりと小さな白い粒
    mat, nt, b = new_material('R_種菌_袋', (0.4, 0.32, 0.25), 0.9)
    tc = nt.nodes.new('ShaderNodeTexCoord')
    oi = nt.nodes.new('ShaderNodeObjectInfo')
    off = nt.nodes.new('ShaderNodeVectorMath'); off.operation = 'ADD'
    comb = nt.nodes.new('ShaderNodeCombineXYZ')
    for i, k in enumerate((13.0, 7.0, 3.0)):
        nt.links.new(math_node(nt, 'MULTIPLY', oi.outputs['Random'], k), comb.inputs[i])
    nt.links.new(tc.outputs['Object'], off.inputs[0])
    nt.links.new(comb.outputs[0], off.inputs[1])
    v = off.outputs[0]
    saw = ramp(nt, noise(nt, v, 160, 4, 0.75), [(0.3, (0.22, 0.14, 0.08)), (0.5, (0.45, 0.36, 0.27)),
                                                 (0.7, (0.68, 0.66, 0.63))])
    myc = ramp(nt, noise(nt, v, 11, 5, 0.7), [(0.44, (0, 0, 0)), (0.58, (1, 1, 1))])
    dots = ramp(nt, noise(nt, v, 90, 2, 0.5), [(0.62, (0, 0, 0)), (0.66, (1, 1, 1))])
    white = math_node(nt, 'MAXIMUM', myc, dots)
    mix = nt.nodes.new('ShaderNodeMix'); mix.data_type = 'RGBA'
    nt.links.new(map_range(nt, white, 0.0, 1.0, 0.12, 0.95), mix.inputs['Factor'])
    nt.links.new(saw, mix.inputs[6])
    mix.inputs[7].default_value = (0.86, 0.85, 0.82, 1.0)
    nt.links.new(mix.outputs[2], b.inputs['Base Color'])
    bump(nt, b, math_node(nt, 'ADD', noise(nt, v, 160, 4, 0.75), white), 0.4, 0.003)
    M['spawn_bag'] = mat
    M['cap_white'] = new_material('R_キャップ_白', (0.92, 0.92, 0.9), 0.35, sss=0.1)[0]
    M['cap_pink'] = new_material('R_キャップ_綿栓', (0.9, 0.6, 0.65), 0.8)[0]


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

    def cyl(self, p0, p1, r, mat, segs=16, r2=None, smooth=True, caps=True):
        p0, p1 = Vector(p0), Vector(p1)
        d = p1 - p0
        rot = d.to_track_quat('Z', 'Y').to_matrix().to_4x4()
        m = Matrix.Translation((p0 + p1) / 2) @ rot
        res = bmesh.ops.create_cone(self.bm, cap_ends=caps, segments=segs,
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

    def cyl_patch(self, x0, x1, r, a0, a1, mat, nx=4, na=6):
        """X 軸まわりの円筒面の一部 (貼ったテープ等)。a は +Z からの角度"""
        vs = [[self.bm.verts.new((x0 + (x1 - x0) * i / nx,
                                  -r * math.sin(a0 + (a1 - a0) * j / na),
                                  r * math.cos(a0 + (a1 - a0) * j / na)))
               for j in range(na + 1)] for i in range(nx + 1)]
        idx = self._mi(mat)
        for i in range(nx):
            for j in range(na):
                f = self.bm.faces.new((vs[i][j], vs[i + 1][j], vs[i + 1][j + 1], vs[i][j + 1]))
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


def build_corrugated(ob, coll, gap=None, z_top=None):
    """コンテナの台形波板壁。壁の箱の範囲を、両面とも同じ波形を持つ鋼板で置き換える

    gap=(ua, ub) またはそのリストを渡すと、その区間 (長手方向) は波板と上レールを抜く (扉の開口用)。
    下レールは敷居として通す。
    """
    mn, mx = bounds(ob)
    along_x = (mx.x - mn.x) >= (mx.y - mn.y)
    u0, u1 = (mn.x, mx.x) if along_x else (mn.y, mx.y)
    t0, t1 = (mn.y, mx.y) if along_x else (mn.x, mx.x)
    z0, z1 = max(mn.z, 0.0) + 0.001, (mx.z if z_top is None else z_top)
    D = 0.036                       # 波の深さ
    seg = (0.10, 0.035, 0.108, 0.035)   # 山の平部 / 斜面 / 谷の平部 / 斜面 (ピッチ 278mm)
    depth_at = (0.0, D, D, 0.0)
    gaps = [] if gap is None else ([gap] if isinstance(gap[0], (int, float)) else sorted(gap))
    ranges, cur = [], u0
    for ga, gb in gaps:
        ranges.append((cur, ga))
        cur = gb
    ranges.append((cur, u1))
    verts, faces = [], []

    def P(uu, tt, zz):
        return (uu, tt, zz) if along_x else (tt, uu, zz)
    for ra, rb in ranges:
        if rb - ra < 0.01:
            continue
        # 波の位相は壁全体 (u0 起点) で連続させる
        prof = []
        u, k = u0, 0
        while u < rb:
            nu = min(u + seg[k % 4], u1)
            if nu > ra:
                if not prof:
                    t = (ra - u) / (nu - u)
                    c0 = depth_at[(k - 1) % 4] if k else 0.0
                    prof.append((ra, c0 + (depth_at[k % 4] - c0) * t))
                if nu >= rb:
                    t = (rb - u) / (nu - u)
                    c0 = prof[-1][1] if u < ra else (depth_at[(k - 1) % 4] if k else 0.0)
                    c0 = depth_at[(k - 1) % 4] if k else 0.0
                    prof.append((rb, c0 + (depth_at[k % 4] - c0) * t))
                    break
                prof.append((nu, depth_at[k % 4]))
            u = nu
            k += 1
        base = len(verts)
        for uu, c in prof:
            lo, hi = t0 + c, t1 - D + c
            verts += [P(uu, lo, z0), P(uu, hi, z0), P(uu, lo, z1), P(uu, hi, z1)]
        for i in range(len(prof) - 1):
            a_, b_ = base + 4 * i, base + 4 * (i + 1)
            faces += [(a_, b_, b_ + 2, a_ + 2),          # 面 lo
                      (a_ + 1, a_ + 3, b_ + 3, b_ + 1),  # 面 hi
                      (a_, a_ + 1, b_ + 1, b_),          # 下
                      (a_ + 2, b_ + 2, b_ + 3, a_ + 3)]  # 上
        e = base + 4 * (len(prof) - 1)
        faces += [(base, base + 2, base + 3, base + 1), (e, e + 1, e + 3, e + 2)]
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
    def rail(za, zb, ua=u0, ub=u1):
        if along_x:
            mb.box_mm((ua, t0 - 0.012, za), (ub, t1 + 0.012, zb), M['container'])
        else:
            mb.box_mm((t0 - 0.012, ua, za), (t1 + 0.012, ub, zb), M['container'])
    if z1 - z0 > 0.3:
        for ra, rb in ranges:
            if rb - ra > 0.01:
                rail(z1 - 0.07, z1 + 0.004, ra, rb)
        if z0 < 0.05:
            rail(0.0005, 0.085)
    mb.build('R_レール_' + ob.name, coll, bevel=0.006)
    return (u0, u1, t0, t1, z0, z1)


def build_container_door(coll, xa, xb, t0, t1, zt, top=None):
    """コンテナの観音開き扉 (X 方向の壁、屋外側 = +Y)。xa..xb が開口、zt が開口上端

    内側: 白い扉、横方向の波、縦框、上下の横板とボルト頭
    外側: 横方向の波、ロッキングバー 2 本 x 2 枚、カムキーパー、ハンドル、ヒンジ、ガスケット、注意ステッカー、銘板
    """
    cm, rb, gv = M['container'], M['rubber'], M['galv_tube']
    zb = 0.085
    mb = MB()
    fw = 0.1                                            # 枠 (縦枠) の幅
    # 縦枠・ヘッダー (壁面より 4mm 出す)
    for xa_, xb_ in ((xa - fw, xa), (xb, xb + fw)):
        mb.box_mm((xa_, t0 - 0.016, zb), (xb_, t1 + 0.016, zt), cm)
    top = zt + 0.104 if top is None else top
    mb.box_mm((xa - fw, t0 - 0.016, zt), (xb + fw, t1 + 0.016, top), cm)
    # 注意ステッカー (外側ヘッダー)
    for x in (xa + 0.15, xb - 0.45):
        mb.box_mm((x, t1 + 0.016, zt + 0.025), (x + 0.3, t1 + 0.018, zt + 0.08), M['hazard'])
    xm = (xa + xb) / 2
    leaves = ((xa + 0.004, xm - 0.004, 'L'), (xm + 0.004, xb - 0.004, 'R'))
    h0, h1 = zb + 0.005, zt - 0.005
    yc0, yc1 = t0 + 0.02, t1 - 0.02                     # 扉本体の厚み
    ext = []
    for la, lb, side in leaves:
        w = lb - la
        mb.box_mm((la, yc0, h0), (lb, yc1, h1), cm)
        # --- 内側 (-Y 面) ---
        yi = yc0
        for xa_, xb_ in ((la, la + 0.09), (lb - 0.09, lb)):          # 縦框
            mb.box_mm((xa_, yi - 0.03, h0), (xb_, yi, h1), cm)
        for za_, zb_ in ((h1 - 0.24, h1), (h0, h0 + 0.24)):             # 上下の横板
            mb.box_mm((la + 0.09, yi - 0.022, za_), (lb - 0.09, yi, zb_), cm)
            for zz in (za_ + 0.07, za_ + 0.15):
                for xx in (la + 0.25, la + 0.33, lb - 0.33, lb - 0.25):
                    mb.cyl((xx, yi - 0.022, zz), (xx, yi - 0.03, zz), 0.014, M['bolt_head'], 10)
        z = h0 + 0.42
        while z < h1 - 0.45:                                          # 横方向の波 (膨らみ)
            mb.box_mm((la + 0.09, yi - 0.018, z), (lb - 0.09, yi, z + 0.16), cm)
            z += 0.34
        for zz, xs_ in ((h0 + 0.9, (la + 0.3, la + 0.38, lb - 0.38, lb - 0.3)), (h0 + 1.25, (la + 0.3, lb - 0.3))):
            for xx in xs_:
                mb.cyl((xx, yi - 0.018, zz), (xx, yi - 0.026, zz), 0.012, M['bolt_head'], 10)
        # --- 外側 (+Y 面) ---
        yo = yc1
        z = h0 + 0.2
        while z < h1 - 0.2:
            mb.box_mm((la + 0.06, yo, z), (lb - 0.06, yo + 0.014, z + 0.13), cm)
            z += 0.3
        # ガスケット (外周)
        for xa_, xb_ in ((la, la + 0.03), (lb - 0.03, lb)):
            mb.box_mm((xa_, yo + 0.014, h0), (xb_, yo + 0.022, h1), rb)
        for za_ in (h0, h1 - 0.03):
            mb.box_mm((la, yo + 0.014, za_), (lb, yo + 0.022, za_ + 0.03), rb)
        # ロッキングバー 2 本
        for f in (0.3, 0.72):
            bx = la + w * f
            yb = yo + 0.05
            mb.cyl((bx, yb, zb - 0.05), (bx, yb, top + 0.02), 0.017, gv, 12)
            for zz in (zb + 0.12, top - 0.1):                          # カムキーパー
                mb.box_mm((bx - 0.045, yo + 0.014, zz - 0.06), (bx + 0.045, yo + 0.06, zz + 0.06), gv)
            for zz in (0.95, 1.55):                                     # バーガイド
                mb.box_mm((bx - 0.04, yo + 0.014, zz - 0.03), (bx + 0.04, yo + 0.035, zz + 0.03), gv)
            # ハンドル (下部)
            mb.box_mm((bx - 0.02, yb - 0.01, 0.48), (bx + 0.02, yb + 0.035, 0.78), gv)
            mb.box_mm((bx - 0.05, yo + 0.014, 0.62), (bx - 0.01, yo + 0.04, 0.7), gv)
            # ハンドル受け
            mb.box_mm((bx + 0.12, yo + 0.014, 0.66), (bx + 0.17, yo + 0.04, 0.78), gv)
        # ヒンジ (外側の縦縁に 4 か所)
        hx = la if side == 'L' else lb
        for zz in (h0 + 0.2, h0 + 0.8, h0 + 1.4, h1 - 0.2):
            sgn = -1 if side == 'L' else 1
            mb.box_mm((min(hx, hx - sgn * 0.14), yo + 0.014, zz - 0.05), (max(hx, hx - sgn * 0.14), yo + 0.03, zz + 0.05), cm)
            mb.cyl((hx + sgn * 0.02, yo + 0.03, zz - 0.07), (hx + sgn * 0.02, yo + 0.03, zz + 0.07), 0.018, cm, 10)
        ext.append((la, lb))
    # 中央の合わせ目 (黒)
    mb.box_mm((xm - 0.004, yc0 - 0.001, h0), (xm + 0.004, yc1 + 0.001, h1), rb)
    mb.box_mm((xm - 0.03, yc1 + 0.014, h0), (xm + 0.03, yc1 + 0.026, h1), rb)
    # 屋外から見て左扉 (= +X 側) に銘板
    (la_r, lb_r) = ext[1]
    mb.box_mm((lb_r - 0.2, yc1 + 0.014, 0.9), (lb_r - 0.1, yc1 + 0.018, 1.05), M['plate'])
    door = mb.build('R_コンテナ扉', coll, bevel=0.004)
    return door


def build_single_door(coll, xa, xb, t0, t1, zt, top):
    """冷凍コンテナ風の片開き扉 (X 方向の壁、屋外側 = +Y)

    外側: 平らな白い扉 + アルミ額縁 (リベット)、亜鉛メッキの大型ヒンジ 3 か所 (屋外から見て右 = -X 側)、
          クロームのレバーラッチと受け、引き手、下部の黒いプランジャー
    内側: ステンレスの扉 (縦溝で 3 分割)、ベージュのパドル付き内開放ノブ、ステンレスの敷居
    """
    cm = M['container']
    zb = 0.1
    fw = 0.1
    mb = MB()
    # 周囲の平らな枠 (壁面より 4mm 出す)
    for xa_, xb_ in ((xa - fw, xa), (xb, xb + fw)):
        mb.box_mm((xa_, t0 - 0.016, 0.085), (xb_, t1 + 0.016, zt), cm)
    mb.box_mm((xa - fw, t0 - 0.016, zt), (xb + fw, t1 + 0.016, top), cm)
    mb.box_mm((xa, t0 - 0.016, 0.085), (xb, t1 + 0.016, zb), M['steel'])            # 敷居
    # アルミ額縁 (外側) + リベット
    yo = t1 + 0.016
    for p0, p1 in (((xa - 0.03, zb - 0.01), (xa, zt + 0.03)), ((xb, zb - 0.01), (xb + 0.03, zt + 0.03)),
                   ((xa - 0.03, zt), (xb + 0.03, zt + 0.03))):
        mb.box_mm((p0[0], yo, p0[1]), (p1[0], yo + 0.008, p1[1]), M['alu'])
    # 扉本体
    la, lb = xa + 0.006, xb - 0.006
    h0, h1 = zb + 0.004, zt - 0.006
    mb.box_mm((la, t0 + 0.01, h0), (lb, t1 + 0.006, h1), cm)
    mb.box_mm((la + 0.02, t1 + 0.006, h0 + 0.02), (lb - 0.02, t1 + 0.014, h1 - 0.02), M['white_panel'])   # 外皮
    for k in range(9):                                                                   # 外皮のリベット
        zz = h0 + 0.05 + k * (h1 - h0 - 0.1) / 8
        for xx in (la + 0.035, lb - 0.035):
            mb.cyl((xx, t1 + 0.014, zz), (xx, t1 + 0.018, zz), 0.005, M['alu'], 8)
    for k in range(6):
        xx = la + 0.05 + k * (lb - la - 0.1) / 5
        for zz in (h0 + 0.035, h1 - 0.035):
            mb.cyl((xx, t1 + 0.014, zz), (xx, t1 + 0.018, zz), 0.005, M['alu'], 8)
    # ヒンジ 3 か所 (-X 側)
    for zz in (h0 + 0.3, (h0 + h1) / 2, h1 - 0.3):
        mb.box_mm((la + 0.02, t1 + 0.014, zz - 0.055), (la + 0.25, t1 + 0.026, zz + 0.055), M['galv_tube'])
        mb.box_mm((xa - 0.09, t1 + 0.016, zz - 0.06), (xa - 0.02, t1 + 0.03, zz + 0.06), M['white_panel'])
        mb.cyl((xa - 0.005, t1 + 0.035, zz - 0.09), (xa - 0.005, t1 + 0.035, zz + 0.09), 0.02, M['galv_tube'], 12)
        for i in range(3):
            for dz in (-0.03, 0.03):
                xx = la + 0.06 + i * 0.07
                mb.cyl((xx, t1 + 0.026, zz + dz), (xx, t1 + 0.034, zz + dz), 0.011, M['galv_tube'], 6)
    # レバーラッチ (+X 側) + 受け + 引き手
    lz = 1.08
    mb.box_mm((lb - 0.24, t1 + 0.014, lz - 0.03), (lb - 0.03, t1 + 0.034, lz + 0.03), M['chrome'])
    mb.box_mm((lb - 0.33, t1 + 0.034, lz - 0.012), (lb - 0.05, t1 + 0.046, lz + 0.012), M['chrome'])
    mb.box_mm((xb + 0.005, t1 + 0.016, lz - 0.04), (xb + 0.07, t1 + 0.04, lz + 0.04), M['chrome'])
    gx = lb - 0.12
    mb.box_mm((gx - 0.025, t1 + 0.014, lz - 0.1), (gx + 0.025, t1 + 0.02, lz - 0.07), M['chrome'])
    mb.box_mm((gx - 0.025, t1 + 0.014, lz - 0.3), (gx + 0.025, t1 + 0.02, lz - 0.27), M['chrome'])
    mb.cyl((gx, t1 + 0.05, lz - 0.09), (gx, t1 + 0.05, lz - 0.28), 0.012, M['chrome'], 10)
    for zz in (lz - 0.09, lz - 0.28):
        mb.cyl((gx, t1 + 0.02, zz), (gx, t1 + 0.05, zz), 0.01, M['chrome'], 8)
    # 下部の黒いプランジャー
    px = (la + lb) / 2
    mb.cyl((px, t1 + 0.014, h0 + 0.12), (px, t1 + 0.05, h0 + 0.12), 0.025, M['black_plastic'], 16)
    mb.cyl((px, t1 + 0.05, h0 + 0.12), (px, t1 + 0.052, h0 + 0.12), 0.018, M['white_panel'], 16)
    # 内側: ステンレス面 + 縦溝 + 内開放ノブ
    yi = t0 + 0.01
    mb.box_mm((la + 0.015, yi - 0.006, h0 + 0.015), (lb - 0.015, yi, h1 - 0.015), M['steel'])
    for f in (1 / 3, 2 / 3):
        xx = la + (lb - la) * f
        mb.box_mm((xx - 0.004, yi - 0.009, h0 + 0.03), (xx + 0.004, yi - 0.006, h1 - 0.03), M['steel_worn'])
    mb.box_mm((lb - 0.14, yi - 0.03, lz - 0.035), (lb - 0.07, yi - 0.006, lz + 0.035), M['chrome'])
    mb.sphere((lb - 0.17, yi - 0.035, lz - 0.01), (0.05, 0.012, 0.035), M['knob_beige'], 16, 8)
    mb.cyl(((la + lb) / 2, yi - 0.006, lz + 0.1), ((la + lb) / 2, yi - 0.012, lz + 0.1), 0.006, M['chrome'], 8)
    return mb.build('R_片開き扉', coll, bevel=0.003)


def build_air_shower(coll, x0, x1, y0, y1):
    """エアシャワー (PCJ-88JPM4 の寸法図を参考): 外形 W1200 x D1000 x H2100、通路幅 800

    通り抜けは Y 方向。-Y 側が入口 (前室側)、+Y 側が出口 (作業場側)。
    """
    W, D, H = x1 - x0, y1 - y0, 2.1
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    iw = 0.4                                # 通路の半幅 (800)
    ih = 1.91                               # 通路の高さ
    pn, st, al = M['as_panel'], M['steel'], M['alu']
    mb = MB()
    # 左右のダクト柱
    for s_ in (-1, 1):
        mb.box_mm((min(s_ * iw, s_ * W / 2), -D / 2, 0), (max(s_ * iw, s_ * W / 2), D / 2, H), pn)
        xi = s_ * (iw - 0.004)
        # 内壁 (ステンレス) + 下部のプレフィルタ・ルーバー
        mb.box_mm((min(xi, s_ * iw), -D / 2 + 0.06, 0.32), (max(xi, s_ * iw), D / 2 - 0.06, ih), st)
        mb.box_mm((min(xi, s_ * iw), -D / 2 + 0.06, 0.02), (max(xi, s_ * iw), D / 2 - 0.06, 0.3), M['black_plastic'])
        for k in range(9):
            z = 0.04 + k * 0.03
            mb.box_mm((min(xi - s_ * 0.004, s_ * iw), -D / 2 + 0.07, z), (max(xi - s_ * 0.004, s_ * iw), D / 2 - 0.07, z + 0.014), st)
        # ノズル 3 段 x 2 列
        for zz in (0.75, 1.1, 1.45):
            for yy in (-0.165, 0.165):
                mb.cyl((xi, yy, zz), (xi - s_ * 0.012, yy, zz), 0.04, st, 20)
                mb.sphere((xi - s_ * 0.014, yy, zz), (0.012, 0.026, 0.026), M['nozzle'], 16, 8)
        # ドアロック解除スイッチ (カバー付き、床から 1.5m)
        mb.box_mm((min(xi - s_ * 0.02, xi), -D / 2 + 0.1, 1.46), (max(xi - s_ * 0.02, xi), -D / 2 + 0.18, 1.56), M['lamp_y'])
        # 外側のパネル目地
        xo = s_ * W / 2
        for zz in (0.7, 1.4):
            mb.box_mm((min(xo, xo + s_ * 0.002), -D / 2 + 0.02, zz), (max(xo, xo + s_ * 0.002), D / 2 - 0.02, zz + 0.004), M['black_plastic'])
    # 天井ボックス
    mb.box_mm((-iw, -D / 2, ih), (iw, D / 2, H), pn)
    mb.box_mm((-0.3, -0.25, ih - 0.004), (0.3, 0.25, ih), M['led'])                       # LED 照明
    for yy in (-0.2, 0.2):                                                               # 天井ノズル
        mb.cyl((0, yy, ih), (0, yy, ih - 0.012), 0.04, st, 20)
        mb.sphere((0, yy, ih - 0.014), (0.026, 0.026, 0.012), M['nozzle'], 16, 8)
    # イオン発生器 (入口側の天井際)
    mb.box_mm((-0.25, -D / 2 + 0.05, ih - 0.06), (0.25, -D / 2 + 0.12, ih), M['gray_paint'])
    mb.box_mm((0.18, -D / 2 + 0.048, ih - 0.04), (0.22, -D / 2 + 0.05, ih - 0.02), M['lamp_g'])
    # 床のステンレス板
    mb.box_mm((-iw, -D / 2, 0.0), (iw, D / 2, 0.012), M['steel_worn'])
    # 入口・出口の扉 (アルミ枠 + 強化ガラス窓) と枠、ドアクローザー、取っ手
    for s_ in (-1, 1):
        yf = s_ * D / 2
        # 扉枠
        for xa_, xb_ in ((-iw - 0.03, -iw), (iw, iw + 0.03)):
            mb.box_mm((xa_, min(yf, yf + s_ * 0.012), 0), (xb_, max(yf, yf + s_ * 0.012), ih + 0.03), al)
        mb.box_mm((-iw - 0.03, min(yf, yf + s_ * 0.012), ih), (iw + 0.03, max(yf, yf + s_ * 0.012), ih + 0.03), al)
        # 扉 (少し内側に)
        yd0, yd1 = sorted((yf - s_ * 0.045, yf - s_ * 0.005))
        la, lb, h0, h1 = -iw + 0.005, iw - 0.005, 0.015, ih - 0.005
        f = 0.07
        mb.box_mm((la, yd0, h0), (la + f, yd1, h1), al)
        mb.box_mm((lb - f, yd0, h0), (lb, yd1, h1), al)
        mb.box_mm((la + f, yd0, h0), (lb - f, yd1, 0.35), al)
        mb.box_mm((la + f, yd0, h1 - 0.1), (lb - f, yd1, h1), al)
        mb.box_mm((la + f, (yd0 + yd1) / 2 - 0.0025, 0.35), (lb - f, (yd0 + yd1) / 2 + 0.0025, h1 - 0.1), M['glass'])
        # 取っ手 (内外)
        hx = lb - 0.04
        for ys in (yd0, yd1):
            out = -1 if ys == yd0 else 1
            mb.cyl((hx, ys + out * 0.035, 0.85), (hx, ys + out * 0.035, 1.2), 0.012, st, 10)
            for zz in (0.87, 1.18):
                mb.cyl((hx, ys, zz), (hx, ys + out * 0.035, zz), 0.008, st, 8)
        # ドアクローザー (外側上部)
        yo = yf + s_ * 0.012
        mb.box_mm((la + 0.1, min(yo, yo + s_ * 0.05), ih + 0.035), (la + 0.4, max(yo, yo + s_ * 0.05), ih + 0.09), M['alu'])
        # 表示器 (入口側の外側ヘッダー)
        if s_ < 0:
            mb.box_mm((0.12, yo + s_ * 0.004, ih + 0.1), (0.3, yo, ih + 0.16), M['black_plastic'])
            mb.box_mm((0.14, yo + s_ * 0.005, ih + 0.11), (0.28, yo + s_ * 0.004, ih + 0.15), M['counter'])
            mb.box_mm((-0.3, yo + s_ * 0.003, ih + 0.11), (-0.1, yo, ih + 0.15), M['white_panel'])
    return mb.build('R_エアシャワー', coll, bevel=0.003, loc=(cx, cy, 0))


def rect_minus(r, h):
    """XY 矩形 r=(x0, x1, y0, y1) から h を抜いた残りを矩形のリストで返す"""
    x0, x1, y0, y1 = r
    hx0, hx1, hy0, hy1 = max(h[0], x0), min(h[1], x1), max(h[2], y0), min(h[3], y1)
    if hx0 >= hx1 or hy0 >= hy1:
        return [r]
    out = []
    if y0 < hy0:
        out.append((x0, x1, y0, hy0))
    if hy1 < y1:
        out.append((x0, x1, hy1, y1))
    if x0 < hx0:
        out.append((x0, hx0, hy0, hy1))
    if hx1 < x1:
        out.append((hx1, x1, hy0, hy1))
    return out


def build_doma(coll):
    """住宅の玄関のような土間: 床を DOMA_DEPTH 下げてタイル貼り、奥側に木の上がり框"""
    x0, x1, y0, y1 = DOMA
    zb = -DOMA_DEPTH
    t = 0.01
    mb = MB()
    mb.box_mm((x0, y0, zb - 0.02), (x1, y1, zb), M['doma_tile'])                 # 土間のタイル面
    # 段差の立ち上がり (周囲 3 辺はタイル、奥は上がり框)
    mb.box_mm((x0 - t, y0 - t, zb - 0.02), (x0, y1 + t, 0.0), M['doma_tile'])
    mb.box_mm((x1, y0 - t, zb - 0.02), (x1 + t, y1 + t, 0.0), M['doma_tile'])
    mb.box_mm((x0, y0 - t, zb - 0.02), (x1, y0, 0.0), M['doma_tile'])
    mb.box_mm((x0, y1, zb - 0.02), (x1, y1 + t, -0.06), M['doma_tile'])
    mb.build('R_土間', coll)
    mb = MB()
    mb.box_mm((x0, y1, -0.06), (x1, y1 + 0.09, 0.012), M['wood_kamachi'])            # 上がり框
    mb.build('R_上がり框', coll, bevel=0.004)


def wall_with_holes(name, coll, x0, x1, y0, y1, z0, z1, holes, mat):
    """XZ 面の長方形から holes [(xa, xb, za, zb), ...] を抜いた壁を、箱の組み合わせで作る"""
    xs = sorted({x0, x1} | {h[0] for h in holes} | {h[1] for h in holes})
    xs = [x for x in xs if x0 <= x <= x1]
    mb = MB()
    for xa, xb in zip(xs[:-1], xs[1:]):
        xm = (xa + xb) / 2
        cut = sorted((h[2], h[3]) for h in holes if h[0] <= xm <= h[1])
        z = z0
        for za, zb in cut + [(z1, z1)]:
            if za > z + 1e-4:
                mb.box_mm((xa, y0, z), (xb, y1, min(za, z1)), mat)
            z = max(z, zb)
    return mb.build(name, coll)


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


PALLET_H = 0.15


KINBOU_R = 0.06    # 菌棒の半径 (直径 12cm)
KINBOU_L = 0.52    # 菌棒の長さ (袋の口の結束を除く)
KINBOU_TIE = 0.026  # 両端の結束の飛び出し


def asset_kinbou(root):
    """袋入りの菌棒 (横倒し、軸は X、軸の高さ z=0)

    白い菌糸がまわったおが粉培地を透明の袋に詰めたもの。接種穴の種菌 (茶色) が 3 列並び、
    上からフィルムで覆ってある。表面には白いマスキングテープを 1 枚貼る。袋の両端は絞って白く結束する。
    """
    c = make_asset_collection('_A_菌棒', root)
    rng = random.Random(26)   # 他の設備の乱数列を変えないよう専用の乱数を使う
    mb = MB()
    r, L = KINBOU_R, KINBOU_L
    # 中身 (端は少し丸める)
    mb.cyl((-L / 2 + 0.02, 0, 0), (L / 2 - 0.02, 0, 0), r, M['kinbou'], 40)
    for sx in (-1, 1):
        mb.sphere((sx * (L / 2 - 0.02), 0, 0), (0.02, r, r), M['kinbou'], 40, 12)
    # 接種穴の種菌: 3 列 (上から 75°, -75°, 180°)。4 個と 3 個の列を交互にして千鳥にする
    hr, pitch = 0.022, 0.125
    for ang, n in ((math.radians(75), 4), (math.radians(-75), 3), (math.pi, 4)):
        d = Vector((0, -math.sin(ang), math.cos(ang)))
        for k in range(n):
            x = (k - (n - 1) / 2) * pitch
            mb.cyl(Vector((x, 0, 0)) + d * (r - 0.012), Vector((x, 0, 0)) + d * (r + 0.0012),
                   hr * rng.uniform(0.9, 1.1), M['kinbou_spawn'], 20)
    # 袋 (透明フィルム)。端は中身に沿って丸く折り込む
    fr = r + 0.003
    mb.cyl((-L / 2 + 0.02, 0, 0), (L / 2 - 0.02, 0, 0), fr, M['film'], 40)
    for sx in (-1, 1):
        mb.sphere((sx * (L / 2 - 0.02), 0, 0), (0.023, fr, fr), M['film'], 40, 12)
        # 絞った口と結束 (白く丸まった袋の余り)
        mb.cyl((sx * (L / 2 - 0.004), 0, 0), (sx * (L / 2 + 0.01), 0, 0), 0.026, M['film'], 16, r2=0.012, caps=False)
        mb.sphere((sx * (L / 2 + 0.012), 0, 0), (0.014, 0.022, 0.02), M['tie'], 16, 10)
        mb.sphere((sx * (L / 2 + 0.008), 0.006, 0.008), (0.012, 0.016, 0.014), M['tie'], 12, 8)
    # マスキングテープ (袋の上に貼る)
    mb.cyl_patch(0.02, 0.07, fr + 0.0006, math.radians(-14), math.radians(28), M['masking'])
    mb.build('菌棒', c)
    return c


def asset_kinbou_pallet(root):
    """パレット積み用の接種前の菌棒 (種菌の穴なし) の元形状。軸は X、軸の高さ z=0

    1 パレットに数百本積むので、ストレッチフィルム越しに見える程度の形に絞った軽量版。
    袋のフィルムは省き (外側のストレッチフィルムが映り込みを受け持つ)、分割数を少なくしている。
    """
    c = make_asset_collection('_A_菌棒_接種前', root)
    mb = MB()
    r, L = KINBOU_R, KINBOU_L
    mb.cyl((-L / 2 + 0.02, 0, 0), (L / 2 - 0.02, 0, 0), r, M['kinbou_raw'], 12, caps=False)
    for sx in (-1, 1):
        # 丸めた端 (面取りした円錐台) と、絞った口の結束
        mb.cyl((sx * (L / 2 - 0.02), 0, 0), (sx * L / 2, 0, 0), r, M['kinbou_raw'], 12, r2=r * 0.55)
        mb.sphere((sx * (L / 2 + 0.008), 0, 0), (0.016, 0.024, 0.022), M['tie'], 8, 4)
    mb.cyl_patch(0.02, 0.07, r + 0.0008, math.radians(-14), math.radians(28), M['masking'], nx=1, na=2)
    mb.build('菌棒_接種前', c)
    return c


PALLET_LAYERS = 16     # 菌棒の段数 (パレット込みで高さ約 2m)


def asset_pallet_load(root, kinbou):
    """パレットに積んだ接種前の菌棒: 1 段 3 本 x 12 列を、段ごとに向きを 90° 変えて積み、ストレッチフィルムで巻く"""
    c = make_asset_collection('_A_菌棒パレット積み', root)
    rng = random.Random(1500)
    r = KINBOU_R
    along = KINBOU_L + 0.01                 # 長手方向のピッチ (端の結束は隣とかみ合う)
    across = 2 * r + 0.002
    lay = 2 * r - 0.004                     # 段のピッチ (重みで少しつぶれる)
    n_across = 12
    # 菌棒は 1 パレット分を 1 メッシュにまとめる (数百個のインスタンスより表示が軽い)
    src = kinbou.objects[0].data
    logs = bmesh.new()
    for k in range(PALLET_LAYERS):
        z = PALLET_H + r + k * lay
        for i in range(3):
            for j in range(n_across):
                if k < PALLET_LAYERS - 1 and i == 1 and 0 < j < n_across - 1:
                    continue   # 荷の内側に隠れて見えない菌棒は置かない (表示を軽くする)
                u = (i - 1) * along + rng.uniform(-0.01, 0.01)
                w = (j - (n_across - 1) / 2) * across + rng.uniform(-0.002, 0.002)
                x, y, rz = (u, w, 0.0) if k % 2 == 0 else (w, u, math.pi / 2)
                m = (Matrix.Translation((x, y, z)) @ Matrix.Rotation(rz + rng.uniform(-0.02, 0.02), 4, 'Z')
                     @ Matrix.Rotation(rng.uniform(-math.pi, math.pi), 4, 'X'))
                n0 = len(logs.verts)
                logs.from_mesh(src)
                logs.verts.ensure_lookup_table()
                bmesh.ops.transform(logs, matrix=m, verts=logs.verts[n0:])
    me = bpy.data.meshes.new('菌棒_積み')
    logs.to_mesh(me); logs.free()
    for mat in src.materials:
        me.materials.append(mat)
    c.objects.link(bpy.data.objects.new('菌棒_積み', me))
    # ストレッチフィルム: 角を丸めた筒を、段の境目で少しくびれさせる
    half = (2 * along + KINBOU_L + 2 * KINBOU_TIE) / 2 + 0.006
    cr = 0.07
    z0, z1 = PALLET_H - 0.06, PALLET_H + PALLET_LAYERS * lay + 0.012
    ring = []
    for q in range(4):
        cx_ = (half - cr) * (1 if q in (0, 3) else -1)
        cy_ = (half - cr) * (1 if q in (0, 1) else -1)
        for t in range(7):
            a = math.pi / 2 * q + math.pi / 2 * t / 6
            ring.append((cx_ + cr * math.cos(a), cy_ + cr * math.sin(a), math.cos(a), math.sin(a)))
    zs = [z0, PALLET_H]
    zz = PALLET_H
    while zz < z1 - 1e-6:
        zs += [zz + lay / 2, min(zz + lay, z1)]
        zz += lay
    bm = bmesh.new()
    rows = []
    for zi, z in enumerate(zs):
        seam = zi >= 1 and (zi - 1) % 2 == 0 and z < z1 - 1e-6
        d = -0.01 if seam else 0.0
        rows.append([bm.verts.new((x + nx * d + rng.uniform(-0.003, 0.003), y + ny * d + rng.uniform(-0.003, 0.003), z))
                     for x, y, nx, ny in ring])
    n = len(ring)
    for a, b in zip(rows, rows[1:]):
        for i in range(n):
            bm.faces.new((a[i], a[(i + 1) % n], b[(i + 1) % n], b[i]))
    top = [bm.verts.new((x * 0.97, y * 0.97, z1 + 0.012)) for x, y, nx, ny in ring]
    for i in range(n):
        bm.faces.new((rows[-1][i], rows[-1][(i + 1) % n], top[(i + 1) % n], top[i]))
    bm.faces.new(top)
    for f in bm.faces:
        f.smooth = True
    me = bpy.data.meshes.new('ストレッチフィルム')
    bm.to_mesh(me); bm.free()
    me.materials.append(M['stretch'])
    c.objects.link(bpy.data.objects.new('ストレッチフィルム', me))
    return c


def asset_pallet(root):
    """1500 角の樹脂 (プラスチック) パレット: 一体成形の平デッキ + 9 脚 + 下桁 3 本"""
    c = make_asset_collection('_A_樹脂パレット', root)
    mb = MB()
    S = 1.5
    m = M['pallet']
    top = PALLET_H
    # 天面デッキ (一枚もの)
    mb.box((0, 0, top - 0.02), (S, S, 0.034), m)
    # 縁のリム
    rim = 0.045
    for sx, sy, w, d in ((0, 1, S, rim), (0, -1, S, rim), (1, 0, rim, S - 2 * rim), (-1, 0, rim, S - 2 * rim)):
        mb.box((sx * (S / 2 - rim / 2), sy * (S / 2 - rim / 2), top - 0.001), (w, d, 0.006), m)
    # 成形の格子リブ
    pitch = 0.118
    n = int((S - 2 * rim) / pitch)
    for k in range(1, n):
        u = -S / 2 + rim + k * (S - 2 * rim) / n
        mb.box((u, 0, top - 0.002), (0.012, S - 2 * rim, 0.004), m)
        mb.box((0, u, top - 0.002), (S - 2 * rim, 0.012, 0.004), m)
    # 脚ブロック 9 か所 (フォーク差し込み口は 4 方向)
    for x in (-S / 2 + 0.12, 0, S / 2 - 0.12):
        for y in (-S / 2 + 0.12, 0, S / 2 - 0.12):
            mb.box((x, y, 0.074), (0.24, 0.24, 0.09), m)
    # 下桁 3 本
    for x in (-S / 2 + 0.12, 0, S / 2 - 0.12):
        mb.box((x, 0, 0.0145), (0.24, S, 0.029), m)
    mb.build('樹脂パレット', c, bevel=0.008)
    return c


SPAWN_R = 0.053    # 種菌袋の半径 (直径 11cm 弱)
SPAWN_H = 0.2      # 種菌袋の中身の高さ


def spawn_bag(mb, x, y, z, rng):
    """袋入りの種菌 1 袋 (立てた状態、底の中心が (x, y, z))

    透明の袋におが粉の種菌を詰め、口を白い樹脂のカラーに通してキャップをしたもの。
    """
    r, h = SPAWN_R * rng.uniform(0.96, 1.03), SPAWN_H * rng.uniform(0.95, 1.03)
    # 中身 (底と肩は少し丸める)
    mb.cyl((x, y, z + 0.012), (x, y, z + h - 0.015), r, M['spawn_bag'], 16)
    mb.sphere((x, y, z + 0.012), (r, r, 0.012), M['spawn_bag'], 16, 4)
    mb.sphere((x, y, z + h - 0.015), (r, r, 0.02), M['spawn_bag'], 16, 4)
    # 袋: 中身に沿った胴と、口元へ絞った余り
    fr = r + 0.003
    mb.cyl((x, y, z + 0.012), (x, y, z + h - 0.012), fr, M['film'], 16, caps=False)
    mb.sphere((x, y, z + 0.012), (fr, fr, 0.0135), M['film'], 16, 4)
    mb.cyl((x, y, z + h - 0.012), (x, y, z + h + 0.012), fr, M['film'], 16, r2=fr * 0.8, caps=False)
    mb.cyl((x, y, z + h + 0.012), (x, y, z + h + 0.028), fr * 0.8, M['film'], 16, r2=0.027, caps=False)
    for k in range(4):   # 絞ったときにできる袋のひだ
        a = rng.uniform(0, 2 * math.pi)
        mb.box((x + math.cos(a) * 0.036, y + math.sin(a) * 0.036, z + h + 0.022), (0.04, 0.0015, 0.04), M['film'],
               rz=a + math.pi / 2 + rng.uniform(-0.3, 0.3))
    # カラー (つば付きのリング) とキャップ、すき間から見える綿栓
    top = z + h + 0.026
    mb.cyl((x, y, top), (x, y, top + 0.005), 0.034, M['cap_white'], 16)
    mb.cyl((x, y, top + 0.005), (x, y, top + 0.01), 0.026, M['cap_pink'], 12)
    mb.cyl((x, y, top + 0.01), (x, y, top + 0.032), 0.029, M['cap_white'], 16)
    mb.cyl((x, y, top + 0.032), (x, y, top + 0.034), 0.027, M['cap_white'], 16)


def asset_seed_stack(root):
    """袋入りの種菌を立てて並べた格子カゴ 2 段 (台車付き)"""
    c = make_asset_collection('_A_種菌カゴ', root)
    rng = random.Random(44)
    mb = MB()
    W, D = 0.5, 0.4
    # 台車
    mb.box((0, 0, 0.07), (W, D, 0.025), M['steel'])
    for x in (-W / 2 + 0.05, W / 2 - 0.05):
        for y in (-D / 2 + 0.05, D / 2 - 0.05):
            mb.cyl((x, y, 0.0), (x, y, 0.058), 0.028, M['rubber'], 12)
            mb.box((x, y, 0.061), (0.04, 0.04, 0.01), M['steel'])
    z = 0.083
    H, t = 0.26, 0.012
    for k in range(2):
        # 格子カゴ: 底板、上下の枠、四隅と中間の縦桟 (袋が見えるよう側面は抜く)
        mb.box((0, 0, z + 0.004), (W - 0.01, D - 0.01, 0.008), M['tray'])
        for zz, hh in ((z + 0.015, 0.03), (z + H - 0.012, 0.024)):
            for sy in (-1, 1):
                mb.box((0, sy * (D / 2 - t / 2), zz), (W, t, hh), M['tray'])
            for sx in (-1, 1):
                mb.box((sx * (W / 2 - t / 2), 0, zz), (t, D - 2 * t, hh), M['tray'])
        for i in range(5):
            x = -W / 2 + t / 2 + (W - t) * i / 4
            for sy in (-1, 1):
                mb.box((x, sy * (D / 2 - t / 2), z + H / 2), (t * 0.9, t * 0.9, H - 0.002), M['tray'])
        for j in range(1, 3):
            y = -D / 2 + t / 2 + (D - t) * j / 3
            for sx in (-1, 1):
                mb.box((sx * (W / 2 - t / 2), y, z + H / 2), (t * 0.9, t * 0.9, H - 0.002), M['tray'])
        # 中身: 4 x 3 = 12 袋
        for i in range(4):
            for j in range(3):
                bx = (i - 1.5) * (W - 0.03) / 4 + rng.uniform(-0.004, 0.004)
                by = (j - 1) * (D - 0.03) / 3 + rng.uniform(-0.004, 0.004)
                spawn_bag(mb, bx, by, z + 0.008, rng)
        z += H + 0.002
    mb.build('種菌カゴ', c)
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
    # 菌棒を積んでフィルムで巻いた荷 (向きはパレットごとに 90° 単位で変える)
    turn = random.Random(ob.name).randrange(4) * math.pi / 2
    instance(A['pallet_load'], '荷_' + ob.name, coll, (cx, cy, 0), rz + turn)


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
    # 元の箱の -X 端に寄せる (+X 側に隣接するテープ貼り機と重ならないように)
    cx, cy = mn.x + 0.64, (mn.y + mx.y) / 2
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
            r = bmesh.ops.create_cone(mb.bm, cap_ends=True, segments=4, radius1=0.036, radius2=0.07,
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


def stadium(mb, x0, ya, yb, w, zc, h, th, mat, teeth=True):
    """縦置きの周回ベルト (上から見て長円形)。歯付き"""
    r = w / 2
    n_arc, step = 10, 0.018
    pts = []
    # 直線 (右側, +y 方向)
    y = ya
    while y < yb:
        pts.append((x0 + r, y, math.pi / 2)); y += step
    for k in range(n_arc):
        a = math.pi * k / n_arc
        pts.append((x0 + r * math.cos(a), yb + r * math.sin(a), math.pi / 2 + a))
    y = yb
    while y > ya:
        pts.append((x0 - r, y, -math.pi / 2)); y -= step
    for k in range(n_arc):
        a = math.pi + math.pi * k / n_arc
        pts.append((x0 + r * math.cos(a), ya + r * math.sin(a), math.pi / 2 + a))
    for i, (x, y, ang) in enumerate(pts):
        out = th * (1.8 if (teeth and i % 2 == 0) else 1.0)
        # ang はベルト進行方向。外向き法線方向に厚みを持たせる
        nx, ny = math.cos(ang - math.pi / 2), math.sin(ang - math.pi / 2)
        mb.box((x + nx * out / 2, y + ny * out / 2, zc), (step * 1.05, out, h), mat, rz=ang)


def build_taper(ob, coll, A):
    """テープ貼り機 (写真ベース): 接種機とコンベヤの間の、緑のグリップベルト 2 本 + テープロール

    元の箱の長手方向 (接種機 → コンベヤ) にベルトを走らせる。
    ローカルでは長手を Y で組み、長手が X の場合は 90° 回して置く。
    """
    mn, mx = bounds(ob)
    cx, cy = (mn.x + mx.x) / 2, (mn.y + mx.y) / 2
    dx, dy = mx.x - mn.x, mx.y - mn.y
    L, rot = (dx, math.pi / 2) if dx >= dy else (dy, 0.0)
    W = min(0.3, min(dx, dy))
    ya, yb = -L / 2, L / 2
    st, wn = M['steel'], M['steel_worn']
    mb = MB()
    hw = W / 2 - 0.006
    bz = 0.66                                  # ベルト高さの中心
    # 側板 (フラットバー) と前の枠
    for s in (-1, 1):
        mb.box((s * hw, 0, 0.6), (0.01, L, 0.09), wn)
    mb.box((0, ya - 0.005, 0.6), (W, 0.01, 0.09), wn)
    mb.box((0, yb + 0.005, 0.6), (W, 0.01, 0.09), wn)
    # 赤い押しボタン
    mb.box((-hw - 0.012, 0.2, 0.62), (0.016, 0.05, 0.035), M['black_plastic'])
    mb.cyl((-hw - 0.02, 0.2, 0.62), (-hw - 0.028, 0.2, 0.62), 0.011, M['lamp_r'], 14)
    # フランジ軸受
    mb.cyl((-hw - 0.005, yb - 0.06, 0.6), (-hw - 0.016, yb - 0.06, 0.6), 0.035, st, 20)
    mb.cyl((-hw - 0.016, yb - 0.06, 0.6), (-hw - 0.026, yb - 0.06, 0.6), 0.014, M['brass'], 12)
    # 緑のグリップベルト 2 本 (縦プーリーで周回)
    for x0 in (-0.03, 0.03):
        stadium(mb, x0, ya + 0.08, yb - 0.08, 0.05, bz, 0.08, 0.006, M['grip_belt'])
        for yy in (ya + 0.08, yb - 0.08):
            mb.cyl((x0, yy, bz - 0.05), (x0, yy, bz + 0.05), 0.022, st, 16)
    # 脚 (テーパー金具付き) + つなぎ + 足
    for yy in (ya + 0.05, yb - 0.05):
        for s in (-1, 1):
            x = s * (hw - 0.012)
            mb.box((x, yy, 0.3), (0.03, 0.03, 0.5), st)
            m = Matrix.Translation((x, yy, 0.53)) @ Matrix.Rotation(math.pi / 4, 4, 'Z')
            r = bmesh.ops.create_cone(mb.bm, cap_ends=True, segments=4, radius1=0.022, radius2=0.05,
                                      depth=0.06, matrix=m)
            mb._assign(r['verts'], wn)
            mb.cyl((x, yy, 0.0), (x, yy, 0.05), 0.007, M['brass'], 8)
            mb.cyl((x, yy, 0.0), (x, yy, 0.012), 0.025, M['rubber'], 12)
        mb.box((0, yy, 0.18), (W - 0.03, 0.025, 0.025), st)
    for s in (-1, 1):
        mb.box((s * (hw - 0.012), 0, 0.18), (0.025, L - 0.1, 0.025), st)
    # モーター (ファン付き、下部)
    mb.cyl((0, 0.05, 0.36), (0, -0.14, 0.36), 0.05, M['motor_gray'], 20)
    mb.box((0, 0.1, 0.36), (0.09, 0.1, 0.1), M['motor_gray'])
    mb.cyl((0, -0.14, 0.36), (0, -0.145, 0.36), 0.045, M['black_plastic'], 20)
    mb.box((0, 0.05, 0.43), (0.02, 0.02, 0.2), st)
    # 上部: ボルト留めの大きなステンレス板 (2 本の縦ブラケットで支持)
    pz0, pz1 = 0.72, 0.9
    for yy in (-0.15, 0.2):
        mb.box((hw - 0.004, yy, (0.62 + pz0) / 2 + 0.02), (0.008, 0.05, pz0 - 0.62 + 0.04), wn)
    mb.box((hw, 0.02, (pz0 + pz1) / 2), (0.006, L - 0.1, pz1 - pz0), wn)
    for k in range(6):
        for zz in (pz0 + 0.03, pz1 - 0.03):
            mb.cyl((hw + 0.003, -0.3 + k * 0.13, zz), (hw + 0.009, -0.3 + k * 0.13, zz), 0.008, st, 6)
    mb.box((-hw, 0.02, (pz0 + pz1) / 2 - 0.02), (0.006, L - 0.2, pz1 - pz0 - 0.04), wn)
    # ガイドの樋とロッド
    mb.box((0, 0.02, pz1 - 0.05), (W - 0.03, L - 0.2, 0.004), st)
    for yy in (-0.25, 0.0, 0.28):
        mb.cyl((-hw, yy, pz1 - 0.02), (hw, yy, pz1 - 0.02), 0.009, st, 10)
    mb.cyl((-hw - 0.02, yb - 0.12, pz1 - 0.04), (hw, yb - 0.12, pz1 - 0.04), 0.02, M['alu'], 16)
    # テープロール (縦アームの先、軸は X)
    ax, ay = hw - 0.012, 0.05
    mb.box((ax, ay, pz1 + 0.13), (0.006, 0.05, 0.28), wn)
    rz_ = pz1 + 0.26
    mb.cyl((ax - 0.006, ay, rz_), (ax - 0.05, ay, rz_), 0.075, M['tape_roll'], 32)
    mb.cyl((ax - 0.005, ay, rz_), (ax - 0.051, ay, rz_), 0.042, M['black_plastic'], 24)
    mb.cyl((ax - 0.004, ay, rz_), (ax - 0.058, ay, rz_), 0.012, st, 6)
    mb.build('R_' + ob.name, coll, bevel=0.002, loc=(cx, cy, 0), rz=rot)


RACK_W = 1.8        # ラックの幅 (観音開き扉の開口 2.2m よりひと回り小さく)
RACK_H = 2.15       # 観音開き扉の開口 (約 2.3m) より少し低く
RACK_D = 0.8
RACK_LEVELS = 21


def asset_tray(root, w, d):
    """菌床用の黒い樹脂トレー (のこぎり状の山が並ぶ)"""
    c = make_asset_collection('_A_菌床トレー', root)
    mb = MB()
    m = M['tray_black']
    mb.box((0, 0, 0.004), (w, d, 0.008), m)
    for s in (-1, 1):                              # 前後の縁
        mb.box((0, s * (d / 2 - 0.008), 0.012), (w, 0.016, 0.024), m)
    n = int(w / 0.095)
    for k in range(n):
        x = -w / 2 + (k + 0.5) * w / n
        # 三角の山 (Y 方向に通る)
        mt = (Matrix.Translation((x, 0, 0.008)) @ Matrix.Rotation(math.pi / 4, 4, 'Y')
              @ Matrix.Diagonal((0.04, d - 0.03, 0.04, 1)))
        r = bmesh.ops.create_cube(mb.bm, size=1.0, matrix=mt)
        mb._assign(r['verts'], m)
        # 抜き穴 (暗い凹み)
        xh = x + w / n / 2
        if k < n - 1:
            for yy in (-d / 4, d / 4):
                mb.box((xh, yy, 0.0085), (0.035, d / 2 - 0.08, 0.001), M['black_plastic'])
    mb.build('菌床トレー', c, bevel=0.002)
    return c


def build_shelf(ob, coll, A):
    """作業室の棚 → 菌床トレー用の移動ラック (亜鉛メッキ角パイプ + 黒トレー + 縦ワイヤー)。観音開き扉の正面に置く"""
    mn, mx = bounds(ob)
    W, D = mx.x - mn.x, mx.y - mn.y
    cx, cy = (mn.x + mx.x) / 2, (mn.y + mx.y) / 2
    n_rack = 1
    rw = min(RACK_W, W - 0.04)
    dd = min(D, RACK_D)
    t = 0.05
    tray_w = rw - 2 * t - 0.02
    tray_d = dd / 2 - t / 2 - 0.02
    if 'tray' not in A:
        A['tray'] = asset_tray(bpy.data.collections['_アセット'], tray_w, tray_d)
    g = M['galv_tube']
    for i in range(n_rack):
        rx = (DOOR_X[0] + DOOR_X[1]) / 2
        mb = MB()
        z0, z1 = 0.13, RACK_H
        ys = (-dd / 2 + t / 2, 0.0, dd / 2 - t / 2)
        xs = (-rw / 2 + t / 2, rw / 2 - t / 2)
        for x in xs:
            for y in ys:
                mb.box((x, y, (z0 + z1) / 2), (t, t, z1 - z0), g)
            for z in (z0 + 0.03, 1.12, z1 - t / 2):     # 側面の横桟
                mb.box((x, 0, z), (t, dd, t), g)
        for y in (ys[0], ys[2]):                          # 前後の上下枠
            for z in (z0 + 0.03, z1 - t / 2):
                mb.box((0, y, z), (rw, t, t), g)
        # 台車ベース + キャスター
        mb.box((0, 0, z0 - 0.01), (rw, dd, 0.02), g)
        for x in (-rw / 2 + 0.1, rw / 2 - 0.1):
            for y in (-dd / 2 + 0.08, dd / 2 - 0.08):
                mb.box((x, y, 0.1), (0.08, 0.07, 0.008), g)
                mb.box((x, y, 0.075), (0.04, 0.04, 0.045), g)
                mb.cyl((x - 0.018, y, 0.045), (x + 0.018, y, 0.045), 0.045, M['rubber'], 16)
        # 各段のトレー受け (L アングル)
        levels = [0.2 + k * (z1 - 0.3) / RACK_LEVELS for k in range(RACK_LEVELS)]
        for z in levels:
            for x in (xs[0] + t / 2 + 0.006, xs[1] - t / 2 - 0.006):
                mb.box((x, 0, z - 0.004), (0.012, dd - 0.06, 0.008), M['galv'])
        # 前後面の縦ワイヤー
        nw = int(rw / 0.1)
        for k in range(1, nw):
            x = -rw / 2 + k * rw / nw
            for y in (ys[0] - t / 2 - 0.004, ys[2] + t / 2 + 0.004):
                mb.cyl((x, y, z0 + 0.05), (x, y, z1 - 0.05), 0.0025, M['steel'], 6)
        for y in (ys[0] - t / 2 - 0.004, ys[2] + t / 2 + 0.004):
            mb.cyl((-rw / 2, y, z1 - 0.06), (rw / 2, y, z1 - 0.06), 0.003, M['steel'], 6)
        # 目印テープ (赤・緑)
        for x in xs:
            for y in (ys[0], ys[2]):
                mb.box((x, y, 1.12), (t + 0.004, t + 0.004, 0.05), M['red_tape'])
        mb.box((xs[1], ys[0], 1.5), (t + 0.004, t + 0.004, 0.08), M['green_tape'])
        mb.build(f'R_{ob.name}_ラック{i + 1}', coll, bevel=0.004, loc=(rx, cy, 0))
        # トレー (前後 2 列 x 20 段)
        for z in levels:
            for yy in (-(tray_d / 2 + t / 2 + 0.01), tray_d / 2 + t / 2 + 0.01):
                instance(A['tray'], '菌床トレー', coll, (rx, cy + yy, z), 0.0)


def build_hanger(ob, coll, A):
    """ハンガーラック + 吊るした防塵服"""
    mn, mx = bounds(ob)
    W, D, H = mx.x - mn.x, mx.y - mn.y, mx.z
    cx, cy = HANGER_MOVE.get(ob.name, ((mn.x + mx.x) / 2, (mn.y + mx.y) / 2))
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

KINBOU_ON_TABLE = (   # (台, 並べる中心の Y オフセット, 本数)
    ('台', 0.55, 8),
    ('台.002', -0.45, 8),
    ('台.003', 0.3, 5),
)


def place_kinbou(coll, A):
    r = KINBOU_R
    rng = random.Random(926)
    for _ in range(3):
        random.random()   # 以前ここで置いていた培養ビンコンテナと同じだけ乱数を進める (後続の配置を変えない)
    for n, oy, count in KINBOU_ON_TABLE:
        ob = bpy.data.objects.get(n)
        if ob is None:
            continue
        mn, mx = bounds(ob)
        cx, cy = (mn.x + mx.x) / 2, (mn.y + mx.y) / 2 + oy
        pitch = 2 * r + 0.012
        for k in range(count):
            y = cy + (k - (count - 1) / 2) * pitch + rng.uniform(-0.003, 0.003)
            e = instance(A['kinbou'], f'菌棒_{n}_{k + 1:02d}', coll,
                         (cx + rng.uniform(-0.02, 0.02), y, mx.z + r + 0.001))
            # 転がす向き (どの面が上か) と、わずかな置きずれ
            e.rotation_euler = (rng.uniform(-0.7, 0.7), 0.0, math.radians(rng.uniform(-3, 3)))


def build_extras(coll):
    # 靴箱 (前室)
    mb = MB()
    W, D, H = 0.7, 0.32, 0.9
    mb.box((0, 0, H / 2), (W, D, H), M['white_panel'])
    for i in range(3):
        for j in range(4):
            x = -W / 2 + 0.03 + (W - 0.06) * (i + 0.5) / 3
            z = 0.05 + (H - 0.1) * (j + 0.5) / 4
            mb.box((x, -D / 2 + 0.14, z), ((W - 0.06) / 3 - 0.02, 0.3, (H - 0.1) / 4 - 0.02), M['black_plastic'])
            if random.random() < 0.6:
                mb.box((x - 0.05, -D / 2 + 0.1, z - 0.06), (0.09, 0.26, 0.07), M['boot'])
                mb.box((x + 0.05, -D / 2 + 0.1, z - 0.06), (0.09, 0.26, 0.07), M['boot'])
    mb.build('R_靴箱', coll, bevel=0.004, loc=(*SHOE_BOX_XY, 0), rz=SHOE_BOX_RZ)


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
        'kinbou': asset_kinbou(assets),
        'kinbou_raw': asset_kinbou_pallet(assets),
        'pallet': asset_pallet(assets),
        'seed': asset_seed_stack(assets),
        'box_a': asset_box(assets, '段ボール_大', (0.45, 0.4, 0.32)),
        'box_b': asset_box(assets, '段ボール_小', (0.35, 0.3, 0.25)),
    }
    A['pallet_load'] = asset_pallet_load(assets, A['kinbou_raw'])

    # 壁の高さを WALL_SCALE 倍にする (床 z=0 基準で上下方向だけ拡大)
    for ob in bpy.data.objects:
        if ob.type == 'MESH' and ob.name.split('.')[0] == 'Wall':
            mw, mi = ob.matrix_world, ob.matrix_world.inverted()
            for v in ob.data.vertices:
                w = mw @ v.co
                w.z *= WALL_SCALE
                v.co = mi @ w
            ob.data.update()
    bpy.context.view_layer.update()   # bound_box を更新

    objs = list(bpy.data.objects)
    workers = []
    perimeter_walls = []
    partition_src = []
    partition_src_as = []
    window_bounds = []
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
            # 床スラブ: 土間の部分を抜いて作り直す
            fmn, fmx = bounds(ob)
            hole = (DOMA[0] - 0.01, DOMA[1] + 0.01, DOMA[2] - 0.01, DOMA[3] + 0.01)
            mb = MB()
            for (a0, a1, b0, b1) in rect_minus((fmn.x, fmx.x, fmn.y, fmx.y), hole):
                mb.box_mm((a0, b0, fmn.z), (a1, b1, fmx.z), M['floor'])
            mb.build('R_床スラブ', real)
            move(ob, old)
        elif n == 'Floor.001':
            move(ob, old)   # 前室の緑シート → 鉄板の床に置き換え (下で作成)
        elif base == 'Wall':
            if n in ('Wall.007', 'Wall.012'):
                # 作業室との仕切り: 後で 1 枚の穴あき壁として作り直す
                partition_src.append(ob)
                continue
            if n in ('Wall.013', 'Wall.014'):
                # 元の窓帯: ガラス窓は付けず、仕切り壁で塞ぐ
                move(ob, old)
                continue
            mn_, mx_ = bounds(ob)
            thin = min(mx_.x - mn_.x, mx_.y - mn_.y) < 0.2
            on_perimeter = (mx_.y < 0.25 or mn_.y > 15.75) if (mx_.x - mn_.x) > (mx_.y - mn_.y) \
                else (mx_.x < 0.25 or mn_.x > 11.75)
            if thin and on_perimeter and (mx_.z - mn_.z) > 1.0 and n not in ('Wall.022',):
                # 外周 = コンテナの壁 → 台形波板 (仕切り壁はクリーンルームパネルのまま)
                if n == DOOR_WALL:
                    xa, xb = DOOR_X
                    sa, sb = SINGLE_DOOR_X
                    u0, u1, t0, t1, z0, z1 = build_corrugated(
                        ob, real, gap=[(xa - 0.05, xb + 0.05), (sa - 0.05, sb + 0.05)], z_top=CONTAINER_H)
                    build_container_door(real, xa, xb, t0, t1, 0.085 + DOOR_H, top=z1 + 0.004)
                    build_single_door(real, sa, sb, t0, t1, SINGLE_DOOR_TOP, z1 + 0.004)
                else:
                    build_corrugated(ob, real, z_top=CONTAINER_H)
                perimeter_walls.append(ob)
                move(ob, old)
                continue
            if n in ('Wall.010', 'Wall.011', 'Wall.018'):
                # 旧エアシャワー (側壁・扉) → 後で既製品のエアシャワーに置き換え
                move(ob, old)
                continue
            if n == AIR_SHOWER_BACK_WALL:
                partition_src_as.append(ob)
                continue
            if n in ('Wall.005', 'Wall.022'):
                mat = M['door']
                ob.data.materials.clear(); ob.data.materials.append(mat)
                mn, mx = bounds(ob)
                mb = MB()
                for y in (mn.y - 0.02, mx.y + 0.02):
                    x = mx.x - 0.08
                    mb.box((x, y, mx.z * 0.48), (0.1, 0.02, 0.02), M['steel'])
                mb.box_mm((mn.x + 0.1, mn.y - 0.002, mx.z * 0.57), (mx.x - 0.1, mx.y + 0.002, mx.z * 0.86), M['glass'])
                mb.build('R_取っ手_' + n, real)
            else:
                ob.data.materials.clear(); ob.data.materials.append(M['wall'])
            shrink_mesh(ob, random.uniform(0.0006, 0.0025))
            if not any(m.type == 'BEVEL' for m in ob.modifiers):
                bv = ob.modifiers.new('Bevel', 'BEVEL')
                bv.width = 0.006
                bv.segments = 2
                bv.limit_method = 'ANGLE'

    # 作業室との仕切り壁: 隙間の無い 1 枚壁 + ガラス窓 + コンベヤの通る長方形の開口
    if partition_src:
        bb = [bounds(o) for o in partition_src]
        y0 = min(b[0].y for b in bb)
        y1 = max(b[1].y for b in bb)
        top = max(b[1].z for b in bb)
        holes = [(b[0].x, b[1].x, b[0].z, b[1].z) for b in window_bounds]
        conv = bpy.data.objects.get('R_コンベヤ')
        if conv is not None:
            cmn, cmx = bounds(bpy.data.objects['コンベヤ'])
            ccx = (cmn.x + cmx.x) / 2
            ox0, ox1, oz0, oz1 = ccx - 0.43, ccx + 0.43, 0.3, 1.05
            holes.append((ox0, ox1, oz0, oz1))
        # 両端は隣の壁の中に少し入れる (同一平面の重なりを避ける)
        pw = wall_with_holes('R_仕切り壁_作業室', real, 6.152, 11.87, y0 + 0.001, y1 - 0.001, 0.0, top, holes, M['wall'])
        if conv is not None:
            mb = MB()
            f = 0.03
            for yy in (y0 - 0.004, y1 + 0.004):
                mb.box_mm((ox0 - f, yy - 0.004, oz0 - f), (ox1 + f, yy + 0.004, oz0), M['steel'])
                mb.box_mm((ox0 - f, yy - 0.004, oz1), (ox1 + f, yy + 0.004, oz1 + f), M['steel'])
                mb.box_mm((ox0 - f, yy - 0.004, oz0), (ox0, yy + 0.004, oz1), M['steel'])
                mb.box_mm((ox1, yy - 0.004, oz0), (ox1 + f, yy + 0.004, oz1), M['steel'])
            mb.box_mm((ox0, y0, oz0 - 0.002), (ox1, y1, oz0), M['steel'])
            mb.build('R_開口枠_コンベヤ', real, bevel=0.003)
        for o in partition_src:
            move(o, old)

    # エアシャワー + 出口側の壁 (開口つき)
    ax0, ax1, ay0, ay1 = AIR_SHOWER_XY
    build_air_shower(real, ax0, ax1, ay0, ay1)
    for o in partition_src_as:
        wmn, wmx = bounds(o)
        acx = (ax0 + ax1) / 2
        wall_with_holes('R_壁_' + o.name, real, wmn.x, 11.87, wmn.y + 0.001, wmx.y - 0.001, 0.0, wmx.z,
                        [(acx - 0.4, acx + 0.4, 0.0, 1.91)], M['wall'])
        move(o, old)

    # 壁で仕切られた部屋 (前室・作業室) の床は鉄板
    doma_cut = (DOMA[0] - 0.01, DOMA[1] + 0.01, DOMA[2] - 0.01, DOMA[3] + 0.09)   # 土間 + 上がり框
    for name, rect in STEEL_FLOOR_ROOMS.items():
        mb = MB()
        for (fx0, fx1, fy0, fy1) in rect_minus(rect, doma_cut):
            mb.box_mm((fx0, fy0, 0.0), (fx1, fy1, 0.006), M['floor_steel'])
        mb.build('R_鉄板床_' + name, real, bevel=0.002)
    build_doma(real)

    # コンテナの四隅のコーナーポスト
    if perimeter_walls:
        mb = MB()
        for cx_, cy_ in ((0.075, 0.08), (11.925, 0.08), (0.075, 15.925), (11.925, 15.925)):
            mb.box((cx_, cy_, CONTAINER_H / 2), (0.19, 0.19, CONTAINER_H + 0.004), M['container'])
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

    # 作業台の上の菌棒 (横倒しで、長手を台の奥行き方向にそろえて並べる)
    place_kinbou(real, A)

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
