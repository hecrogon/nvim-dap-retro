    device amstradcpc6128

    org 0x4000

print_char equ 0xbb5a
wait_char equ 0xbb06

main:
    ld a,#0
    call 0xbc0e

    ld a,'h'
    call print_char
    ld a,'e'
    call print_char
    ld a,'l'
    call print_char
    ld a,'l'
    call print_char
    ld a,'o'
    call print_char

    ld hl,0xc370
    ld c,10d
pixel_loop:
    ld (hl),255
    inc hl

    dec c
    jp nz,pixel_loop

    ld hl,0xcb70
    ld (hl),255

    call wait_char

    ret

    savebin "build/hello.bin",0x4000,0x1000
