#!/usr/bin/env bash
# Run as the checkout/venv owner, not via sudo. Also accepted over SSH stdin.
set -Eeuo pipefail
cd "${EASY_TEL_APP_DIR:-/opt/easy_tel}"
exec 9>.deploy.lock
flock -n 9 || { echo 'Another deployment is running.' >&2; exit 1; }

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo 'Tracked local edits found; commit or move them before deploying.' >&2; exit 1;
fi
test -x .venv/bin/python
sudo -n /usr/local/sbin/easy-tel-services preflight
git fetch --no-tags origin main
target=${1:-$(git rev-parse origin/main)}
[[ "$target" =~ ^[0-9a-f]{40}$ ]] || { echo 'Expected a full commit SHA.' >&2; exit 1; }
git merge-base --is-ancestor "$target" origin/main || {
    echo 'Deployment commit is not on origin/main.' >&2; exit 1;
}
git merge-base --is-ancestor HEAD "$target" || {
    echo 'Checkout is newer than or diverged from this deployment; refusing to rewind.' >&2; exit 1;
}
# Build/download requirements before stopping the running services.
wheels=$(mktemp -d)
stopped=0
finish() {
    result=$?
    trap - EXIT
    if (( stopped )); then
        echo 'Deployment failed; attempting to start the services. Check the Actions log.' >&2
        sudo -n /usr/local/sbin/easy-tel-services start || true
    fi
    rm -rf -- "$wheels"
    exit "$result"
}
trap finish EXIT
git show "$target:requirements.txt" > "$wheels/requirements.txt"
.venv/bin/python -m pip wheel --wheel-dir "$wheels" -r "$wheels/requirements.txt"
stopped=1
sudo -n /usr/local/sbin/easy-tel-services stop
git merge --ff-only "$target"
.venv/bin/python -m pip install --no-index --find-links "$wheels" -r requirements.txt
.venv/bin/python -m pip check
sudo -n /usr/local/sbin/easy-tel-services start
stopped=0
sudo -n /usr/local/sbin/easy-tel-services check
printf 'Deployed %s\n' "$(git rev-parse HEAD)"
