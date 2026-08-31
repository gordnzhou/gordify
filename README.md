# Gordify
Download songs/playlists from Youtube or Spotify to your local library using [yt-dlp](https://github.com/yt-dlp/yt-dlp), and stream them locally using [Navidrome](https://www.navidrome.org/). No subscriptions needed. Can host multiple users, each with their own personal, isolated music library.


## Requirements
- Machine running Linux with Docker, Python 3 installed
- Create Spotify App at: https://developer.spotify.com/dashboard (for Spotify Client ID and Secret)


## Setup
1. Run `pip install -r requirements.txt`
1. Create `.env`. The `.env` requires Navidrome admin credentials, session secret, and Spotify Client ID and Secret.
1. Create `spotdl/config.json`. Include Spotify Client ID and Secret in JSON.
1. Customize network config `compose.yml`. Change or remove `ND_BASEURL`
1. Start all services with `docker compose up -d --build`
    - test by going to both: `localhost:4533` `localhost:8080` and logging in with your admin credentials


## Managing Users
`scripts/manage_users.py` contains commands for managing Navidrome users including adding/deleting users, resetting passwords, managing user folders. 
For more details, run 
```bash
python3 scripts/manage_users.py -h
```


## Tests
Run basic unit tests using `pytest`. Some tests require write access to `HOST_MUSIC_ROOT` so make sure the local user running Pytest has access. Or, simply run `sudo pytest`. 
Make sure `localhost:8080` and `NAVIDROME_URL_LOCAL` are reachable and running.


## Website and Subsonic API
- `gordify-navidrome:4533` exposes Navidrome web interface and Subsonic API (for client applications like Arpeggi, NaviBeat to connect to)
- `gordify-web:8080` exposes song downloader website

**Example caddyfile:**
```
# exposes song downloader at: example.com
# and exposes Subsonic API and Navidrome web client at:  example.com/nd 
example.com {
    # requires ND_BASEURL to be set to "/nd" in Navidrome container
    @navidrome path /nd /nd/*
    
    handle @navidrome {
        reverse_proxy gordify-navidrome:4533
    }

    reverse_proxy gordify-web:8080
}
```