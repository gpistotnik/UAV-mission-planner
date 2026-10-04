#!/usr/bin/env python3
"""MJPEG video stream iz Raspberry Pi kamere.

Uporaba na Raspberry Pi 5:

    sudo apt install -y python3-picamera2
    python3 scripts/camera_stream.py

Privzeto odpre kamero ``camera_num=0`` (CAM/DISP0 port na RPi 5) in
servira:

* ``/stream.mjpg`` — downscaled predogled (privzeto 1280×720),
* ``/snapshot.jpg`` — en okvir iz predogleda (isti buffer kot stream),
* ``/still.jpg`` — opcijski polni senzorski still (prekine predogled).

Dashboard shutter in ``camera_trigger`` bereta ``/snapshot.jpg`` (preview).

Argumenti:

    --port          privzeto 8090
    --camera        indeks kamere (0 ali 1 na RPi 5), privzeto 0
    --width/--height  velikost predogleda, privzeto 1280×720
    --still-width/--still-height  still; privzeto PixelArraySize senzorja
    --fps           framerate predogleda, privzeto 30
    --quality       JPEG kakovost predogleda 1–95, privzeto 75
    --still-quality JPEG kakovost stilla 1–95, privzeto 92
"""
from __future__ import annotations

import argparse
import io
import logging
import socketserver
import sys
import threading
import time
from http import server
from typing import Any, Optional

try:
    from picamera2 import Picamera2  # type: ignore
    from picamera2.encoders import MJPEGEncoder  # type: ignore
    from picamera2.outputs import FileOutput  # type: ignore
except ImportError as exc:  # pragma: no cover
    print(f"[camera_stream] picamera2 ni nameščen: {exc}", file=sys.stderr)
    print("Namestitev: sudo apt install -y python3-picamera2", file=sys.stderr)
    sys.exit(1)


LOGGER = logging.getLogger("camera_stream")


# ---------------------------------------------------------------------------
# Notranji buffer za en MJPEG okvir z notification-om
# ---------------------------------------------------------------------------
class StreamingOutput(io.BufferedIOBase):
    """Hrani zadnji MJPEG okvir. Bralci cakajo na ``condition`` za novi."""

    def __init__(self) -> None:
        self.frame: bytes | None = None
        self.condition = threading.Condition()

    def write(self, buf: bytes) -> int:  # type: ignore[override]
        with self.condition:
            self.frame = buf
            self.condition.notify_all()
        return len(buf)


def sensor_still_size(picam: Any) -> tuple[int, int]:
    """Native ločljivost senzorja (brez upscale na napačen HQ format)."""
    props = getattr(picam, "camera_properties", None) or {}
    size = props.get("PixelArraySize")
    if size is not None and len(size) >= 2:
        return int(size[0]), int(size[1])
    return 3280, 2464


class CameraRuntime:
    """Predogled (video) + polni still z zaklepom."""

    def __init__(
        self,
        picam: Any,
        *,
        preview_size: tuple[int, int],
        still_size: tuple[int, int],
        fps: float,
        preview_quality: int,
        still_quality: int,
    ) -> None:
        self.picam = picam
        self.preview_size = preview_size
        self.still_size = still_size
        self.fps = fps
        self.preview_quality = preview_quality
        self.still_quality = still_quality
        self.output = StreamingOutput()
        self._lock = threading.RLock()
        self._recording = False

        self.video_cfg = picam.create_video_configuration(
            main={"size": preview_size, "format": "RGB888"},
            controls={"FrameRate": float(fps)},
        )
        self.still_cfg = picam.create_still_configuration(
            main={"size": still_size},
        )
        picam.configure(self.video_cfg)
        self._start_recording()

    def _start_recording(self) -> None:
        encoder = MJPEGEncoder(bitrate=10_000_000, qp=self.preview_quality)
        self.picam.start_recording(encoder, FileOutput(self.output))
        self._recording = True

    def _stop_recording(self) -> None:
        if self._recording:
            try:
                self.picam.stop_recording()
            except Exception as exc:
                LOGGER.warning("stop_recording: %s", exc)
            self._recording = False

    def latest_preview_frame(self, timeout_s: float = 2.0) -> bytes | None:
        with self.output.condition:
            frame = self.output.frame
            if frame is None:
                self.output.condition.wait(timeout=timeout_s)
                frame = self.output.frame
        return frame

    def _ensure_preview(self) -> None:
        """Po stillu obnovi video predogled (po potrebi tudi configure)."""
        try:
            self._start_recording()
            return
        except Exception as exc:
            LOGGER.warning("restart predogleda: %s — ponovna konfiguracija", exc)
        self.picam.configure(self.video_cfg)
        try:
            self.picam.start()
        except Exception:
            pass
        self._start_recording()

    def capture_still_jpeg(self) -> bytes:
        """Polni senzorski still; predogled za hip prekine, nato se nadaljuje."""
        with self._lock:
            t0 = time.time()
            self._stop_recording()
            data = b""
            try:
                data = self._grab_still_jpeg()
            finally:
                self._ensure_preview()

            if len(data) < 2 or data[:2] != b"\xff\xd8":
                raise RuntimeError("still ni veljaven JPEG")
            LOGGER.info(
                "still %dx%d %d B v %.0f ms",
                self.still_size[0], self.still_size[1], len(data),
                (time.time() - t0) * 1000,
            )
            return data

    def _grab_still_jpeg(self) -> bytes:
        # Najprej nativni JPEG iz picamera2; sicer RGB → PIL.
        try:
            buf = io.BytesIO()
            try:
                self.picam.switch_mode_and_capture_file(
                    self.still_cfg, buf, format="jpeg",
                )
            except TypeError:
                self.picam.switch_mode_and_capture_file(self.still_cfg, buf)
            data = buf.getvalue()
            if len(data) >= 2 and data[:2] == b"\xff\xd8":
                return data
        except Exception as exc:
            LOGGER.warning("switch_mode_and_capture_file: %s", exc)

        from PIL import Image  # type: ignore
        arr = self.picam.switch_mode_and_capture_array(self.still_cfg)
        im = Image.fromarray(arr)
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=self.still_quality)
        return out.getvalue()
    def close(self) -> None:
        with self._lock:
            self._stop_recording()
            try:
                self.picam.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# HTTP handler — MJPEG odgovor je vecjedrno-mixed-replace
