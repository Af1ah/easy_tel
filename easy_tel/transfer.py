import asyncio
import hashlib
import json
import re
from pathlib import Path


def safe_name(name: str) -> str:
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^\w.() -]", "_", name).strip(" .")
    return name[:180] or "file.bin"


async def md5(path: Path) -> str:
    def digest():
        h = hashlib.md5()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                h.update(block)
        return h.hexdigest()
    return await asyncio.to_thread(digest)


async def rclone(*args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "rclone", *args, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await process.communicate()
    except asyncio.CancelledError:
        process.terminate()
        await process.wait()
        raise
    if process.returncode:
        # Do not expose remote diagnostics that may contain credentials or names.
        raise RuntimeError(f"rclone {args[0]} failed (exit {process.returncode})")
    return stdout.decode()


async def upload_verified(path: Path, target: str) -> None:
    await rclone("copyto", str(path), target, "--checksum", "--immutable",
                 "--transfers", "1", "--drive-chunk-size", "64M")
    result = json.loads(await rclone("lsjson", target, "--stat", "--hash"))
    hashes = {key.lower(): value for key, value in result.get("Hashes", {}).items()}
    remote_hash = hashes.get("md5", "")
    if result.get("Size") != path.stat().st_size or remote_hash.lower() != await md5(path):
        raise RuntimeError("Drive size/checksum verification failed; local file retained")
    path.unlink()
