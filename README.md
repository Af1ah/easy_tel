# easy_tel

Send or forward files privately to **@easy_tel2_bot**. Telethon downloads them
through production MTProto; rclone uploads them to `gdrive:easy_tel`.
One worker processes a durable SQLite queue. Local files are deleted only after
Drive size and MD5 verification. Failed downloads/uploads retain local files.

## VPS setup

Use Python 3.10+ and a current stable rclone. Place the checkout at `/opt/easy_tel`,
owned by the non-root user who already has access to the `gdrive:` remote.

```bash
cd /opt/easy_tel
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# First setup only; preserve an existing .env.
cp -n .env.example .env
chmod 600 .env
```

Edit `.env` locally: set production `TEL_APPID`, `TEL_APP_HASH`, `TEL_BOT_TOKEN`,
and your numeric `TEL_ALLOWED_USER_IDS` (comma-separated). The bot's `/id`
command reveals your user ID even before you are allowed. Set `TEL_DRIVE_DEST`
if you want another folder. If needed, set `RCLONE_CONFIG` to the existing VPS
configuration path. Never commit secrets, rclone tokens, or session files.
Do not use Telegram test-server settings.

For manual runs, load your trusted `.env`:

```bash
set -a
. ./.env
set +a
rclone listremotes
rclone lsd gdrive:
.venv/bin/python -m easy_tel --check-telegram
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m easy_tel
```

The authentication check writes no session and starts no transfers. Normal
startup checks Drive read access. Send `/id`, allow that ID, restart, and send
a small document. Confirm "uploaded and verified" and check Drive before
testing 2–3 GB files. A 3 GB source upload requires a Telegram Premium sender.
Real VPS throughput still needs measurement. Large documents use four concurrent
MTProto chunk requests by default; small files/photos use Telethon's standard
downloader. This does not require a Premium subscription.

## Continuous operation

Replace `YOUR_VPS_USER` in `deploy/easy-tel.service` with the user owning the
rclone configuration. As an administrator:

```bash
sudo install -m 644 deploy/easy-tel.service /etc/systemd/system/easy-tel.service
sudo systemctl daemon-reload
sudo systemctl enable --now easy-tel
sudo systemctl status easy-tel
sudo journalctl -u easy-tel -n 50
```

Only run one instance per state directory. Protect `.env` and `state/` as
credentials. Keep state on persistent storage. Default maximum size is 4 GiB,
disk reserve is 1 GiB, and queue capacity is 20. Reserve space for retained
failed files too. No public Drive links are created.

## Commands and recovery

- `/start` and `/help`: instructions; `/id`: your user ID; `/status`: recent jobs
  and attempt counts; `/retry JOB_ID`: requeue your failed job without creating
  a new job or remote filename.
- Restarted running jobs return to the queue. Complete local files are reused;
  incomplete downloads restart from the beginning.
- Remote names include job IDs. Immutable uploads prevent replacing conflicting
  objects; checksums verify recovery after an interrupted upload.
- Transient Telegram/network errors and rclone temporary failures are retried
  automatically, up to four total attempts by default. Delays start at about
  15 seconds, then double (with jitter, capped around five minutes). Telegram
  flood waits use the full server-requested delay. Scheduled retries persist
  across restarts and do not block other ready jobs. Rclone makes up to three
  low-level retries; the app controls whole-job retries.
- Syntax errors, missing source files, insufficient disk space, fatal rclone
  errors, and checksum conflicts need operator attention. Local files are kept.
  After fixing the cause, use `/retry JOB_ID`; only the job's owner can retry it.
  It resets the attempt budget and uses the existing complete download. Partial
  downloads restart from the beginning into a `.part` file. Do not resend the
  same file as a new message unless you want a separate job. There is no cancel
  command yet. Avoid changing the Drive destination while jobs are active.
- Group messages and unapproved users cannot queue transfers.

## Cloud development

Use `/workspace/telegram-transfer-venv/bin/python` in the existing checkout.
This machine does not share the VPS's `gdrive` login. The local mocked tests
cover queue recovery and checksum verification; live Telegram and Drive tests
are separate. See the setup report for the current connection-test result.

## Progress updates

The bot edits the job's existing status message, showing the file name/size,
download or upload percentage, transferred bytes, speed, and estimated time
remaining for the **current stage**. At 100% upload it still checks the Drive
checksum before reporting success. ETA is an estimate, not a promised completion
time; it does not include queue waiting or later stages.

Transfer callbacks only update in-memory counters. A separate notifier edits at
most once every five seconds by default (`TEL_PROGRESS_INTERVAL`, minimum three).
Terminal updates may be immediate, but still respect flood waits.
Unchanged messages are skipped. Telegram flood waits pause notifications without
pausing data transfer. If the status message was deleted or cannot be edited,
the bot creates a replacement and saves its ID. Network failures retry the edit
on a later tick instead of creating duplicate messages. A terminal update can
be delayed or missed during a Telegram outage; `/status` reads the durable job
state. Upload statistics are sampled every two seconds using rclone JSON logs,
with a text-stats fallback for older versions. If no readable stats are emitted,
the bot shows the stage without inventing transfer progress. No rclone remote
control port or additional HTTP service is exposed.

