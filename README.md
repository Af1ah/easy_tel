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
Large-file speed and real transfers have not yet been validated. This first
version uses a single download stream, without parallel Telegram connections.

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

- `/start` and `/help`: instructions; `/id`: your user ID; `/status`: recent jobs.
- Restarted running jobs return to the queue. Complete local files are reused;
  incomplete downloads restart from the beginning.
- Remote names include job IDs. Immutable uploads prevent replacing conflicting
  objects; checksums verify recovery after an interrupted upload.
- Failed jobs are not automatically retried. Diagnose disk space, message
  availability, network, or rclone access. Resend the file as a new message to
  retry, then manually remove retained files when no longer needed. Resending
  may duplicate an already-uploaded object. There is no cancel command yet.
- Group messages and unapproved users cannot queue transfers.

## Cloud development

Use `/workspace/telegram-transfer-venv/bin/python` in the existing checkout.
This machine does not share the VPS's `gdrive` login. The local mocked tests
cover queue recovery and checksum verification; live Telegram and Drive tests
are separate. See the setup report for the current connection-test result.
