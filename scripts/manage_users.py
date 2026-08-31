#!/usr/bin/env python3
import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Any
import requests

from mutagen import File as MutagenFile

sys.path.append(str(Path(__file__).resolve().parent.parent))
from app.job_db import delete_jobs_by_temp_users
from dotenv import load_dotenv

load_dotenv()

NAVIDROME_URL         = os.environ.get("NAVIDROME_URL_LOCAL", "http://localhost:4533").rstrip("/")
ADMIN_USER            = os.environ.get("NAVIDROME_ADMIN_USER")
ADMIN_PASS            = os.environ.get("NAVIDROME_ADMIN_PASS")
HOST_MUSIC_ROOT       = Path(os.environ.get("HOST_MUSIC_ROOT"))
CONTAINER_MUSIC_ROOT  = os.environ.get("CONTAINER_MUSIC_ROOT")
MUSIC_USERLIBS_FOLDER = os.environ.get("MUSIC_USERLIBS_FOLDER", "userlibs")
MUSIC_STORE_FOLDER    = os.environ.get("MUSIC_STORE_FOLDER", "tracks")

MASTER_STORE_PATH = os.path.join(HOST_MUSIC_ROOT, MUSIC_STORE_FOLDER)
DEFAULT_PASSWORD = "default@123"
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".opus", ".ogg", ".wav"}


def login(username: str, password: str) -> str:
    r = requests.post(f"{NAVIDROME_URL}/auth/login", json={"username": username, "password": password})
    r.raise_for_status()
    return r.json()["token"]


def api(method: str, endpoint: str, token: str, **kwargs) -> Any:
    headers = kwargs.pop("headers", {})
    headers["X-ND-Authorization"] = f"Bearer {token}"
    r = requests.request(method, f"{NAVIDROME_URL}{endpoint}", headers=headers, **kwargs)
    if not r.ok:
        print(f"  -> {method} {endpoint} failed: {r.status_code} {r.text}", file=sys.stderr)
    r.raise_for_status()
    return r.json()


def get_user(token: str, username: str) -> dict[str, Any] | None:
    users = api("GET", "/api/user", token, params={ 
        "_where": f'userName="{username}"' 
    })

    return next((u for u in users if u.get("userName") == username), None)


def add_user(token: str, username: str, password: str=None, name: str=None, make_admin: bool=False) -> str | None:
    display_name = name or username
    password = password if password else DEFAULT_PASSWORD

    if get_user(token, username) is not None:
        raise RuntimeError(f"User named {username} already exists")

    print(f"Creating {"admin" if make_admin else "non-admin"} user '{username}'...")
    user = api("POST", "/api/user", token, json={
        "userName": username,
        "name": display_name,
        "password": password,
        "isAdmin": make_admin,
    })

    user_dir = None
    if not make_admin:
        user_dir = HOST_MUSIC_ROOT / MUSIC_USERLIBS_FOLDER / user["id"]
        library_path = os.path.join(CONTAINER_MUSIC_ROOT, MUSIC_USERLIBS_FOLDER, user["id"])
        user_dir.mkdir(parents=True, exist_ok=True)
        lib_resp = api(
            "POST", "/api/library", token,
            json={"name": f"{username}'s library", "path": library_path},
        )
        library_id = int(lib_resp["id"])
        print(f"Created folder '{user_dir}' and pointed user's library to this folder (library id: {library_id}).")

        print(f"Editing perms for '{username}'...")
        api("PUT", f"/api/user/{user["id"]}/library", token, json={"libraryIds": [library_id]})

    print(f"\nDone. new user created.\n  username: {username}\n  password: {password}")

    return user_dir

    
def reset_user_password(token: str, username: str):
    user = get_user(token, username)
    if user is None:
        raise RuntimeError(f"User named {username} does not exist")
    
    user["password"] = DEFAULT_PASSWORD
    api("PUT", f"/api/user/{user['id']}", token, json=user)

    print(f"reset {username}'s password to: {DEFAULT_PASSWORD}")


def delete_user(token: str, username: str):
    user = get_user(token, username)
    if user is None:
        raise RuntimeError(f"User named {username} does not exist")

    user_dir = HOST_MUSIC_ROOT / MUSIC_USERLIBS_FOLDER / user["id"]
    library_path = os.path.join(CONTAINER_MUSIC_ROOT, MUSIC_USERLIBS_FOLDER, user["id"])

    if not user["isAdmin"]:
        libraries = api("GET", "/api/library", token)
        library = next((
            lib for lib in libraries
            if lib.get("path") == library_path
            or lib.get("name") == f"{username}'s library"
        ), None)
        if library:
            library_id = int(library["id"])
            print(f"Deleting library '{library.get('name')}' (id: {library_id})...")
            api("DELETE", f"/api/library/{library_id}", token)
        else:
            print(f"Warning: no library associated with '{username}' not found in Navidrome.")

        resolved_user_dir = user_dir.resolve()
        resolved_music_root = HOST_MUSIC_ROOT.resolve()
        if resolved_user_dir == resolved_music_root:
            raise RuntimeError(f"Refusing to delete music root: {resolved_user_dir}")
        if resolved_music_root not in resolved_user_dir.parents:
            raise RuntimeError(f"Refusing to delete path outside music root: {resolved_user_dir}")
        if user_dir.exists():
            print(f"Deleting music folder '{user_dir}'...")
            shutil.rmtree(user_dir)
        else:
            print(f"Skipping deleting '{user_dir}' as it does not exist.")

    print(f"Deleting Navidrome user '{username}'...")
    api("DELETE", f"/api/user/{user["id"]}", token)

    print(f"\nDone. User '{username}', their library and music folder have been deleted.")


