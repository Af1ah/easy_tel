# Browser playback on a small Linux VPS

Easy Tel adds a **Watch video** button to newly uploaded video files when enabled.
Drive uploads, download links, and storage management still work. The watch page
uses self-hosted **Plyr 3.8.5 (MIT)** and **hls.js 1.7.3 (Apache-2.0)**. Vendored
licenses, versions, and package integrity hashes are in `easy_tel/static/vendor`.
No third-party CDN, analytics, fonts, npm build, or external web player is needed.

## Resource policy

Defaults favor your other work, even on a 4-core / 16 GB server:

- No conversion on upload. Preparation starts when the watch page is opened.
- One preparation job at a time, two encoder threads, one decoder/filter thread,
  and low-priority FFmpeg processes (idle I/O priority when `ionice` is available). No background quality ladder or per-viewer
  transcoding. Multiple viewers share the same prepared disk files.
- Compatible H.264 8-bit 4:2:0 video is copied, preserving source video quality.
  Compatible mono/stereo AAC-LC is copied too. This is the cheapest common path.
- Other video becomes H.264 at up to 1080p/30, CRF 22, veryfast preset. Smaller
  sources are not enlarged. Other audio becomes 192 kbps stereo AAC. Originals
  remain downloadable untouched; browser conversions cannot be lossless.
- The systemd template caps the entire media service and its children at two CPU
  cores and 2 GB RAM, with lower CPU/IO priority where the host controllers support it. Those are maximums, not reserved
  resources. Cached playback generally needs very little application CPU.
- Source and prepared files share a 40 GiB budget. Admission reserves estimated
  output space; disk space is monitored while preparing. There is a 1 GiB free
  disk reserve. Keep room for the bot's current download and other VPS services.
- 24-hour watch links, periodic expired-file cleanup, a 4K input pixel ceiling,
  at most eight audio and sixteen text subtitle tracks. A full cache does not
  fail the Drive upload; it can prevent a watch copy from being created/prepared.

This is VOD, not live streaming: normal HLS with cached fMP4 segments suits it
better than a low-latency live-streaming stack. The browser does not fetch media
segments before Play. It buffers about 20–30 seconds, stops loading on pause,
keeps a short back buffer, and uses private caching and persistent connections.
The HTTP server uses file responses with byte-range/HEAD support, including files
above 2 GiB. A normal website ping is not a guarantee of video startup or throughput.

## Deploy alongside the existing bot

1. Stop the bot, back up `state/jobs.sqlite3` privately, and update the checkout:

   ```bash
   cd /opt/easy_tel
   sudo systemctl stop easy-tel
   git pull --ff-only origin main
   sudo apt-get update
   sudo apt-get install -y ffmpeg caddy
   .venv/bin/python -m pip install -r requirements.txt
   mkdir -p state/media
   chmod 700 state state/media
   cp -n deploy/media.env.example .media.env
   chmod 600 .media.env
   ```

2. Preserve your existing `.env`. Add the media settings from `.env.example` and
   set `TEL_MEDIA_ENABLED=1`. Set `TEL_MEDIA_BASE_URL` to the public HTTPS origin
   without a trailing path, for example `https://tel.aflahdev.me`. Both services
   must use the same state directory. Edit `.media.env` with the matching media
   settings; keep Telegram/rclone credentials out of this separate file. The template expects `/opt/easy_tel/state`.
   Existing Drive-only uploads are not downloaded again automatically; send a
   new video to create a local watch copy.

3. Edit `deploy/easy-tel-media.service`: replace `YOUR_VPS_USER` with the same
   **existing Linux user** as the working bot. Do not leave the placeholder:
   `217/USER` means that user does not exist. Ensure this account owns `state/`.
   If you change `TEL_STATE_DIR`, also update `ReadWritePaths` in the service.
   It is deliberately restricted to writing the media cache, without access to
   home folders, the bot environment file, or the bot session database.

   ```bash
   sudo install -m 644 deploy/easy-tel-media.service /etc/systemd/system/easy-tel-media.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now easy-tel-media
   sudo systemctl start easy-tel
   curl --fail http://127.0.0.1:8080/healthz
   sudo systemctl status easy-tel-media
   ```

4. Point the hostname's DNS to this VPS and allow TCP 80/443. Merge
   `deploy/Caddyfile.media.example` into `/etc/caddy/Caddyfile`; preserve any other
   sites. Caddy supplies HTTPS and proxies to the loopback-only HTTP service.

   ```bash
   sudo caddy validate --config /etc/caddy/Caddyfile
   sudo systemctl reload caddy
   ```

   Do not publish port 8080 directly. If another service uses it, change both
   the unit's `--port` and Caddy's upstream. Avoid request-URL logs in Caddy,
   monitoring, or other proxies: URLs contain the access token. The application
   disables HTTP access logs. The media service reads only `.media.env`, with no Telegram/rclone credentials.

