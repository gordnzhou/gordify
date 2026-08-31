"""
Worker process. Runs as its own container (`python worker.py`), separate
from the web app. Loop: ask the DB for the next queued job, run it, write
the result back, repeat. No message broker involved - db.claim_next_job()
is the entire "queue".
"""
from datetime import date, datetime
import os
import time
import traceback
from urllib.parse import parse_qs, urlparse
import docker
import httpx

import job_db
from navidrome_client import fetch_playlist_paths, fetch_user, trigger_scan, wait_for_scan, assign_playlist_owner
from worker_spotdl import sp_resolve_tracks, sp_download_and_store_tracks
from worker_ytdlp import yt_resolve_tracks, yt_download_and_store_tracks, WorkerContainerError
from worker_store import track_already_stored, identify_key_from_metadata, link_into_user_dir, write_m3u8, sanitize, AudioSource

SPOTDL_IMAGE          = os.environ.get("SPOTDL_IMAGE", "spotdl/spotify-downloader:latest")
HOST_MUSIC_ROOT       = os.environ.get("HOST_MUSIC_ROOT")
CONTAINER_MUSIC_ROOT  = os.environ.get("CONTAINER_MUSIC_ROOT")
MUSIC_USERLIBS_FOLDER = os.environ.get("MUSIC_USERLIBS_FOLDER", "userlibs")

PUID = os.environ.get("PUID", 1000)
PGID = os.environ.get("PGID", 1000)

POLL_INTERVAL_SECONDS = float(os.environ.get("JOB_POLL_INTERVAL_SECS", "3"))
MAX_PLAYLIST_SIZE = 500
DOWNLOAD_MAX_RETRIES = 3

def worker_log(job_id: str, *args, **kwargs):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] (job_id={job_id})", *args, **kwargs)


def complete_job_failed(job_id: str, error: str, logs: str = ""):
    job_db.complete_job(job_id, status="failed", error=error, logs=logs)
    return f"failed to finish: {error}"


def complete_job_success(job_id: str, logs: str = "", rescan_triggered: bool = False):
    job_db.complete_job(job_id, status="finished", logs=logs, rescan_triggered=rescan_triggered)
    return f"successfully finished (rescan_triggered={rescan_triggered})"