def list_users(token: str, extra: bool = False):
    users = api("GET", "/api/user", token)
    libraries = api("GET", "/api/library", token)

    for user in users:
        username = user["userName"]
        user_id = user["id"]
        is_admin = user["isAdmin"]
        folder = HOST_MUSIC_ROOT / MUSIC_USERLIBS_FOLDER / user_id
        library_path = os.path.join(CONTAINER_MUSIC_ROOT, MUSIC_USERLIBS_FOLDER, user_id)

        print(f"{username} (ID: {user_id})", "ADMIN" if is_admin else "")
        library = next((lib for lib in libraries if lib.get("path") == library_path), None)
        if library:
            print(f"  Library Name: {library['name']}")
            print(f"  Library Folder: {folder}")
            if extra and folder.exists():
                size = 0
                audio_count = 0
                m3u8_count = 0
                for f in folder.rglob("*"):
                    if f.is_file():
                        size += f.stat().st_size
                        if f.suffix.lower() in AUDIO_EXTENSIONS:
                            audio_count += 1
                        elif f.suffix.lower() == ".m3u8":
                            m3u8_count += 1

                print(f"  Folder Size: {size / (1024 ** 3):.2f} GB")
                print(f"  Songs: {audio_count}")
                print(f"  Playlists: {m3u8_count}")
        else:
            if not is_admin:
                print("  WARN: user's associated library not found")


def find_audio_file_name(path: str) -> str | None:
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        return None
    if audio is None or audio.tags is None:
        return None

    if 'title' in audio:
        return audio['title'][0]

    return None


def prune_folder(token: str, output_path: str = None, username: str = None):
    prune_dir = MASTER_STORE_PATH
    other_dir = os.path.join(HOST_MUSIC_ROOT, MUSIC_USERLIBS_FOLDER)

    def audio_files_recursive(root):
        for dirpath, _, filenames in os.walk(root):
            for filename in filenames:
                if os.path.splitext(filename)[1].lower() in AUDIO_EXTENSIONS:
                    yield os.path.join(dirpath, filename)

    if username:
        user = get_user(token, username)
        if user is None:
            raise RuntimeError(f"User named {username} does not exist")
        prune_dir = HOST_MUSIC_ROOT / MUSIC_USERLIBS_FOLDER / user["id"]
        other_dir = MASTER_STORE_PATH

    other_inodes = { 
        os.stat(path).st_ino 
        for path in audio_files_recursive(other_dir)
    }

    pruned_paths = [
        path
        for path in audio_files_recursive(prune_dir)
        if os.stat(path).st_ino not in other_inodes
    ]

    if len(pruned_paths) == 0:
        print(f"No files to prune in {prune_dir}")
        return

    prune_lines = []
    for path in pruned_paths:
        name = find_audio_file_name(path)
        prune_lines.append(f"{path}{" - " + name if name else ""}")

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(prune_lines))
            f.write("\nIn Same Folder:\n")
        print(f"{len(pruned_paths)} files to prune in {prune_dir}, outputting list to: {output_path}")
    else:
        print(f"the following {len(pruned_paths)} files can be pruned in {prune_dir}:")
        for line in prune_lines:
            print(line)

    while True:
        response = input(f"Confirm pruning of {len(pruned_paths)} files with 'Y/YES': ").strip().lower()
        if response in ['yes', 'y']:
            break
        elif response in ['no', 'n']:
            return

    for p in pruned_paths:
        path = Path(p)
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            print(f"Permission denied: {path}, try running with 'sudo'")
        except Exception as e:
            print(f"Error deleting {path}: {e}")

    print("Complete")


def delete_temp_db_records(token: str):
    deleted = delete_jobs_by_temp_users()
    print(f"Deleted {deleted} rows")


def main():
    parser = argparse.ArgumentParser(description="Mange Navidrome users and music folders")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Create new Navidrome user")
    add_parser.add_argument("username", help="new user's login name; also used as their folder name")
    add_parser.add_argument("--password", help="user password (set to default password if left empty)")
    add_parser.add_argument("--name", help="display name (defaults to username)")
    add_parser.add_argument("--makeadmin", action="store_true", help="make admin user")

    rm_parser = subparsers.add_parser("rm", help="Remove an existing Navidrome user")
    rm_parser.add_argument("username", help="name of user to remove")

    list_parser = subparsers.add_parser("list", help="Show information of all Navidrome users")
    list_parser.add_argument("-e", "--extra", action="store_true", help="print extra info")

    prune_parser = subparsers.add_parser("prune", help="Prune unlinked audio tracks in master store and user libraries")
    prune_parser.add_argument("-o", "--output", help="Output to a txt file a list of filepaths to remove")
    prune_parser.add_argument("-u", "--username", help="Prune unlinked audio in a user's library")

    pwdreset_parser = subparsers.add_parser("pwdreset", help="Reset Navidrome user's password")
    pwdreset_parser.add_argument("username", help="name of user to reset password on")

    subparsers.add_parser("rmtemp", help="Remove job by temp users in DB")

    if not ADMIN_USER or not ADMIN_PASS:
        sys.exit("Set NAVIDROME_ADMIN_USER and NAVIDROME_ADMIN_PASS in your environment (or `source .env`).")

    token = login(ADMIN_USER, ADMIN_PASS)

    args = parser.parse_args()
    if args.command == "add":
        add_user(token, args.username, args.password, args.name, args.makeadmin)
    elif args.command == "rm":
        delete_user(token, args.username)
    elif args.command == "list":
        list_users(token, args.extra)
    elif args.command == "prune":
        prune_folder(token, args.output, args.username)
    elif args.command == "pwdreset":
        reset_user_password(token, args.username)
    elif args.command == "rmtemp":
        delete_temp_db_records(token)
    else:
        print("unknown command: ", args.command)
        exit(1)


if __name__ == "__main__":
    main()