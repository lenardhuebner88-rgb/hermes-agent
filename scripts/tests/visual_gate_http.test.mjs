import assert from "node:assert/strict";
import test from "node:test";

import { partitionConsoleErrors } from "../lib/visual_gate_http.mjs";

const gateUrl = "http://127.0.0.1:9119/control/projekte";
const generic405 = {
  text: "Failed to load resource: the server responded with a status of 405 (Method Not Allowed)",
  location: { url: "http://127.0.0.1:9119/api/agent-questions/visibility", lineNumber: 0, columnNumber: 0 },
  atMs: 343,
};
const previewHeartbeat405 = {
  status: 405,
  method: "POST",
  url: "http://127.0.0.1:9119/api/agent-questions/visibility",
  resourceType: "fetch",
  isNavigationRequest: false,
  frameUrl: gateUrl,
  atMs: 343,
};

test("tolerates only the known same-origin preview visibility heartbeat 405", () => {
  const result = partitionConsoleErrors({
    consoleErrors: [generic405],
    httpFailures: [previewHeartbeat405],
    gateUrl,
  });

  assert.deepEqual(result.blocking, []);
  assert.deepEqual(result.tolerated, [{ consoleError: generic405, httpFailure: previewHeartbeat405 }]);
});

test("keeps document, bundle, and relevant API failures fail-closed", () => {
  const failures = [
    { ...previewHeartbeat405, resourceType: "document", isNavigationRequest: true },
    { ...previewHeartbeat405, url: "http://127.0.0.1:9119/assets/app.js", resourceType: "script" },
    { ...previewHeartbeat405, url: "http://127.0.0.1:9119/api/projects" },
    { ...previewHeartbeat405, status: 500 },
    { ...previewHeartbeat405, method: "GET" },
    { ...previewHeartbeat405, url: "http://example.test/api/agent-questions/visibility" },
  ];

  for (const httpFailure of failures) {
    const consoleError = { ...generic405, location: { ...generic405.location, url: httpFailure.url } };
    const result = partitionConsoleErrors({ consoleErrors: [consoleError], httpFailures: [httpFailure], gateUrl });
    assert.deepEqual(result.tolerated, [], JSON.stringify(httpFailure));
    assert.deepEqual(result.blocking, [consoleError], JSON.stringify(httpFailure));
  }
});

test("does not suppress an uncorrelated generic console 405", () => {
  const result = partitionConsoleErrors({ consoleErrors: [generic405], httpFailures: [], gateUrl });
  assert.deepEqual(result.tolerated, []);
  assert.deepEqual(result.blocking, [generic405]);
});
