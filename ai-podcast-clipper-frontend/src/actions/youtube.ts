"use server";

import { after } from "next/server";
import { revalidatePath } from "next/cache";
import { v4 as uuidv4 } from "uuid";
import { env } from "~/env";
import { inngest } from "~/inngest/client";
import { auth } from "~/server/auth";
import { db } from "~/server/db";

function extractYoutubeVideoId(url: string): string | null {
  try {
    const parsed = new URL(url);
    if (parsed.hostname === "youtu.be") {
      return parsed.pathname.slice(1) || null;
    }
    if (
      parsed.hostname === "www.youtube.com" ||
      parsed.hostname === "youtube.com" ||
      parsed.hostname === "m.youtube.com"
    ) {
      if (parsed.pathname === "/watch") {
        return parsed.searchParams.get("v");
      }
      if (parsed.pathname.startsWith("/shorts/")) {
        return parsed.pathname.split("/")[2] ?? null;
      }
    }
    return null;
  } catch {
    return null;
  }
}

export async function createYoutubeUploadJob(youtubeUrl: string): Promise<{
  success: boolean;
  uploadedFileId?: string;
  error?: string;
}> {
  const session = await auth();
  if (!session?.user?.id) throw new Error("Unauthorized");

  const videoId = extractYoutubeVideoId(youtubeUrl.trim());
  if (!videoId) {
    return { success: false, error: "That doesn't look like a valid YouTube URL." };
  }

  const s3Key = `${uuidv4()}/original.mp4`;

  const uploadedFile = await db.uploadedFile.create({
    data: {
      userId: session.user.id,
      s3Key,
      displayName: `youtube:${videoId}`,
      // No separate browser-upload step to gate on here (the whole download
      // happens server-side), so mark this submitted immediately so it shows
      // up in the dashboard queue right away with status "downloading".
      uploaded: true,
      status: "downloading",
      youtubeUrl,
      youtubeVideoId: videoId,
    },
    select: { id: true },
  });

  // Runs after the response is sent to the browser so the caller isn't stuck
  // waiting on a multi-minute Modal download inside one HTTP request.
  after(async () => {
    try {
      const response = await fetch(env.YOUTUBE_DOWNLOAD_ENDPOINT, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${env.PROCESS_VIDEO_ENDPOINT_AUTH}`,
        },
        body: JSON.stringify({ youtube_url: youtubeUrl, s3_key: s3Key }),
      });

      if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Modal download endpoint returned ${response.status}: ${detail}`);
      }

      await db.uploadedFile.update({
        where: { id: uploadedFile.id },
        data: { status: "queued" },
      });

      await inngest.send({
        name: "process-video-events",
        data: { uploadedFileId: uploadedFile.id, userId: session.user.id },
      });
    } catch (error) {
      console.error("YouTube download failed", error);
      await db.uploadedFile.update({
        where: { id: uploadedFile.id },
        data: { status: "download_failed" },
      });
    }
  });

  revalidatePath("/dashboard");

  return { success: true, uploadedFileId: uploadedFile.id };
}
