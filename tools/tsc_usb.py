#!/usr/bin/env python3
"""Talk to a TSC printer over USB from macOS or Linux (libusb, no driver).

    python3 tools/tsc_usb.py info                 model, firmware, Bluetooth module and mode, fix status
    python3 tools/tsc_usb.py send FILE            send a file (firmware, logo, ...) and wait for the restart
    python3 tools/tsc_usb.py cmd 'OUT GETSETTING$("CONFIG","BT","MODULE")'
    python3 tools/tsc_usb.py btmode Dual          Dual | BT2.1 | BT3.0 | BT4.0, then restart (applied at power-on)
    python3 tools/tsc_usb.py autobas              install tools/AUTO.BAS (the no-reflash fix)

Needs libusb: `brew install libusb` (macOS) or `apt install libusb-1.0-0` (Linux).
Any TSC device (USB vendor 0x1203) is used; set TSC_PID to pick one.
On Windows use TSC's DiagTool instead.
"""
from __future__ import annotations

import ctypes as C
import ctypes.util
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TSC_VENDOR = 0x1203
EP_OUT = int(os.environ.get("TSC_EP_OUT", "0x01"), 0)
EP_IN = int(os.environ.get("TSC_EP_IN", "0x82"), 0)


def _libusb():
    names = [ctypes.util.find_library("usb-1.0"), "/opt/homebrew/lib/libusb-1.0.dylib",
             "/usr/local/lib/libusb-1.0.dylib", "libusb-1.0.so.0"]
    for name in filter(None, names):
        try:
            return C.CDLL(name)
        except OSError:
            continue
    sys.exit("libusb not found: brew install libusb  (or apt install libusb-1.0-0)")


class _DeviceDescriptor(C.Structure):
    _fields_ = [("bLength", C.c_uint8), ("bDescriptorType", C.c_uint8), ("bcdUSB", C.c_uint16),
                ("bDeviceClass", C.c_uint8), ("bDeviceSubClass", C.c_uint8), ("bDeviceProtocol", C.c_uint8),
                ("bMaxPacketSize0", C.c_uint8), ("idVendor", C.c_uint16), ("idProduct", C.c_uint16),
                ("bcdDevice", C.c_uint16), ("iManufacturer", C.c_uint8), ("iProduct", C.c_uint8),
                ("iSerialNumber", C.c_uint8), ("bNumConfigurations", C.c_uint8)]


lib = _libusb()
lib.libusb_get_device_list.restype = C.c_ssize_t
lib.libusb_get_device_list.argtypes = [C.c_void_p, C.POINTER(C.POINTER(C.c_void_p))]
lib.libusb_free_device_list.argtypes = [C.POINTER(C.c_void_p), C.c_int]
lib.libusb_get_device_descriptor.argtypes = [C.c_void_p, C.POINTER(_DeviceDescriptor)]
lib.libusb_open.argtypes = [C.c_void_p, C.POINTER(C.c_void_p)]
lib.libusb_close.argtypes = [C.c_void_p]
lib.libusb_claim_interface.argtypes = [C.c_void_p, C.c_int]
lib.libusb_release_interface.argtypes = [C.c_void_p, C.c_int]
lib.libusb_set_auto_detach_kernel_driver.argtypes = [C.c_void_p, C.c_int]
lib.libusb_bulk_transfer.argtypes = [C.c_void_p, C.c_ubyte, C.POINTER(C.c_ubyte), C.c_int,
                                     C.POINTER(C.c_int), C.c_uint]


def _find(ctx) -> C.c_void_p | None:
    want_pid = int(os.environ["TSC_PID"], 0) if "TSC_PID" in os.environ else None
    devs = C.POINTER(C.c_void_p)()
    n = lib.libusb_get_device_list(ctx, C.byref(devs))
    try:
        for i in range(max(n, 0)):
            d = _DeviceDescriptor()
            if lib.libusb_get_device_descriptor(devs[i], C.byref(d)) != 0:
                continue
            if d.idVendor == TSC_VENDOR and (want_pid is None or d.idProduct == want_pid):
                handle = C.c_void_p()
                if lib.libusb_open(devs[i], C.byref(handle)) == 0:
                    return handle
    finally:
        if n > 0:
            lib.libusb_free_device_list(devs, 1)
    return None


def present() -> bool:
    ctx = C.c_void_p()
    lib.libusb_init(C.byref(ctx))
    try:
        h = _find(ctx)
        if h:
            lib.libusb_close(h)
        return bool(h)
    finally:
        lib.libusb_exit(ctx)


