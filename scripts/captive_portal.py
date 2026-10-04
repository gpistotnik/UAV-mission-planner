#!/usr/bin/env python3
"""Captive portal HTTP redirector za dron-F450 AP.

Posluša na port 80 in vrne HTTP 302 redirect na nadzorno ploščo
(privzeto ``http://10.0.0.1/nadzor/``). Skupaj z DNS
hijack-om (``/etc/NetworkManager/dnsmasq-shared.d/captive.conf``)
to ustvari t.i. *captive portal*: ko se klient (telefon, laptop)
poveže na hotspot »dron-F450«, OS samodejno pošlje probe na znan URL
(``captive.apple.com``, ``connectivitycheck.gstatic.com``…), DNS to
preusmeri na nas, in mi vrnemo redirect → dashboard se odpre kot
»WiFi login« stran.

Posebnih ``do_GET`` ovojnic za posamezne OS-je ne potrebujemo, ker
**vsak** OS sproži captive-portal browser, če odgovor na probe ni
takšen, kot pričakuje (Success za iOS, 204 za Android, OK za Windows).
HTTP 302 odgovor zadovolji vse tri.
"""
from __future__ import annotations

import argparse
import logging
import socketserver
import sys
from http import server


LOGGER = logging.getLogger("captive_portal")

DEFAULT_TARGET = "http://dron.local/nadzor/"


class CaptiveHandler(server.BaseHTTPRequestHandler):

    target_url: str = DEFAULT_TARGET

    def log_message(self, fmt: str, *args) -> None:  # type: ignore[override]
        LOGGER.info(
            "%s %s %s -> 302",
            self.address_string(),
            self.command,
            self.path,
        )

    def _redirect(self) -> None:
        body = (
            b"<!doctype html><html><head>"
            b"<meta http-equiv=\"refresh\" content=\"0;url="
            + self.target_url.encode("utf-8")
            + b"\">"
            b"<title>Dron F450 - Nadzor</title></head>"
            b"<body><p>Preusmerjam na <a href=\""
            + self.target_url.encode("utf-8")
            + b"\">nadzorno ploscho</a>...</p></body></html>"
        )
        self.send_response(302)
        self.send_header("Location", self.target_url)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        # Connection: close zagotovi, da klient ne mantra keep-alive seje
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:  # type: ignore[override]
        self._redirect()

    def do_POST(self) -> None:  # type: ignore[override]
        self._redirect()

    def do_HEAD(self) -> None:  # type: ignore[override]
        self.send_response(302)
        self.send_header("Location", self.target_url)
        self.send_header("Content-Length", "0")
        self.end_headers()


class ThreadedHTTPServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="0.0.0.0",
                    help="Naslov za listen (privzeto 0.0.0.0)")
    ap.add_argument("--port", type=int, default=80,
                    help="Port (privzeto 80, zahteva CAP_NET_BIND_SERVICE)")
    ap.add_argument("--target", default=DEFAULT_TARGET,
                    help=f"URL preusmeritve (privzeto {DEFAULT_TARGET})")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    CaptiveHandler.target_url = args.target

    try:
        srv = ThreadedHTTPServer((args.host, args.port), CaptiveHandler)
    except PermissionError as exc:
        LOGGER.error("Ne morem bind-ati na port %d: %s", args.port, exc)
        LOGGER.error("Za port <1024 potrebujes CAP_NET_BIND_SERVICE ali root.")
        return 1
    except OSError as exc:
        LOGGER.error("Ne morem zagnati streznika: %s", exc)
        return 1

    LOGGER.info(
        "Captive portal: %s:%d -> %s",
        args.host, args.port, args.target,
    )

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("SIGINT, zaustavljanje...")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
