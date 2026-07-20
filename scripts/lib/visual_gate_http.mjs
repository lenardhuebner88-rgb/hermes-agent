const PREVIEW_VISIBILITY_PATH = "/api/agent-questions/visibility";
const LOAD_RESOURCE_405 = /^Failed to load resource: .*\b405\b.*Method Not Allowed/i;

function isKnownPreviewVisibility405(consoleError, httpFailure, gateOrigin) {
  if (!LOAD_RESOURCE_405.test(consoleError.text ?? "")) return false;
  if (httpFailure.status !== 405 || httpFailure.method !== "POST") return false;
  if (httpFailure.resourceType !== "fetch" || httpFailure.isNavigationRequest) return false;
  if (consoleError.location?.url !== httpFailure.url) return false;

  let requestUrl;
  let frameUrl;
  try {
    requestUrl = new URL(httpFailure.url);
    frameUrl = new URL(httpFailure.frameUrl);
  } catch {
    return false;
  }
  if (requestUrl.origin !== gateOrigin || frameUrl.origin !== gateOrigin) return false;
  if (requestUrl.pathname !== PREVIEW_VISIBILITY_PATH || requestUrl.search) return false;

  const timingDelta = Math.abs((consoleError.atMs ?? 0) - (httpFailure.atMs ?? 0));
  return timingDelta <= 2_000;
}

export function partitionConsoleErrors({ consoleErrors, httpFailures, gateUrl }) {
  const gateOrigin = new URL(gateUrl).origin;
  const tolerated = [];
  const blocking = [];

  for (const consoleError of consoleErrors) {
    const httpFailure = httpFailures.find((candidate) =>
      isKnownPreviewVisibility405(consoleError, candidate, gateOrigin),
    );
    if (httpFailure) tolerated.push({ consoleError, httpFailure });
    else blocking.push(consoleError);
  }

  return { blocking, tolerated };
}
