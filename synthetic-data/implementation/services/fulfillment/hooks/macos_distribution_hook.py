"""macOS distribution hook.

Issues a download URL for the macOS variant of an app (a notarized .pkg
in production; a stubbed URL here).
"""
import time
import hashlib


PLATFORM = "macos"


def issue_download(app_id: str, user_id: str) -> dict:
    expires_at = int(time.time()) + 3600
    sig = hashlib.sha256(f"{app_id}|{user_id}|{expires_at}|mac".encode()).hexdigest()[:16]
    return {
        "platform": PLATFORM,
        "url": f"https://cdn.pearstore.dev/mac/{app_id}.pkg?sig={sig}&exp={expires_at}",
        "expires_at": expires_at,
    }
