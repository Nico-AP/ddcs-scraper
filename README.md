# ddcs-scraper

External scraping worker for [ddcs-app](https://github.com/Nico-AP/ddcs-app).

ddcs-app keeps a queue of TikTok videos that are missing metadata. This worker
**claims** videos from that queue, loads their public page on tiktok.com, and
**posts** what it found back to the app, which stores it. It needs no database
and no access to the app's server other than its API, so it can run on any
machine with Python and an internet connection. Several instances can run at
the same time; the app makes sure no two get the same video.

What is scraped, how the queue is ordered and what the app stores is described
in ddcs-app's `docs/8_scraper.md`. This README covers running the worker.


## Setup

Requires Python 3.12 or newer.

**On the ddcs-app side** (once per scraper, in the Django admin):

1. Create a user for the scraper (no staff status, no usable password).
2. Give it the permission **"Can claim scrape targets and submit scraped
   results"**.
3. Create a token for the user (Auth Token → Tokens).

**On the machine that scrapes:**

```bash
git clone https://github.com/Nico-AP/ddcs-scraper.git
```

```bash
cd ddcs-scraper && cp .env.example .env
```

Enter the app's URL and the token in `.env`, then install with
[uv](https://docs.astral.sh/uv/):

```bash
uv sync --no-dev
```

Without uv: `python -m venv .venv`, activate it, `pip install .`, and leave out
`uv run` in the commands below.


## Running

Start with a small test. It claims ten videos, scrapes them, and exits:

```bash
uv run ddcs-scraper --max-videos 10
```

Then let it run:

```bash
uv run ddcs-scraper
```

| Option           | Effect                                                                 |
|------------------|------------------------------------------------------------------------|
| `--once`         | Do a single round (one claim) and exit                                 |
| `--max-videos N` | Exit after N videos were claimed, or when the queue is empty           |

The worker runs until it is stopped. **Ctrl+C** (or `SIGTERM`) makes it finish
the video it is on, submit what it has and give the remaining claimed videos
back to the queue. A second Ctrl+C quits at once; the videos it still holds
then return to the queue when their lease runs out.

To keep it running on a server, use whatever the machine offers (systemd,
`tmux`, a container). A minimal systemd unit:

```ini
[Unit]
Description=ddcs-scraper
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/ddcs-scraper
ExecStart=/opt/ddcs-scraper/.venv/bin/ddcs-scraper
Restart=on-failure
RestartSec=300

[Install]
WantedBy=multi-user.target
```

Exit codes: `0` stopped normally, `1` the round was aborted (only with `--once`
or `--max-videos`), `2` configuration error or the server rejected a request.


## Configuration

Read from the environment or from a `.env` file in the working directory.
Variables set in the environment take precedence.

| Variable                 | Default   | Effect                                                                                                    |
|--------------------------|-----------|-----------------------------------------------------------------------------------------------------------|
| `DDCS_API_URL`           | required  | Base URL of the ddcs-app instance, e.g. `https://ddcs.example.org`                                        |
| `DDCS_API_TOKEN`         | required  | Token of the scraper's account                                                                            |
| `DDCS_SCRAPER_ID`        | host name | Name of this instance. Stored with the videos it claims and shown in the app's admin.                     |
| `DDCS_CLAIM_LIMIT`       | `100`     | Videos claimed per round, 1 to 500                                                                        |
| `DDCS_SUBMIT_BATCH_SIZE` | `10`      | Results sent per request, 1 to 50                                                                         |
| `DDCS_IDLE_SECONDS`      | `300`     | Wait before asking again when the queue is empty or the server cannot be reached                          |
| `TIKTOK_RATE_DELAY`      | `1.0`     | Minimum seconds between the starts of two video-page requests                                             |
| `TIKTOK_RATE_JITTER`     | `0.3`     | Random variation of that interval as a fraction (0.3 = ±30%); `0` switches it off                         |
| `TIKTOK_CAPTION_DELAY`   | `0.0`     | Seconds to wait before each caption download                                                              |
| `TIKTOK_FETCH_CAPTIONS`  | `True`    | Also download each video's original-language caption. If off, videos are stored without one for good.     |
| `LOG_LEVEL`              | `INFO`    |                                                                                                           |

The server reserves claimed videos for a limited time (60 minutes by default).
`DDCS_CLAIM_LIMIT` should be what this instance scrapes well within that time;
at the default pace 100 videos take about two minutes. The worker stops
starting new videos one minute before the lease ends and gives the rest back.

The pace applies per instance. Instances behind the same IP address add up from
TikTok's point of view.


## How it works

One **round**:

1. Claim up to `DDCS_CLAIM_LIMIT` videos.
2. For each video: load the page, read the video object embedded in it, and, if
   the video has an original-language caption, download that file.
3. Send results in batches of `DDCS_SUBMIT_BATCH_SIZE` while scraping.
4. Give back every claimed video that got no result.

Rounds follow each other without a gap while there is work.

| What happened                                                    | Reported as   | Effect in the app                              |
|------------------------------------------------------------------|---------------|------------------------------------------------|
| The page contained the video                                     | `success`     | Stored. The page's video object is sent as is. |
| TikTok reports the video as gone or private                      | `unavailable` | Not tried again                                |
| No data on the page, unexpected structure, or a network error    | `failed`      | Tried again later, a limited number of times   |
| TikTok refused the request (HTTP 403 or 429), or the round ended | `released`    | Back in the queue, no attempt counted          |

### When TikTok blocks

The rules are the ones ddcs-app's built-in scraper uses.

- A refused request is retried once with fresh cookies.
- A round is **aborted** after 3 refused videos in a row, or after 5 videos in
  a row that failed for another reason. The second rule also catches TikTok
  changing its page structure.
- The videos of the streak that caused the abort are released, not reported as
  failed, so a block does not use up their attempts. A failure is only reported
  as `failed` once a later video shows that pages are still being served.
- After an aborted round the worker pauses for 1 hour; each further aborted
  round in a row doubles the pause, up to 24 hours. A round in which TikTok
  answered for at least one video resets this. The pause is kept in memory:
  restarting the worker ends it.

An abort is logged as an error. If rounds keep aborting with
`consecutive_failures` while tiktok.com is reachable, the page structure has
probably changed and the parser (`src/ddcs_scraper/tiktok/parsers.py`) needs
fixing, here and in ddcs-app.

### When the app cannot be reached

Requests are retried for about two minutes. If that fails, the worker waits
`DDCS_IDLE_SECONDS` and starts a new round. Results it could not send are
dropped; their videos return to the queue when the lease runs out and are
scraped again by whoever claims them next. A rejected token, a missing
permission or a request the server considers malformed ends the worker.


## Development

```bash
uv sync
```

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run ruff format --check .
```

The tests run offline. The TikTok code in `src/ddcs_scraper/tiktok` is a copy
of the page-scraping part of `ddcs/metadata/scraper` in ddcs-app, reduced to
videos; fixes to the parser belong in both places.
