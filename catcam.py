#!/usr/bin/env python3
"""catcam - record an RTSP camera, cut motion clips, prune old files, serve a bare web page.

One ffmpeg process connects to the camera and produces two outputs:
  1. continuous recording, copied (no re-encode) into short .ts segments
  2. a low-rate MJPEG frame feed used for the live view and for motion detection
When motion ends, the matching span is cut out of the segments into an .mp4 clip.
"""
import configparser
import hmac
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from io import BytesIO

import numpy as np
from flask import (Flask, Response, abort, redirect, request, send_from_directory,
                   session, url_for)
from markupsafe import escape
from PIL import Image, ImageFilter

log = logging.getLogger("catcam")

# ---------------------------------------------------------------- config

CONFIG_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
cfg = configparser.ConfigParser()
if not cfg.read(CONFIG_PATH):
    sys.exit(f"Could not read config file {CONFIG_PATH}")

CAM_URL = cfg.get("camera", "url")
CAM_INPUT_ARGS = cfg.get("camera", "input_args", fallback="").split()
RECORD_AUDIO = cfg.getboolean("camera", "record_audio", fallback=False)

HOST = cfg.get("server", "host", fallback="0.0.0.0")
PORT = cfg.getint("server", "port", fallback=8080)
PASSCODE = cfg.get("server", "passcode")
SECRET_KEY = cfg.get("server", "secret_key")

DATA_DIR = os.path.abspath(cfg.get("storage", "data_dir", fallback="data"))
SEGMENT_SECONDS = cfg.getint("storage", "segment_seconds", fallback=10)
RECORDING_KEEP_HOURS = cfg.getfloat("storage", "recording_keep_hours", fallback=24)
CLIP_KEEP_DAYS = cfg.getfloat("storage", "clip_keep_days", fallback=7)
MAX_TOTAL_GB = cfg.getfloat("storage", "max_total_gb", fallback=50)

LIVE_FPS = cfg.getint("motion", "fps", fallback=5)
LIVE_WIDTH = cfg.getint("motion", "live_width", fallback=640)
DIFF_THRESHOLD = cfg.getint("motion", "pixel_threshold", fallback=25)
MIN_AREA = cfg.getfloat("motion", "min_area_percent", fallback=0.5) / 100
MAX_AREA = cfg.getfloat("motion", "max_area_percent", fallback=60) / 100
TRIGGER_FRAMES = cfg.getint("motion", "trigger_frames", fallback=3)
PRE_ROLL = cfg.getfloat("motion", "pre_roll_seconds", fallback=5)
POST_ROLL = cfg.getfloat("motion", "post_roll_seconds", fallback=10)
MIN_EVENT = cfg.getfloat("motion", "min_event_seconds", fallback=2)
MAX_CLIP = cfg.getfloat("motion", "max_clip_seconds", fallback=600)

REC_DIR = os.path.join(DATA_DIR, "recordings")
CLIP_DIR = os.path.join(DATA_DIR, "clips")
os.makedirs(REC_DIR, exist_ok=True)
os.makedirs(CLIP_DIR, exist_ok=True)

SEG_FMT = "%Y%m%d-%H%M%S"


# ---------------------------------------------------------------- shared state

class State:
    def __init__(self):
        self.cond = threading.Condition()
        self.frame = None          # latest JPEG bytes
        self.frame_id = 0
        self.frame_time = 0.0
        self.motion = False        # an event is currently in progress
        self.recorder_up = False


state = State()
clip_jobs = queue.Queue()


# ---------------------------------------------------------------- camera / recorder

def ffmpeg_cmd():
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if CAM_URL.startswith("rtsp"):
        cmd += ["-rtsp_transport", "tcp", "-timeout", "10000000"]
    cmd += CAM_INPUT_ARGS + ["-i", CAM_URL]
    # output 1: continuous recording segments, video copied as-is
    cmd += ["-map", "0:v:0"]
    if RECORD_AUDIO:
        cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "64k"]
    cmd += ["-c:v", "copy", "-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
            "-segment_format", "mpegts", "-reset_timestamps", "1", "-strftime", "1",
            os.path.join(REC_DIR, SEG_FMT + ".ts")]
    # output 2: small MJPEG frames on stdout for live view + motion detection
    cmd += ["-map", "0:v:0", "-an", "-vf", f"fps={LIVE_FPS},scale={LIVE_WIDTH}:-2",
            "-c:v", "mjpeg", "-q:v", "6", "-flush_packets", "1", "-f", "mjpeg", "pipe:1"]
    return cmd


