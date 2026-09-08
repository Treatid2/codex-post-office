#!/usr/bin/env node
// SPDX-License-Identifier: MPL-2.0

import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

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
const outputRoot = path.resolve(required("--output-root"));
const timeoutMs = Number(values.get("--timeout-ms") ?? 120_000);
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const idPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/;

let manifest;
let collectionId;
let threadId;
let attachmentName;

async function validateManifest() {
  const stat = await fs.stat(manifestPath);
  if (!stat.isFile() || stat.size < 2 || stat.size > 1_048_576) {
    throw new Error("Collection manifest must be a regular JSON file no larger than 1 MiB");
  }
  manifest = JSON.parse(await fs.readFile(manifestPath, "utf8"));
  if (manifest?.schemaVersion !== 1) throw new Error("Unsupported collection manifest schemaVersion");
  if (!idPattern.test(manifest.collectionId ?? "")) throw new Error("Invalid collectionId");
  if (!uuidPattern.test(manifest.threadId ?? "")) throw new Error("Invalid threadId");
  if (!idPattern.test(manifest.mailboxId ?? "")) throw new Error("Invalid mailboxId");
  if (!Number.isSafeInteger(manifest.mailboxGeneration) || manifest.mailboxGeneration < 1) {
    throw new Error("Invalid mailboxGeneration");
  }
  if (!["BROWSER_SWEEP", "AUTOMATIC_REVIEW"].includes(manifest.scopeKind)) {
    throw new Error("Invalid scopeKind");
  }
  if (!idPattern.test(manifest.scopeId ?? "")) throw new Error("Invalid scopeId");
  if (!uuidPattern.test(manifest.sourceTurnId ?? "")) throw new Error("Invalid sourceTurnId");
  if (typeof manifest.attachmentReference !== "string" || !manifest.attachmentReference ||
      manifest.attachmentReference.length > 2048) {
    throw new Error("Invalid attachmentReference");
  }
  if (typeof manifest.attachmentName !== "string" || path.basename(manifest.attachmentName) !== manifest.attachmentName ||
      !manifest.attachmentName || manifest.attachmentName.length > 240) {
    throw new Error("attachmentName must be one leaf filename");
  }
  if (!Number.isSafeInteger(manifest.expectedBytes) || manifest.expectedBytes < 1 ||
      manifest.expectedBytes > 268_435_456) {
    throw new Error("expectedBytes is outside the 1..268435456 boundary");
  }
  if (!/^[0-9a-f]{64}$/.test(manifest.expectedSha256 ?? "")) {
    throw new Error("expectedSha256 must be 64 lowercase hexadecimal characters");
  }
  if (typeof manifest.observedAt !== "string" || !/^\d{4}-\d{2}-\d{2}T/.test(manifest.observedAt)) {
    throw new Error("Invalid observedAt");
  }
  if (!Array.isArray(manifest.requiredText) || manifest.requiredText.length > 16 ||
      manifest.requiredText.some((item) => typeof item !== "string" || !item || item.length > 512)) {
    throw new Error("requiredText must contain at most 16 non-empty strings no longer than 512 characters");
  }
  collectionId = manifest.collectionId;
  threadId = manifest.threadId;
  attachmentName = manifest.attachmentName;
}

async function fileIdentity(filePath) {
  const bytes = await fs.readFile(filePath);
  return {
    bytes: bytes.length,
    sha256: crypto.createHash("sha256").update(bytes).digest("hex"),
  };
}

async function removeAttempt(attemptRoot) {
  try { await fs.rm(attemptRoot, { recursive: true, force: true }); } catch { /* best effort */ }
}

