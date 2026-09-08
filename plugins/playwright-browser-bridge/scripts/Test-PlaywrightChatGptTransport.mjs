#!/usr/bin/env node
// SPDX-License-Identifier: MPL-2.0

import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const scriptsRoot = path.dirname(new URL(import.meta.url).pathname.replace(/^\/(?:([A-Za-z]:))/, "$1"));
const bootstrapScript = path.join(scriptsRoot, "playwright_chatgpt_bootstrap.mjs");
const collectorScript = path.join(scriptsRoot, "playwright_chatgpt_collect.mjs");
const deliveryScript = path.join(scriptsRoot, "playwright_chatgpt_deliver.mjs");
const testRoot = await fs.mkdtemp(path.join(os.tmpdir(), "post-office-playwright-transport-"));
const outputRoot = path.join(testRoot, "output");
await fs.mkdir(outputRoot);

const attachmentName = "CSX-REVIEW-TEST_RESULT.md";
const payload = Buffer.from("REVIEW RESULT\nReview ID: test\nVerdict: PASS\n", "utf8");
const expectedSha256 = crypto.createHash("sha256").update(payload).digest("hex");
const threadId = "11111111-2222-4333-8444-555555555555";
let authenticated = false;
let clickCount = 0;
let lastNavigation = null;
let deleteCount = 0;
let sessionCount = 0;
const deletedSessions = [];
let previewOpen = false;
let uploadMenuOpen = false;
let chooserOpen = false;
let uploadedPaths = [];
let sentText = null;
let draftText = null;
let hoveredTarget = null;

function textResult(text) {
  return { content: [{ type: "text", text }] };
}

const server = http.createServer(async (request, response) => {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : null;
  response.setHeader("Content-Type", "application/json");
  const suppliedSession = request.headers["mcp-session-id"];
  const responseSession = body?.method === "initialize"
    ? "transport-test-session-" + (++sessionCount)
    : suppliedSession;
  if (responseSession) response.setHeader("Mcp-Session-Id", responseSession);

  if (request.method === "DELETE") {
    deleteCount += 1;
    deletedSessions.push(String(suppliedSession ?? ""));
    response.end("{}");
    return;
  }
  if (body?.method === "initialize") {
    response.end(JSON.stringify({
      jsonrpc: "2.0",
      id: body.id,
      result: {
        protocolVersion: "2025-06-18",
        serverInfo: { name: "transport-test", version: "0.1.0" },
        capabilities: {},
      },
    }));
    return;
  }
  if (body?.method === "notifications/initialized") {
    response.end(JSON.stringify({ jsonrpc: "2.0", result: {} }));
    return;
  }
  if (body?.method !== "tools/call") {
    response.statusCode = 400;
    response.end(JSON.stringify({ error: "unsupported" }));
    return;
  }

  const name = body.params.name;
  const args = body.params.arguments ?? {};
  let result;
  if (name === "browser_navigate") {
    lastNavigation = args.url;
    result = textResult(authenticated
      ? "### Page URL: " + args.url + "\n- button \"Account menu\" [ref=e1]"
      : "### Page URL: " + args.url + "\n- button \"Log in\" [ref=e1]\n- button \"Sign up\" [ref=e2]");
  } else if (name === "browser_wait_for") {
    result = textResult("### Result\nWait complete");
  } else if (name === "browser_snapshot") {
    result = textResult(authenticated && lastNavigation === "https://chatgpt.com/c/" + threadId
      ? uploadMenuOpen
        ? "### Page snapshot\n- menuitem \"Upload from computer\" [ref=e11]"
        : "### Page snapshot\n- button \"Add files and more\" [ref=e10]\n- textbox \"Message ChatGPT\" [ref=e4]\n- button \"Send prompt\" [ref=e5]\n- button \"" + attachmentName + "\" [ref=e40]\n- button \"" + attachmentName + "\" [ref=e42]\n  - generic: Document" + (hoveredTarget === "e42" ? "\n- button \"Download file\" [ref=e41]" : "")
      : authenticated
        ? "### Page snapshot\n- button \"New chat\" [ref=e3]\n- textbox \"Message ChatGPT\" [ref=e4]"
        : "### Page snapshot\n- button \"Log in\" [ref=e1]\n- button \"Sign up\" [ref=e2]");
  } else if (name === "browser_tabs") {
    result = textResult("### Result\n- 0: (current) [ChatGPT](https://chatgpt.com/)");
  } else if (name === "browser_find") {
    const sought = String(args.text ?? "");
    if (sought === attachmentName) {
      result = textResult("- link \"" + attachmentName + "\" [ref=e42]");
    } else if (sought === "Download" && previewOpen) {
      result = textResult("- button \"Download\" [ref=e43]");
    } else if (sought === "REVIEW-CORRELATION") {
      result = textResult("- paragraph \"REVIEW-CORRELATION\" [ref=e41]");
    } else if (sentText?.includes(sought)) {
      result = textResult("- paragraph \"" + sought + "\" [ref=e60]");
    } else {
      result = textResult("### Result\nNo matches");
    }
  } else if (name === "browser_click") {
    if (args.target === "e5") {
      sentText = draftText;
      result = textResult("### Result\nMessage sent");
    } else if (args.target === "e10") {
      uploadMenuOpen = true;
      result = textResult("### Result\nUpload menu opened");
    } else if (args.target === "e11") {
      uploadMenuOpen = false;
      chooserOpen = true;
      result = textResult("### Result\nFile chooser opened");
    } else if (args.target === "e40" || args.target === "e41") {
      clickCount += 1;
      await fs.writeFile(path.join(outputRoot, "download-" + clickCount + ".md"), payload);
      result = textResult("### Result\nDownload started");
    } else if (args.target === "e42") {
      previewOpen = true;
      result = textResult("### Result\nPreview opened");
    } else {
      assert.equal(args.target, "e43");
      clickCount += 1;
      await fs.writeFile(path.join(outputRoot, "download-" + clickCount + ".md"), payload);
      result = textResult("### Result\nDownload started");
    }
  } else if (name === "browser_file_upload") {
    assert.equal(chooserOpen, true);
    chooserOpen = false;
    uploadedPaths = args.paths;
    result = textResult("### Result\nFiles uploaded");
  } else if (name === "browser_type") {
    assert.equal(args.target, "e4");
    assert.equal(args.submit, false);
    draftText = args.text;
    result = textResult("### Result\nText entered");
  } else if (name === "browser_hover") {
    hoveredTarget = args.target;
    result = textResult("### Result\nHover complete");
  } else if (name === "browser_press_key") {
    result = textResult("### Result\nKey pressed");
  } else {
    result = { isError: true, content: [{ type: "text", text: "unsupported tool" }] };
  }
  response.end(JSON.stringify({ jsonrpc: "2.0", id: body.id, result }));
});

