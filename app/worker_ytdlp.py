import io
import os
import json
import shutil
from typing import Any
from urllib.parse import quote_plus

import docker
from docker.errors import ImageNotFound

from worker_store import AudioSource, identify_key_from_metadata, store_track, AUDIO_EXTENSIONS

HOST_MUSIC_ROOT      = os.environ.get("HOST_MUSIC_ROOT")
CONTAINER_MUSIC_ROOT = os.environ.get("CONTAINER_MUSIC_ROOT")
MUSIC_STORE_FOLDER   = os.environ.get("MUSIC_STORE_FOLDER")
PUID = os.environ.get("PUID", 1000)
PGID = os.environ.get("PGID", 1000)

DOWNLOAD_TIMEOUT_SECONDS = int(os.environ.get("JOB_TIMEOUT_SECS", "2700"))
RESOLVE_TIMEOUT_SECONDS = 300

HOST_STORE_ROOT = os.path.join(HOST_MUSIC_ROOT, MUSIC_STORE_FOLDER) 
YTDLP_IMAGE_NAME = "ytdlp:latest"

class WorkerContainerError(Exception):
    def __init__(self, message, logs):
        self.message = message 
        self.logs = logs
        super().__init__(message)


def yt_resolve_tracks(client, query: str, is_search_query: bool) -> list[dict[str: Any]]:
    """Returns the list of resolved track dicts for `query`."""
    def parse_single_track(track_json, list_name: str = ""):
        track = {}
        track["song_id"] = track_json["id"]
        track["name"] = track_json["track"] if track_json.get("track") else track_json["title"]
        if track_json.get("artists"):
            track["artists"] = track_json["artists"].copy()
        elif track_json.get("artist"):
            track["artists"] = [ track_json["artist"] ]
        else:
            track["artists"] = []
        track["url"] = track_json["original_url"]
        track["title"] = track_json["fulltitle"]
        track["list_name"] = list_name

        return track

    def is_resolved_music_entry(t: dict) -> bool:
        # Entries for private/unavailable/removed videos come back as
        # sparse stubs (often just {"_type": "url", "id": ..., "url": ...,
        # ...} plus sometimes "error"/"availability"). Skip anything that
        # wasn't actually resolved before touching required fields.
        if t is None or t.get("_type") == "url":
            return False
        if t.get("availability") in {"private", "needs_auth", "premium_only", "subscriber_only"}:
            return False
        required = ("id", "original_url", "fulltitle")
        if not all(t.get(k) for k in required):
            return False
        categories = t.get("categories", [])
        return "Music" in categories

    _check_ytdlp_image(client)
    extra_args = []
    if is_search_query:
        query = f"https://music.youtube.com/search?q={quote_plus(query)}"
        extra_args = ["--playlist-end", "1"]
    stdout, _ = _run_ytdlp_container(client,
        command=["--dump-single-json", "--skip-download", "--no-warnings", "-i", *extra_args, query],
        timeout_secs=RESOLVE_TIMEOUT_SECONDS,
    )
    stdout = stdout.strip()
    if not stdout:
        return []

    # DROP all videos in query that are not in 'Music' category
    json_out = json.loads(stdout)
    if json_out.get("entries"):
        list_name = json_out["title"]
        return [
            parse_single_track(t, list_name) for t in json_out["entries"] 
            if is_resolved_music_entry(t)
        ] 
    return [parse_single_track(json_out)] if "Music" in (json_out.get("categories", [])) else []


