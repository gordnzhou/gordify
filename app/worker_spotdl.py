import base64
from datetime import datetime, timedelta
import io
import os
import json
import re
import shutil
from typing import Any
import docker
from docker.errors import ImageNotFound

from worker_ytdlp import WorkerContainerError
from worker_store import AudioSource, identify_key_from_metadata, store_track, AUDIO_EXTENSIONS

SPOTDL_IMAGE         = os.environ.get("SPOTDL_IMAGE", "spotdl/spotify-downloader:latest")
SPOTDL_CONFIG_DIR    = os.environ.get("SPOTDL_CONFIG_DIR")
HOST_MUSIC_ROOT      = os.environ.get("HOST_MUSIC_ROOT")
CONTAINER_MUSIC_ROOT = os.environ.get("CONTAINER_MUSIC_ROOT")
MUSIC_STORE_FOLDER   = os.environ.get("MUSIC_STORE_FOLDER")
PUID = os.environ.get("PUID", 1000)
PGID = os.environ.get("PGID", 1000)

DOWNLOAD_TIMEOUT_SECONDS = int(os.environ.get("JOB_TIMEOUT_SECS", "2700"))
RESOLVE_TIMEOUT_SECONDS = 300

HOST_STORE_ROOT = os.path.join(HOST_MUSIC_ROOT, MUSIC_STORE_FOLDER) 
HOST_RESOLVE_ROOT = os.path.join(HOST_MUSIC_ROOT, ".spotdl", "resolve")
CONTAINER_RESOLVE_ROOT = os.path.join(CONTAINER_MUSIC_ROOT, ".spotdl", "resolve")
RESOLVE_SPOTDL_IMAGE_NAME = "spotdl-fast:latest"

GRAVEYARD_PATH = os.path.join(HOST_MUSIC_ROOT, ".spotdl", "graveyard.json")
GRAVEYARD_TTL_DAYS = 5


def sp_resolve_tracks(client, query: str, job_id: str) -> list[dict[str: Any]]:
    """Returns the list of resolved track dicts for `query`."""
    os.makedirs(HOST_RESOLVE_ROOT, exist_ok=True)
    os.chown(HOST_RESOLVE_ROOT, int(PUID), int(PGID))

    _check_resolve_spotdl_image(client)

    try:
        resolve_file = f"{job_id}.spotdl"
        host_resolve_path = os.path.join(HOST_RESOLVE_ROOT, resolve_file)
        container_resolve_path = os.path.join(CONTAINER_RESOLVE_ROOT, resolve_file)

        _run_spotdl_container(client,
            command=["save", query, "--save-file", container_resolve_path, "--config"],
            spotdl_image_name=RESOLVE_SPOTDL_IMAGE_NAME,
            timeout_secs=RESOLVE_TIMEOUT_SECONDS,
        )

        with open(host_resolve_path) as f:
            tracks = json.load(f)

        for track in tracks:
            track["title"] = f"{', '.join(track['artists'])} - {track['name']}"
    finally:
        if os.path.exists(host_resolve_path):
            os.remove(host_resolve_path)

    return tracks


SPOTDL_ERROR_RE = re.compile(
    r"https?://open\.spotify\.com/track/"
    r"(?P<id>[A-Za-z0-9]+)"
    r"\s+-\s+"
    r"(?P<type>[A-Za-z_][A-Za-z0-9_]*):"
    r"\s*(?P<message>.*)"
)
def _parse_spotdl_errors(errors_path: str, error: set[str], unavailable: set[str]) -> None:
    """
    Parses errors outputted from spotdl --save-errors.
    UPDATES error and unavailable with ids of tracks
    """
    if not os.path.exists(errors_path):
        return
    
    with open(errors_path, "r") as f:
        for line in f:
            match = SPOTDL_ERROR_RE.search(line.strip())
            if not match:
                continue
            
            track_id = match.group("id")
            key = identify_key_from_metadata({"song_id": track_id}, AudioSource.SPOTIFY)

            error_type = match.group("type")
            error_msg = match.group("message")
            if error_type == "LookupError":
                unavailable.add(key)
            elif error_type == "SongError" and ("Track no longer exists" in error_msg or "Couldn't get metadata" in error_msg):
                unavailable.add(key)
            else: # normal error
                if not track_id in unavailable:
                    error.add(key)