class Printer:
    def __init__(self):
        self.ctx = C.c_void_p()
        lib.libusb_init(C.byref(self.ctx))
        self.h = _find(self.ctx)
        if not self.h:
            lib.libusb_exit(self.ctx)
            raise SystemExit("no TSC printer on USB")
        lib.libusb_set_auto_detach_kernel_driver(self.h, 1)
        if lib.libusb_claim_interface(self.h, 0) != 0:
            raise SystemExit("cannot claim the printer (is another program using it?)")
        self.read(150)

    def close(self):
        lib.libusb_release_interface(self.h, 0)
        lib.libusb_close(self.h)
        lib.libusb_exit(self.ctx)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def write(self, data: bytes, progress: bool = False):
        sent, step, t0 = 0, 8192, time.time()
        while sent < len(data):
            part = data[sent:sent + step]
            buf = (C.c_ubyte * len(part)).from_buffer_copy(part)
            n = C.c_int()
            rc = lib.libusb_bulk_transfer(self.h, EP_OUT, buf, len(part), C.byref(n), 60000)
            if rc != 0 and n.value == 0:
                raise SystemExit(f"USB write failed after {sent} of {len(data)} bytes (rc {rc})")
            sent += n.value
            if progress and (sent % (512 * 1024) < step or sent == len(data)):
                print(f"  sent {sent:,} / {len(data):,} bytes ({time.time() - t0:.0f} s)")

    def read(self, timeout_ms: int = 1500, quiet_ms: int = 250) -> bytes:
        out, deadline = b"", time.time() + timeout_ms / 1000
        while time.time() < deadline:
            buf, n = (C.c_ubyte * 4096)(), C.c_int()
            lib.libusb_bulk_transfer(self.h, EP_IN, buf, 4096, C.byref(n), 100)
            if n.value:
                out += bytes(buf[:n.value])
                deadline = time.time() + quiet_ms / 1000
        return out

    def ask(self, line: str, timeout_ms: int = 2000) -> str:
        self.write(line.encode("latin-1") + b"\r\n")
        return self.read(timeout_ms).decode("latin-1").strip()


def _setting(p: Printer, group: str, key: str) -> str:
    section = "SYSTEM" if group == "INFORMATION" else "CONFIG"
    return p.ask(f'OUT GETSETTING$("{section}","{group}","{key}")')


def _fix_status(p: Printer, model: str, version: str) -> str:
    index = json.loads((ROOT / "docs/patches/index.json").read_text())
    for name in index["patches"]:
        patch = json.loads((ROOT / "docs/patches" / name).read_text())
        if patch["model"].endswith(model) and patch["firmware"].startswith("M" + version):
            value = p.ask(patch["check"]["command"])
            if value == patch["check"]["fixed"]:
                return "fixed firmware"
            if value == patch["check"]["original"]:
                autobas = p.ask('OUT LOF("AUTO.BAS")')
                return "original firmware" + (" + AUTO.BAS" if autobas not in ("", "0") else "")
            return f"unknown ({value})"
    return "no patch for this model/firmware yet"


def info():
    with Printer() as p:
        model, version = _setting(p, "INFORMATION", "MODEL"), _setting(p, "INFORMATION", "VERSION")
        print(f"model       {model}")
        print(f"firmware    {version}")
        print(f"bt module   {_setting(p, 'BT', 'MODULE')}")
        print(f"bt mode     {_setting(p, 'BT', 'MODE')}")
        print(f"bt name     {_setting(p, 'BT', 'NAME')}")
        print(f"fix         {_fix_status(p, model, version)}")


def wait_restart(limit_s: int = 600):
    t0, gone = time.time(), False
    while time.time() - t0 < limit_s:
        if not present():
            if not gone:
                print(f"  {time.time() - t0:.0f} s: printer restarting")
            gone = True
        elif gone:
            time.sleep(12)
            print(f"  {time.time() - t0:.0f} s: printer is back")
            return info()
        time.sleep(1)
    print("printer did not restart; if it was a firmware file, check the screen")


def send(path: str):
    data = Path(path).read_bytes()
    with Printer() as p:
        print(f"sending {path} ({len(data):,} bytes)")
        p.write(data, progress=True)
    if b"FuseA" in data[:200_000]:
        print("firmware sent: the printer checks it, writes it and restarts by itself - do not unplug")
        wait_restart()


def btmode(mode: str):
    if mode not in ("Dual", "BT2.1", "BT3.0", "BT4.0"):
        raise SystemExit("mode: Dual | BT2.1 | BT3.0 | BT4.0")
    with Printer() as p:
        p.write(f"BT MODE {mode}\r\n".encode())
        time.sleep(0.8)
        p.write(b"\x1b!R")
    print(f"Bluetooth mode {mode} saved; restarting so the module takes it")
    wait_restart(120)


def autobas():
    lines = (ROOT / "tools/AUTO.BAS").read_bytes()
    with Printer() as p:
        p.write(b'DOWNLOAD F,"AUTO.BAS"\r\n' + lines.rstrip(b"\r\n") + b"\r\nEOP\r\n")
        time.sleep(1)
        size = p.ask('OUT LOF("AUTO.BAS")')
    print(f"AUTO.BAS installed ({size} bytes). Turn the printer off and on.")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "info":
        info()
    elif cmd == "send" and args:
        send(args[0])
    elif cmd == "cmd" and args:
        with Printer() as p:
            for line in args:
                print(p.ask(line))
    elif cmd == "btmode" and args:
        btmode(args[0])
    elif cmd == "autobas":
        autobas()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
