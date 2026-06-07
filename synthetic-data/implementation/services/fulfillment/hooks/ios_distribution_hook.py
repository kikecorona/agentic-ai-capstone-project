"""iOS / iPadOS distribution hook.

Issues a signed download URL pointing at the iOS variant of the app
binary. In a real system this would call into the App Store CDN to mint
a per-user, time-limited URL. Here we just return a deterministic stub.
"""
import time
import hashlib


PLATFORM = "ios"


def issue_download(app_id: str, user_id: str) -> dict:
    """Return a download URL + expiry for the iOS binary of `app_id`."""
    expires_at = int(time.time()) + 3600
    sig = hashlib.sha256(f"{app_id}|{user_id}|{expires_at}".encode()).hexdigest()[:16]
    return {
        "platform": PLATFORM,
        "url": f"https://cdn.pearstore.dev/ios/{app_id}.ipa?sig={sig}&exp={expires_at}",
        "expires_at": expires_at,
    }
