# Geodo Batch Comment Assistant

Add these files to the GitHub repo root.

## What it does

- Reads multiple Facebook post URLs from `post_urls.csv`
- Opens each post in Chrome
- Generates a Geodo-relevant comment using OpenAI if you set `OPENAI_API_KEY`
- Falls back to built-in Geodo comment templates if no API key exists
- Types the comment into the comment box
- Leaves it as a draft by default so you can manually review before posting
- Logs results to `logs/comments_log.csv`

## Install

```bash
pip install -r requirements-geodo.txt
```

## Run

Generate comments only:

```bash
python batch_runner.py --csv post_urls.csv --dry-run
```

Type comments into Facebook but leave them as drafts:

```bash
python batch_runner.py --csv post_urls.csv --max-comments 5
```

Type comments and ask for explicit POST confirmation before submitting:

```bash
python batch_runner.py --csv post_urls.csv --max-comments 5 --submit-after-approval
```

## CSV format

```csv
post_url,context,status
https://www.facebook.com/groups/example/posts/123456789/,"Post asking about B2B lead gen",pending
```

Use Facebook post URLs your logged-in account can view.
