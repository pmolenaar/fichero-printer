"""
Fichero / D11s thermal label printer - BLE interface.

Protocol reverse-engineered from decompiled Fichero APK (com.lj.fichero).
Device class: AiYinNormalDevice (LuckPrinter SDK)
96px wide printhead (12 bytes/row), 203 DPI, prints 1-bit raster images.
"""

import argparse
import asyncio
import os
import sys
from contextlib import asynccontextmanager

from bleak import BleakClient, BleakGATTCharacteristic, BleakScanner
from PIL import Image, ImageDraw, ImageFont

PRINTER_NAME_PREFIXES = ("FICHERO", "D11s_")


async def find_printer() -> str:
    """Scan BLE for a Fichero/D11s printer. Returns the address."""
    print("Scanning for printer...")
    devices = await BleakScanner.discover(timeout=8)
    for d in devices:
        if d.name and any(d.name.startswith(p) for p in PRINTER_NAME_PREFIXES):
            print(f"  Found {d.name} at {d.address}")
            return d.address
    print("  ERROR: No Fichero/D11s printer found. Is it turned on?")
    sys.exit(1)

# Using the 18f0 service (any of the four BLE UART services work)
WRITE_UUID = "00002af1-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "00002af0-0000-1000-8000-00805f9b34fb"

PRINTHEAD_PX = 96
BYTES_PER_ROW = PRINTHEAD_PX // 8  # 12
CHUNK_SIZE = 200

# Paper types for 10 FF 84 00 nn
PAPER_GAP = 0x00
PAPER_BLACK_MARK = 0x01
PAPER_CONTINUOUS = 0x02


class PrinterStatus:
    """Parsed status byte from 10 FF 40."""

    def __init__(self, byte: int):
        self.raw = byte
        self.printing = bool(byte & 0x01)
        self.cover_open = bool(byte & 0x02)
        self.no_paper = bool(byte & 0x04)
        self.low_battery = bool(byte & 0x08)
        self.overheated = bool(byte & 0x10 or byte & 0x40)
        self.charging = bool(byte & 0x20)

    def __str__(self):
        flags = []
        if self.printing:
            flags.append("printing")
        if self.cover_open:
            flags.append("cover open")
        if self.no_paper:
            flags.append("no paper")
        if self.low_battery:
            flags.append("low battery")
        if self.overheated:
            flags.append("overheated")
        if self.charging:
            flags.append("charging")
        return ", ".join(flags) if flags else "ready"

    @property
    def ok(self) -> bool:
        return not (self.cover_open or self.no_paper or self.overheated)


