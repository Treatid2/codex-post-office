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
const deliveryScript = path.join(scriptsRoot, "playwright_chatgpt_deliver_direct.mjs");
const testRoot = await fs.mkdtemp(path.join(os.tmpdir(), "post-office-direct-delivery-"));
const runtimeRoot = path.join(testRoot, "runtime");
const moduleRoot = path.join(runtimeRoot, "node_modules", "playwright-core");
await fs.mkdir(moduleRoot, { recursive: true });
await fs.writeFile(path.join(moduleRoot, "package.json"), JSON.stringify({ type: "module" }));
await fs.writeFile(path.join(moduleRoot, "index.mjs"), `
let markerVisible = false;
class Locator {
  constructor(kind) { this.kind = kind; }
  filter() { return this; }
  first() { return this; }
  nth() { return this; }
  or(other) { return this.kind === "none" ? other : this; }
  locator(selector) {
    if (selector === "xpath=ancestor::form[1]") return new Locator("form");
    if (this.kind === "form" && selector.includes('type="submit"')) return new Locator("send");
    return new Locator("none");
  }
  async count() {
    if (this.kind === "messages") return markerVisible ? 1 : 0;
    if (this.kind === "login" || this.kind === "none") return 0;
    return 1;
  }
  async waitFor() {}
  async setInputFiles() {}
  async fill() {}
  async evaluate() { return true; }
  async press(key) { if (key === "Enter") markerVisible = true; }
  async click() {}
  async isVisible() { return this.kind !== "none"; }
  async isEditable() { return this.kind === "composer"; }
  async isEnabled() { return true; }
}
class Page {
  url() { return "https://chatgpt.com/c/11111111-2222-4333-8444-555555555555"; }
  async goto() {}
  async waitForLoadState() {}
  async waitForTimeout() {}
  locator(selector) {
    if (selector === '[data-message-id]') return new Locator("messages");
    if (selector === 'input[type="file"]') return new Locator("file");
    if (selector.includes("send-button")) return new Locator("send");
    if (selector.includes("composer-plus-btn")) return new Locator("add");
    return new Locator("composer");
  }
  getByRole(role, options = {}) {
    if (role === "button" && options.name === "Log in") return new Locator("login");
    if (role === "textbox") return new Locator("composer");
    if (role === "button") return new Locator("send");
    return new Locator("none");
  }
  getByText() { return new Locator("attachment"); }
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

const attachment = path.join(testRoot, "payload.zip");
const bytes = Buffer.from("exact package bytes", "utf8");
await fs.writeFile(attachment, bytes);
const manifest = {
  schemaVersion: 1,
  dispatchId: "PON-DISPATCH-DIRECT-TEST",
  threadId: "11111111-2222-4333-8444-555555555555",
  messageId: "PON-MESSAGE-DIRECT-TEST",
  mailboxId: "TEST-MBX-0001",
  mailboxGeneration: 1,
  prompt: "Process this exact package.",
  attachments: [{
    path: attachment,
    sourceName: "payload.zip",
    sizeBytes: bytes.length,
    sha256: crypto.createHash("sha256").update(bytes).digest("hex"),
  }],
};
const manifestPath = path.join(testRoot, "manifest.json");
await fs.writeFile(manifestPath, JSON.stringify(manifest));

try {
  const result = await execFileAsync(process.execPath, [
    deliveryScript,
    "--cdp-endpoint", "ws://127.0.0.1:9222/devtools/browser/test",
    "--playwright-root", runtimeRoot,
    "--manifest", manifestPath,
    "--timeout-ms", "6000",
  ], { timeout: 10_000 });
  const receipt = JSON.parse(result.stdout);
  assert.equal(receipt.ok, true);
  assert.equal(receipt.attachmentCount, 1);
  assert.match(receipt.sendDiscovery, /enter-fallback/);
  console.log(JSON.stringify({ ok: true, nativeFileInput: true, enterFallback: true }));
} finally {
  await fs.rm(testRoot, { recursive: true, force: true });
}
