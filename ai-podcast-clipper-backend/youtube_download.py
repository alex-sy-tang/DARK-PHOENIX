import os
import pathlib
import shutil
import subprocess
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


class YoutubeDownloadRequest(BaseModel):
    youtube_url: str
    s3_key: str


downloader_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install("pytubefix", "boto3", "fastapi[standard]")
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
    from pytubefix import YouTube

    if token.credentials != os.environ["AUTH_TOKEN"]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Incorrect bearer token", headers={"WWW-Authenticate": "Bearer"})

    run_id = str(uuid.uuid4())
    base_dir = pathlib.Path("/tmp") / run_id
    base_dir.mkdir(parents=True, exist_ok=True)

    try:
        try:
            yt = YouTube(request.youtube_url)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Invalid or unreachable YouTube URL: {e}")

        video_stream = yt.streams.filter(
            only_video=True, file_extension="mp4").order_by("resolution").desc().first()
        audio_stream = yt.streams.filter(
            only_audio=True, file_extension="mp4").order_by("abr").desc().first()

        if video_stream is None or audio_stream is None:
            raise HTTPException(
                status_code=422, detail="No downloadable video/audio stream found for this URL")

        video_path = video_stream.download(
            output_path=str(base_dir), filename="video.mp4")
        audio_path = audio_stream.download(
            output_path=str(base_dir), filename="audio.mp4")

        merged_path = base_dir / "original.mp4"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", video_path, "-i", audio_path,
                 "-c:v", "copy", "-c:a", "aac", str(merged_path)],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as e:
            raise HTTPException(
                status_code=500, detail=f"Failed to merge video/audio streams: {e.stderr}")

        try:
            s3_client = boto3.client("s3")
            s3_client.upload_file(
                str(merged_path), os.environ["S3_BUCKET_NAME"], request.s3_key)
        except Exception as e:
            raise HTTPException(
                status_code=502, detail=f"Failed to upload video to S3: {e}")

        return {"success": True, "s3_key": request.s3_key, "title": yt.title}
    finally:
        if base_dir.exists():
            shutil.rmtree(base_dir, ignore_errors=True)
