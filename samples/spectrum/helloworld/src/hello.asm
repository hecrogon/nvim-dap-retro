    device zxspectrum48

    org 0x8000

main:
    ld iy,0x5c3a    ; ROM routines address system variables via (IY+off);
                     ; jumping straight in after reset leaves IY garbage
    ld a,2
    call 0x1601     ; CHAN-OPEN: open channel 'S' (screen) for RST 0x10

    ld hl,message
print_loop:
    ld a,(hl)
    or a
    jr z,done
    rst 0x10
    inc hl
    jr print_loop

done:
    ret

message:
    db "HELLO",0

    savebin "build/hello.bin",0x8000,0x100
