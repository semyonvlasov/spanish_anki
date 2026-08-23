"""Mirror the local image cache into a Google Drive folder.

Only the manifest (``data/images.json``) is meant to live in git; the JPEGs
themselves sit in Drive and are pulled back on demand at build time.

Auth uses a service account: put its JSON key in ``GDRIVE_SERVICE_ACCOUNT_JSON``
(the literal JSON, or a path to it) and share the destination folder with the
service account's e-mail as Editor, otherwise it cannot write into My Drive.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/drive"]


def build_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    raw = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise SystemExit("GDRIVE_SERVICE_ACCOUNT_JSON is not set")
    info = json.loads(Path(raw).read_text()) if raw.startswith("/") else json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def ensure_folder(service, name: str, parent_id: str | None) -> str:
    query = [
        "mimeType = 'application/vnd.google-apps.folder'",
        "trashed = false",
        f"name = '{name}'",
    ]
    if parent_id:
        query.append(f"'{parent_id}' in parents")
    found = (
        service.files()
        .list(q=" and ".join(query), fields="files(id,name)", pageSize=1,
              supportsAllDrives=True, includeItemsFromAllDrives=True)
        .execute()
        .get("files", [])
    )
    if found:
        return found[0]["id"]
    metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = service.files().create(body=metadata, fields="id", supportsAllDrives=True).execute()
    return folder["id"]


def existing_files(service, folder_id: str) -> dict[str, str]:
    """Map filename -> file id for everything already in the folder."""
    out: dict[str, str] = {}
    token = None
    while True:
        resp = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id,name)",
                pageSize=1000,
                pageToken=token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        for item in resp.get("files", []):
            out[item["name"]] = item["id"]
        token = resp.get("nextPageToken")
        if not token:
            return out


def download_url(file_id: str) -> str:
    return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, default=Path("data/images.json"))
    ap.add_argument("--images-dir", type=Path, default=Path("build/images"))
    ap.add_argument("--folder-name", default="anki_spanish_images")
    ap.add_argument("--parent-id", default=os.environ.get("GDRIVE_PARENT_ID") or None)
    ap.add_argument("--share", action="store_true",
                    help="make each uploaded file readable by anyone with the link")
    args = ap.parse_args()

    from googleapiclient.http import MediaFileUpload

    manifest_doc = json.loads(args.manifest.read_text(encoding="utf-8"))
    images = manifest_doc.get("images", {})

    service = build_service()
    folder_id = ensure_folder(service, args.folder_name, args.parent_id)
    print(f"Drive folder {args.folder_name} -> {folder_id}")

    remote = existing_files(service, folder_id)
    print(f"{len(remote)} files already in the folder")

    uploaded = 0
    for guid, entry in images.items():
        local = args.images_dir / entry["file"]
        file_id = remote.get(entry["file"])

        if file_id is None:
            if not local.exists():
                continue
            file_id = (
                service.files()
                .create(
                    body={"name": entry["file"], "parents": [folder_id]},
                    media_body=MediaFileUpload(str(local), mimetype="image/jpeg", resumable=False),
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()["id"]
            )
            remote[entry["file"]] = file_id
            uploaded += 1
            if args.share:
                try:
                    service.permissions().create(
                        fileId=file_id,
                        body={"role": "reader", "type": "anyone"},
                        supportsAllDrives=True,
                    ).execute()
                except Exception as exc:
                    print(f"  could not share {entry['file']}: {exc}", file=sys.stderr)
            if uploaded % 50 == 0:
                print(f"  uploaded {uploaded}")

        entry["drive_id"] = file_id
        entry["download_url"] = download_url(file_id)

    manifest_doc["images"] = images
    manifest_doc["drive_folder_id"] = folder_id
    args.manifest.write_text(
        json.dumps(manifest_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"uploaded {uploaded} new files; manifest now carries Drive links for {len(images)} images")


if __name__ == "__main__":
    main()
