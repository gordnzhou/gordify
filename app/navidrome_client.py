"""
Thin client for talking to Navidrome's Subsonic-compatible API.
"""

import hashlib
import os
from pathlib import Path
import secrets
import time
import httpx

NAVIDROME_URL = os.environ["NAVIDROME_URL"].rstrip("/")
ADMIN_USER = os.environ["NAVIDROME_ADMIN_USER"]
ADMIN_PASS = os.environ["NAVIDROME_ADMIN_PASS"]

APP_NAME = "spotdl-job-queue"
API_VERSION = "1.16.1"


def _subsonic_auth_params(username: str, password: str) -> dict:
    salt = secrets.token_hex(6)
    token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()
    return {
        "u": username,
        "t": token,
        "s": salt,
        "v": API_VERSION,
        "c": APP_NAME,
        "f": "json",
    }


def _admin_token() -> str:
    resp = httpx.post(
        f"{NAVIDROME_URL}/auth/login",
        json={"username": ADMIN_USER, "password": ADMIN_PASS},
        timeout=10,
    )

    resp.raise_for_status()
    return resp.json()["token"]


def verify_user(username: str, password: str, ignore_errors: bool = True) -> bool:
    """Return True if these credentials are valid for this Navidrome user."""
    params = _subsonic_auth_params(username, password)
    try:
        resp = httpx.get(f"{NAVIDROME_URL}/rest/ping.view", params=params, timeout=10)
        resp.raise_for_status()
        status = resp.json().get("subsonic-response", {}).get("status")
        return status == "ok"
    except httpx.HTTPError:
        if ignore_errors:
            return False
        raise


def trigger_scan() -> bool:
    """Kick off a Navidrome library scan using the admin account."""
    params = _subsonic_auth_params(ADMIN_USER, ADMIN_PASS)
    try:
        resp = httpx.get(f"{NAVIDROME_URL}/rest/startScan.view", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json().get("subsonic-response", {}).get("status") == "ok"
    except httpx.HTTPError:
        return False


def fetch_user(username: str, ignore_errors: bool = True) -> dict | None:
    try:
        token = _admin_token()
        resp = httpx.get(
            f"{NAVIDROME_URL}/api/user",
            headers={"X-ND-Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        for user in resp.json():
            if user.get("userName").lower() == username.lower():
                return user
        return None
    except httpx.HTTPError:
        if ignore_errors:
            return None
        raise

def change_user_password(username: str, new_password: str, ignore_errors: bool = True) -> bool:
    user = fetch_user(username)
    if not user:
        return False

    try:
        token = _admin_token()
        headers = {"X-ND-Authorization": f"Bearer {token}"}

        resp = httpx.get(f"{NAVIDROME_URL}/api/user/{user['id']}", headers=headers, timeout=10)
        resp.raise_for_status()
        full_user = resp.json()
        full_user["password"] = new_password

        resp = httpx.put(
            f"{NAVIDROME_URL}/api/user/{user['id']}",
            headers=headers,
            json=full_user,
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPError:
        if ignore_errors:
            return False
        raise
    
def fetch_playlist(playlist_name: str, user_id) -> dict | None:
    """Gets playlist by name whose path contains `user_id`"""
    try:
        token = _admin_token()
        resp = httpx.get(
            f"{NAVIDROME_URL}/api/playlist",
            headers={"X-ND-Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()

        for playlist in resp.json():
            # ASSUMES that playlist's m3u8 file is in a folder named after owning user's navidrome id
            path = Path(playlist.get("path"))
            if playlist.get("name") == playlist_name and user_id in path.parts[:-1]:
                return playlist
        return None
    except httpx.HTTPError:
        return None


def assign_playlist_owner(playlist_name: str, username: str, ignore_errors: bool = True) -> bool:
    """Reassign a synced playlist (imported as admin-owned by default) to its actual user."""
    user = fetch_user(username)
    playlist = fetch_playlist(playlist_name, user["id"]) if user else None
    if not user or not playlist:
        return False
    
    try:
        token = _admin_token()
        resp = httpx.put(
            f"{NAVIDROME_URL}/api/playlist/{playlist['id']}",
            headers={"X-ND-Authorization": f"Bearer {token}"},
            json={"ownerId": user["id"]},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPError:
        if ignore_errors:
            return False
        raise

def fetch_playlist_paths(playlist_name: str, username: str) -> list[str]:
    user = fetch_user(username)
    playlist = fetch_playlist(playlist_name, user["id"]) if user else None
    if not user or not playlist:
        return []

    try:
        token = _admin_token()
        resp = httpx.get(
            f"{NAVIDROME_URL}/api/playlist/{playlist['id']}/tracks",
            headers={"X-ND-Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        return [track["path"] for track in resp.json() if track.get("path")]
    except httpx.HTTPError as exc:
        print("ERROR: ", exc)
        return []
    
def wait_for_scan(timeout_secs: int = 300, poll_interval: float = 3.0) -> bool:
    """Block until Navidrome's current scan finishes. Returns False on timeout or error."""
    token = _admin_token()
    headers = {"X-ND-Authorization": f"Bearer {token}"}
    start = time.monotonic()
    try:
        while time.monotonic() - start < timeout_secs:
            resp = httpx.get(f"{NAVIDROME_URL}/api/scan/status", headers=headers, timeout=10)
            resp.raise_for_status()
            if not resp.json().get("scanning"):
                return True
            time.sleep(poll_interval)
        return False
    except httpx.HTTPError:
        return False