def camera_loop():
    backoff = 2
    while True:
        log.info("connecting to camera")
        proc = subprocess.Popen(ffmpeg_cmd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=_log_stderr, args=(proc,), daemon=True).start()
        buf = b""
        got_frame = False
        try:
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    start = buf.find(b"\xff\xd8")
                    end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                    if start < 0 or end < 0:
                        if start > 0:
                            buf = buf[start:]
                        break
                    jpg = buf[start:end + 2]
                    buf = buf[end + 2:]
                    if not got_frame:
                        got_frame = True
                        backoff = 2
                        state.recorder_up = True
                        log.info("camera connected, recording")
                    with state.cond:
                        state.frame = jpg
                        state.frame_id += 1
                        state.frame_time = time.time()
                        state.cond.notify_all()
        finally:
            state.recorder_up = False
            proc.kill()
            proc.wait()
        log.warning("camera stream ended; reconnecting in %ss", backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


def _log_stderr(proc):
    for line in proc.stderr:
        log.warning("ffmpeg: %s", line.decode(errors="replace").rstrip())


# ---------------------------------------------------------------- motion detection

def to_gray_small(jpg):
    img = Image.open(BytesIO(jpg)).convert("L")
    img = img.resize((160, max(1, round(160 * img.height / img.width))))
    img = img.filter(ImageFilter.GaussianBlur(2))
    return np.asarray(img, dtype=np.float32)


def motion_loop():
    background = None
    hits = 0
    event_start = None
    last_motion = 0.0
    seen = 0
    while True:
        with state.cond:
            state.cond.wait_for(lambda: state.frame_id != seen, timeout=5)
            if state.frame_id == seen:
                frame = None
            else:
                seen, frame, now = state.frame_id, state.frame, state.frame_time
        if frame is None:            # camera down: close any open event
            if event_start is not None:
                finish_event(event_start, last_motion)
                event_start = None
            background = None
            continue
        try:
            gray = to_gray_small(frame)
        except Exception:
            continue
        if background is None or background.shape != gray.shape:
            background = gray
            continue
        changed = (np.abs(gray - background) > DIFF_THRESHOLD).mean()
        background = background * 0.9 + gray * 0.1   # slowly adapt to lighting
        if changed > MAX_AREA:                        # whole-frame change (IR switch, exposure)
            background = gray
            moving = False
        else:
            moving = changed >= MIN_AREA
        hits = hits + 1 if moving else 0

        if hits >= TRIGGER_FRAMES:
            last_motion = now
            if event_start is None:
                event_start = now
                state.motion = True
                log.info("motion started (%.1f%% of frame)", changed * 100)

        if event_start is not None:
            if now - last_motion > POST_ROLL or now - event_start > MAX_CLIP:
                finish_event(event_start, last_motion)
                event_start = None
                if hits >= TRIGGER_FRAMES:          # still moving: continue in a new clip
                    event_start = now
                    state.motion = True


def finish_event(start, last_motion):
    state.motion = False
    if last_motion - start < MIN_EVENT:
        log.info("motion too short (%.1fs), ignored", last_motion - start)
        return
    clip_start = start - PRE_ROLL
    clip_end = last_motion + POST_ROLL
    log.info("motion ended, queueing %.0fs clip", clip_end - clip_start)
    clip_jobs.put((clip_start, clip_end))


# ---------------------------------------------------------------- clip cutting

def list_segments():
    segs = []
    for name in sorted(os.listdir(REC_DIR)):
        if name.endswith(".ts"):
            try:
                segs.append((datetime.strptime(name[:-3], SEG_FMT).timestamp(), os.path.join(REC_DIR, name)))
            except ValueError:
                pass
    return segs


def clip_loop():
    while True:
        start, end = clip_jobs.get()
        # wait for the segment that contains `end` to be closed
        deadline = end + SEGMENT_SECONDS + 5
        while time.time() < deadline and not any(t > end for t, _ in list_segments()):
            time.sleep(1)
        try:
            make_clip(start, end)
        except Exception:
            log.exception("clip failed")


def make_clip(start, end):
    segs = list_segments()
    use = []
    for i, (t, path) in enumerate(segs):
        nxt = segs[i + 1][0] if i + 1 < len(segs) else time.time()
        if nxt > start and t < end:
            use.append((t, path))
    if not use:
        log.warning("no recording found for clip")
        return
    offset = max(0.0, start - use[0][0])
    duration = end - max(start, use[0][0])
    name = datetime.fromtimestamp(max(start, use[0][0])).strftime(SEG_FMT) + f"_{round(duration)}s.mp4"
    out = os.path.join(CLIP_DIR, name)
    tmp = out + ".part.mp4"
    listfile = out + ".txt"
    with open(listfile, "w") as f:
        for _, p in use:
            if os.path.getsize(p) > 0:
                f.write("file '%s'\n" % p.replace("\\", "/").replace("'", "'\\''"))
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-f", "concat", "-safe", "0", "-ss", f"{offset:.2f}", "-i", listfile,
           "-t", f"{duration:.2f}", "-map", "0", "-c", "copy", "-movflags", "+faststart", tmp]
    r = subprocess.run(cmd, capture_output=True)
    os.remove(listfile)
    if r.returncode != 0 or not os.path.exists(tmp):
        log.error("ffmpeg clip error: %s", r.stderr.decode(errors="replace")[-500:])
        return
    os.replace(tmp, out)
    log.info("saved clip %s", name)


