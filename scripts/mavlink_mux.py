#!/usr/bin/env python3
"""Lahek MAVLink multiplexor: en UART/USB master → več lokalnih UDP odjemalcev.

Namen: Django most in ``camera_trigger`` lahko hkrati bereta/pišeta Pixhawk,
brez nameščenega ``mavlink-router``.

Odjemalci (pymavlink ``udp:127.0.0.1:PORT``) se vežejo na PORT; ta skript
jim pošilja bajte iz serije in sprejema njihove ukaze nazaj na master.

Uporaba::

    python scripts/mavlink_mux.py --device /dev/ttyACM0 --baud 115200 \\
        --out 127.0.0.1:14550 --out 127.0.0.1:14551
"""
from __future__ import annotations

import argparse
import glob
import os
import select
import socket
import sys
import time
from typing import Optional

try:
    import serial  # type: ignore
except Exception as exc:  # pragma: no cover
    print(f"pyserial ni na voljo: {exc}", file=sys.stderr)
    sys.exit(2)


def resolve_device(device: str) -> str:
    if device != "auto":
        return device
    # Enak vrstni red kot Django autoconnect: USB ACM/USB najprej,
    # GPIO UART (serial0) šele nato — na benchu je Pixhawk na ttyACM0.
    candidates = [
        *sorted(glob.glob("/dev/ttyACM*")),
        *sorted(glob.glob("/dev/ttyUSB*")),
        "/dev/serial0",
        "/dev/ttyAMA0",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    raise FileNotFoundError(
        "auto: ni serijske naprave (ttyACM*/ttyUSB*/serial0)")


def open_serial(device: str, baud: int) -> "serial.Serial":
    ser = serial.Serial(device, baudrate=baud, timeout=0, write_timeout=1)
    # Pixhawk USB CDC včasih potrebuje DTR
    try:
        ser.dtr = True
        ser.rts = False
    except Exception:
        pass
    return ser


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="MAVLink UART→UDP mux")
    # Ne beri UAV_SERIAL_DEVICE iz .env — po prestavitvi Djangaa na UDP
    # bi bil tam udp:..., ne UART. Mux vedno drži fizični port.
    ap.add_argument("--device", default=os.environ.get("UAV_MUX_DEVICE", "auto"))
    ap.add_argument("--baud", type=int,
                    default=int(os.environ.get("UAV_MUX_BAUD", "115200")))
    ap.add_argument(
        "--out", action="append", default=[],
        help="UDP odjemalec host:port (ponovljivo). "
             "Privzeto 127.0.0.1:14550 in 127.0.0.1:14551",
    )
    args = ap.parse_args(argv)
    outs = args.out or ["127.0.0.1:14550", "127.0.0.1:14551"]

    targets: list[tuple[str, int]] = []
    for spec in outs:
        host, _, port_s = spec.rpartition(":")
        if not host or not port_s:
            print(f"neveljaven --out {spec!r} (pričakujem host:port)",
                  file=sys.stderr)
            return 2
        targets.append((host, int(port_s)))

    try:
        dev = resolve_device(args.device)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"[mux] odpiram {dev} @ {args.baud}", flush=True)
    try:
        ser = open_serial(dev, args.baud)
    except Exception as exc:
        print(f"[mux] serija: {exc}", file=sys.stderr)
        return 2

    # En UDP socket na odjemalca: pošiljamo na njihov bind port;
    # odgovore (ukaze) beremo nazaj in pišemo na UART.
    socks: list[socket.socket] = []
    for host, port in targets:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setblocking(False)
        socks.append(s)
        print(f"[mux] → udp {host}:{port}", flush=True)

    print("[mux] teče (Ctrl+C za izhod)", flush=True)
    try:
        while True:
            # UART → UDP
            try:
                n = ser.in_waiting
            except Exception as exc:
                print(f"[mux] serija branje: {exc}", file=sys.stderr)
                time.sleep(0.5)
                continue
            if n:
                data = ser.read(n)
                if data:
                    for s, (host, port) in zip(socks, targets):
                        try:
                            s.sendto(data, (host, port))
                        except OSError:
                            # odjemalec še ne posluša — OK
                            pass

            # UDP → UART (+ fan-out na druge odjemalce).
            # Brez fan-outa bi IMAGE_START_CAPTURE iz Djangaa šel samo na
            # Pixhawk; camera_trigger na drugem UDP portu ga ne bi videl
            # (FC ukaza ne echo-a, tipično vrne le FAILED ACK).
            readable, _, _ = select.select(socks, [], [], 0.02)
            for idx, s in enumerate(socks):
                if s not in readable:
                    continue
                try:
                    while True:
                        packet, _addr = s.recvfrom(4096)
                        if not packet:
                            break
                        try:
                            ser.write(packet)
                        except Exception as exc:
                            print(f"[mux] serija zapis: {exc}", file=sys.stderr)
                            break
                        for j, (s2, (host, port)) in enumerate(
                                zip(socks, targets)):
                            if j == idx:
                                continue
                            try:
                                s2.sendto(packet, (host, port))
                            except OSError:
                                pass
                except BlockingIOError:
                    pass
                except OSError as exc:
                    print(f"[mux] udp: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\n[mux] izhod", flush=True)
    finally:
        for s in socks:
            s.close()
        try:
            ser.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
