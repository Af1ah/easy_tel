import random
from telethon import errors


class PermanentError(RuntimeError):
    pass


class RcloneError(RuntimeError):
    def __init__(self, command, code):
        self.code = code
        super().__init__(f'rclone {command} failed (exit {code})')


def retry_delay(exc, attempt, base=15):
    """None means operator action is needed. Flood waits are never shortened."""
    if isinstance(exc, (errors.FloodWaitError, errors.FloodPremiumWaitError)):
        return max(1, exc.seconds)
    if isinstance(exc, PermanentError):
        return None
    if isinstance(exc, RcloneError):
        if exc.code not in (1, 5):
            return None
    elif isinstance(exc, errors.RPCError):
        if exc.code < 500 and not isinstance(exc, errors.FileReferenceExpiredError):
            return None
    elif not isinstance(exc, (OSError, TimeoutError, ConnectionError)):
        return None
    return min(300, base * 2 ** min(attempt - 1, 8)) + random.uniform(0, 3)



def error_summary(exc):
    if isinstance(exc, RcloneError):
        return f"rclone exit {exc.code}"
    if isinstance(exc, PermanentError):
        return str(exc)
    return type(exc).__name__