# ---------------------------------------------------------------- cleanup

def cleanup_loop():
    while True:
        try:
            cleanup()
        except Exception:
            log.exception("cleanup failed")
        time.sleep(60)


def cleanup():
    now = time.time()
    rec_cutoff = now - max(RECORDING_KEEP_HOURS * 3600, MAX_CLIP + 600)  # never drop segments a pending clip needs
    clip_cutoff = now - CLIP_KEEP_DAYS * 86400
    files = []
    for d, cutoff in ((REC_DIR, rec_cutoff), (CLIP_DIR, clip_cutoff)):
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                st = os.stat(p)
            except FileNotFoundError:
                continue
            if st.st_mtime < cutoff:
                os.remove(p)
                log.info("deleted old %s", name)
            else:
                files.append((st.st_mtime, st.st_size, p))
    # disk cap: delete oldest files first (recordings and clips alike)
    total = sum(s for _, s, _ in files)
    cap = MAX_TOTAL_GB * 1024 ** 3
    for mtime, size, p in sorted(files):
        if total <= cap or now - mtime < 600:
            break
        os.remove(p)
        total -= size
        log.info("deleted %s (over %.0f GB cap)", os.path.basename(p), MAX_TOTAL_GB)


# ---------------------------------------------------------------- web

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["PERMANENT_SESSION_LIFETIME"] = 30 * 86400


@app.before_request
def require_login():
    if request.endpoint not in ("login", "static") and not session.get("ok"):
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("passcode", "").encode(), PASSCODE.encode()):
            session.permanent = True
            session["ok"] = True
            return redirect(url_for("index"))
        time.sleep(1)
        error = "<p>Wrong passcode.</p>"
    return ("<!doctype html><title>catcam</title>"
            f"<form method=post>{error}<input type=password name=passcode autofocus> "
            "<button>Enter</button></form>")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    clips = sorted((n for n in os.listdir(CLIP_DIR) if n.endswith(".mp4") and ".part" not in n), reverse=True)
    rows = []
    day = None
    for n in clips:
        try:
            t = datetime.strptime(n.split("_")[0], SEG_FMT)
        except ValueError:
            continue
        if t.date() != day:
            if day is not None:
                rows.append("</ul>")
            day = t.date()
            rows.append(f"<h3>{day.strftime('%a %d %b %Y')}</h3><ul>")
        dur = n.split("_")[1].removesuffix(".mp4") if "_" in n else ""
        rows.append(f'<li><a href="/clips/{escape(n)}">{t.strftime("%H:%M:%S")}</a> {escape(dur)}</li>')
    if day is not None:
        rows.append("</ul>")
    status = "recording" if state.recorder_up else "camera offline"
    if state.motion:
        status += ", motion now"
    return ("<!doctype html><title>catcam</title>"
            "<h1>catcam</h1>"
            f"<p>Status: {status}. <a href=/>Refresh</a> <a href=/logout>Log out</a></p>"
            "<h2>Live</h2><img src=/live.mjpg alt=live>"
            f"<h2>Clips ({len(clips)})</h2>" + ("".join(rows) or "<p>No clips yet.</p>"))


@app.route("/live.mjpg")
def live():
    def gen():
        seen = 0
        while True:
            with state.cond:
                state.cond.wait_for(lambda: state.frame_id != seen, timeout=10)
                if state.frame_id == seen:
                    continue
                seen, jpg = state.frame_id, state.frame
            yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n"
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store"})


@app.route("/snapshot.jpg")
def snapshot():
    if state.frame is None:
        abort(503)
    return Response(state.frame, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@app.route("/clips/<path:name>")
def clip(name):
    return send_from_directory(CLIP_DIR, name)


# ---------------------------------------------------------------- main

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH")
    if PASSCODE in ("", "change-me") or SECRET_KEY in ("", "change-me-too"):
        log.warning("set passcode and secret_key in %s", CONFIG_PATH)
    for target in (camera_loop, motion_loop, clip_loop, cleanup_loop):
        threading.Thread(target=target, daemon=True, name=target.__name__).start()
    log.info("web page on http://%s:%s", HOST, PORT)
    app.run(host=HOST, port=PORT, threaded=True)


if __name__ == "__main__":
    main()