def run_one_job(job: dict) -> str:
    job_id, username, query = job["id"], job["username"], job["query"]
    worker_log(job_id, f"starting job for '{username}', query: {query!r}")

    audio_source = None
    is_search_query = False
    if is_url(query):
        if is_spotify_url(query):
            audio_source = AudioSource.SPOTIFY
        elif is_youtube_url(query):
            audio_source = AudioSource.YOUTUBE
        else:
            return complete_job_failed(job_id, error=f"Invalid url: {query}, only Spotify and Youtube URLs allowed")
    else:
        is_search_query = True
        audio_source = AudioSource.YOUTUBE
    assert audio_source

    try:
        user = fetch_user(username, ignore_errors=False)
    except httpx.HTTPError as exc:
        worker_log(job_id, f"HTTP Exception for {exc.request.url} - {exc}")
        return complete_job_failed(job_id, error=f"Could not reach Navidrome for user auth")

    if not user:
        return complete_job_failed(job_id, error=f"Username not found: {username}")
    if user["isAdmin"]:
        return complete_job_failed(job_id, error=f"Restricted to non-admin users only")

    user_dir = os.path.join(HOST_MUSIC_ROOT, MUSIC_USERLIBS_FOLDER, user["id"])
    if not os.path.isdir(user_dir):
        return complete_job_failed(job_id, error=f"No library folder for user '{username}'")

    client = docker.from_env()

    # 1. fetch metadata of all tracks in query
    # each track dict has fields: song_id, name, artists, list_name, url, title 
    metadata_start = time.time()
    try:
        worker_log(job_id, f"resolving track metadata...")
        tracks = []
        if audio_source == AudioSource.SPOTIFY:
            tracks = sp_resolve_tracks(client, query, job_id)
        elif audio_source == AudioSource.YOUTUBE:
            tracks = yt_resolve_tracks(client, query, is_search_query)
    except WorkerContainerError as e:
        return complete_job_failed(job_id, logs=e.logs, error=f"Fetching metadata failed: {e.message} (is the playlist private?)")
    except Exception as e:
        return complete_job_failed(job_id, logs=traceback.format_exc(), error=f"Fetching metadata failed: {e}")
    worker_log(job_id, f"query's metadata contains {len(tracks)} item(s). Fetching it took {int(time.time() - metadata_start)}s")

    if not tracks:
        return complete_job_failed(job_id, error="Query contains no tracks")
    if len(tracks) > MAX_PLAYLIST_SIZE:
        return complete_job_failed(job_id, error=f"Playlist is too large ({len(tracks)} > {MAX_PLAYLIST_SIZE})")

    # 2. find which tracks are already downloaded and which need download
    store_paths = {}
    needs_download = []
    for t in tracks:
        key = identify_key_from_metadata(t, audio_source)
        if key:
            store_path = track_already_stored(key)
            if store_path:
                store_paths[key] = store_path
            else:
                needs_download.append(t)

    # 3. download missing tracks and detect unavailable ones
    logs = ""
    unavailable_keys = set()
    error_keys = set()
    for i in range(DOWNLOAD_MAX_RETRIES):
        if len(needs_download) == 0:
            break

        worker_log(job_id, f"[Attempt {i+1}] downloading {len(needs_download)} missing track(s)")
        error_keys = set()
        download_start = time.time()
        new_store_paths = {}
        try:
            if audio_source == AudioSource.SPOTIFY:
                new_logs = sp_download_and_store_tracks(client, needs_download, job_id, new_store_paths, error_keys, unavailable_keys)
            elif audio_source == AudioSource.YOUTUBE:
                new_logs = yt_download_and_store_tracks(client, needs_download, job_id, new_store_paths, error_keys)
            logs += f"\n{new_logs}"
        except WorkerContainerError as e:
            worker_log(job_id, f"Container error: {e.message}")
            logs += f"\n{e.logs}"
        except Exception as e:
            return complete_job_failed(job_id, logs=traceback.format_exc(), error=f"download failed: {e}")

        worker_log(job_id, f"[Attempt {i+1}] finished in {int(time.time() - download_start)}s. Downloaded: {len(new_store_paths)}, Error: {len(error_keys)}, Unavailable: {len(unavailable_keys)}")

        needs_download = [
            t for t in tracks
            if identify_key_from_metadata(t, audio_source) in error_keys
        ]

        store_paths.update(new_store_paths)

    # 4. link all tracks to user's folder and seperate failing tracks between retryable and unretryable
    link_count = 0
    unretryable = []
    retryable = [] # retryable
    track_userpaths = []
    for t in tracks:
        key = identify_key_from_metadata(t, audio_source)
        if key in store_paths:
            store_path = store_paths[key]
            ext = os.path.splitext(store_path)[1].lower()
            track_path = link_into_user_dir(store_path, user_dir, f"{sanitize(t["title"])}{ext}")
            track_userpaths.append(track_path)
            link_count += 1
        else:
            if key in unavailable_keys:
                unretryable.append(t)
            elif key in error_keys:
                retryable.append(t)
            else:
                worker_log(job_id, f"WARNING: track '{t["name"]}' is not downloaded and could not be classified as retryable or unretryable")
    worker_log(job_id, f"total: {len(tracks)}, saved: {link_count}, error retryable: {len(retryable)}, error unretryable: {len(unretryable)}")

    if len(unretryable) > 0:
        names = "\n  - ".join(t.get("name", "?") for t in unretryable)
        logs += f"\nWARNING: {len(unretryable)} track(s) couldn't be downloaded and cannot be retryed due to missing metadata, or I couldn't find matching audio:\n  - {names}"
    if len(retryable) > 0:
        names = "\n  -".join(t.get("name", "?") for t in retryable)
        logs += f"\nWARNING: {len(retryable)} track(s) couldn't be downloaded due to errors but can be retryed:\n  - {names}"

    # 5. playlist creation for multi-track queries
    playlist_name = tracks[0].get("list_name", f"New Playlist {date.today()}") if len(tracks) > 1 else None
    rescanned = False
    if playlist_name:
        worker_log(job_id, f"Appending {len(track_userpaths)} new tracks to .m3u8 file and setting owner of playlist '{playlist_name}' to: {username}")

        old_filenames = { os.path.basename(p) for p in fetch_playlist_paths(playlist_name, username) }
        new_filenames = { os.path.basename(t) for t in track_userpaths if os.path.basename(t) }
        write_m3u8(user_dir, playlist_name, old_filenames | new_filenames)
        rescanned = trigger_scan()

        wait_for_scan()
        try:
            assign_playlist_owner(playlist_name, username, ignore_errors=False)
        except httpx.HTTPError as exc:
            worker_log(job_id, f"WARNING: could not assign owner of {playlist_name} to {username}")
            worker_log(job_id, f"HTTP Exception for {exc.request.url} - {exc}")

        logs += f"\nAdded {len(new_filenames)} new track(s) to playlist named '{playlist_name}'."
    else:
        rescanned = trigger_scan()

    logs += f"\nComplete. Added {link_count}/{len(tracks)} track(s) to {username}'s library.\n"

    return complete_job_success(job_id, logs=logs, rescan_triggered=rescanned)


def is_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    return True


def is_spotify_url(value: str) -> bool:
    SPOTIFY_TYPES = {"track", "album", "playlist", "artist"}

    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    
    host = parsed.netloc.lower().split(":")[0]
    if host == "open.spotify.com":
        parts = [p for p in parsed.path.split("/") if p]
        return (len(parts) == 2 and parts[0].lower() in SPOTIFY_TYPES and bool(parts[1]))

    return False


def is_youtube_url(value: str) -> bool:
    YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
    YOUTUBE_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}

    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False

    host = parsed.netloc.lower().split(":")[0]
    if host in YOUTUBE_SHORT_HOSTS:
        return bool(parsed.path.lstrip("/"))
    if host in YOUTUBE_HOSTS:
        query = parse_qs(parsed.query)
        if parsed.path == "/watch":
            return bool(query.get("v"))
        if parsed.path == "/playlist":
            return bool(query.get("list"))
        return False

    return False

    
def main():
    job_db.init_db()
    worker_log("", f"polling every {POLL_INTERVAL_SECONDS}s, db at {job_db.DB_PATH}")
    while True:
        job = job_db.claim_next_job()
        if job is None:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        try:
            job_id = job["id"]
            start = time.time()
            job_msg = run_one_job(job)
            worker_log(job_id, job_msg)
            total_secs = int(time.time() - start)
            mins, secs = divmod(int(total_secs), 60)
            worker_log(job_id, f"job took {mins}m {secs}s")
        except Exception as e:
            worker_log(job['id'], f"job crashed the handler: {e}")
            job_db.complete_job(job["id"], status="failed", error=str(e), logs=traceback.format_exc())


if __name__ == "__main__":
    main()