#!/usr/bin/env node
// SPDX-License-Identifier: MPL-2.0

import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { discoverComposer, discoverSendAction } from "./chatgpt_composer.mjs";

const values = new Map();
for (let index = 2; index < process.argv.length; index += 1) {
  const argument = process.argv[index];
  if (!argument.startsWith("--")) throw new Error(`Unexpected argument: ${argument}`);
  const [key, inlineValue] = argument.split("=", 2);
  if (inlineValue !== undefined) values.set(key, inlineValue);
  else if (process.argv[index + 1] && !process.argv[index + 1].startsWith("--")) {
    values.set(key, process.argv[index + 1]);
    index += 1;
  } else values.set(key, true);
}

function required(name) {
  const value = values.get(name);
  if (typeof value !== "string" || !value) throw new Error(`${name} is required`);
  return value;
}

const cdpEndpoint = required("--cdp-endpoint");
const playwrightRoot = path.resolve(required("--playwright-root"));
const manifestPath = path.resolve(required("--manifest"));
const timeoutMs = Number(values.get("--timeout-ms") ?? 120_000);
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const idPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/;
let browser;
let manifest;

async function sha256File(filePath) {
  const handle = await fs.open(filePath, "r");
  try {
    const hash = crypto.createHash("sha256");
    for await (const chunk of handle.createReadStream()) hash.update(chunk);
    return hash.digest("hex");
  } finally {
    await handle.close();
  }
}

async function validateManifest() {
  const stat = await fs.stat(manifestPath);
  if (!stat.isFile() || stat.size < 2 || stat.size > 1_048_576) {
    throw new Error("Delivery manifest must be a regular JSON file no larger than 1 MiB");
  }
  manifest = JSON.parse(await fs.readFile(manifestPath, "utf8"));
  if (manifest?.schemaVersion !== 1) throw new Error("Unsupported delivery manifest schemaVersion");
  if (!idPattern.test(manifest.dispatchId ?? "")) throw new Error("Invalid dispatchId");
  if (!uuidPattern.test(manifest.threadId ?? "")) throw new Error("Invalid threadId");
  if (!idPattern.test(manifest.messageId ?? "")) throw new Error("Invalid messageId");
  if (!idPattern.test(manifest.mailboxId ?? "")) throw new Error("Invalid mailboxId");
  if (!Number.isSafeInteger(manifest.mailboxGeneration) || manifest.mailboxGeneration < 1) {
    throw new Error("Invalid mailboxGeneration");
  }
  if (typeof manifest.prompt !== "string" || !manifest.prompt.trim() ||
      Buffer.byteLength(manifest.prompt, "utf8") > 65_536) {
    throw new Error("Manifest prompt is missing or exceeds 65536 bytes");
  }
  if (!Array.isArray(manifest.attachments) || manifest.attachments.length < 1 || manifest.attachments.length > 16) {
    throw new Error("Manifest must contain 1..16 attachments");
  }
  let totalBytes = 0;
  const seen = new Set();
  for (const attachment of manifest.attachments) {
    const resolved = path.resolve(String(attachment.path ?? ""));
    if (!path.isAbsolute(attachment.path ?? "") || seen.has(resolved.toLowerCase())) {
      throw new Error("Every attachment path must be unique and absolute");
    }
    seen.add(resolved.toLowerCase());
    if (path.basename(resolved) !== attachment.sourceName || path.basename(attachment.sourceName) !== attachment.sourceName) {
      throw new Error("Attachment sourceName must exactly match the path leaf");
    }
    const item = await fs.stat(resolved);
    if (!item.isFile() || item.size !== attachment.sizeBytes) {
      throw new Error(`Attachment size mismatch: ${attachment.sourceName}`);
    }
    if (!/^[0-9a-f]{64}$/.test(attachment.sha256 ?? "") ||
        await sha256File(resolved) !== attachment.sha256) {
      throw new Error(`Attachment SHA-256 mismatch: ${attachment.sourceName}`);
    }
    totalBytes += item.size;
  }
  if (totalBytes > 268_435_456) throw new Error("Combined attachment size exceeds 268435456 bytes");
}

function marker() {
  return `POST-OFFICE-PLAYWRIGHT-DISPATCH ${manifest.dispatchId}`;
}

async function markerExists(page) {
  return await page.locator('[data-message-id]')
    .filter({ hasText: marker() }).count() > 0;
}

async function waitForSubmittedMarker(page, timeout) {
  const deadline = Date.now() + Math.max(timeout, 0);
  do {
    if (await markerExists(page)) return true;
    if (Date.now() >= deadline) return false;
    await page.waitForTimeout(Math.min(500, deadline - Date.now()));
  } while (true);
}

async function composerStillContainsMarker(composer) {
  return composer.evaluate((element, expected) => {
    const value = "value" in element ? element.value : element.textContent;
    return typeof value === "string" && value.includes(expected);
  }, marker());
}

