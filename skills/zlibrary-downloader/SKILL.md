---
name: zlibrary-downloader
description: Search and download ebooks from Z-Library using a bundled unofficial Python client and local credential storage. Use when working with this specific Z-Library workflow: setting credentials, searching by title or author, filtering by extension such as epub or pdf, checking remaining download quota, and downloading the first matching result. Read references/authentication.md when credentials or cookie-based login details are needed.
---

# Z-Library Downloader

Use the bundled scripts from this skill directory. Do not rely on hardcoded absolute paths.

## Cautions

- Treat this integration as brittle: it depends on an unofficial API and site behavior may change.
- Respect local law, copyright, and the service's terms before using it.
- Keep credentials private. Prefer local config storage over echoing secrets back to the user.

## Files

- `scripts/zlib_config.py`: store, inspect, and clear credentials in `~/.config/zlib_downloader.json`
- `scripts/zlib_download.py`: preferred CLI for search + download with automatic token refresh
- `scripts/download_book.py`: legacy CLI using explicit `remix_userid` + `remix_userkey`
- `references/authentication.md`: how to obtain credentials

## Prerequisite

Install the Python dependency if needed:

```bash
pip3 install requests
```

## Preferred workflow

From this skill directory, configure credentials once:

```bash
python3 scripts/zlib_config.py --set-email "your@email.com" --set-password "your_password"
```

Then download a book:

```bash
python3 scripts/zlib_download.py "book title" --ext epub --output-dir ./downloads
```

Credential behavior:

- Try saved `remix_userid` + `remix_userkey` first
- Fall back to email/password if token auth fails
- Save refreshed token automatically after successful login

## Useful commands

Show current config with secrets masked:

```bash
python3 scripts/zlib_config.py --show
```

Set token directly:

```bash
python3 scripts/zlib_config.py --set-userid "xxx" --set-userkey "xxx"
```

Clear saved credentials:

```bash
python3 scripts/zlib_config.py --clear
```

Legacy explicit-token download:

```bash
python3 scripts/download_book.py \
  --userid "<remix_userid>" \
  --userkey "<remix_userkey>" \
  --query "book title" \
  --output-dir ./downloads
```

## Notes

- The scripts usually download the first matching result after printing several hits.
- Free accounts may have a daily download cap.
- Read `references/authentication.md` when the user needs help obtaining credentials from browser cookies or a login flow.
