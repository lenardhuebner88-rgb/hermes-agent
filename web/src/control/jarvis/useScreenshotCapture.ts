/** Capture exactly one screen-share frame and immediately release the stream. */

type VideoWithFrameCallback = HTMLVideoElement & {
  requestVideoFrameCallback?: (callback: () => void) => number;
};

function waitForLoadedData(video: HTMLVideoElement): Promise<void> {
  if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      video.removeEventListener("loadeddata", onLoadedData);
      video.removeEventListener("error", onError);
    };
    const onLoadedData = () => {
      cleanup();
      resolve();
    };
    const onError = () => {
      cleanup();
      reject(new Error("screen video could not be loaded"));
    };
    video.addEventListener("loadeddata", onLoadedData, { once: true });
    video.addEventListener("error", onError, { once: true });
  });
}

function encodeJpeg(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error("screen frame could not be encoded"));
    }, "image/jpeg", 0.85);
  });
}

export function isScreenCaptureCancelled(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    error.name === "NotAllowedError"
  );
}

export async function captureScreenFrame(): Promise<File> {
  let stream: MediaStream | null = null;
  const video = document.createElement("video") as VideoWithFrameCallback;
  try {
    stream = await navigator.mediaDevices.getDisplayMedia({ video: true });
    video.srcObject = stream;
    video.muted = true;
    video.playsInline = true;

    const firstFrame = video.requestVideoFrameCallback
      ? new Promise<void>((resolve) => video.requestVideoFrameCallback?.(() => resolve()))
      : waitForLoadedData(video);
    await video.play();
    await firstFrame;

    if (!video.videoWidth || !video.videoHeight) {
      throw new Error("screen frame has no dimensions");
    }
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("screen canvas unavailable");
    context.drawImage(video, 0, 0);

    const blob = await encodeJpeg(canvas);
    return new File([blob], "screenshot.jpg", { type: "image/jpeg" });
  } finally {
    video.pause();
    video.srcObject = null;
    stream?.getTracks().forEach((track) => track.stop());
  }
}
