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

function sha256Text(value) {
  return crypto.createHash("sha256").update(value, "utf8").digest("hex");
}

async function validateManifest() {
  const stat = await fs.stat(manifestPath);
  if (!stat.isFile() || stat.size < 2 || stat.size > 1_048_576) {
    throw new Error("Review activation manifest must be a regular JSON file no larger than 1 MiB");
  }
  manifest = JSON.parse(await fs.readFile(manifestPath, "utf8"));
  if (manifest?.schemaVersion !== 1 || manifest?.kind !== "AUTOMATIC_REVIEW_ACTIVATION") {
    throw new Error("Unsupported review activation manifest contract");
  }
  if (!idPattern.test(manifest.reviewId ?? "")) throw new Error("Invalid reviewId");
  if (!idPattern.test(manifest.dispatchId ?? "")) throw new Error("Invalid dispatchId");
  if (!uuidPattern.test(manifest.threadId ?? "")) throw new Error("Invalid threadId");
  if (!idPattern.test(manifest.mailboxId ?? "")) throw new Error("Invalid mailboxId");
  if (!Number.isSafeInteger(manifest.mailboxGeneration) || manifest.mailboxGeneration < 1) {
    throw new Error("Invalid mailboxGeneration");
  }
  if (typeof manifest.prompt !== "string" || !manifest.prompt.trim() ||
      Buffer.byteLength(manifest.prompt, "utf8") > 65_536) {
    throw new Error("Manifest prompt is missing or exceeds 65536 bytes");
  }
  if (!/^[0-9a-f]{64}$/.test(manifest.promptSha256 ?? "") ||
      sha256Text(manifest.prompt) !== manifest.promptSha256) {
    throw new Error("Review activation prompt SHA-256 mismatch");
  }
  for (const requiredText of [manifest.reviewId, manifest.dispatchId]) {
    if (!manifest.prompt.includes(requiredText)) {
      throw new Error("Review activation prompt lacks a manifest-bound identifier");
    }
  }
}

function marker() {
  return `POST-OFFICE-REVIEW-ACTIVATION ${manifest.reviewId} ${manifest.dispatchId}`;
}

async function findSubmittedTurn(page) {
  const candidates = page.locator('[data-message-id]').filter({ hasText: marker() });
  const count = await candidates.count();
  if (count === 0) return null;
  if (count !== 1) throw new Error(`Expected one submitted review activation marker, found ${count}`);
  const candidate = candidates.first();
  const sourceMessageId = await candidate.getAttribute("data-message-id");
  if (!uuidPattern.test(sourceMessageId ?? "")) {
    throw new Error("Submitted review activation has no exact source message UUID");
  }
  return sourceMessageId;
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
  await page.goto(`https://chatgpt.com/c/${manifest.threadId}`, {
    waitUntil: "domcontentloaded", timeout: timeoutMs,
  });
  if (new URL(page.url()).pathname !== `/c/${manifest.threadId}` ||
      await page.getByRole("button", { name: "Log in", exact: true }).count()) {
    throw new Error("Dedicated ChatGPT profile is unauthenticated or left the manifest-bound conversation");
  }
  const composerDiscovery = await discoverComposer(page, timeoutMs);
  const composer = composerDiscovery.locator;
  // Conversation turns hydrate after DOMContentLoaded. Do not mistake that
  // interval for an absent marker and emit a duplicate activation.
  await page.waitForTimeout(1500);
  let sourceMessageId = await findSubmittedTurn(page);
  const replayed = Boolean(sourceMessageId);
  let sendStrategy = null;
  if (!sourceMessageId) {
    await composer.fill(`${marker()}\n\n${manifest.prompt}`, { timeout: timeoutMs });
    const sendDiscovery = await discoverSendAction(page, composer, timeoutMs);
    sendStrategy = sendDiscovery.strategy;
    const send = sendDiscovery.locator;
    const deadline = Date.now() + timeoutMs;
    while (!await send.isEnabled() && Date.now() < deadline) await page.waitForTimeout(500);
    if (!await send.isEnabled()) throw new Error("ChatGPT send action did not become enabled");
    await send.click({ force: true, timeout: timeoutMs });
    await page.locator('[data-message-id]').filter({ hasText: marker() }).first()
      .waitFor({ state: "attached", timeout: timeoutMs });
    sourceMessageId = await findSubmittedTurn(page);
  }
  if (!sourceMessageId) throw new Error("Submitted review activation marker was not observed");
  console.log(JSON.stringify({
    ok: true,
    kind: manifest.kind,
    reviewId: manifest.reviewId,
    dispatchId: manifest.dispatchId,
    threadId: manifest.threadId,
    sourceMessageId,
    marker: marker(),
    promptSha256: manifest.promptSha256,
    receiptReference: `playwright-chatgpt-review-activation:${manifest.reviewId}:${manifest.dispatchId}:${manifest.threadId}:${sourceMessageId}`,
    replayed,
    composerDiscovery: composerDiscovery.strategy,
    sendDiscovery: sendStrategy,
  }));
} catch (error) {
  console.error(JSON.stringify({
    ok: false,
    manifestPath,
    reviewId: manifest?.reviewId ?? null,
    dispatchId: manifest?.dispatchId ?? null,
    threadId: manifest?.threadId ?? null,
    error: String(error?.message ?? error),
  }));
  process.exitCode = 1;
} finally {
  if (browser) await browser.close().catch(() => {});
}
