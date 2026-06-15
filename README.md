# Geodo Facebook Draft Assistant

This repo now uses `reliable_runner.py` as the main Selenium runner for Facebook group discovery and human-reviewed comment drafting.

Important: this tool only drafts comments. It does not auto-submit comments, press Enter to post, or click Facebook's final send/post button. Draft tabs are left open so a human can review, edit, and decide whether to post.

Older runners such as `main.py`, `batch_runner.py`, `discover_posts.py`, `live_runner.py`, and `live_runner_v2.py` are experimental/deprecated. Keep them only as reference code; use `reliable_runner.py` for the current flow.

## Setup

1. Create and activate a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create `.env` from the example:

```bash
cp .env.example .env
```

4. Optional: add an OpenAI key to `.env`.

```bash
OPENAI_API_KEY=your_key_here
OPENAI_MODEL=gpt-4o-mini
```

If `OPENAI_API_KEY` is missing, the runner uses a local template generator. The local generator returns `SKIP` for posts that are not clearly relevant.

5. Add Facebook group URLs to `group_urls.csv` with a `group_url` header, and edit `keywords.txt` as needed.

## Run

Normal run:

```bash
python reliable_runner.py --groups group_urls.csv --keywords keywords.txt --max-drafts 5
```

Fast test run with short waits:

```bash
python reliable_runner.py --fast-test --max-drafts 1 --debug
```

Fast test still scans at least 15 scrolls per group by default. It only shortens wait times.

Demo mode with no OpenAI key:

```bash
OPENAI_API_KEY= python reliable_runner.py --fast-test --max-drafts 1
```

Chrome uses the local `chrome_data/` profile folder so you can log into Facebook manually once and reuse that session. The runner starts with one scanner tab, reuses it for group browsing, and only leaves a review tab open after a draft was successfully typed.

For demos, keep `--debug` on so you can see candidate snippets, relevance scores, matched keywords, composer detection, and draft output in Terminal. The runner creates at most one draft per group, leaves that drafted tab open for review, then moves on.

## State Files

The runner creates and maintains:

- `state/seen_posts.json`: stable post fingerprints that have already been drafted, skipped, or found not commentable.
- `state/group_status.json`: last checked timestamp and status per group.
- `state/run_log.csv`: group-level and post-level scan events.
- `state/draft_queue.csv`: drafts that were typed or skipped by the generator.

Group statuses are:

- `ok`
- `private_or_join_required`
- `inactive_no_recent_posts`
- `not_commentable`
- `no_matches`
- `drafted`
- `error`

By default, the runner makes one pass through `group_urls.csv` and does not repeat duplicate group URLs in the same run. It also skips groups previously marked as private, inactive, not commentable, or no-match so it does not keep reopening bad group URLs. Pass `--repeat` to ignore those skips for a run.

Reset post and group state:

```bash
python reliable_runner.py --reset-state
```

That command only clears `state/seen_posts.json` and `state/group_status.json`, then exits.

## Useful Options

```bash
python reliable_runner.py \
  --groups group_urls.csv \
  --keywords keywords.txt \
  --max-drafts 5 \
  --max-scrolls-per-group 60 \
  --min-scrolls-per-group 15 \
  --empty-scroll-limit 15 \
  --cooldown-min 120 \
  --cooldown-max 180 \
  --max-open-draft-tabs 5
```

Use `--no-close-skipped-tabs` only when debugging failed review tabs. The default is to close tabs where a draft was not typed.