## Upgrade an existing VPS deployment

Stop the service before replacing code. Back up `state/jobs.sqlite3` privately;
it contains file metadata. Preserve `.env`, the bot session, and the rclone config.
After deploying this version to `/opt/easy_tel`:

```bash
cd /opt/easy_tel
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
sudo systemctl start easy-tel
sudo journalctl -u easy-tel -n 50
```

The database migrates automatically, retaining existing jobs and assigning them
an initial attempt count of zero. Add the optional retry/progress settings from
`.env.example` to your existing `.env` only if you want different defaults.
Validate a small real Telegram-to-Drive transfer, then a 2–3 GB file on the VPS.
The automated suite includes a throttled real local rclone copy to exercise the
progress parser, plus simulated Telegram edits and transfer failures. It cannot
verify live Telegram authentication or Google Drive access in this cloud machine.

## Documentation checked for progress and retries

Reviewed current official upstream docs/source before implementation:

- [Telegram MTProto `messages.editMessage`](https://core.telegram.org/method/messages.editMessage)
  and the [official current API schema](https://github.com/telegramdesktop/tdesktop/blob/dev/Telegram/SourceFiles/mtproto/scheme/api.tl).
- [Telethon message editing](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.messages.MessageMethods.edit_message)
  and [download callbacks](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.downloads.DownloadMethods.download_media),
  cross-checked against its upstream v1 source and our pinned 1.42.0 package.
- [Rclone JSON logs](https://rclone.org/docs/#use-json-log),
  [statistics](https://rclone.org/docs/#stats-duration), and
  [retry flags](https://rclone.org/docs/#retries-int), from the
  [official current documentation source](https://github.com/rclone/rclone/blob/master/docs/content/docs.md).

Direct documentation sites were blocked by this environment's egress proxy;
the linked official upstream sources were accessible. Compatibility was tested
with the installed rclone 1.60.1 and Telethon 1.42.0.

## Download performance without Premium

The original downloader fetched one chunk at a time. For example, 850 MB in
five minutes is about 2.8 MB/s. A 512 KiB request followed by a roughly 180 ms
round trip can produce a similar rate even with bandwidth available. This is a
possible explanation, not a measurement of your VPS's Telegram connection.

For documents at least 32 MiB, the app now pipelines four independent download
iterators through Telethon's public API. They use supported 512 KiB requests and
write disjoint offsets into the partial file. Memory use stays bounded; files
are never buffered entirely in RAM. All workers must complete and every chunk
must have the expected length before the file is eligible for upload. Workers
are cancelled and joined before a failed partial file is closed.

- `TEL_DOWNLOAD_WORKERS=4`: conservative default, clamped to 1–8. Set 1 to use
  the standard downloader. Start with 4; don't increase it just because your
  VPS is advertised as 1 Gbps.
- `TEL_PARALLEL_MIN_BYTES=33554432`: use the standard downloader below this size.
- Incompatible iterator/media responses fall back to the standard downloader.
  Network, disk, or flood errors follow the persistent retry policy instead.
- Retries reduce request concurrency (4 → 2 → 1 with default settings). Telegram
  flood waits, including Premium-related waits, are honored in full. This does
  not bypass Telegram account or file-size limits.
- A native `cryptg` encryption/decryption accelerator is installed by
  `requirements.txt`. Confirm it is active in the **same virtualenv** used by
  systemd; pure Python encryption can bottleneck on CPU.

After updating the VPS and loading its trusted `.env`:

```bash
.venv/bin/python -m easy_tel --diagnose
sudo journalctl -u easy-tel -f
```

Diagnostics require neither credentials nor network access. Successful download
logs include the job ID, downloader mode, elapsed seconds, and average MiB/s.
Compare the same representative file with `TEL_DOWNLOAD_WORKERS=1` and `4`
(restart the service after each setting change). A newly sent file creates a
new job; previously completed local downloads will be reused and will not
measure download speed again. Check CPU usage, disk throughput, and the VPS's
actual speed-test result while downloading. Compare Mbps and MiB/s carefully:
1 Gbps is about 119 MiB/s before overhead, not a guaranteed per-service rate.
An upload to Google Drive at 14 MB/s does not establish the download bandwidth
available from Telegram. Provider shaping, routing to Telegram's data center,
shared CPU, and Telegram-side limits can still dominate.

The automated suite exercises the actual pinned Telethon iterators with
simulated MTProto replies: concurrent requests, out-of-order arrival, tail
chunks, interrupted transfers, short disk writes, and Premium flood waits.
It proves assembly and failure handling, not real Telegram throughput. The
cloud environment still cannot connect to Telegram, so verify the improvement
on your VPS before relying on a particular speed. Relevant upstream reference:
[Telethon iter_download](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.downloads.DownloadMethods.iter_download)
and [current download implementation](https://github.com/LonamiWebs/Telethon/blob/v1/telethon/client/downloads.py).
