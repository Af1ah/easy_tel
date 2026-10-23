# Automatic updates from GitHub

GitHub Actions runs the backend/codec tests and real Chromium playback checks on
pull requests and pushes to `main`. A successful `main` run deploys the tested
commit over SSH when the repository variable `DEPLOY_ENABLED` is `true`. You can
also run **Actions → Test and deploy → Run workflow** on `main`. Tests run on
GitHub's runner, not your VPS. No extra daemon or domain is needed for deployment.

The VPS updater downloads/builds Python wheels first, then briefly stops the bot
and enabled media service, fast-forwards the code, installs dependencies offline,
and starts/checks the services. Deployments are serialized on GitHub and locked
on the VPS. It preserves ignored `.env`, `.media.env`, sessions, rclone config,
and `state/`. Tracked local edits, divergent history, and stale deployments fail
instead of resetting the checkout. In-progress transfers may restart/retry using
the existing durable queue; playback is briefly interrupted.

## One-time VPS setup

Use the existing account that owns `/opt/easy_tel`, its virtual environment and
rclone configuration. Prefer a non-root account; do not change the working bot's
account without moving its rclone configuration/session access too. These commands
assume the existing bot is already installed as `easy-tel.service`.

```bash
cd /opt/easy_tel
git pull --ff-only origin main
sudo apt-get update
sudo apt-get install -y git python3-venv curl util-linux sudo
test -x .venv/bin/python || python3 -m venv .venv

# Root owns this small helper; the deployment user cannot rewrite it.
sudo install -o root -g root -m 755 deploy/easy-tel-services /usr/local/sbin/easy-tel-services

# Only allow these fixed service actions without a password.
rules=$(mktemp)
printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/easy-tel-services preflight, /usr/local/sbin/easy-tel-services stop, /usr/local/sbin/easy-tel-services start, /usr/local/sbin/easy-tel-services check\n' "$(id -un)" > "$rules"
sudo visudo -cf "$rules" && sudo install -o root -g root -m 440 "$rules" /etc/sudoers.d/easy-tel-deploy
rm -f "$rules"
sudo -n /usr/local/sbin/easy-tel-services preflight

# Apply the current release now (also the command for future manual updates).
bash deploy/update.sh
```

The checkout must be able to fetch GitHub noninteractively. For this public repo,
an HTTPS origin such as `https://github.com/Af1ah/easy_tel.git` needs no token.
For a private repo, configure a separate read-only GitHub deploy key on the VPS.
Never put a token in the remote URL. The deployment SSH key below is a different
key: it allows GitHub Actions to connect **to the VPS**.

Systemd/Caddy templates are not automatically installed on each push. This keeps
account names, service restrictions and other sites under your control. If you
change those templates later, install/reload them manually. The helper manages
`easy-tel-media.service` only when it is **enabled**. It expects media health on
`127.0.0.1:8080`; update the root-owned helper if you chose another port.

## Connect GitHub Actions to the VPS

1. On your trusted computer, create a dedicated, passphrase-free deployment key
   and authorize its public key for the same VPS account:

   ```bash
   ssh-keygen -t ed25519 -N '' -C easy-tel-actions -f ~/.ssh/easy-tel-actions
   ssh-copy-id -i ~/.ssh/easy-tel-actions.pub -p 22 VPS_USER@VPS_IP
   ```

   Replace the user, IP, and port. Keep the private key private. You can prefix
   this key's line in VPS `~/.ssh/authorized_keys` with `restrict` to disable
   forwarding and PTY access while allowing the deployment command. The key
   grants shell access as that user: protect repository write access accordingly.

2. In **GitHub repo → Settings → Secrets and variables → Actions → Secrets**, add:

   | Secret | Value |
   | --- | --- |
   | `VPS_HOST` | VPS IPv4 or SSH hostname; use the direct VPS IP if the website is proxied |
   | `VPS_USER` | Existing checkout/venv owner |
   | `VPS_PORT` | SSH port; optional, defaults to `22` |
   | `VPS_SSH_KEY` | Entire private key from `~/.ssh/easy-tel-actions` |
   | `VPS_KNOWN_HOSTS` | Verified SSH host-key line described below |

   Read the **public** host key through your trusted VPS console:

   ```bash
   sudo cat /etc/ssh/ssh_host_ed25519_key.pub
   ```

   Prefix its key type and key with the exact `VPS_HOST`, making a line like
   `31.97.202.121 ssh-ed25519 AAAAC3...`. For a nonstandard port use
   `[31.97.202.121]:2222 ssh-ed25519 AAAAC3...`. Use your actual address/port and
   complete public key. Do not use unverified `ssh-keyscan` output as your trust
   source. The workflow requires strict host verification.

3. In the **Variables** tab add `DEPLOY_ENABLED` = `true`. Push to `main` or run
   the workflow manually on `main`. Only enable this after the manual updater
   succeeds. Add branch protection requiring the `test` job if desired.

No VPS credentials are sent to pull-request tests. Missing settings fail the
deployment with a clear error. Turning `DEPLOY_ENABLED` off leaves CI running.

## Optional browser player: tv.ariise.cloud

The Telegram/Drive bot and CI/CD need no domain. The browser player needs HTTPS.
For that feature, first complete [MEDIA.md](MEDIA.md), which now uses
`https://tv.ariise.cloud`. Point an **A record** for `tv.ariise.cloud` to your VPS;
add an AAAA record only if IPv6 reaches this VPS too. Permit ports 80/443.

For an existing media installation, edit these two files locally and set:

```dotenv
TEL_MEDIA_BASE_URL=https://tv.ariise.cloud
```

Set that value in both `.env` and `.media.env`; set `TEL_MEDIA_ENABLED=1` in the
bot's `.env` if enabling the feature. Merge `deploy/Caddyfile.media.example`
into `/etc/caddy/Caddyfile` while preserving other sites, then:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
sudo systemctl enable easy-tel-media
bash /opt/easy_tel/deploy/update.sh
curl --fail https://tv.ariise.cloud/healthz
```

The media unit must already be installed with its real Linux user as described
in `MEDIA.md`. New bot buttons use the new hostname after restart; already sent
Telegram buttons retain their old URLs. HTTPS is handled by Caddy automatically.

## Failure and recovery

The Actions log reports the failed step. On the VPS:

```bash
sudo systemctl status easy-tel easy-tel-media --no-pager
sudo journalctl -u easy-tel -u easy-tel-media -n 60 --no-pager
```

If a dependency installation or update fails after stopping, the script attempts
to start the services and still exits with failure. It does **not** claim a
successful rollback, revert databases, or restore old dependency versions.
Health checks verify systemd startup and local media HTTP, not a real Telegram
login, Drive transfer, or public DNS/TLS. After changing credentials, check an
actual bot transfer yourself. A failed startup can require a manual correction.

For a code regression, revert the bad commit in Git and push the revert to `main`;
the next successful run deploys it. Preserve/back up state before any manual
database recovery. Never use `git reset --hard` to discard unreviewed VPS edits.