5. Send a small disposable movie to the bot. After upload, open **Watch video**.
   The first visit queues preparation; leave the page open while it finishes.
   Test audio, subtitle selection/off, seeking, mute, fullscreen, and a phone.
   Then test a representative 2–3 GB file and inspect actual VPS CPU/RAM/disk:

   ```bash
   sudo systemd-cgtop
   sudo journalctl -u easy-tel-media -n 50 --no-pager
   df -h /opt/easy_tel/state
   ```

A first conversion can take minutes on two CPU threads; an already prepared
movie has no conversion delay. Setting `TEL_MEDIA_THREADS=1` and lowering
`CPUQuota` trades preparation speed for even lower resource use. Do not install
Playwright or a browser on the production VPS unless you want browser tests.

## Controls and compatibility

The player offers a seek bar, play/pause, rewind/forward 10 seconds, mute/volume,
speed, fullscreen, and picture-in-picture where available. The top-right audio
and subtitle icon menus contain language selectors; subtitles also have an on/off
switch. Double-tap either side to skip 10 seconds. A single mobile tap reveals
controls. Keyboard controls include Space/K, arrows, and M. Paused controls stay
visible. Native iOS fullscreen may use Apple's system controls.

Generated fixtures and decoded HLS outputs cover H.264/AAC MP4, H.264 MKV,
HEVC 10-bit, HDR10/PQ and HLG, AV1, VP8/VP9, MPEG-4/AVI, MPEG-2/TS, WMV/ASF,
AAC, MP3, AC-3, E-AC-3, DTS, FLAC, PCM, Opus, Vorbis, surround downmix, silent
video, variable frame rate, rotation, H.264 High 10, and 4:4:4 sources. Tagged HDR
is tone-mapped to SDR, with a notice; the untouched original retains HDR. These
are synthetic compatibility checks, not a guarantee for every movie/encoder.
Keep your distribution's FFmpeg updated; tests used FFmpeg 7.1.5. The rotation
fixture uses its `-display_rotation` option.

SRT, ASS/SSA, mov_text, and other supported text subtitles become WebVTT. Complex
ASS styling/karaoke is simplified. PGS/VobSub image subtitles are not OCR'd or
burned in. Dolby Vision profile 5, DRM, damaged files, very large inputs beyond
the 4K pixel ceiling, Blu-ray/DVD menus, and unsupported encoders are not promised.
The page offers the original download when preparation fails. Safari's native
HLS fallback can switch audio only if that browser exposes `audioTracks`; use a
current Chromium browser for the tested selector behavior. WebKit/iOS hardware
playback still needs device testing.

## Link and storage behavior

Anyone holding a watch link can watch/download until expiry; there is no separate
browser login. Random 256-bit tokens gate the page, API, original, segments, and
subtitles. Responses carry restrictive CSP, no-referrer, and anti-framing headers.
File paths are allowlisted and isolated per token. The HTTP service exposes no
upload, arbitrary-file browsing, admin, or delete endpoints. Management remains
in the owner-restricted Telegram bot. Successful bot deletion revokes the local
watch link as well. Expiry/revocation denies new requests; already downloaded or
buffered data and requests already in progress cannot be recalled.

Cache cleanup removes local originals/HLS after expiry, not the Drive copy.
Renewal is currently by sending the movie again as a new job. Back up credentials
and bot state separately; the media cache is disposable. Failed preparations are
not endlessly retried on page refresh, avoiding repeated CPU use.

## Reproduce checks

```bash
.venv/bin/python -m unittest discover -s tests -v
# Optional developer-only browser checks (Chromium must be installed):
.venv/bin/python -m pip install -r requirements-browser.txt
CHROMIUM_PATH=/usr/bin/chromium .venv/bin/python tests/browser_media.py
```

The browser harness generates a two-audio/two-subtitle movie, serves a real local
HTTP service, and tests playback, both selectors, captions off/on, desktop/mobile
±10-second seeking, the seek bar, mute, no mobile overflow, no JS errors, and no
external network requests. It simulates 80 ms latency and 2 MiB/s throughput.
Screenshots and a JSON report go to `/tmp/easy-tel-browser-artifacts`. Reported
startup numbers apply only to this small generated clip with a warm prepared
cache; they are not a benchmark of full movies, your VPS, or mobile devices.
