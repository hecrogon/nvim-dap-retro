import bpy, os, sys
from collections import Counter

WIDTH, HEIGHT = 96, 96
FRAME_START = 1
FRAME_END = 4
# =========================================================
# CONFIGURACIÓN DE RESOLUCIÓN PARA MODO 0
# =========================================================
WIDTH_RENDER = 96  # Lo que mide tu render de Blender
HEIGHT = 96
WIDTH_CPC = 48    # Lo que debe medir para que el CPC no lo estire (96 / 2)
BYTES_PER_LINE = WIDTH_CPC 
FRAME_SIZE = BYTES_PER_LINE * HEIGHT

# =========================================================
# REDIRECCIÓN DE STDOUT A LA CONSOLA DE BLENDER
# =========================================================
class StdOutOverride:
    def write(self, text):
        sys.__stdout__.write(text)
        if text.strip():
            for area in bpy.context.screen.areas:
                if area.type == 'CONSOLE':
                    with bpy.context.temp_override(area=area):
                        bpy.ops.console.scrollback_append(text=text.rstrip(), type='OUTPUT')

sys.stdout = StdOutOverride()

RUTA_CARPETA = bpy.path.abspath("//Anim_CPC")
ASM_PATH = os.path.join(RUTA_CARPETA, "ANIM.ASM")
Z80_PATH = os.path.join(RUTA_CARPETA, "ANIM.Z80")
bpy.data.scenes["Scene"].node_tree.nodes["Value.005"].outputs[0].default_value = 3

BYTES_PER_LINE = WIDTH // 2
FRAME_SIZE = BYTES_PER_LINE * HEIGHT

MAPA_CPC = {
    0x54:(0,0,0), 0x50:(0,0,128), 0x55:(0,0,255),
    0x56:(0,128,0),0x46:(0,128,128),0x57:(0,128,255),
    0x52:(0,255,0),0x51:(0,255,128),0x53:(0,255,255),
    0x5C:(128,0,0),0x58:(128,0,128),0x5D:(128,0,255),
    0x5E:(128,128,0),0x40:(128,128,128),0x5F:(128,128,255),
    0x5A:(128,255,0),0x59:(128,255,128),0x5B:(128,255,255),
    0x4C:(255,0,0),0x48:(255,0,128),0x4D:(255,0,255),
    0x4E:(255,128,0),0x47:(255,128,128),0x4F:(255,128,255),
    0x4A:(255,255,0),0x49:(255,255,128),0x4B:(255,255,255)
}

# -------------------------------------------------
# PALETA MANUAL DESDE NODOS BLENDER
# -------------------------------------------------

CPC_DICT = {
    1: 0x54,
    2: 0x40,
    3: 0x4B,
    4: 0x5C,
    5: 0x4C,
    6: 0x47,
    7: 0x4E,
    8: 0x49,
    9: 0x4A,
    10: 0x5E,
    11: 0x56,
    12: 0x52,
    13: 0x5A,
    14: 0x59,
    15: 0x51,
    16: 0x46,
    17: 0x53,
    18: 0x5B,
    19: 0x57,
    20: 0x55,
    21: 0x50,
    22: 0x5D,
    23: 0x5F,
    24: 0x4F,
    25: 0x4D,
    26: 0x48,
    27: 0x58
}

palette = []

for i in range(16):

    node_name = f"Value.{154 + i}"

    value = bpy.data.scenes["Scene"] \
        .node_tree.nodes[node_name] \
        .outputs[0].default_value

    value = int(value)

    hw_color = CPC_DICT.get(value, 0x54)

    palette.append(hw_color)

# Tabla HW -> tinta
hw_to_tinta = {hw:i for i,hw in enumerate(palette)}

def snap_cpc_channel(v):

    if v < 28:
        return 0

    elif v < 155:
        return 128

    else:
        return 255

def find_best_palette_color(r, g, b, palette_hw):

    r = snap_cpc_channel(r * 255)
    g = snap_cpc_channel(g * 255)
    b = snap_cpc_channel(b * 255)

    rgb = (r, g, b)

    for hw in palette_hw:

        if MAPA_CPC[hw] == rgb:
            return hw

    return palette_hw[0]


def cpc_addr(base,line):
    block = line // 8
    row = line % 8
    return base + row * 0x800 + block * 80