def sp_download_and_store_tracks(client, 
    tracks: list[dict[str, Any]], 
    job_id: str, 
    track_paths: dict[str, str], 
    error_keys: set[str],
    unavailable_keys: set[str]
) -> str:
    """
    Runs spotdl download on all track_urls and stores audio files in master store.
    UPDATES: `track_paths` with of each downloaded track's key and Path in Master Store
    UPDATES: `error_keys` with list of track keys that failed to download
    UPDATES: `unavailable_keys` with list of track keys that are unavailable
    Returns container's logs
    """
    now = datetime.now()
    host_staging_dir = os.path.join(HOST_MUSIC_ROOT, "staging", job_id)
    container_staging_dir = os.path.join(CONTAINER_MUSIC_ROOT, "staging", job_id)
    os.makedirs(host_staging_dir, exist_ok=True)
    os.chown(host_staging_dir, int(PUID), int(PGID))

    # do not try again songs that have no results
    graveyard = {}
    if os.path.exists(GRAVEYARD_PATH):
        with open(GRAVEYARD_PATH, "r") as file:
            graveyard = json.load(file)
    graveyard = { k: v for k, v in graveyard.items() if datetime.fromisoformat(v) > now }
    graveyard_keys = graveyard.keys()

    track_urls = []
    for t in tracks:
        key = identify_key_from_metadata(t, AudioSource.SPOTIFY)
        if key in graveyard_keys:
            unavailable_keys.add(key)
        else:
            track_urls.append(t["url"])
    in_graveyard = len(tracks) - len(track_urls)

    if len(track_urls) == 0:
        return "Skipped download step as all songs already downloaded or are in graveyard."

    err: WorkerContainerError = None
    logs = ""
    try:
        try:
            logs = _run_spotdl_container(client, 
                command=[
                    "download", 
                    *track_urls, 
                    "--config", 
                    "--save-errors", "./errors.txt",
                    "--yt-dlp-args", "--extractor-args 'youtube:player_client=android'",
                    "--output", "{track-id}.{output-ext}",
                ], 
                working_dir=container_staging_dir,
                timeout_secs=DOWNLOAD_TIMEOUT_SECONDS, 
            )
        except WorkerContainerError as e:
            err = e

        errors_path = os.path.join(host_staging_dir, "errors.txt")
        _parse_spotdl_errors(errors_path, error_keys, unavailable_keys)

        for t in tracks:
            key = identify_key_from_metadata(t, AudioSource.SPOTIFY)
            if key in error_keys or key in unavailable_keys:
                continue

            src = None
            for ext in AUDIO_EXTENSIONS:
                path = os.path.join(host_staging_dir, f"{t["song_id"]}{ext}")
                if os.path.exists(path):
                    src = path
                    break

            if not src:
                error_keys.add(key)
            else:
                store_path = store_track(src, key)
                track_paths[key] = store_path 
    finally:
        shutil.rmtree(host_staging_dir, ignore_errors=True)

    graveyard_added = 0
    for key in unavailable_keys:
        if key in graveyard:
            continue
        graveyard_added += 1
        graveyard[key] = (now + timedelta(days=GRAVEYARD_TTL_DAYS)).isoformat()
    with open(GRAVEYARD_PATH, "w") as f:
        json.dump(graveyard, f, indent=2)

    success = len(re.findall(r"(?m)^Downloaded ", logs)) 
    skipped = len(re.findall(r"(?m)^Skipping ", logs))
    error       = len(error_keys)
    unavailable = len(unavailable_keys)

    logs += f"tracks in graveyard (skipped): {in_graveyard}, tracks added to graveyard: {graveyard_added} (TTL={GRAVEYARD_TTL_DAYS} days)\n"
    logs += f"Complete. Downloaded: {success}, Error: {error}, Unavailable: {unavailable}, Skipped: {skipped}\n"

    if err is not None:
        raise err
    
    return logs