async function main() {
  await validateManifest();
  if (!/^ws:\/\/127\.0\.0\.1:\d+\/devtools\/browser\/[A-Za-z0-9-]+$/.test(cdpEndpoint)) {
    throw new Error("The dedicated Chrome CDP endpoint must be an exact loopback browser WebSocket");
  }
  const playwrightEntry = path.join(playwrightRoot, "node_modules", "playwright-core", "index.mjs");
  const { chromium } = await import(pathToFileURL(playwrightEntry).href);
  const browser = await chromium.connectOverCDP(cdpEndpoint, { timeout: Math.min(timeoutMs, 30_000) });
  const context = browser.contexts()[0];
  if (!context) throw new Error("Dedicated Chrome has no browser context");
  let page = context.pages().find((candidate) => {
    try { return new URL(candidate.url()).pathname === `/c/${threadId}`; } catch { return false; }
  });
  if (!page) page = context.pages()[0] ?? await context.newPage();
  const threadUrl = `https://chatgpt.com/c/${threadId}`;
  if (page.url() !== threadUrl) await page.goto(threadUrl, { waitUntil: "domcontentloaded", timeout: timeoutMs });
  await page.waitForLoadState("domcontentloaded", { timeout: timeoutMs });
  const current = new URL(page.url());
  if (current.origin !== "https://chatgpt.com" || current.pathname !== `/c/${threadId}`) {
    throw new Error("Dedicated ChatGPT profile did not remain on the manifest-bound conversation");
  }
  if (await page.getByRole("button", { name: "Log in", exact: true }).count()) {
    throw new Error("Dedicated ChatGPT profile is not authenticated");
  }

  const sourceTurn = page.locator(`[data-message-id="${manifest.sourceTurnId}"]`);
  await sourceTurn.waitFor({ state: "attached", timeout: timeoutMs });
  if (await sourceTurn.count() !== 1) throw new Error("The manifest-bound source turn is not unique");
  const sourceText = await sourceTurn.innerText({ timeout: timeoutMs });
  for (const marker of manifest.requiredText) {
    if (!sourceText.includes(marker)) throw new Error(`Required correlation text was not found: ${marker}`);
  }

  const downloads = page.locator('button[aria-label="Download file"]');
  const candidates = [];
  for (let scan = 0; scan < 40; scan += 1) {
    const count = await downloads.count();
    for (let index = 0; index < count; index += 1) {
      const candidate = downloads.nth(index);
      const correlated = await candidate.evaluate((button, filename) => {
        let cursor = button;
        for (let depth = 0; cursor && depth < 12; depth += 1, cursor = cursor.parentElement) {
          if ((cursor.textContent ?? "").includes(filename)) return true;
        }
        return false;
      }, attachmentName);
      if (correlated) candidates.push(candidate);
    }
    if (candidates.length) break;
    await page.keyboard.press("PageUp");
    await page.waitForTimeout(250);
  }
  if (!candidates.length || candidates.length > 8) {
    throw new Error(`Expected 1..8 exact attachment download controls, found ${candidates.length}`);
  }

  let lastMismatch = "no download completed";
  for (const candidate of candidates) {
    const attemptRoot = path.join(outputRoot, "collection-attempts", collectionId, crypto.randomUUID());
    await fs.mkdir(attemptRoot, { recursive: true });
    try {
      const signedRequestPromise = page.waitForRequest((request) => {
        try {
          const url = new URL(request.url());
          return url.origin === "https://chatgpt.com" &&
            url.pathname === "/backend-api/estuary/content" &&
            url.searchParams.get("fn") === attachmentName;
        } catch { return false; }
      }, { timeout: Math.min(timeoutMs, 30_000) });
      await candidate.evaluate((button) => button.click());
      const signedRequest = await signedRequestPromise;
      const signedUrl = new URL(signedRequest.url());
      if (signedUrl.searchParams.get("fn") !== attachmentName) throw new Error("Signed attachment filename changed");
      const response = await context.request.get(signedUrl.href, {
        headers: { referer: threadUrl },
        timeout: Math.min(timeoutMs, 30_000),
      });
      if (!response.ok()) throw new Error(`Authenticated attachment fetch returned HTTP ${response.status()}`);
      const temporaryPath = path.join(attemptRoot, attachmentName);
      await fs.writeFile(temporaryPath, await response.body());
      const identity = await fileIdentity(temporaryPath);
      if (identity.bytes !== manifest.expectedBytes || identity.sha256 !== manifest.expectedSha256) {
        lastMismatch = `download identity was ${identity.bytes}:${identity.sha256}`;
        await removeAttempt(attemptRoot);
        continue;
      }
      const retainedRoot = path.join(outputRoot, "collections", collectionId, crypto.randomUUID());
      await fs.mkdir(retainedRoot, { recursive: true });
      const retainedPath = path.join(retainedRoot, attachmentName);
      await fs.rename(temporaryPath, retainedPath);
      await removeAttempt(attemptRoot);
      const retainedIdentity = await fileIdentity(retainedPath);
      if (retainedIdentity.bytes !== manifest.expectedBytes || retainedIdentity.sha256 !== manifest.expectedSha256) {
        throw new Error("Normalized attachment identity differs from the verified download");
      }
      let conversationRestored = true;
      if (page.url() !== threadUrl) {
        try { await page.goto(threadUrl, { waitUntil: "domcontentloaded", timeout: timeoutMs }); }
        catch { conversationRestored = false; }
      }
      console.log(JSON.stringify({
        ok: true,
        collectionId,
        threadId,
        sourceTurnId: manifest.sourceTurnId,
        attachmentName,
        path: retainedPath,
        bytes: retainedIdentity.bytes,
        sha256: retainedIdentity.sha256,
        evidenceMarkers: manifest.requiredText.length,
        matchingControls: candidates.length,
        conversationRestored,
        receiptReference: `playwright-chatgpt-collection:${collectionId}:${threadId}:${retainedIdentity.sha256}`,
      }));
      process.exit(0);
    } catch (error) {
      lastMismatch = String(error?.message ?? error);
      await removeAttempt(attemptRoot);
      if (page.isClosed()) page = await context.newPage();
      if (page.url() !== threadUrl) {
        try { await page.goto(threadUrl, { waitUntil: "domcontentloaded", timeout: timeoutMs }); } catch { /* next candidate fails closed */ }
      }
    }
  }
  throw new Error(`No manifest-correlated attachment matched the declared identity: ${lastMismatch}`);
}

try {
  await main();
} catch (error) {
  console.error(JSON.stringify({
    ok: false,
    manifestPath,
    collectionId: collectionId ?? null,
    threadId: threadId ?? null,
    attachmentName: attachmentName ?? null,
    error: String(error?.message ?? error),
  }));
  process.exit(1);
}
