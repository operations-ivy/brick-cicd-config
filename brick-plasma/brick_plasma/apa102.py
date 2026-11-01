"""The cabinet's 40 APA102 LEDs, clocked straight through /dev/gpiomem.

The Picade wires them to GPIO 14 and 15, which are no SPI peripheral's pins,
so every clock edge is software. Pimoroni's library takes one call and one
time.sleep(0) per edge: about 220 ms a frame. Writing the GPIO set and clear
registers takes about 0.5 ms (brick-cicd-config: measured on brick9000), and
they only touch the bits they're given, so nothing else's pins can be hurt.
At that speed, about 3 MHz, the chain misread a bit now and then (a button
changing colour for a frame, wild at 60 fps), so each edge is now held.
"""

DATA_PIN, CLOCK_PIN = 14, 15
# BCM2711 GPIO registers, as indexes of 32-bit words.
GPSET0, GPCLR0 = 0x1C // 4, 0x28 // 4
GPIO_CHIP = "/dev/gpiochip0"
GPIO_MEM = "/dev/gpiomem"

# 32 zero clocks start a frame; 36 end it, which the chain's smaller LEDs need
# to latch. Whole bytes here, so 40.
START, END = bytes(4), bytes(5)
FULL = 0xE0 | 31  # the LED's own brightness: three marker bits, then full
# Register writes that change nothing, after each edge: what sets the clock speed.
HOLD = 4


def encode(frame) -> bytes:
    """One frame of (r, g, b) as the bytes to clock out."""
    return START + bytes(v for r, g, b in frame for v in (FULL, b, g, r)) + END


class Chain:
    """Clocks bytes out through a sequence of GPIO registers."""

    def __init__(self, regs, data_pin: int = DATA_PIN, clock_pin: int = CLOCK_PIN):
        self.regs, self.data, self.clock = regs, 1 << data_pin, 1 << clock_pin

    def write(self, frame: bytes) -> None:
        regs, data, clock = self.regs, self.data, self.clock
        hold = range(HOLD)
        for byte in frame:
            for bit in (128, 64, 32, 16, 8, 4, 2, 1):
                regs[GPSET0 if byte & bit else GPCLR0] = data
                for _ in hold:
                    regs[GPCLR0] = 0
                regs[GPSET0] = clock
                for _ in hold:
                    regs[GPCLR0] = 0
                regs[GPCLR0] = clock


def open_chain() -> Chain:
    """Claim the two pins as outputs and map the registers. The request is
    kept on the Chain: the kernel holds the pins for as long as it lives."""
    import gpiod
    import mmap
    import os
    from gpiod.line import Direction, Value

    request = gpiod.request_lines(GPIO_CHIP, consumer="brick-plasma", config={
        (DATA_PIN, CLOCK_PIN): gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE)})
    fd = os.open(GPIO_MEM, os.O_RDWR | os.O_SYNC)
    try:
        registers = mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
    finally:
        os.close(fd)
    chain = Chain(memoryview(registers).cast("I"))
    chain.request = request
    return chain
