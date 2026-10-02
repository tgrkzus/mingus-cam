# mingus-cam

Records the camera continuously, saves a clip whenever there's motion, deletes old files, and serves a bare passcode-protected page with the live view and the clip list.

## Setup

Needs Python 3.9+ and ffmpeg.

```
# macOS:   brew install ffmpeg
# Ubuntu:  sudo apt install ffmpeg python3-venv
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Copy `config.example.ini` to `config.ini` and set the camera `url`, `passcode` and `secret_key` (any long random string).
`config.ini` is git-ignored so your camera login stays out of the repo.

On Windows you can just double-click `run.bat`: it creates `config.ini` on first run, installs ffmpeg (via winget) and the Python packages if needed, then starts the app.

## Run

```
venv/bin/python catcam.py            # uses ./config.ini
venv/bin/python catcam.py other.ini  # or a different config
```

Open http://localhost:8080 and enter the passcode.

## Run with Docker

Create `config.ini` as above, then:

```
docker compose up -d --build
docker compose logs -f
```

`config.ini` is mounted read-only into the container, and `data/` lives in the named volume `mingus-cam_mingus-data`, which Docker creates automatically and keeps across rebuilds.
Set `TZ` (default `Australia/Sydney`) so clip names match the camera's clock, e.g. `TZ=Europe/London docker compose up -d`.
Leave `data_dir = data` in the config so files land in the volume.

### HTTPS

Point a domain at the server, open ports 80 and 443, and create a `.env` next to `docker-compose.yml`:

```
DOMAIN=cam.example.com
COMPOSE_PROFILES=https
PORT_BIND=127.0.0.1:8080
```

`docker compose up -d` then also starts Caddy, which gets and renews a Let's Encrypt certificate and serves the page on https://cam.example.com.
`PORT_BIND` keeps the plain HTTP port reachable only from the server itself.

## What's on disk

- `data/recordings/` — continuous 10-second files, kept for `recording_keep_hours`
- `data/clips/` — one .mp4 per motion event (5 s before to 10 s after), kept for `clip_keep_days`
- Everything in `data/` is also capped at `max_total_gb`; the oldest files go first.

Cleanup runs every minute.

## Tuning motion

If you get too many clips (shadows, light changes), raise `min_area_percent` or `pixel_threshold`.
If it misses the cat, lower them. The settings are in the `[motion]` section of `config.ini`.

## Notes

- Clips show up about 10–20 seconds after motion stops, once the recording files they come from are complete.
- The passcode page is plain HTTP. If you open it to the internet, put it behind HTTPS
  (for example Tailscale, or a Caddy/nginx reverse proxy). Otherwise keep it on your home network.
- To keep it running after reboot, run it as a service (systemd on Linux, launchd on macOS) or in `tmux`.
