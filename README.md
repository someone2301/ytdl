# VideoToMP3

Command-line tool that downloads the audio from YouTube or other video links
and saves it as MP3. Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp), so it
works with YouTube and the many other sites yt-dlp supports (Vimeo, SoundCloud,
Dailymotion, Bandcamp, Twitch clips, and pages with common embedded players).

## Requirements

- Python 3.9+
- `pip install -r requirements.txt`

ffmpeg is used from your PATH if installed. If not, the `static-ffmpeg`
package downloads a bundled copy on first run, so Homebrew is not needed
(useful on Intel Macs, which Homebrew no longer installs on).

### macOS quick setup

```sh
xcode-select --install          # provides python3
cd VideoToMP3
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```sh
# Single link
python3 video_to_mp3.py "https://www.youtube.com/watch?v=VIDEO_ID"

# Several links, 320 kbps, custom folder
python3 video_to_mp3.py URL1 URL2 -q 320 -o ~/Music/rips

# Links from a text file (one per line, # for comments)
python3 video_to_mp3.py -f links.txt

# Whole playlist
python3 video_to_mp3.py -p "https://www.youtube.com/playlist?list=LIST_ID"

# No arguments: prompts for a URL
python3 video_to_mp3.py
```

Options:

| Flag | Meaning |
|------|---------|
| `-o, --output DIR` | Output folder (default `./mp3_downloads`) |
| `-q, --quality N` | Bitrate: 96, 128, 160, 192, 256, 320 (default 192) |
| `-p, --playlist` | Download every item when the URL is a playlist |
| `-f, --file PATH` | Read URLs from a file |
| `--no-art` | Skip embedding the thumbnail as cover art |

Output files are named `Title [videoID].mp3` and include title/artist tags
and cover art.

## Troubleshooting

Sites change often. If downloads start failing, update yt-dlp first:
`pip install -U yt-dlp`.

## Legal

Only download content you own or have permission to download. YouTube's
Terms of Service prohibit downloading except where YouTube provides a
download option.

## Flask web app on Debian or Ubuntu

The web app in `web/` supports MP3 and WAV output. It listens only on
`127.0.0.1:8000`; use a Cloudflare Tunnel to publish it, not a direct
public port. Only download content you own or are authorized to use.

### Install the app

Install the system packages:

    sudo apt update
    sudo apt install -y git python3 python3-venv python3-pip ffmpeg

After this repository has been pushed to GitHub, clone it:

    sudo git clone https://github.com/someone2301/ytdl.git /opt/VideoToMP3

Create a dedicated service account and output directory:

    sudo useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin videotomp3
    sudo install -d -o videotomp3 -g videotomp3 /var/lib/videotomp3/jobs

Install the Python dependencies and prepare the settings file:

    sudo python3 -m venv /opt/VideoToMP3/.venv
    sudo /opt/VideoToMP3/.venv/bin/pip install -r /opt/VideoToMP3/requirements.txt
    sudo install -m 600 /opt/VideoToMP3/deploy/videotomp3.env.example /etc/videotomp3.env
    sudoedit /etc/videotomp3.env

Replace both placeholder values in `/etc/videotomp3.env` with long random
values. Keep the output-directory and cookie settings as shown. Do not
commit this server settings file.

Install and start the service:

    sudo install -m 644 /opt/VideoToMP3/deploy/videotomp3.service /etc/systemd/system/videotomp3.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now videotomp3
    sudo systemctl status videotomp3
    curl -I http://127.0.0.1:8000

The local check should return an HTTP response. Configure the Cloudflare
Tunnel to send `mp3.eddyrock.stream` to `http://127.0.0.1:8000`; keep the
tunnel credentials on the server, not in this repository.
