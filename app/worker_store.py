import os
import re
from enum import Enum
from mutagen import File as MutagenFile

HOST_MUSIC_ROOT    = os.environ.get("HOST_MUSIC_ROOT")
MUSIC_STORE_FOLDER = os.environ.get("MUSIC_STORE_FOLDER", "tracks")

HOST_STORE_ROOT = os.path.join(HOST_MUSIC_ROOT, MUSIC_STORE_FOLDER)
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".opus", ".ogg", ".wav"}


class AudioSource(Enum):
    SPOTIFY = 1
    YOUTUBE = 2


def path_for_key(key: str, ext: str) -> str:
    return os.path.join(HOST_STORE_ROOT, f"{key}{ext}")


def sanitize(s):
    return re.sub(r'[^A-Za-z0-9_\-]', '_', s)


def identify_key_from_metadata(track_meta, source_type: AudioSource) -> str | None:
    """
    track's store key dervied from metadata. "isrc" field is prioritized over "song_id".
    Key is prefixed "isrc_" if isrc field is found, otherwise it is based on `source_type`.

    Returns:
        Track's store key or None if could not identify key.
    """
    if track_meta.get("isrc"):
        return f"isrc_{sanitize(track_meta['isrc'])}"

    if track_meta.get("song_id"):
        match source_type:
            case AudioSource.SPOTIFY:
                return f"sp_{sanitize(track_meta['song_id'])}"
            case AudioSource.YOUTUBE:
                return f"yt_{sanitize(track_meta['song_id'])}"
    
    return None


def identify_key_from_file(path: str, source_type: AudioSource) -> str | None:
    """
    Key from track's metadata WOAS or ISRC. Calls `identify_key_from_metadata`,
    which prioritizes isrc over other song ids

    Returns:
        Track's store key or None if could not identify key.
    """
    try:
        audio = MutagenFile(path, easy=False)
    except Exception:
        return None
    if audio is None or audio.tags is None:
        return None

    if "WOAS" in audio.tags:
        url = audio.tags["WOAS"].url
        m = re.search(r"/track/([A-Za-z0-9]+)", url)
        track_id = m.group(1) if m else None
        if track_id:
            return identify_key_from_metadata({"song_id": track_id}, source_type)

    if "TSRC" in audio.tags:
        v = audio.tags["TSRC"]
        isrc = str(v.text[0] if hasattr(v, "text") else v)
        if isrc:
            return identify_key_from_metadata({"isrc": isrc}, source_type)
        
    return None


def track_already_stored(key: str) -> str | None:
    """Look up a track already in master store. Returns its path or None if not in store."""
    if key is None:
        return None
    for ext in AUDIO_EXTENSIONS:
        p = path_for_key(key, ext)
        if os.path.isfile(p):
            return p
    return None


def store_track(downloaded_path: str, key: str) -> str | None:
    """
    Adds downloaded track into the master store if key is new.
    Returns the track's path in master store
    """
    ext = os.path.splitext(downloaded_path)[1].lower()
    store_path = path_for_key(key, ext)
    if os.path.exists(store_path):
        return store_path
        
    os.makedirs(os.path.dirname(store_path), exist_ok=True)
    os.replace(downloaded_path, store_path)
    return store_path


def link_into_user_dir(store_path, user_dir, filename):
    """
    Hardlink file at store_path into user_dir 
    Returns path of linked file in user_dir
    """
    dest_path = os.path.join(user_dir, filename)

    if os.path.exists(dest_path):
        try:
            if os.path.samefile(dest_path, store_path):
                return dest_path
        except OSError:
            pass
        os.remove(dest_path)

    os.makedirs(user_dir, exist_ok=True)
    os.link(store_path, dest_path)
    return dest_path


def write_m3u8(user_dir: str, playlist_name: str, filenames: set[str]) -> str:
    path = os.path.join(user_dir, f"{playlist_name}.m3u8")
    
    with open(path, "w") as f:
        f.write("#EXTM3U\n")
        for fname in filenames:
            f.write(f"{fname}\n")

    return path