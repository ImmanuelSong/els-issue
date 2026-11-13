"""
Els Issue Uploader - upload one Short to the owner's own YouTube channel.

Complete working script (YouTube Data API v3, OAuth 2.0, resumable upload).

Setup:
    pip install -r requirements.txt
    Put the OAuth client file (Desktop app) next to this script as client_secret.json

Examples:
    # first run opens a browser for Google sign-in, then saves token.json
    python upload_video.py --file short.mp4 --title "오늘 이 키워드가 1위인 이유" \
        --description-file desc.txt --tags "엘스이슈,오늘의 이슈" --privacy private

    # scheduled publish (requires privacy=private; time in KST is converted to UTC)
    python upload_video.py --file short.mp4 --title "..." --publish-at "2026-11-14 12:30"

    # mark as realistic altered/synthetic content (AI disclosure)
    python upload_video.py --file short.mp4 --title "..." --synthetic
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

# Minimal scopes: upload videos + read own channel info (to confirm the target channel).
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

BASE_DIR = Path(__file__).resolve().parent
CLIENT_SECRET = BASE_DIR / "client_secret.json"
TOKEN_FILE = BASE_DIR / "token.json"

KST = timezone(timedelta(hours=9))
RETRIABLE_STATUS = {500, 502, 503, 504}
MAX_RETRIES = 8


def get_credentials() -> Credentials:
    """Load saved OAuth token, refresh it, or run the browser sign-in flow."""
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:  # refresh token revoked or expired
            print(f"[auth] token refresh failed ({exc}); signing in again")
            creds = None

    if not creds or not creds.valid:
        if not CLIENT_SECRET.exists():
            sys.exit(f"[auth] {CLIENT_SECRET.name} not found next to this script")
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")

    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def kst_to_rfc3339_utc(text: str) -> str:
    """'2026-11-14 12:30' (KST) -> '2026-11-14T03:30:00Z'."""
    local = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    if local <= datetime.now(KST) + timedelta(minutes=15):
        sys.exit("[args] --publish-at must be at least 15 minutes in the future")
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_body(args: argparse.Namespace) -> dict:
    description = ""
    if args.description_file:
        description = Path(args.description_file).read_text(encoding="utf-8")
    elif args.description:
        description = args.description

    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []

    status: dict = {
        "privacyStatus": args.privacy,
        "selfDeclaredMadeForKids": False,
    }
    if args.publish_at:
        # YouTube only accepts publishAt on private videos.
        status["privacyStatus"] = "private"
        status["publishAt"] = kst_to_rfc3339_utc(args.publish_at)
    if args.synthetic:
        # AI disclosure: realistic altered or synthetic content.
        status["containsSyntheticMedia"] = True

    return {
        "snippet": {
            "title": args.title[:100],          # YouTube title limit
            "description": description[:5000],  # YouTube description limit
            "tags": tags,
            "categoryId": args.category,
            "defaultLanguage": "ko",
            "defaultAudioLanguage": "ko",
        },
        "status": status,
    }


def resumable_upload(request) -> dict:
    """Upload in chunks with exponential backoff on transient errors."""
    response = None
    retry = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"[upload] {int(status.progress() * 100)}%")
        except HttpError as err:
            if err.resp.status in RETRIABLE_STATUS and retry < MAX_RETRIES:
                retry += 1
                wait = min(2 ** retry, 64)
                print(f"[upload] HTTP {err.resp.status}, retry {retry} in {wait}s")
                time.sleep(wait)
                continue
            raise
        except (ConnectionError, TimeoutError) as err:
            if retry < MAX_RETRIES:
                retry += 1
                wait = min(2 ** retry, 64)
                print(f"[upload] network error ({err}), retry {retry} in {wait}s")
                time.sleep(wait)
                continue
            raise
    return response


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload a Short to my own YouTube channel")
    parser.add_argument("--file", required=True, help="video file (mp4, 9:16)")
    parser.add_argument("--title", required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--description-file", help="UTF-8 text file with the description")
    parser.add_argument("--tags", default="", help="comma-separated tags")
    parser.add_argument("--category", default="25", help="category id (25 = News & Politics)")
    parser.add_argument("--privacy", default="private", choices=["private", "unlisted", "public"])
    parser.add_argument("--publish-at", help="scheduled publish time in KST, 'YYYY-MM-DD HH:MM'")
    parser.add_argument("--synthetic", action="store_true", help="AI disclosure flag")
    args = parser.parse_args()

    video_path = Path(args.file)
    if not video_path.exists():
        sys.exit(f"[args] file not found: {video_path}")

    youtube = build("youtube", "v3", credentials=get_credentials(), cache_discovery=False)

    # Confirm which channel we are uploading to.
    me = youtube.channels().list(part="snippet", mine=True).execute()
    if not me.get("items"):
        sys.exit("[auth] no YouTube channel found for this Google account")
    channel = me["items"][0]
    print(f"[channel] {channel['snippet']['title']} ({channel['id']})")

    body = build_body(args)
    media = MediaFileUpload(str(video_path), mimetype="video/*", chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    print(f"[upload] {video_path.name} -> privacy={body['status']['privacyStatus']}"
          + (f", publishAt={body['status']['publishAt']}" if "publishAt" in body["status"] else ""))
    response = resumable_upload(request)

    video_id = response["id"]
    print(f"[done] video id: {video_id}")
    print(f"[done] https://youtube.com/shorts/{video_id}")
    print(f"[done] Studio: https://studio.youtube.com/video/{video_id}/edit")


if __name__ == "__main__":
    main()
