#!/usr/bin/env node
// SPDX-License-Identifier: MPL-2.0

import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const scriptsRoot = path.dirname(new URL(import.meta.url).pathname.replace(/^\/(?:([A-Za-z]:))/, "$1"));
const activationScript = path.join(scriptsRoot, "playwright_chatgpt_review_activate_direct.mjs");
const testRoot = await fs.mkdtemp(path.join(os.tmpdir(), "post-office-review-activation-"));
const playwrightRoot = path.join(testRoot, "runtime");
const moduleRoot = path.join(playwrightRoot, "node_modules", "playwright-core");
await fs.mkdir(moduleRoot, { recursive: true });
await fs.writeFile(path.join(moduleRoot, "package.json"), JSON.stringify({ type: "module" }));
await fs.writeFile(path.join(moduleRoot, "index.mjs"), `
let markerVisible = process.env.PWB_TEST_REPLAY === "1";
const clickStalls = process.env.PWB_TEST_CLICK_STALL === "1";
const sourceMessageId = "12345678-1234-4234-8234-123456789abc";
class Locator {
  constructor(kind) { this.kind = kind; }
  filter() { return this; }
  first() { return this; }
  nth() { return this; }
  locator(selector) {
    if (selector === "xpath=ancestor::form[1]") return new Locator("form");
    if (this.kind === "form" && selector.includes('type="submit"')) return new Locator("send");
    return new Locator("none");
  }
  async count() {
    if (this.kind === "messages") return markerVisible ? 1 : 0;
    if (this.kind === "login") return 0;
    if (this.kind === "none") return 0;
    return 1;
  }
  async getAttribute(name) { return name === "data-message-id" ? sourceMessageId : null; }
  async waitFor() {}
  async fill() {}
  async setInputFiles() {}
  async evaluate() { return true; }
  async press(key) { if (key === "Enter") markerVisible = true; }
  async isVisible() { return this.kind !== "none"; }
  async isEditable() { return this.kind === "composer"; }
  async isEnabled() { return true; }
  async click() { if (!clickStalls) markerVisible = true; }
}
class Page {
  url() { return "https://chatgpt.com/c/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"; }
  async goto() {}
  locator(selector) {
    if (selector === "[data-message-id]") return new Locator("messages");
    if (selector.includes("send-button")) return new Locator("send");
    return new Locator("composer");
  }
  getByRole(role, options = {}) {
    if (role === "textbox") return new Locator("composer");
    if (role === "button" && options.name === "Log in") return new Locator("login");
    if (role === "button") return new Locator("send");
    return new Locator("none");
  }
  getByText() { return new Locator("attachment"); }
  async waitForTimeout() {}
}
const page = new Page();
export const chromium = {
  async connectOverCDP() {
    return {
      contexts() { return [{ pages() { return [page]; }, async newPage() { return page; } }]; },
      async close() {},
    };
  },
};
`);

const reviewId = "CSX-REVIEW-P2-3-TEST";
const dispatchId = "ARD-11111111-2222-4333-8444-555555555555";
const threadId = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const prompt = `AUTOMATIC CSX CODE REVIEW\n\nReview ID: ${reviewId}\nActivation Dispatch ID: ${dispatchId}\n`;
const packagePath = path.join(testRoot, "review-package.zip");
await fs.writeFile(packagePath, "review package");
const packageBytes = (await fs.stat(packagePath)).size;
const packageSha256 = crypto.createHash("sha256").update(await fs.readFile(packagePath)).digest("hex");
const manifest = {
  schemaVersion: 1,
  kind: "AUTOMATIC_REVIEW_ACTIVATION",
  activationId: "PWB-REVIEW-ACTIVATION-TEST",
  reviewId,
  dispatchId,
  threadId,
  mailboxId: "CSXAR-MBX-0001",
  mailboxGeneration: 1,
  prompt,
  promptSha256: crypto.createHash("sha256").update(prompt, "utf8").digest("hex"),
  attachments: [{
    path: packagePath,
    sourceName: path.basename(packagePath),
    sizeBytes: packageBytes,
    sha256: packageSha256,
  }],
  issuedAt: "2026-09-10T00:00:00Z",
};
const manifestPath = path.join(testRoot, "activation-manifest.json");
await fs.writeFile(manifestPath, JSON.stringify(manifest));

async function run(replay, clickStall = false) {
  try {
    const result = await execFileAsync(process.execPath, [activationScript,
      "--cdp-endpoint", "ws://127.0.0.1:9222/devtools/browser/test",
      "--playwright-root", playwrightRoot,
      "--manifest", manifestPath,
      "--timeout-ms", "5000",
    ], { env: { ...process.env, PWB_TEST_REPLAY: replay ? "1" : "0", PWB_TEST_CLICK_STALL: clickStall ? "1" : "0" }, timeout: 10_000 });
    return { code: 0, stdout: result.stdout, stderr: result.stderr };
  } catch (error) {
    return { code: Number.isInteger(error.code) ? error.code : 1,
      stdout: error.stdout ?? "", stderr: error.stderr ?? String(error) };
  }
}

try {
  let result = await run(false);
  assert.equal(result.code, 0, result.stderr);
  let receipt = JSON.parse(result.stdout);
  assert.equal(receipt.replayed, false);
  assert.equal(receipt.reviewId, reviewId);
  assert.equal(receipt.dispatchId, dispatchId);
  assert.equal(receipt.sourceMessageId, "12345678-1234-4234-8234-123456789abc");
  assert.equal(receipt.attachmentCount, 1);
  assert.equal(receipt.packageSha256, packageSha256);
  assert.equal(receipt.composerDiscovery, "accessible-textbox");
  assert.equal(receipt.sendDiscovery, "composer-form-submit");
  assert.equal(receipt.receiptReference,
    `playwright-chatgpt-review-activation:${reviewId}:${dispatchId}:${threadId}:12345678-1234-4234-8234-123456789abc`);

  result = await run(false, true);
  assert.equal(result.code, 0, result.stderr);
  receipt = JSON.parse(result.stdout);
  assert.match(receipt.sendDiscovery, /enter-fallback/);

  result = await run(true);
  assert.equal(result.code, 0, result.stderr);
  receipt = JSON.parse(result.stdout);
  assert.equal(receipt.replayed, true);

  manifest.promptSha256 = "0".repeat(64);
  await fs.writeFile(manifestPath, JSON.stringify(manifest));
  result = await run(false);
  assert.notEqual(result.code, 0);
  assert.match(result.stderr, /prompt SHA-256 mismatch/);

  console.log(JSON.stringify({
    ok: true,
    exactReviewBinding: true,
    exactReceipt: true,
    replaySuppressed: true,
    tamperedPromptRejected: true,
  }));
} finally {
  await fs.rm(testRoot, { recursive: true, force: true });
}