def main():
    scene = bpy.context.scene

    # Color management correcto para evitar desviaciones raras
    scene.display_settings.display_device = 'sRGB'
    scene.view_settings.view_transform = 'Raw'
    scene.view_settings.exposure = 1.0

    all_frames_indices = []

    # =====================================================
    # RENDER + CONVERSIÓN A PALETA CPC (MATRIZ 2D REAL)
    # =====================================================
    for frame in range(FRAME_START, FRAME_END + 1):

        scene.frame_set(frame)

        bmp = os.path.join(RUTA_CARPETA, f"frame_{frame}.bmp")
        scene.render.filepath = bmp
        bpy.ops.render.render(write_still=True)

        img = bpy.data.images.load(bmp)
        pixels = list(img.pixels)

        frame_data = []

        for y in range(HEIGHT):

            row = []

            for x in range(0, WIDTH_RENDER, 2):

                idx = (y * WIDTH_RENDER + x) * 4

                r = pixels[idx]
                g = pixels[idx + 1]
                b = pixels[idx + 2]

                best_tinta = 0
                min_dist = 1e9

                # búsqueda en paleta CPC
                for tinta, hw in enumerate(palette):

                    cr, cg, cb = MAPA_CPC[hw]
                    tr, tg, tb = cr / 255.0, cg / 255.0, cb / 255.0

                    dist = (r - tr) ** 2 + (g - tg) ** 2 + (b - tb) ** 2

                    if dist < min_dist:
                        min_dist = dist
                        best_tinta = tinta

                row.append(best_tinta)

            frame_data.append(row)

        all_frames_indices.append(frame_data)
        bpy.data.images.remove(img)

    # =====================================================
    # GENERACIÓN VRAM CPC MODE 0 (48 bytes por línea)
    # =====================================================
    vrams = []

    for frame in all_frames_indices:

        vram = bytearray(FRAME_SIZE)

        for y in range(HEIGHT):

            inv_y = HEIGHT - 1 - y
            row = frame[inv_y]

            for x_byte in range(WIDTH_CPC // 2):

                # píxeles izquierdo/derecho seguros
                px_izq = row[x_byte * 2]
                px_der = row[x_byte * 2 + 1]

                byte = (
                    ((px_izq >> 0) & 1) << 7 |
                    ((px_izq >> 1) & 1) << 3 |
                    ((px_izq >> 2) & 1) << 5 |
                    ((px_izq >> 3) & 1) << 1 |

                    ((px_der >> 0) & 1) << 6 |
                    ((px_der >> 1) & 1) << 2 |
                    ((px_der >> 2) & 1) << 4 |
                    ((px_der >> 3) & 1) << 0
                )

                vram[y * WIDTH_CPC + x_byte] = byte

        vrams.append(vram)

    # =====================================================
    # EXPORT ASM
    # =====================================================
    with open(ASM_PATH, "w") as f:

        f.write("ORG &1200\nDI\n")
        f.write("LD BC,&7F8C\nOUT (C),C\n")
        f.write("LD BC,&7F10\nOUT (C),C\nLD BC,&7F54\nOUT (C),C\n")

        for i, hw in enumerate(palette):
            f.write(f"LD BC,&7F{i:02X}\nOUT (C),C\n")
            f.write(f"LD BC,&7F{hw:02X}\nOUT (C),C\n")

        f.write("\nLD HL,&C000\nLD DE,&C001\nLD BC,&3FFF\nLD (HL),0\nLDIR\n")

        f.write("\nMAIN:\n")
        for i in range(len(vrams)):
            f.write(f"    LD HL,FRAME_{i}\n    CALL DRAW\n    CALL WAIT\n")
        f.write("    JR MAIN\n")

        f.write(f"\nDRAW:\n    LD IX,SCREEN_TABLE\n    LD B,{HEIGHT}\nLINE_LOOP:\n    PUSH BC\n    LD E,(IX+0)\n    LD D,(IX+1)\n    INC IX\n    INC IX\n    LD BC,{BYTES_PER_LINE}\n    LDIR\n    POP BC\n    DJNZ LINE_LOOP\n    RET\n")

        f.write("\nWAIT:\n    LD BC,12000\nDELAY_LOOP:\n    DEC BC\n    LD A,B\n    OR C\n    JR NZ,DELAY_LOOP\n    RET\n")

        f.write("\nSCREEN_TABLE:\n")
        for line in range(HEIGHT):
            addr = cpc_addr(0xC000, line)
            f.write(f"    DW &{addr:04X}\n")

        for i, vram in enumerate(vrams):
            f.write(f"\nFRAME_{i}:\n")
            for j in range(0, len(vram), 16):
                chunk = vram[j:j+16]
                f.write("    DB " + ",".join(f"&{b:02x}" for b in chunk) + "\n")

    print("✔ ASM generado correctamente")

    # =====================================================
    # BINARIO
    # =====================================================
    BIN_PATH = os.path.join(RUTA_CARPETA, "ANIM.BIN")

    with open(BIN_PATH, "wb") as b:
        for vram in vrams:
            b.write(vram)

    print(f"✔ BIN generado en: {BIN_PATH}")

    # =====================================================
    # Z80 EXPORT
    # =====================================================
    with open(Z80_PATH, "w") as z:

        z.write("SCREEN_MODE EQU 0\n\n")

        z.write("PALETTE:\n    DB ")
        z.write(",".join(f"0x{hw:02X}" for hw in palette))

        if len(palette) < 16:
            z.write("," + ",".join("0x54" for _ in range(16 - len(palette))))

        z.write("\n\n")

        for i, vram in enumerate(vrams):

            z.write(f"FRAME_{i}:\n")

            for j in range(0, len(vram), 16):
                chunk = vram[j:j+16]
                z.write("    DB " + ",".join(f"0x{b:02X}" for b in chunk) + "\n")

            z.write("\n")

    print(f"✔ Z80 exportado en: {Z80_PATH}")

main()