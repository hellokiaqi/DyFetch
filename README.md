# DyFetch

[简体中文](README.zh-CN.md) | English

DyFetch downloads videos, image posts, user profiles, collections and music pages from Douyin.
It provides a command-line interface and a desktop GUI, and can extract frames from downloaded
videos at a fixed rate.

The main program is `downloader.py`; the GUI is `ui.py`.

## Scope

- Single video and image posts
- User profiles: all posts, liked posts, collections
- Profile links carrying `modal_id` or `vid` are treated as a single post
- Collection pages and music pages
- Short links of the form `v.douyin.com/...`

Background music, cover images, author avatars and the raw JSON payload can be saved next to the
media. Videos can be sliced into frames under `frames/`.

When an API call returns no data, DyFetch falls back to a browser and reads the page. Public posts
therefore stay downloadable with an expired session, at the cost of about six to eight seconds per
post.

## Requirements

Python 3.8 or newer.

```bash
pip install -r requirements.txt
```

Frame extraction uses the ffmpeg binary bundled with `imageio-ffmpeg`, so no separate installation is
needed. An existing system ffmpeg can be used instead.

The browser fallback requires Playwright:

```bash
pip install playwright
playwright install chromium
```

`cut.py` is a standalone frame exporter built on OpenCV:

```bash
pip install opencv-python tqdm
```

## Credentials

The Douyin web APIs generally require a signed-in session. There are two ways to provide one.

**Automatic**

```bash
python cookie_extractor.py
```

This writes `cookies.txt`, `cookies.pkl` and `storage_state.json`, which DyFetch reads
automatically.

**Manual**

Set `cookies` in the configuration file, either as a cookie string or as a mapping.
`config_simple.yml` uses `cookies: auto`, which reads local files first and falls back to the browser
when none are present.

## Usage

### Command line

Download a single post:

```bash
python downloader.py -c config_simple.yml -u "https://www.douyin.com/video/1234567890123456789"
```

Download every post on a profile:

```bash
python downloader.py -c config_simple.yml -u "https://www.douyin.com/user/MS4wLjABAAAA..."
```

If the profile link carries `modal_id` or `vid`, only that post is downloaded.

Download your own liked or collected posts:

```bash
python downloader.py -c config_simple.yml -u "https://www.douyin.com/user/self?...&showTab=like"
```

Change the output directory, or backfill frames for an existing tree:

```bash
python downloader.py -c config_simple.yml -u "https://www.douyin.com/user/SEC_USER_ID" --path "./Downloaded/"
python downloader.py -c config_simple.yml -u "https://www.douyin.com/user/SEC_USER_ID" --slice-backfill
```

| Option | Description |
| --- | --- |
| `-c, --config` | Configuration file. Defaults to `config.yml`, falling back to `config_simple.yml`. |
| `-u, --url` | One or more links. |
| `-p, --path` | Output directory, overriding `path` from the configuration. |
| `--auto-cookie` | Obtain cookies through Playwright. |
| `--cookie` | Pass a cookie string directly. |
| `--slice-backfill` / `--no-slice-backfill` | Enable or disable frame backfill before a run. |
| `--refresh-session` | Refresh and save the session without downloading anything. |

### Desktop GUI

```bash
python ui.py
```

![Interface](docs/ui-preview.png)

Links are queued and processed in sequence. The window offers a live log with level filtering,
keyword search and export, a settings page for frame rate, item limits, download mode, concurrency
and retries, and light and dark themes. `Ctrl+Enter` starts a run and `Esc` stops it.

Settings changed in the GUI are written to `ui_run_config.yml` and leave the original configuration
files untouched.

## Output layout

```
<path>/<author>/<published_at>_<description>_<aweme_id>/
├── clip_1.mp4               video segment of an image post, or a split of a video post
├── image_1.jpg              image of an image post
├── <folder name>.mp4        single video post
├── <folder name>_music.mp3  background music
├── <folder name>_cover.jpg  cover image
├── <folder name>_data.json  raw metadata
└── frames/                  extracted frames
    └── clip_01_000001.jpg
```

Folder names include the post ID, so different posts never share a directory. Existing files are
skipped, which makes repeated runs safe.

## Configuration

Common options in `config_simple.yml`:

```yaml
link:                     # link list, an alternative to -u
  - https://www.douyin.com/user/SEC_USER_ID

path: ./Downloaded/       # output directory

music: true               # save background music
cover: true               # save cover image
avatar: false             # save author avatar
json: true                # save raw JSON

slice_videos: true        # enable frame extraction
slice_fps: 10             # frames per second
slice_subdir: frames      # frame directory name
slice_backfill_on_run: false   # backfill frames before a run
auto_install_ffmpeg: true      # install ffmpeg automatically when missing
# ffmpeg_bin: ffmpeg           # or point at a specific binary

mode:                     # what to download from a profile
  - post                  # post / like / mix

number:                   # item limits, 0 means unlimited
  post: 0
  like: 0

thread: 5                 # concurrency
retry_times: 3            # retry count

cookies: auto             # auto / cookie string / cookie mapping

start_time: ""            # date filter, YYYY-MM-DD, both ends inclusive
end_time: ""
```

Browser fallback options:

```yaml
user_posts_browser_recovery: true    # use a browser when profile paging is incomplete
user_posts_browser_headless: false   # run that browser headless

single_aweme_browser_recovery: true  # use a browser when a single post returns no data
single_aweme_browser_headless: true  # headless by default, which is faster
```