async function setAttachments(page) {
  const paths = manifest.attachments.map((item) => path.resolve(item.path));
  const fileInput = page.locator('input[type="file"]').first();
  const setNativeFileInput = async () => {
    if (await fileInput.count() === 0) return false;
    await fileInput.setInputFiles(paths, { timeout: timeoutMs });
    return true;
  };
  const add = page.locator([
    'button[data-testid="composer-plus-btn"]',
    'button[aria-label="Add files and more"]',
    'button[aria-label="Attach files"]',
  ].join(", ")).first();
  let attached = await setNativeFileInput();
  if (!attached) {
    await add.waitFor({ state: "attached", timeout: timeoutMs });
    await add.click({ force: true, timeout: timeoutMs });
    await page.waitForTimeout(250);
    attached = await setNativeFileInput();
  }
  if (!attached) {
    const upload = page.getByRole("menuitem")
      .filter({ hasText: /^(Upload from computer|Upload files|Add photos & files)$/ })
      .or(page.getByText(/^(Upload from computer|Upload files|Add photos & files)$/))
      .first();
    await upload.waitFor({ state: "attached", timeout: timeoutMs });
    const chooserPromise = page.waitForEvent("filechooser", { timeout: timeoutMs });
    await upload.click({ force: true, timeout: timeoutMs });
    const chooser = await chooserPromise;
    await chooser.setFiles(paths, { timeout: timeoutMs });
  }
  for (const attachment of manifest.attachments) {
    await page.getByText(attachment.sourceName, { exact: true }).first()
      .waitFor({ state: "attached", timeout: timeoutMs });
  }
}

try {
  await validateManifest();
  if (!/^ws:\/\/127\.0\.0\.1:\d+\/devtools\/browser\/[A-Za-z0-9-]+$/.test(cdpEndpoint)) {
    throw new Error("The dedicated Chrome CDP endpoint must be an exact loopback browser WebSocket");
  }
  const entry = path.join(playwrightRoot, "node_modules", "playwright-core", "index.mjs");
  const { chromium } = await import(pathToFileURL(entry).href);
  browser = await chromium.connectOverCDP(cdpEndpoint, { timeout: Math.min(timeoutMs, 30_000) });
  const context = browser.contexts()[0];
  if (!context) throw new Error("Dedicated Chrome has no browser context");
  let page = context.pages().find((candidate) => {
    try { return new URL(candidate.url()).pathname === `/c/${manifest.threadId}`; } catch { return false; }
  });
  if (!page) page = context.pages()[0] ?? await context.newPage();
  const threadUrl = `https://chatgpt.com/c/${manifest.threadId}`;
  // Always navigate, including an exact-URL replay, so an earlier interrupted
  // upload cannot leave duplicate attachment cards or a stale draft behind.
  await page.goto(threadUrl, { waitUntil: "domcontentloaded", timeout: timeoutMs });
  await page.waitForLoadState("domcontentloaded", { timeout: timeoutMs });
  if (new URL(page.url()).pathname !== `/c/${manifest.threadId}` ||
      await page.getByRole("button", { name: "Log in", exact: true }).count()) {
    throw new Error("Dedicated ChatGPT profile is unauthenticated or left the manifest-bound conversation");
  }
  let replayed = await markerExists(page);
  let composerStrategy = null;
  let sendStrategy = null;
  if (!replayed) {
    await setAttachments(page);
    const composerDiscovery = await discoverComposer(page, timeoutMs);
    composerStrategy = composerDiscovery.strategy;
    const composer = composerDiscovery.locator;
    await composer.fill(`${marker()}\n\n${manifest.prompt}`, { timeout: timeoutMs });
    const sendDiscovery = await discoverSendAction(page, composer, timeoutMs);
    sendStrategy = sendDiscovery.strategy;
    const send = sendDiscovery.locator;
    const sendDeadline = Date.now() + timeoutMs;
    while (!await send.isEnabled() && Date.now() < sendDeadline) {
      await page.waitForTimeout(500);
    }
    if (!await send.isEnabled()) {
      throw new Error("ChatGPT send action did not become enabled after attachment processing");
    }
    await send.click({ force: true, timeout: timeoutMs });
    let submitted = await waitForSubmittedMarker(page, Math.min(5_000, timeoutMs));
    if (!submitted && await composerStillContainsMarker(composer)) {
      await composer.press("Enter", { timeout: timeoutMs });
      sendStrategy = `${sendStrategy}+enter-fallback`;
    }
    submitted ||= await waitForSubmittedMarker(page, Math.max(0, sendDeadline - Date.now()));
    if (!submitted) throw new Error("Submitted delivery marker was not observed");
  }
  console.log(JSON.stringify({
    ok: true,
    threadId: manifest.threadId,
    messageId: manifest.messageId,
    dispatchId: manifest.dispatchId,
    marker: marker(),
    attachmentCount: manifest.attachments.length,
    receiptReference: `playwright-chatgpt:${manifest.dispatchId}:${manifest.threadId}`,
    attachments: manifest.attachments.map(({ sourceName, sizeBytes, sha256 }) => ({ sourceName, sizeBytes, sha256 })),
    replayed,
    composerDiscovery: composerStrategy,
    sendDiscovery: sendStrategy,
  }));
} catch (error) {
  console.error(JSON.stringify({
    ok: false,
    manifestPath,
    dispatchId: manifest?.dispatchId ?? null,
    threadId: manifest?.threadId ?? null,
    error: String(error?.message ?? error),
  }));
  process.exitCode = 1;
} finally {
  if (browser) await browser.close().catch(() => {});
}
