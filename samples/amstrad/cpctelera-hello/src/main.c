#include <cpctelera.h>

u8 sumUpTo(u8 n);

void main(void) {
    u8 result;

    // Must disable firmware first — otherwise it restores video/palette changes
    cpct_disableFirmware();

    cpct_setVideoMode(1);
    cpct_clearScreen(0x00);

    // Red border: program started
    cpct_setPALColour(16, HW_BRIGHT_YELLOW);

    result = sumUpTo(50);  // expected: 55

    // Green border: computation done
    cpct_setPALColour(16, HW_BRIGHT_BLUE);

    // Fill top of screen with white pixels (110 bytes = visible stripe)
    cpct_memset(CPCT_VMEM_START, 0xFF, (u16)result * 2);

    while(1) {}
}

// Sum 1..n — a simple function useful for breakpoint and step-through testing
u8 sumUpTo(u8 n) {
    u8 i, total = 0;
    for (i = 1; i <= n; i++) {
        total += i;
    }
    return total;
}
