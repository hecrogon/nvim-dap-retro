import bpy
import os
import subprocess
import math
import sys

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
    def flush(self):
        pass

sys.stdout = StdOutOverride()

# =========================================================
# CONFIGURACIÓN
# =========================================================
RUTA_PASMO_EXE = r"C:\ZEsarUX\pasmo\pasmo.exe"
RUTA_CARPETA = bpy.path.abspath("//Anim_ZX")
NOMBRE_ASM = "animacion_final.asm"
NOMBRE_TAP = "animacion_final.tap"

ZX_PALETTE = [
    (0.0, 0.0, 0.0), (0.0, 0.0, 0.8), (0.8, 0.0, 0.0), (0.8, 0.0, 0.8),
    (0.0, 0.8, 0.0), (0.0, 0.8, 0.8), (0.8, 0.8, 0.0), (0.8, 0.8, 0.8),
    (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0), (1.0, 0.0, 1.0),
    (0.0, 1.0, 0.0), (0.0, 1.0, 1.0), (1.0, 1.0, 0.0), (1.0, 1.0, 1.0)
]

def get_best_zx_color(rgb):
    best_idx = 0
    min_dist = 100.0
    for i, p_rgb in enumerate(ZX_PALETTE):
        dist = math.sqrt(sum((rgb[j] - p_rgb[j])**2 for j in range(3)))
        if dist < min_dist:
            min_dist = dist
            best_idx = i
    return best_idx

def procesar_y_compilar():
    scene = bpy.context.scene
    # RES_X, RES_Y = 96, 96
    RES_X = scene.render.resolution_x
    RES_Y = scene.render.resolution_y
    BYTES_ANCHO = RES_X // 8
    
    print(f"--- Iniciando proceso ---")
    
    if not os.path.exists(RUTA_CARPETA): os.makedirs(RUTA_CARPETA)
    ruta_asm = os.path.join(RUTA_CARPETA, NOMBRE_ASM)
    tmp_path = os.path.join(RUTA_CARPETA, "temp.bmp")

    frames_pixel_data = []
    frames_attr_data = []

    for f_idx in range(scene.frame_start, scene.frame_end + 1):
        scene.frame_set(f_idx)
        print(f"Renderizando frame: {f_idx}")
        scene.render.filepath = tmp_path
        bpy.ops.render.render(write_still=True)
        
        img = bpy.data.images.load(tmp_path)
        pix = list(img.pixels)
        
        bitmap = bytearray(BYTES_ANCHO * RES_Y)
        attr_w, attr_h = RES_X // 8, RES_Y // 8
        attributes = bytearray(attr_w * attr_h)

        for y_px in range(RES_Y):
            for x_px in range(RES_X):
                idx = ((RES_Y - 1 - y_px) * RES_X + x_px) * 4
                color_idx = get_best_zx_color(pix[idx:idx+3])
                
                if color_idx % 8 != 0:
                    byte_idx = (y_px * BYTES_ANCHO) + (x_px // 8)
                    bitmap[byte_idx] |= (1 << (7 - (x_px % 8)))
                    
                    attr_idx = (y_px // 8 * attr_w) + (x_px // 8)
                    bright = 0x40 if color_idx > 7 else 0x00
                    ink = color_idx % 8
                    attributes[attr_idx] = 0x00 | bright | ink

        frames_pixel_data.append(bitmap)
        frames_attr_data.append(attributes)
        bpy.data.images.remove(img)

    print("Generando archivo ASM...")
    with open(ruta_asm, "w", encoding="utf-8") as f:
        f.write("    org 25000\n\n")
        f.write("inicio:\n")
        f.write("    di\n")
        f.write("    xor a\n")
        f.write("    out (254), a\n")
        
        # --- LIMPIEZA DE PÍXELES (Negro) ---
        f.write("    ld hl, 16384\n")
        f.write("    ld de, 16385\n")
        f.write("    ld bc, 6143\n")
        f.write("    ld (hl), 0\n")
        f.write("    ldir\n")
        
        # --- LIMPIEZA DE ATRIBUTOS (Fondo Negro) ---
        # 0 es Negro sobre Negro. Si quieres blanco sería 56 (7*8).
        f.write("    ld hl, 22528\n")
        f.write("    ld de, 22529\n")
        f.write("    ld bc, 767\n")
        f.write("    ld (hl), 0\n") # 0 = Ink Black, Paper Black
        f.write("    ldir\n")
        
        f.write("    ei\n\n")
        
        f.write("main_loop:\n")
        for i in range(len(frames_pixel_data)):
            f.write("    halt\n")
            # Pixeles
            f.write(f"    ld hl, pix_frame_{i}\n")
            f.write("    ld de, 16384\n")
            f.write(f"    ld b, {RES_Y}\n")
            f.write(f"l_px_{i}:\n")
            f.write("    push bc\n")
            f.write("    push de\n")
            f.write(f"    ld bc, {BYTES_ANCHO}\n")
            f.write("    ldir\n")
            f.write("    pop de\n")
            f.write("    call next_line\n")
            f.write("    pop bc\n")
            f.write(f"    djnz l_px_{i}\n")
            
            # Atributos
            f.write(f"    ld hl, attr_frame_{i}\n")
            f.write("    ld de, 22528\n")
            for row in range(RES_Y // 8):
                f.write("    push de\n")
                f.write(f"    ld bc, {RES_X // 8}\n")
                f.write("    ldir\n")
                f.write("    pop de\n")
                f.write("    ld a, e\n")
                f.write("    add a, 32\n")
                f.write("    ld e, a\n")
                f.write(f"    jp nc, skp_{i}_{row}\n")
                f.write("    inc d\n")
                f.write(f"skp_{i}_{row}:\n")
            
            f.write("    ld b, 4\n")
            f.write(f"w_{i}:\n")
            f.write("    halt\n")
            f.write(f"    djnz w_{i}\n")
            
        f.write("    jp main_loop\n\n")

        # Subrutina pantalla
        f.write("next_line:\n")
        f.write("    inc d\n")
        f.write("    ld a, d\n")
        f.write("    and 7\n")
        f.write("    ret nz\n")
        f.write("    ld a, e\n")
        f.write("    add a, 32\n")
        f.write("    ld e, a\n")
        f.write("    ret c\n")
        f.write("    ld a, d\n")
        f.write("    sub 8\n")
        f.write("    ld d, a\n")
        f.write("    ret\n\n")

        # Datos
        for i, data in enumerate(frames_pixel_data):
            f.write(f"pix_frame_{i}:\n")
            for j in range(0, len(data), 16):
                f.write(f"    defb {','.join(map(str, data[j:j+16]))}\n")
        
        for i, data in enumerate(frames_attr_data):
            f.write(f"attr_frame_{i}:\n")
            for j in range(0, len(data), 16):
                f.write(f"    defb {','.join(map(str, data[j:j+16]))}\n")

    print("Compilando con Pasmo...")
    try:
        res = subprocess.run([RUTA_PASMO_EXE, "--tapbas", NOMBRE_ASM, NOMBRE_TAP], 
                             cwd=RUTA_CARPETA, capture_output=True, text=True)
        if res.stdout: print(res.stdout)
        if res.stderr: print(f"Errores Pasmo:\n{res.stderr}")
        else: print("--- ¡Compilación terminada sin errores y fondo negro! ---")
    except Exception as e:
        print(f"Error fatal: {e}")

if __name__ == "__main__":
    procesar_y_compilar()