def yt_download_and_store_tracks(client, 
    tracks: list[dict[str, Any]], 
    job_id: str, 
    track_paths: dict[str, str], 
    error_keys: set[str],
) -> str:
    """
    Runs ytdlp on all track_urls and stores audio files in master store.
    UPDATES: `track_paths` with of each downloaded track's key and Path in Master Store
    UPDATES: `error_tracks` with list of track keys that failed to download
    Returns container's logs
    """
    host_staging_dir = os.path.join(HOST_MUSIC_ROOT, "staging", job_id)
    container_staging_dir = os.path.join(CONTAINER_MUSIC_ROOT, "staging", job_id)
    os.makedirs(host_staging_dir, exist_ok=True)
    os.chown(host_staging_dir, int(PUID), int(PGID))

    track_urls = []
    for t in tracks:
        key = identify_key_from_metadata(t, AudioSource.YOUTUBE)
        track_urls.append(t["url"])

    err: WorkerContainerError = None
    logs = ""
    try:
        try:
            stdout, stderr = _run_ytdlp_container(client, 
                command=[
                    "--ignore-errors", 
                    "-x", 
                    "--embed-metadata",
                    "--embed-thumbnail",
                    "--convert-thumbnails", "jpg",
                    "--parse-metadata", "%(title)s:%(track)s",
                    "--parse-metadata", "%(uploader)s:%(artist)s",
                    "--audio-format", "mp3", 
                    "--extractor-args", "youtube:player_client=android",
                    "--paths", container_staging_dir,
                    "-o", "%(id)s.%(ext)s",
                    *track_urls
                ], 
                timeout_secs=DOWNLOAD_TIMEOUT_SECONDS, 
            )
            logs = stdout + stderr
        except WorkerContainerError as e:
            err = e

        for t in tracks:
            src = None
            for ext in AUDIO_EXTENSIONS:
                path = os.path.join(host_staging_dir, f"{t["song_id"]}{ext}")
                if os.path.exists(path):
                    src = path
                    break

            if not src:
                error_keys.add(key)
            else:
                key = identify_key_from_metadata(t, AudioSource.YOUTUBE)
                store_path = store_track(src, key)
                track_paths[key] = store_path
    finally:
        shutil.rmtree(host_staging_dir, ignore_errors=True)

    success = len(tracks) 
    error   = len(error_keys)
    logs += f"Complete. Downloaded: {success}, Error: {error}\n"

    if err is not None:
        raise err
    
    return logs


def _run_ytdlp_container(client, 
    command: list[str], 
    timeout_secs: int, 
    working_dir: str = "/", 
    mem_limit: str = "1g"
) -> tuple[str, str]:
    """Creates a temporary yt-dlp container and runs command.
    Returns:
        Tuple of container's (stdout, stderr) logs.
    Raises:
        WorkerContainerError: yt-dlp container timed out or exited with non-zero code.
    """
    container = None
    try:
        container = client.containers.create(
            YTDLP_IMAGE_NAME,
            command=command,
            user=f"{PUID}:{PGID}",
            working_dir=working_dir,
            volumes={
                HOST_MUSIC_ROOT: {"bind": CONTAINER_MUSIC_ROOT, "mode": "rw"},
            },
            mem_limit=mem_limit,
        )
        container.start()

        try:
            result = container.wait(timeout=timeout_secs)
            exit_code = result.get("StatusCode", -1)
        except Exception:
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
            container.kill()
            raise WorkerContainerError(f"job exceeded {timeout_secs}s timeout and was killed", stdout + stderr)

        stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
        stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")

        if exit_code not in {0, 1}:
            raise WorkerContainerError(f"ytdlp exited with exit code {exit_code}", stdout + stderr)
    except docker.errors.APIError as e:
        raise RuntimeError(f"docker error: {e}")
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass

    return (stdout, stderr)


def _check_ytdlp_image(client):
    """Creates ytdlp image"""
    try:
        client.images.get(YTDLP_IMAGE_NAME)
    except ImageNotFound:
        print(f"Creating custom ytdlp image for youtube downloads: '{YTDLP_IMAGE_NAME}'")

        dockerfile = f"""\
FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl unzip \
    && rm -rf /var/lib/apt/lists/* \
    && DENO_INSTALL=/usr/local sh -c "curl -fsSL https://deno.land/install.sh | sh" \
    && chmod -R a+rX /usr/local/bin/deno \
    && pip install --no-cache-dir --upgrade yt-dlp

ENTRYPOINT ["yt-dlp"]
        """

        try:
            image, _logs = client.images.build(
                fileobj=io.BytesIO(dockerfile.encode()),
                tag=YTDLP_IMAGE_NAME,
                rm=True,
            )
        except docker.errors.BuildError as e:
            for chunk in e.build_log:
                if "stream" in chunk:
                    print(chunk["stream"], end="")
            raise

        print(f"Built {image.tags}")