class PrinterClient:
    def __init__(self, client: BleakClient):
        self.client = client
        self._buf = bytearray()
        self._event = asyncio.Event()

    def _on_notify(self, _char: BleakGATTCharacteristic, data: bytearray):
        self._buf.extend(data)
        self._event.set()

    async def start(self):
        await self.client.start_notify(NOTIFY_UUID, self._on_notify)

    async def send(self, data: bytes, wait: bool = False, timeout: float = 2.0) -> bytes:
        if wait:
            self._buf.clear()
            self._event.clear()
        await self.client.write_gatt_char(WRITE_UUID, data, response=False)
        if wait:
            try:
                await asyncio.wait_for(self._event.wait(), timeout=timeout)
                await asyncio.sleep(0.05)
            except asyncio.TimeoutError:
                pass
        return bytes(self._buf)

    async def send_chunked(self, data: bytes, chunk_size: int = CHUNK_SIZE):
        for i in range(0, len(data), chunk_size):
            chunk = data[i : i + chunk_size]
            await self.client.write_gatt_char(WRITE_UUID, chunk, response=False)
            await asyncio.sleep(0.02)

    # --- Info commands (all tested and confirmed on D11s fw 2.4.6) ---

    async def get_model(self) -> str:
        r = await self.send(bytes([0x10, 0xFF, 0x20, 0xF0]), wait=True)
        return r.decode(errors="replace").strip() if r else "?"

    async def get_firmware(self) -> str:
        r = await self.send(bytes([0x10, 0xFF, 0x20, 0xF1]), wait=True)
        return r.decode(errors="replace").strip() if r else "?"

    async def get_serial(self) -> str:
        r = await self.send(bytes([0x10, 0xFF, 0x20, 0xF2]), wait=True)
        return r.decode(errors="replace").strip() if r else "?"

    async def get_boot_version(self) -> str:
        r = await self.send(bytes([0x10, 0xFF, 0x20, 0xEF]), wait=True)
        return r.decode(errors="replace").strip() if r else "?"

    async def get_battery(self) -> int:
        r = await self.send(bytes([0x10, 0xFF, 0x50, 0xF1]), wait=True)
        if r and len(r) >= 2:
            return r[-1]
        return -1

    async def get_status(self) -> PrinterStatus:
        r = await self.send(bytes([0x10, 0xFF, 0x40]), wait=True)
        if r:
            return PrinterStatus(r[-1])
        return PrinterStatus(0xFF)

    async def get_density(self) -> bytes:
        r = await self.send(bytes([0x10, 0xFF, 0x11]), wait=True)
        return r

    async def get_shutdown_time(self) -> int:
        """Returns auto-shutdown timeout in minutes."""
        r = await self.send(bytes([0x10, 0xFF, 0x13]), wait=True)
        if r and len(r) >= 2:
            return (r[0] << 8) | r[1]
        return -1

    async def get_all_info(self) -> dict:
        """10 FF 70: returns pipe-delimited string with all device info."""
        r = await self.send(bytes([0x10, 0xFF, 0x70]), wait=True)
        if not r:
            return {}
        parts = r.decode(errors="replace").split("|")
        if len(parts) >= 6:
            return {
                "bt_name": parts[0],
                "mac_classic": parts[1],
                "mac_ble": parts[2],
                "firmware": parts[3],
                "serial": parts[4],
                "battery": f"{parts[5]}%",
            }
        return {"raw": r.decode(errors="replace")}

    # --- Config commands (tested on D11s) ---

    async def set_density(self, level: int) -> bool:
        """0=light, 1=medium, 2=thick. Returns True if printer responded OK."""
        r = await self.send(bytes([0x10, 0xFF, 0x10, 0x00, level]), wait=True)
        return r == b"OK"

    async def set_paper_type(self, paper: int = PAPER_GAP) -> bool:
        """0=gap/label, 1=black mark, 2=continuous."""
        r = await self.send(bytes([0x10, 0xFF, 0x84, paper]), wait=True)
        return r == b"OK"

    async def set_shutdown_time(self, minutes: int) -> bool:
        hi = (minutes >> 8) & 0xFF
        lo = minutes & 0xFF
        r = await self.send(bytes([0x10, 0xFF, 0x12, hi, lo]), wait=True)
        return r == b"OK"

    async def factory_reset(self) -> bool:
        r = await self.send(bytes([0x10, 0xFF, 0x04]), wait=True)
        return r == b"OK"

    # --- Print control (AiYin-specific, from decompiled APK) ---

    async def wakeup(self):
        await self.send(b"\x00" * 12)

    async def enable(self):
        """AiYin enable: 10 FF FE 01 (NOT 10 FF F1 03)."""
        await self.send(bytes([0x10, 0xFF, 0xFE, 0x01]))

    async def feed_dots(self, dots: int):
        """Feed paper forward by n dots."""
        await self.send(bytes([0x1B, 0x4A, dots & 0xFF]))

    async def form_feed(self):
        """Position to next label."""
        await self.send(bytes([0x1D, 0x0C]))

    async def stop_print(self) -> bool:
        """AiYin stop: 10 FF FE 45. Waits for 0xAA or 'OK'."""
        r = await self.send(bytes([0x10, 0xFF, 0xFE, 0x45]), wait=True, timeout=60.0)
        if r:
            return r[0] == 0xAA or r.startswith(b"OK")
        return False

    async def get_info(self) -> dict:
        status = await self.get_status()
        return {
            "model": await self.get_model(),
            "firmware": await self.get_firmware(),
            "boot": await self.get_boot_version(),
            "serial": await self.get_serial(),
            "battery": f"{await self.get_battery()}%",
            "status": str(status),
            "shutdown": f"{await self.get_shutdown_time()} min",
        }


@asynccontextmanager
async def connect(address=None):
    """Discover printer, connect, and yield a ready PrinterClient."""
    addr = address or await find_printer()
    async with BleakClient(addr) as client:
        pc = PrinterClient(client)
        await pc.start()
        yield pc


# --- Image handling ---


def prepare_image(img: Image.Image, max_rows: int = 240) -> Image.Image:
    """Convert any image to 96px wide, 1-bit, black on white."""
    img = img.convert("L")
    w, h = img.size
    new_h = int(h * (PRINTHEAD_PX / w))
    if new_h > max_rows:
        new_h = max_rows
    img = img.resize((PRINTHEAD_PX, new_h), Image.LANCZOS)
    img = img.point(lambda x: 1 if x < 128 else 0, "1")
    return img


def image_to_raster(img: Image.Image) -> bytes:
    """Pack 1-bit image into raw raster bytes, MSB first."""
    return img.tobytes()


def text_to_image(text: str, label_height: int = 240) -> Image.Image:
    """Render text in landscape, then rotate 90 degrees for label printing."""
    canvas_w = label_height
    canvas_h = PRINTHEAD_PX
    img = Image.new("L", (canvas_w, canvas_h), 255)
    draw = ImageDraw.Draw(img)

    font = ImageFont.load_default(size=24)

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (canvas_w - tw) // 2
    y = (canvas_h - th) // 2
    draw.text((x, y), text, fill=0, font=font)

    img = img.rotate(90, expand=True)
    return img


# --- Print sequence (from decompiled Fichero APK: AiYinNormalDevice) ---


