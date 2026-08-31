import sys
import os
import time
from typing import Any
import uuid
import requests
from pathlib import Path

import pytest

sys.path.append("/home/gordon/gordify")
from scripts.manage_users import add_user, delete_user, login

NAVIDROME_URL         = os.environ.get("NAVIDROME_URL_LOCAL", "http://localhost:4533").rstrip("/")
ADMIN_USER            = os.environ.get("NAVIDROME_ADMIN_USER")
ADMIN_PASS            = os.environ.get("NAVIDROME_ADMIN_PASS")
HOST_MUSIC_ROOT       = os.environ.get("HOST_MUSIC_ROOT")
CONTAINER_MUSIC_ROOT  = os.environ.get("CONTAINER_MUSIC_ROOT")
MUSIC_STORE_FOLDER    = os.environ.get("MUSIC_STORE_FOLDER", "tracks")

JOB_API_URL = "http://localhost:8080"

@pytest.fixture(scope="function")
def setup():
    if not ADMIN_USER or not ADMIN_PASS:
        sys.exit("Set NAVIDROME_ADMIN_USER and NAVIDROME_ADMIN_PASS in your environment (or `source .env`).")

    assert job_api("GET", "/health")["ok"]

    token = login(ADMIN_USER, ADMIN_PASS)

    temp_users = set()
    def create_temp_user(token: str, admin: bool = False) -> tuple[str, str, str]:
        rand_id = uuid.uuid4().hex[:8]
        username = f"temp_{ "admin_" if admin else "" }{rand_id}"
        password = f"password@123"
        user_dir = add_user(token, username, password, make_admin=admin)

        print(f"\n[Setup] Created temp user: {username}")
        temp_users.add(username)
        return username, password, user_dir

    yield token, create_temp_user

    print(f"\n[Cleanup] Deleting temp users... {temp_users}")
    for username in temp_users:
        try:
            delete_user(token, username)
        except Exception as e:
            pytest.fail(f"Failed to delete user {username}: {e}")


def test_single_download_spotify(setup):
    token, create_temp_user = setup
    username, password, user_dir = create_temp_user(token)

    spotify_url = "https://open.spotify.com/track/47IXLhp3c6mu7NqvpuhuLi"
    song_store_path = os.path.join(HOST_MUSIC_ROOT, MUSIC_STORE_FOLDER, "sp_47IXLhp3c6mu7NqvpuhuLi.mp3")
    if os.path.exists(song_store_path):
        os.remove(song_store_path)

    user_path = Path(user_dir)
    all_files_before = [f.name for f in user_path.rglob("*") if f.is_file()]
    job_status, logs = run_single_job(username, password, spotify_url)
    new_files = [f.name for f in user_path.rglob("*") if f.is_file() and f.name not in all_files_before]

    assert job_status == "finished"
    assert len(new_files) == 1, f"Download failed, worker logs: {logs}"
    assert os.path.exists(song_store_path)


def test_single_download_youtube(setup):
    token, create_temp_user = setup
    username, password, user_dir = create_temp_user(token)

    spotify_url = "https://youtu.be/S1GxqV65Tsg"
    song_store_path = os.path.join(HOST_MUSIC_ROOT, MUSIC_STORE_FOLDER, "yt_S1GxqV65Tsg.mp3")
    if os.path.exists(song_store_path):
        os.remove(song_store_path)

    user_path = Path(user_dir)
    all_files_before = [f.name for f in user_path.rglob("*") if f.is_file()]
    job_status, logs = run_single_job(username, password, spotify_url)
    new_files = [f.name for f in user_path.rglob("*") if f.is_file() and f.name not in all_files_before]

    assert job_status == "finished"
    assert len(new_files) == 1, f"Download failed, worker logs: {logs}"
    assert os.path.exists(song_store_path)


def test_invalid_queries(setup):
    token, create_temp_user = setup
    username, password, user_dir = create_temp_user(token)
    admin_username, admin_password, _ = create_temp_user(token, True)

    user_path = Path(user_dir)
    all_files_before = [f.name for f in user_path.rglob("*") if f.is_file()]

    job_status, _ = run_single_job(username, password, "")
    assert job_status == "failed"
    job_status, _ = run_single_job(admin_username, admin_password, "https://youtu.be/S1GxqV65Tsg")
    assert job_status == "failed"
    job_status, _ = run_single_job(username, password, "https://youtube.com/shorts/9JsL7wexSgI")
    assert job_status == "failed"

    new_files = [f.name for f in user_path.rglob("*") if f.is_file() and f.name not in all_files_before]
    assert len(new_files) == 0


def run_single_job(
    username: str, 
    password: str, 
    query: str, 
    timeout_secs: int = 120, 
    poll_interval: int = 3
) -> tuple[str, str]:
    """
    Returns status of job { "finished", "failed" } and job's logs 
    """
    job = job_api("POST", "/api/jobs", username, password, json={ "query": query })
    job_id = job["job_id"]
    job_status = job["status"]
    assert job_status == "queued"

    job = None
    start_time = time.time()
    while (time.time() - start_time) < timeout_secs:
        try:
            job = job_api("GET", f"/api/jobs/{job_id}", username, password)
            job_status = job["status"]

            if job_status == "finished":
                break
            if job_status == "failed": 
                break
            elif job_status not in ("running", "queued"):
                pytest.fail(reason=f"unknown job status: {job_status}")
        except requests.exceptions.HTTPError as e:
            pytest.fail(reason=f"Could not find job id: {job_id}\n{e}")

        time.sleep(poll_interval)

    if job is None:
        pytest.fail(reason=f"Could not fetch job: {job_id}")
    elif job_status in ("running", "queued"):
        pytest.fail(reason=f"Job took too long (timeout={timeout_secs}s): {job_id}")

    return (job_status, job["logs"])


def job_api(method: str, endpoint: str, username: str | None = None, password: str | None = None, **kwargs) -> Any:
    if username is not None and password is not None:
        kwargs["auth"] = (username, password)
    headers = kwargs.pop("headers", {})

    r = requests.request(method, f"{JOB_API_URL}{endpoint}", 
        headers=headers, 
        **kwargs
    )
    if not r.ok:
        print(f"  -> {method} {endpoint} failed: {r.status_code} {r.text}", file=sys.stderr)
    r.raise_for_status()
    return r.json()