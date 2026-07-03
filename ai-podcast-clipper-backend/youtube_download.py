import os
import pathlib
import shutil
import uuid

import modal
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

# Deliberately kept in its own file/App with a lightweight image: main.py has
# heavy top-level imports (cv2, whisperx, ffmpegcv, ...) that only exist in
# the GPU image, and Modal re-imports the defining module inside every
# container -- so a function sharing main.py's module would crash-loop when
# started under this lightweight image. app.include() in main.py merges this
# App's functions into the deployed app under one `modal deploy`.
#
# Uses yt-dlp rather than pytubefix: pytubefix hit YouTube's bot detection
# (pytubefix.exceptions.BotDetection) on the assigned video. yt-dlp is
# updated far more frequently to counter YouTube's anti-bot changes and also
# merges video+audio itself, so no separate ffmpeg merge step is needed.

MAX_FILESIZE_BYTES = 500 * 1024 * 1024  # matches the frontend's file-upload cap


class YoutubeDownloadRequest(BaseModel):
    youtube_url: str
    s3_key: str


downloader_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install("yt-dlp", "boto3", "fastapi[standard]")
)

app = modal.App("ai-podcast-clipper-youtube-download", image=downloader_image)

auth_scheme = HTTPBearer()


@app.function(
    secrets=[modal.Secret.from_name("ai-podcast-clipper-secret")],
    timeout=900,
)
@modal.fastapi_endpoint(method="POST")
def download_youtube_video(request: YoutubeDownloadRequest, token: HTTPAuthorizationCredentials = Depends(auth_scheme)):
    import boto3
    import yt_dlp

    if token.credentials != os.environ["AUTH_TOKEN"]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Incorrect bearer token", headers={"WWW-Authenticate": "Bearer"})

    run_id = str(uuid.uuid4())
    base_dir = pathlib.Path("/tmp") / run_id
    base_dir.mkdir(parents=True, exist_ok=True)

    try:
        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "outtmpl": str(base_dir / "original.%(ext)s"),
            "merge_output_format": "mp4",
            "max_filesize": MAX_FILESIZE_BYTES,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }

        title = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(request.youtube_url, download=True)
                title = info.get("title") if info else None
        except yt_dlp.utils.DownloadError as e:
            raise HTTPException(
                status_code=422, detail=f"Failed to download YouTube video: {e}")

        merged_candidates = sorted(base_dir.glob("original.*"))
        if not merged_candidates:
            raise HTTPException(
                status_code=500, detail="Download completed but no output file was found")
        merged_path = merged_candidates[0]

        try:
            s3_client = boto3.client("s3")
            s3_client.upload_file(
                str(merged_path), os.environ["S3_BUCKET_NAME"], request.s3_key)
        except Exception as e:
            raise HTTPException(
                status_code=502, detail=f"Failed to upload video to S3: {e}")

        return {"success": True, "s3_key": request.s3_key, "title": title}
    finally:
        if base_dir.exists():
            shutil.rmtree(base_dir, ignore_errors=True)