await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const address = server.address();
const endpoint = "http://127.0.0.1:" + address.port + "/mcp";

async function run(script, args) {
  try {
    const result = await execFileAsync(process.execPath, [script, "--endpoint", endpoint, ...args], {
      windowsHide: true,
      timeout: 10_000,
    });
    return { code: 0, stdout: result.stdout, stderr: result.stderr };
  } catch (error) {
    return {
      code: Number.isInteger(error.code) ? error.code : 1,
      stdout: error.stdout ?? "",
      stderr: error.stderr ?? String(error),
    };
  }
}

try {
  const sessionStatePath = path.join(testRoot, "authentication-session.json");
  let result = await run(bootstrapScript, [
    "--timeout-ms", "5000",
    "--keep-open",
    "--session-state", sessionStatePath,
  ]);
  assert.equal(result.code, 0, result.stderr);
  const unauthenticatedBootstrap = JSON.parse(result.stdout);
  assert.equal(unauthenticatedBootstrap.state, "AUTHENTICATION_REQUIRED");
  assert.equal(unauthenticatedBootstrap.authenticationTabRetained, true);
  assert.equal(JSON.parse(await fs.readFile(sessionStatePath, "utf8")).sessionId, "transport-test-session-1");

  authenticated = true;
  result = await run(bootstrapScript, [
    "--timeout-ms", "5000",
    "--keep-open",
    "--session-state", sessionStatePath,
  ]);
  assert.equal(result.code, 0, result.stderr);
  assert.equal(JSON.parse(result.stdout).state, "AUTHENTICATED");
  await assert.rejects(fs.access(sessionStatePath));
  assert.equal(deleteCount, 2);
  assert.deepEqual(deletedSessions.sort(), [
    "transport-test-session-1",
    "transport-test-session-2",
  ]);

  await fs.writeFile(path.join(outputRoot, "stale.md"), payload);
  const collectionManifestPath = path.join(testRoot, "collection.json");
  const collectionManifest = {
    schemaVersion: 1,
    collectionId: "PWB-COLLECTION-TEST-0001",
    threadId,
    mailboxId: "TEST-MBX-0001",
    mailboxGeneration: 1,
    scopeKind: "BROWSER_SWEEP",
    scopeId: "BROWSER-SWEEP-TEST-0001",
    sourceTurnId: "TURN-TEST-0001",
    attachmentReference: "chatgpt-attachment:TEST-0001",
    attachmentName,
    expectedBytes: payload.length,
    expectedSha256,
    observedAt: "2026-09-08T12:00:00.000000Z",
    requiredText: ["REVIEW-CORRELATION"],
  };
  await fs.writeFile(collectionManifestPath, JSON.stringify(collectionManifest));
  result = await run(collectorScript, [
    "--timeout-ms", "5000",
    "--output-root", outputRoot,
    "--manifest", collectionManifestPath,
  ]);
  assert.equal(result.code, 0, result.stderr);
  const collected = JSON.parse(result.stdout);
  assert.equal(collected.sha256, expectedSha256);
  assert.equal(collected.bytes, payload.length);
  assert.equal(path.basename(collected.path), attachmentName);
  assert.match(collected.path, /[\\/]collections[\\/]PWB-COLLECTION-TEST-0001[\\/]/);
  assert.equal(collected.matchingControls, 2);
  assert.equal(collected.collectionId, collectionManifest.collectionId);
  assert.equal(
    collected.receiptReference,
    `playwright-chatgpt-collection:${collectionManifest.collectionId}:${threadId}:${expectedSha256}`,
  );
  assert.equal(lastNavigation, "https://chatgpt.com/c/" + threadId);

  collectionManifest.requiredText = ["MISSING-CORRELATION"];
  await fs.writeFile(collectionManifestPath, JSON.stringify(collectionManifest));
  result = await run(collectorScript, [
    "--timeout-ms", "1000",
    "--output-root", outputRoot,
    "--manifest", collectionManifestPath,
  ]);
  assert.notEqual(result.code, 0);
  assert.match(result.stderr, /Required correlation text was not found/);
  assert.equal(clickCount, 1);

  const deliveryPayloadPath = path.join(testRoot, attachmentName);
  await fs.writeFile(deliveryPayloadPath, payload);
  const deliveryManifestPath = path.join(testRoot, "delivery.json");
  const deliveryManifest = {
    schemaVersion: 1,
    dispatchId: "PWB-DELIVERY-TEST-0001",
    threadId,
    messageId: "MSG-TEST-0001",
    mailboxId: "TEST-MBX-0001",
    mailboxGeneration: 1,
    prompt: "Read the attached retained package and act on its envelope.",
    attachments: [{
      path: deliveryPayloadPath,
      sourceName: attachmentName,
      sizeBytes: payload.length,
      sha256: expectedSha256,
    }],
  };
  await fs.writeFile(deliveryManifestPath, JSON.stringify(deliveryManifest));

  result = await run(deliveryScript, ["--timeout-ms", "5000", "--manifest", deliveryManifestPath]);
  assert.equal(result.code, 0, result.stderr);
  const delivered = JSON.parse(result.stdout);
  assert.equal(delivered.replayed, false);
  assert.equal(delivered.dispatchId, deliveryManifest.dispatchId);
  assert.deepEqual(uploadedPaths, [deliveryPayloadPath]);
  assert.match(sentText, /^POST-OFFICE-PLAYWRIGHT-DISPATCH PWB-DELIVERY-TEST-0001\n\n/);

  uploadedPaths = [];
  result = await run(deliveryScript, ["--timeout-ms", "5000", "--manifest", deliveryManifestPath]);
  assert.equal(result.code, 0, result.stderr);
  assert.equal(JSON.parse(result.stdout).replayed, true);
  assert.deepEqual(uploadedPaths, []);

  deliveryManifest.attachments[0].sha256 = "0".repeat(64);
  await fs.writeFile(deliveryManifestPath, JSON.stringify(deliveryManifest));
  result = await run(deliveryScript, ["--timeout-ms", "5000", "--manifest", deliveryManifestPath]);
  assert.notEqual(result.code, 0);
  assert.match(result.stderr, /SHA-256 mismatch/);

  console.log(JSON.stringify({
    ok: true,
    authenticationBoundary: true,
    retainedAuthenticationSessionBounded: true,
    exactThreadNavigation: true,
    correlationRequired: true,
    staleFileRejected: true,
    exactHashVerified: true,
    collectionManifestBounded: true,
    collectionReceiptCorrelated: true,
    deliveryManifestBounded: true,
    deliveryMarkerIdempotent: true,
    deliveryAttachmentHashVerified: true,
  }));
} finally {
  await new Promise((resolve) => server.close(resolve));
  await fs.rm(testRoot, { recursive: true, force: true });
}
