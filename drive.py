"""Google Drive ingestion for Paige.

This module knows how to authenticate with Google Drive and mirror a Drive
folder's documents into a local directory. It has no knowledge of indexing or
LLMs: library.py calls sync_folder() and then indexes the mirrored files exactly
like any other local source. Google Docs are exported to Markdown; plain
text/markdown files are downloaded as-is; Google Sheets are exported to CSV.

Setup the user must do once:
  1. Create a Google Cloud project and enable the Google Drive API.
  2. Configure an OAuth consent screen and create an OAuth client of type
     "Desktop app".
  3. Download the client secret JSON and save it next to this file as
     credentials.json.
The first sync then opens a browser to authorize read-only Drive access and
caches the resulting token in token.json so later runs are non-interactive.
"""

from datetime import datetime
from pathlib import Path
import os
import re

# Read-only is all Paige needs to ingest; it never writes back to Drive.
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
CREDENTIALS_PATH = "credentials.json"
TOKEN_PATH = "token.json"

FOLDER_MIME = "application/vnd.google-apps.folder"

# Google-native types can't be downloaded directly; they must be exported. Each
# entry maps the native type to (export mime type, file extension to save under).
GOOGLE_EXPORTS = {
    "application/vnd.google-apps.document": ("text/markdown", ".md"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
}

# Characters Windows forbids in filenames, plus the path separators.
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# Raised for any Drive problem (missing credentials, auth failure, API error)
# so the command layer can show one friendly message instead of a stack trace.
class DriveError(Exception):
    pass


# Pulls the folder id out of a Drive URL, or returns the input unchanged if it
# already looks like a bare id. Accepts the common .../folders/<id> form.
def extract_folder_id(url_or_id):
    match = re.search(r"/folders/([A-Za-z0-9_-]+)", url_or_id)
    if match:
        return match.group(1)
    return url_or_id.strip()


# Makes a Drive file/folder name safe to use as a single path component, so a
# title with slashes or reserved characters can't escape the cache directory.
def safe_name(name):
    cleaned = _ILLEGAL_CHARS.sub("_", name).strip().rstrip(".")
    return cleaned or "untitled"


# Converts a Drive RFC-3339 modifiedTime (e.g. "2024-01-02T03:04:05.678Z") into a
# Unix timestamp so the mirrored file's mtime can mirror Drive's, letting the
# indexer's existing timestamp comparison skip unchanged documents.
def _parse_modified(modified_time):
    return datetime.fromisoformat(modified_time.replace("Z", "+00:00")).timestamp()


# Resolves the token file to use: a caller-supplied per-user path (the web app
# stores one per account) or the module default for single-user CLI use.
def _resolve_token_path(token_path):
    return Path(token_path) if token_path else Path(TOKEN_PATH)


# Returns True if a cached, still-valid (or refreshable) token already exists, so
# callers can report connection status without launching the browser flow.
def is_connected(token_path=None):
    token_file = _resolve_token_path(token_path)
    if not token_file.exists():
        return False
    try:
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
        return bool(creds and (creds.valid or creds.refresh_token))
    except Exception:
        return False


# Loads cached credentials, refreshing or running the OAuth browser flow as
# needed, and persists the resulting token for next time.
def _load_credentials(token_path=None):
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as e:
        raise DriveError(
            "Google API libraries are not installed. Run: pip install "
            "google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        ) from e

    token_file = _resolve_token_path(token_path)
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_file.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception:
            creds = None  # fall through to a fresh authorization

    if not Path(CREDENTIALS_PATH).exists():
        raise DriveError(
            f"No {CREDENTIALS_PATH} found. Download an OAuth 'Desktop app' client "
            f"secret from Google Cloud Console and save it as {CREDENTIALS_PATH}."
        )

    try:
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
        creds = flow.run_local_server(port=0)
    except Exception as e:
        raise DriveError(f"Google authorization failed: {e}") from e

    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    return creds


# Builds an authorized Drive API client, triggering authorization if necessary.
def _build_service(token_path=None):
    try:
        from googleapiclient.discovery import build
    except ImportError as e:
        raise DriveError(
            "Google API libraries are not installed. Run: pip install "
            "google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        ) from e
    return build("drive", "v3", credentials=_load_credentials(token_path),
                 cache_discovery=False)


# Forces the OAuth flow now (used by an explicit connect command) so the user can
# authorize up front rather than at first sync. Returns True on success.
def connect(token_path=None):
    _load_credentials(token_path)
    return True


# Lists the non-trashed children of a Drive folder, following pagination.
def _list_children(service, folder_id):
    children = []
    page_token = None
    query = f"'{folder_id}' in parents and trashed = false"
    while True:
        response = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
            pageSize=1000,
            pageToken=page_token,
        ).execute()
        children.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return children


# Returns the extension a Drive file will be saved under without downloading it, or
# None for types Paige doesn't ingest (Slides, drawings, etc.). Knowing the target
# name up front lets _mirror skip the download entirely for unchanged files.
def _target_ext(file_meta):
    mime = file_meta["mimeType"]
    if mime in GOOGLE_EXPORTS:
        return GOOGLE_EXPORTS[mime][1]
    if mime.startswith("application/vnd.google-apps"):
        return None
    return Path(file_meta["name"]).suffix


# Downloads one Drive file's bytes, exporting Google-native docs to text and
# downloading everything else directly.
def _download(service, file_meta):
    mime = file_meta["mimeType"]
    file_id = file_meta["id"]
    if mime in GOOGLE_EXPORTS:
        export_mime = GOOGLE_EXPORTS[mime][0]
        return service.files().export(fileId=file_id, mimeType=export_mime).execute()
    return service.files().get_media(fileId=file_id).execute()


# Recursively mirrors a Drive folder into dest_dir: writes each downloadable file,
# recurses into subfolders, and records every path it wrote into `kept`. Updates
# the running counts dict. Only re-downloads a file whose Drive modifiedTime
# differs from the local copy's mtime, so unchanged docs are left untouched.
def _mirror(service, folder_id, dest_dir, kept, counts):
    dest_dir.mkdir(parents=True, exist_ok=True)
    for child in _list_children(service, folder_id):
        if child["mimeType"] == FOLDER_MIME:
            sub = dest_dir / safe_name(child["name"])
            _mirror(service, child["id"], sub, kept, counts)
            continue

        remote_mtime = _parse_modified(child["modifiedTime"])
        # Resolve the on-disk name first (no download) so an unchanged file is
        # detected by mtime and skipped before any network transfer.
        ext = _target_ext(child)
        if ext is None:
            counts["skipped"] += 1
            continue

        stem = safe_name(Path(child["name"]).stem)
        target = dest_dir / f"{stem}{ext}"
        kept.add(target.resolve())

        if target.exists() and int(target.stat().st_mtime) == int(remote_mtime):
            counts["unchanged"] += 1
            continue

        existed = target.exists()
        data = _download(service, child)
        if isinstance(data, str):
            data = data.encode("utf-8")
        target.write_bytes(data)
        os.utime(target, (remote_mtime, remote_mtime))
        counts["updated" if existed else "added"] += 1


# Deletes files left in the mirror that no longer exist in Drive, then prunes any
# now-empty directories, so a doc removed in Drive disappears from the index too.
def _prune(dest_dir, kept):
    removed = 0
    for path in dest_dir.rglob("*"):
        if path.is_file() and path.resolve() not in kept:
            path.unlink()
            removed += 1
    for path in sorted(dest_dir.rglob("*"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    return removed


# Mirrors a whole Drive folder (recursively) into dest_dir and returns a summary
# of what changed. This is the single entry point library.py uses.
def sync_folder(folder_id, dest_dir, token_path=None):
    service = _build_service(token_path)
    dest_dir = Path(dest_dir)
    kept = set()
    counts = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    try:
        _mirror(service, folder_id, dest_dir, kept, counts)
    except DriveError:
        raise
    except Exception as e:
        raise DriveError(f"Drive sync failed: {e}") from e
    counts["removed"] = _prune(dest_dir, kept) if dest_dir.exists() else 0
    return counts


# Looks up a folder's display name for friendlier listings; falls back to the id.
def folder_name(folder_id, token_path=None):
    try:
        service = _build_service(token_path)
        meta = service.files().get(fileId=folder_id, fields="name").execute()
        return meta.get("name", folder_id)
    except Exception:
        return folder_id
