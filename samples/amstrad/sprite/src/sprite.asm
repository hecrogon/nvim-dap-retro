    device amstradcpc6128

    org 0x4000

wait_char equ 0xbb06

main:
    ld a,#1
    call 0xbc0e

    ; draw 8 rows of 2 bytes each
    ld hl,sprite_data

    ld de,0xc000
    call draw_row
    ld de,0xc800
    call draw_row
    ld de,0xd000
    call draw_row
    ld de,0xd800
    call draw_row
    ld de,0xe000
    call draw_row
    ld de,0xe800
    call draw_row
    ld de,0xf000
    call draw_row
    ld de,0xf800
    call draw_row

loop:
    jr loop

draw_row:
    ld a,(hl)
    ld (de),a
    inc hl
    inc de
    ld a,(hl)
    ld (de),a
    inc hl

    ret

sprite_data:
    defb 0xdf,0x00
    defb 0x99,0x00
    defb 0xff,0x00
    defb 0x66,0x00
    defb 0x66,0x00
    defb 0xff,0x00
    defb 0x99,0x00
    defb 0xff,0x00

    savebin "build/sprite.bin",0x4000,0x1000
