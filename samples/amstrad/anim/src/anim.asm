    device amstradcpc6128
    
    org 0x4000

    ; set screen mode
    ld a,screen_mode
    call 0xbc0e

    di
    
    ; set border colour
    ld bc,0x7f10
    out (c),c
    ld bc,0x7f54
    out (c),c

    ; set palette
    ld a,0
    ld bc,0x7f00
    ld hl,palette
palette_loop:
    ld c,a
    out (c),c

    ld c,(hl)
    out (c),c

    inc a
    inc hl
    cp 16
    jr nz,palette_loop

    ; initialise screen
    ld hl,0xc000
    ld de,0xc001
    ld bc,0x3fff
    ld (hl),0
    ldir

main:
    ld hl,frame_0
    call draw
    call wait
    ld hl,frame_1
    call draw
    call wait
    ld hl,frame_2
    call draw
    call wait
    ld hl,frame_3
    call draw
    call wait
    jr main

draw:
    ld ix,screen_table
    ld b,sprite_height
line_loop:
    push bc
    ld e,(ix+0)
    ld d,(ix+1)
    inc ix
    inc ix
    ld bc,sprite_width
    ldir
    pop bc
    djnz line_loop
    ret

wait:
    ld bc,12000
delay_loop:
    dec bc
    ld a,b
    or c
    jr nz,delay_loop
    ret

screen_table:
    rept 200, line
        dw 0xc000 + (line % 8) * 0x800 + (line / 8) * 0x50
    endr

    include "./src/sprite3.asm"

    savebin "build/anim.bin",0x4000,$ - 0x4000