# ---------------------------------------------------------------------------
INDEX_HTML = b"""<!doctype html>
<title>RPi camera</title>
<style>body{margin:0;background:#000;display:flex;align-items:center;justify-content:center;height:100vh}
img{max-width:100%;max-height:100%}</style>
<img src="/stream.mjpg" alt="stream">
"""


class StreamingHandler(server.BaseHTTPRequestHandler):
    runtime: CameraRuntime  # nastavljen iz zunaj kot razredni atribut

    def log_message(self, fmt: str, *args) -> None:  # type: ignore[override]
        LOGGER.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # type: ignore[override]
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            self._send_bytes(INDEX_HTML, "text/html; charset=utf-8")
            return

        if path == "/health":
            self._send_bytes(b"ok", "text/plain")
            return

        if path.startswith("/stream.mjpg"):
            self._send_stream()
            return

        if path.startswith("/snapshot.jpg") or path.startswith("/preview.jpg"):
            # Preview okvir iz MJPEG bufferja — hitro, ne prekine streama.
            self._send_preview()
            return

        if path.startswith("/still.jpg"):
            self._send_still()
            return

        self.send_error(404)

    def _send_bytes(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_preview(self) -> None:
        """En JPEG iz predogleda — dashboard shutter in camera_trigger."""
        frame = self.runtime.latest_preview_frame()
        if frame is None:
            self.send_error(503, "Ni še nobenega okvirja.")
            return
        self._send_bytes(frame, "image/jpeg")

    def _send_still(self) -> None:
        """Polni senzorski still (opcijsko; prekine predogled za hip)."""
        try:
            frame = self.runtime.capture_still_jpeg()
        except Exception as exc:
            LOGGER.exception("still zajem spodletel: %s", exc)
            self.send_error(503, f"Still zajem spodletel: {exc}")
            return
        self._send_bytes(frame, "image/jpeg")

    def _send_stream(self) -> None:
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header(
            "Content-Type", "multipart/x-mixed-replace; boundary=FRAME"
        )
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            while True:
                with self.runtime.output.condition:
                    self.runtime.output.condition.wait(timeout=5)
                    frame = self.runtime.output.frame
                if frame is None:
                    continue
                self.wfile.write(b"--FRAME\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(
                    f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii")
                )
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.info("Klient %s prekinil povezavo.", self.address_string())
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Stream error za %s: %s", self.address_string(), exc)


class ThreadedHTTPServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--camera", type=int, default=0,
                    help="Indeks kamere (0=CAM0/DISP0, 1=CAM1/DISP1)")
    ap.add_argument("--width", type=int, default=1280,
                    help="Širina predogleda (stream)")
    ap.add_argument("--height", type=int, default=720,
                    help="Višina predogleda (stream)")
    ap.add_argument("--still-width", type=int, default=0,
                    help="Širina stilla (0 = PixelArraySize)")
    ap.add_argument("--still-height", type=int, default=0,
                    help="Višina stilla (0 = PixelArraySize)")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--quality", type=int, default=75,
                    help="JPEG kakovost predogleda")
    ap.add_argument("--still-quality", type=int, default=92,
                    help="JPEG kakovost stilla (če pride do re-encode)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    picam = Picamera2(camera_num=args.camera)
    native_w, native_h = sensor_still_size(picam)
    still_w = args.still_width or native_w
    still_h = args.still_height or native_h

    LOGGER.info(
        "Kamera #%d: predogled %dx%d @ %d fps q=%d; still %dx%d",
        args.camera, args.width, args.height, args.fps, args.quality,
        still_w, still_h,
    )

    runtime = CameraRuntime(
        picam,
        preview_size=(args.width, args.height),
        still_size=(still_w, still_h),
        fps=float(args.fps),
        preview_quality=args.quality,
        still_quality=args.still_quality,
    )
    LOGGER.info("Kamera pozenjena.")

    StreamingHandler.runtime = runtime

    try:
        httpd = ThreadedHTTPServer((args.host, args.port), StreamingHandler)
    except OSError as exc:
        LOGGER.error("Ne morem zagnati streznika: %s", exc)
        runtime.close()
        return 1

    LOGGER.info(
        "MJPEG stream: http://%s:%d/stream.mjpg  snapshot(preview): /snapshot.jpg",
        args.host, args.port,
    )

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("SIGINT, zaustavljanje...")
    finally:
        httpd.server_close()
        runtime.close()
        LOGGER.info("Konec.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
