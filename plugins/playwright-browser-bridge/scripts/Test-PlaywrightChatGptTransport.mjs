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
      ? "### Page snapshot\n- link \"" + attachmentName + "\" [ref=e44]\n- button \"" + attachmentName + "\" [ref=e42]\n  - generic: Document"
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
    } else {
      result = textResult("### Result\nNo matches");
    }
  } else if (name === "browser_click") {
    if (args.target === "e42") {
      previewOpen = true;
      result = textResult("### Result\nPreview opened");
    } else {
      assert.equal(args.target, "e43");
      clickCount += 1;
      await fs.writeFile(path.join(outputRoot, "download-" + clickCount + ".md"), payload);
      result = textResult("### Result\nDownload started");
    }
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
  result = await run(collectorScript, [
    "--timeout-ms", "5000",
    "--thread-id", threadId,
    "--attachment-name", attachmentName,
    "--expected-sha256", expectedSha256,
    "--expected-bytes", String(payload.length),
    "--output-root", outputRoot,
    "--required-text-json", JSON.stringify(["REVIEW-CORRELATION"]),
  ]);
  assert.equal(result.code, 0, result.stderr);
  const collected = JSON.parse(result.stdout);
  assert.equal(collected.sha256, expectedSha256);
  assert.equal(collected.bytes, payload.length);
  assert.equal(path.basename(collected.path), "download-1.md");
  assert.equal(collected.matchingControls, 2);
  assert.equal(lastNavigation, "https://chatgpt.com/c/" + threadId);

  result = await run(collectorScript, [
    "--timeout-ms", "1000",
    "--thread-id", threadId,
    "--attachment-name", attachmentName,
    "--expected-sha256", expectedSha256,
    "--expected-bytes", String(payload.length),
    "--output-root", outputRoot,
    "--required-text-json", JSON.stringify(["MISSING-CORRELATION"]),
  ]);
  assert.notEqual(result.code, 0);
  assert.match(result.stderr, /Required correlation text was not found/);
  assert.equal(clickCount, 1);

  console.log(JSON.stringify({
    ok: true,
    authenticationBoundary: true,
    retainedAuthenticationSessionBounded: true,
    exactThreadNavigation: true,
    correlationRequired: true,
    staleFileRejected: true,
    exactHashVerified: true,
  }));
} finally {
  await new Promise((resolve) => server.close(resolve));
  await fs.rm(testRoot, { recursive: true, force: true });
}