def _run_spotdl_container(client, 
    command: list[str], 
    timeout_secs: int, 
    spotdl_image_name: str = None,
    working_dir: str = "/", 
    mem_limit: str = "1g"
) -> str:
    """Creates a temporary SpotDL container and runs command with volumes at HOST_MUSIC_ROOT and SPOTDL_CONFIG_DIR.

    Returns:
        SpotDL container's logs.

    Raises:
        WorkerContainerError: SpotDL container timed out, or exited with non-zero code.
    """
    container = None
    image_name = spotdl_image_name if spotdl_image_name else SPOTDL_IMAGE
    try:
        container = client.containers.create(
            image_name,
            command=command,
            user=f"{PUID}:{PGID}",
            working_dir=working_dir,
            environment={"PYTHONUNBUFFERED": "1"},
            volumes={
                HOST_MUSIC_ROOT: {"bind": CONTAINER_MUSIC_ROOT, "mode": "rw"},
                SPOTDL_CONFIG_DIR: {"bind": "/home/spotdl/.config/spotdl", "mode": "rw"},
            },
            mem_limit=mem_limit,
        )
        container.start()

        try:
            result = container.wait(timeout=timeout_secs)
            exit_code = result.get("StatusCode", 1)
        except Exception:
            logs = container.logs().decode("utf-8", errors="replace")
            container.kill()
            raise WorkerContainerError(f"job exceeded {timeout_secs}s timeout and was killed", logs)

        logs = container.logs().decode("utf-8", errors="replace")

        if exit_code != 0:
            raise WorkerContainerError(f"spotdl exited with exit code {exit_code}", logs)
    except docker.errors.APIError as e:
        raise RuntimeError(f"docker error: {e}")
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass

    return logs


SPOTDL_SITECUSTOMIZE_SOURCE = '''\
try:
    from spotdl.utils import search
except ImportError:
    pass
else:
    if not hasattr(search, "reinit_song"):
        import sys
        sys.exit(
            "FATAL: spotdl.utils.search.reinit_song no longer exists in "
            "this spotdl version - the patch baked into this image is "
            "targeting a function that's been renamed or removed "
            "upstream."
        )
    search.reinit_song = lambda song: song
'''
SPOTDL_PATCH_CONTAINER_PATH = os.path.join("spotdl-patch")

def _check_resolve_spotdl_image(client):
    """
    Creates a modified image of SPOTDL_IMAGE specifically for fetching song metadata from a query.
    The modified image removes unnessesary API calls in reinit_song(), which fire PER TRACK, reducing time taken
    from ~270-300s -> ~5-10s for a ~50 song playlist
    """
    try:
        client.images.get(RESOLVE_SPOTDL_IMAGE_NAME)
    except ImageNotFound:
        print(f"Creating custom SpotDL image for fetching metadata: '{RESOLVE_SPOTDL_IMAGE_NAME}'")

        encoded = base64.b64encode(SPOTDL_SITECUSTOMIZE_SOURCE.encode()).decode()
        dockerfile = f"""\
FROM {SPOTDL_IMAGE}
USER root
RUN mkdir -p {SPOTDL_PATCH_CONTAINER_PATH} && \\
    python3 -c "import base64,pathlib; pathlib.Path('{SPOTDL_PATCH_CONTAINER_PATH}/sitecustomize.py').write_bytes(base64.b64decode('{encoded}'))"
ENV PYTHONPATH="{SPOTDL_PATCH_CONTAINER_PATH}:${{PYTHONPATH}}"
        """

        try:
            image, _logs = client.images.build(
                fileobj=io.BytesIO(dockerfile.encode()),
                tag=RESOLVE_SPOTDL_IMAGE_NAME,
                rm=True,
            )
        except docker.errors.BuildError as e:
            for chunk in e.build_log:
                if "stream" in chunk:
                    print(chunk["stream"], end="")
            raise

        print(f"Built {image.tags}")