## Session state and renewal

Once a session expires, the API returns HTTP 200 with an empty body, which is easily mistaken for a
bug. DyFetch parses `sid_guard` at startup and reports the expiry. The program prints its own
messages in Chinese:

```
⚠ Cookie 登录状态已于 2026-04-05 22:23 过期，请重新运行 cookie_extractor.py 登录
```

Every `Set-Cookie` value returned by the server is merged back into `cookies.txt`, `cookies.pkl` and
`storage_state.json`. Douyin extends the session while it is in use, so regular operation keeps it
alive without a new sign-in.

Startup output also prints the remaining validity, for example `登录态有效期至 2026-11-18（剩余 30
天）`, meaning the session is valid until 2026-11-18 with 30 days left. The GUI shows the same
information in its header. To renew explicitly:

```bash
python downloader.py -c config_simple.yml --refresh-session
```

Or use **Settings → Renew sign-in** in the GUI. That command downloads nothing; it reads the current
session from the browser and writes it back.

After a failed automatic refresh, DyFetch skips further attempts for six hours, recorded in
`.session_refresh_state.json`, so runs do not stall. Use `--refresh-session` to force a refresh after
signing in again.

A few situations still require a new sign-in: the session expired during a long period of
inactivity, the password was changed, the account was signed out on another device, or the platform
raised a risk control flag.

## Profile downloads

With `mode: post`, every post on the profile is downloaded. `number.post` sets a limit, and 0 means
unlimited.

With a valid session, paging goes through the API and a post usually takes under a second. With an
expired session, DyFetch opens a browser, collects post links screen by screen, then opens each post
page in turn. That is noticeably slower: on a profile holding 26 posts, a run with an expired session
finished 24 posts with no failures in roughly four minutes.

Liked posts and collections still depend on the API and need a valid session.

## Troubleshooting

### Zero downloads, or the API keeps returning 403

Check the expiry line printed at startup. If it reports a date in the past, run
`python cookie_extractor.py` again. If the session is valid, check that `number.post` was not set to
a non-zero value by accident, and whether the target content requires a signed-in account.

### A profile returns fewer posts than expected

DyFetch tries the paged API, the `iteminfo` fallback, `locate_item_id` lookup, and finally a browser
sweep. The first three need a valid session; the last does not, but is slower. Setting
`user_posts_browser_headless` to `false` makes the browser visible, which allows signing in by hand
when the profile is gated.

### No frames are produced

Confirm that `ffmpeg -version` runs, or set `ffmpeg_bin` explicitly. With `auto_install_ffmpeg`
enabled, DyFetch tries to install `imageio-ffmpeg` when ffmpeg is missing.

### Chinese text renders incorrectly in the GUI

The GUI and its subprocesses use UTF-8 throughout. If the system locale is unusual, set
`PYTHONUTF8=1` before starting.

## Project layout

```
DyFetch/
├── downloader.py          main program, contains the download logic
├── ui.py                  desktop GUI
├── cut.py                 standalone frame exporter (OpenCV, optional)
├── cookie_extractor.py    credential extraction
├── get_cookies_manual.py  helper for manual extraction
├── DouYinCommand.py       legacy CLI wrapper, kept for compatibility
├── config_simple.yml      everyday configuration
├── config.yml             full configuration example
├── config.example.yml     configuration template
├── apiproxy/              API wrappers and request strategies
├── utils/                 shared helpers such as logging
├── tests/                 regression scripts
└── dy-downloader/         earlier modular implementation, reference only
```

`dy-downloader/` is an earlier split-into-modules version. It has fewer features than
`downloader.py` and its own dependency set, and is kept for reference.

## Testing

The regression script runs in a temporary directory and leaves existing data and credentials
untouched:

```bash
python tests/regress_downloader.py            # offline, 18 checks
python tests/regress_downloader.py --network  # also exercises the live API
```

It covers link detection, ID extraction, file naming, date filtering, incremental downloads, database
access, frame extraction and backfill, and cookie renewal.

`dy-downloader/` ships its own pytest suite. Its dependencies must be installed separately, and the
tests must be run from that directory, otherwise `utils/` in the repository root shadows the package
of the same name:

```bash
cd dy-downloader
pip install -r requirements.txt
python -m pytest tests -q
```

## Generated files

The following are created at runtime, can be deleted and regenerated, and are listed in
`.gitignore`:

```
cookies.txt / cookies.pkl / storage_state.json        credentials
downloader.log / logs/ / ui_startup.log               logs
data.db                                               incremental download database
Downloaded/ / my_videos/ / my_videos_fix/             downloaded content
ui_history.json / ui_state.json / ui_run_config.yml   GUI state and settings
.session_refresh_state.json                           renewal attempt record
```

## Credits

DyFetch is derived from [jiji262/douyin-downloader](https://github.com/jiji262/douyin-downloader),
which is released under the MIT license. The `dy-downloader/` directory comes from that project's
modular implementation, and the API wrappers under `apiproxy/` follow the layout of its earlier
versions. `downloader.py` is a rewritten and extended version of the original download logic.

That upstream also bundles an X-Bogus implementation written by Evil0ctal, found in
`dy-downloader/utils/xbogus.py`, used under the Apache License 2.0. Its copyright header is retained
in the file. All third-party notices are collected in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

## License

MIT, see [LICENSE](LICENSE).

Third-party components and their license terms are listed in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). The full Apache License 2.0 text is available in
[licenses/Apache-2.0.txt](licenses/Apache-2.0.txt).