async def do_print(
    pc: PrinterClient,
    img: Image.Image,
    density: int = 1,
    paper: int = PAPER_GAP,
    copies: int = 1,
):
    img = prepare_image(img)
    rows = img.height
    raster = image_to_raster(img)

    print(f"  Image: {img.width}x{rows}, {len(raster)} bytes, {copies} copies")

    status = await pc.get_status()
    if not status.ok:
        print(f"  ERROR: printer not ready ({status})")
        return False

    await pc.set_density(density)
    await asyncio.sleep(0.1)

    for copy_num in range(copies):
        if copies > 1:
            print(f"  Copy {copy_num + 1}/{copies}...")

        # AiYin print sequence (from decompiled APK)
        await pc.set_paper_type(paper)
        await asyncio.sleep(0.05)
        await pc.wakeup()
        await asyncio.sleep(0.05)
        await pc.enable()
        await asyncio.sleep(0.05)

        # Raster image: GS v 0 m xL xH yL yH <data>
        yl = rows & 0xFF
        yh = (rows >> 8) & 0xFF
        header = bytes([0x1D, 0x76, 0x30, 0x00, BYTES_PER_ROW, 0x00, yl, yh])
        await pc.send_chunked(header + raster)

        await asyncio.sleep(0.5)
        await pc.form_feed()
        await asyncio.sleep(0.3)

        ok = await pc.stop_print()
        if not ok:
            print("  WARNING: no OK/0xAA from stop command")

    return True


# --- CLI ---


async def cmd_info(args):
    async with connect(args.address) as pc:
        info = await pc.get_info()
        for k, v in info.items():
            print(f"  {k}: {v}")

        print()
        all_info = await pc.get_all_info()
        for k, v in all_info.items():
            print(f"  {k}: {v}")


async def cmd_status(args):
    async with connect(args.address) as pc:
        status = await pc.get_status()
        print(f"  Status: {status}")
        print(f"  Raw: 0x{status.raw:02X} ({status.raw:08b})")
        print(f"  printing={status.printing} cover_open={status.cover_open} "
              f"no_paper={status.no_paper} low_battery={status.low_battery} "
              f"overheated={status.overheated} charging={status.charging}")


async def cmd_text(args):
    text = " ".join(args.text)
    img = text_to_image(text)
    async with connect(args.address) as pc:
        print(f'Printing "{text}"...')
        ok = await do_print(pc, img, args.density, copies=args.copies)
        print("Done." if ok else "FAILED.")


async def cmd_image(args):
    img = Image.open(args.path)
    async with connect(args.address) as pc:
        print(f"Printing {args.path}...")
        ok = await do_print(pc, img, args.density, copies=args.copies)
        print("Done." if ok else "FAILED.")


async def cmd_set(args):
    async with connect(args.address) as pc:
        if args.setting == "density":
            ok = await pc.set_density(int(args.value))
            print(f"  Set density={args.value}: {'OK' if ok else 'FAILED'}")
        elif args.setting == "shutdown":
            ok = await pc.set_shutdown_time(int(args.value))
            print(f"  Set shutdown={args.value}min: {'OK' if ok else 'FAILED'}")
        elif args.setting == "paper":
            types = {"gap": 0, "black": 1, "continuous": 2}
            ok = await pc.set_paper_type(types.get(args.value, int(args.value)))
            print(f"  Set paper={args.value}: {'OK' if ok else 'FAILED'}")
        else:
            print(f"  Unknown setting: {args.setting}")
            print("  Available: density (0-2), shutdown (minutes), paper (gap/black/continuous)")


def main():
    parser = argparse.ArgumentParser(description="Fichero D11s Label Printer")
    parser.add_argument("--address", default=os.environ.get("FICHERO_ADDR"),
                        help="BLE address (skip scanning, or set FICHERO_ADDR)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_info = sub.add_parser("info", help="Show device info")
    p_info.set_defaults(func=cmd_info)

    p_status = sub.add_parser("status", help="Show detailed status")
    p_status.set_defaults(func=cmd_status)

    p_text = sub.add_parser("text", help="Print text label")
    p_text.add_argument("text", nargs="+", help="Text to print")
    p_text.add_argument("--density", type=int, default=1, choices=[0, 1, 2],
                        help="Print density: 0=light, 1=medium, 2=thick")
    p_text.add_argument("--copies", type=int, default=1, help="Number of copies")
    p_text.set_defaults(func=cmd_text)

    p_image = sub.add_parser("image", help="Print image file")
    p_image.add_argument("path", help="Path to image file")
    p_image.add_argument("--density", type=int, default=1, choices=[0, 1, 2],
                         help="Print density: 0=light, 1=medium, 2=thick")
    p_image.add_argument("--copies", type=int, default=1, help="Number of copies")
    p_image.set_defaults(func=cmd_image)

    p_set = sub.add_parser("set", help="Change printer settings")
    p_set.add_argument("setting", choices=["density", "shutdown", "paper"],
                       help="Setting to change")
    p_set.add_argument("value", help="New value")
    p_set.set_defaults(func=cmd_set)

    args = parser.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
