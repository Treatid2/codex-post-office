#!/usr/bin/env node
// SPDX-License-Identifier: MPL-2.0

import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  CollectionError,
  correlateOriginMessages,
  publicCollectionFailure,
  selectLibraryCandidates,
} from "./playwright_chatgpt_library.mjs";

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

async function retainVerifiedCollection(temporaryPath, attemptRoot, transport, metadata = {}) {
  const identity = await fileIdentity(temporaryPath);
  if (identity.bytes !== manifest.expectedBytes || identity.sha256 !== manifest.expectedSha256) {
    throw new CollectionError(
      "ATTACHMENT_IDENTITY_MISMATCH",
      `Downloaded attachment identity was ${identity.bytes}:${identity.sha256}`,
    );
  }
  const retainedRoot = path.join(outputRoot, "collections", collectionId, crypto.randomUUID());
  await fs.mkdir(retainedRoot, { recursive: true });
  const retainedPath = path.join(retainedRoot, attachmentName);
  await fs.rename(temporaryPath, retainedPath);
  await removeAttempt(attemptRoot);
  const retainedIdentity = await fileIdentity(retainedPath);
  if (retainedIdentity.bytes !== manifest.expectedBytes || retainedIdentity.sha256 !== manifest.expectedSha256) {
    throw new CollectionError(
      "ATTACHMENT_RETENTION_MISMATCH",
      "Retained attachment identity differs from the verified download",
    );
  }
  return {
    collectionId,
    threadId,
    sourceTurnId: manifest.sourceTurnId,
    attachmentName,
    path: retainedPath,
    bytes: retainedIdentity.bytes,
    sha256: retainedIdentity.sha256,
    evidenceMarkers: manifest.requiredText.length,
    transport,
    ...metadata,
    receiptReference: `playwright-chatgpt-collection:${collectionId}:${threadId}:${retainedIdentity.sha256}`,
  };
}

async function captureConversationMetadata(page, context, threadUrl) {
  const metadataUrl = `https://chatgpt.com/backend-api/conversations/${threadId}`;
  let directPayload = null;
  try {
    const directResponse = await context.request.get(metadataUrl, {
      headers: { referer: threadUrl },
      timeout: Math.min(timeoutMs, 30_000),
    });
    if (directResponse.ok()) directPayload = await directResponse.json();
  } catch { /* retain the page-response fallback below */ }
  const responsePromise = directPayload ? null : page.waitForResponse((response) => {
    try {
      const url = new URL(response.url());
      return url.origin === "https://chatgpt.com" &&
        url.pathname === `/backend-api/conversations/${threadId}` &&
        response.status() === 200;
    } catch { return false; }
  }, { timeout: Math.min(timeoutMs, 30_000) });
  if (page.url() === threadUrl) {
    await page.reload({ waitUntil: "domcontentloaded", timeout: timeoutMs });
  } else {
    await page.goto(threadUrl, { waitUntil: "domcontentloaded", timeout: timeoutMs });
  }
  if (directPayload) return directPayload;
  try {
    return await (await responsePromise).json();
  } catch (error) {
    throw new CollectionError(
      "CONVERSATION_METADATA_UNAVAILABLE",
      `Unable to capture authenticated ChatGPT conversation metadata: ${String(error?.message ?? error)}`,
    );
  }
}

async function collectFromChatGptLibrary(context, conversationPayload) {
  const { originMessageIds } = correlateOriginMessages(
    conversationPayload,
    manifest.sourceTurnId,
    attachmentName,
    manifest.requiredText,
  );
  const libraryPage = await context.newPage();
  const libraryPayloads = [];
  const pendingPayloads = new Set();
  const observeLibraryResponse = (response) => {
    try {
      const url = new URL(response.url());
      if (url.origin !== "https://chatgpt.com" || url.pathname !== "/backend-api/files/library" || response.status() !== 200) {
        return;
      }
      const pending = response.json()
        .then((payload) => { libraryPayloads.push(payload); })
        .catch(() => { /* a malformed response is reported below */ })
        .finally(() => pendingPayloads.delete(pending));
      pendingPayloads.add(pending);
    } catch { /* ignore unrelated responses */ }
  };
  libraryPage.on("response", observeLibraryResponse);
  try {
    const initialLibraryResponse = libraryPage.waitForResponse((response) => {
      try {
        const url = new URL(response.url());
        return url.origin === "https://chatgpt.com" &&
          url.pathname === "/backend-api/files/library" &&
          response.status() === 200;
      } catch { return false; }
    }, { timeout: Math.min(timeoutMs, 30_000) });
    await libraryPage.goto("https://chatgpt.com/library", {
      waitUntil: "domcontentloaded",
      timeout: timeoutMs,
    });
    await initialLibraryResponse;
    await libraryPage.waitForTimeout(500);
    let candidates = [];
    for (let pageIndex = 0; pageIndex < 8; pageIndex += 1) {
      await Promise.all([...pendingPayloads]);
      candidates = libraryPayloads.flatMap((payload) => {
        return selectLibraryCandidates(payload, manifest, originMessageIds);
      });
      candidates = [...new Map(candidates.map((item) => [item.id, item])).values()];
      if (candidates.length) break;
      const before = libraryPayloads.length;
      await libraryPage.keyboard.press("End");
      await libraryPage.waitForTimeout(750);
      if (libraryPayloads.length === before) break;
    }
    if (!candidates.length) {
      throw new CollectionError(
        "ATTACHMENT_LIBRARY_UNRESOLVED",
        "No exact filename, size, conversation, and originating-turn match was found in ChatGPT Library",
        {
          originMessageCount: originMessageIds.size,
          libraryPayloadCount: libraryPayloads.length,
          libraryItemCount: libraryPayloads.reduce((total, payload) => total + (Array.isArray(payload?.items) ? payload.items.length : 0), 0),
        },
      );
    }
    if (candidates.length > 8) {
      throw new CollectionError(
        "ATTACHMENT_LIBRARY_AMBIGUOUS",
        `ChatGPT Library returned ${candidates.length} manifest-correlated candidates`,
      );
    }

    const mismatches = [];
    for (const candidate of candidates) {
      const attemptRoot = path.join(outputRoot, "collection-attempts", collectionId, crypto.randomUUID());
      await fs.mkdir(attemptRoot, { recursive: true });
      try {
        const actionButton = libraryPage.getByRole("button", {
          name: `Open actions menu for ${attachmentName}`,
          exact: true,
        });
        const actionCount = await actionButton.count();
        if (actionCount !== 1) {
          throw new CollectionError(
            "ATTACHMENT_LIBRARY_CONTROL_UNAVAILABLE",
            `Expected one Library action control for the manifest candidate, found ${actionCount}`,
          );
        }
        await actionButton.click({ force: true, timeout: Math.min(timeoutMs, 30_000) });
        const menu = libraryPage.locator('[role="menu"]:visible');
        await menu.waitFor({ state: "visible", timeout: Math.min(timeoutMs, 30_000) });
        const downloadPromise = libraryPage.waitForEvent("download", {
          timeout: Math.min(timeoutMs, 30_000),
        });
        await menu.getByText("Download", { exact: true }).click({ timeout: Math.min(timeoutMs, 30_000) });
        const download = await downloadPromise;
        const suggestedFilename = download.suggestedFilename();
        if (!isChromeCollisionFilename(suggestedFilename, attachmentName)) {
          throw new CollectionError(
            "ATTACHMENT_LIBRARY_FILENAME_MISMATCH",
            `Library download filename changed to ${suggestedFilename}`,
          );
        }
        const temporaryPath = path.join(attemptRoot, attachmentName);
        await download.saveAs(temporaryPath);
        return await retainVerifiedCollection(temporaryPath, attemptRoot, "CHATGPT_LIBRARY", {
          libraryFileId: candidate.id,
          originMessageId: candidate.origination_message_id,
          libraryCandidates: candidates.length,
        });
      } catch (error) {
        mismatches.push(publicCollectionFailure(error));
        await removeAttempt(attemptRoot);
        await libraryPage.keyboard.press("Escape").catch(() => {});
      }
    }
    throw new CollectionError(
      "ATTACHMENT_LIBRARY_IDENTITY_MISMATCH",
      "No manifest-correlated ChatGPT Library download matched the declared identity",
      { attempts: mismatches },
    );
  } finally {
    libraryPage.off("response", observeLibraryResponse);
    await libraryPage.close().catch(() => {});
  }
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function isChromeCollisionFilename(suggestedFilename, expectedFilename) {
  if (suggestedFilename === expectedFilename) return true;
  if (path.basename(suggestedFilename) !== suggestedFilename) return false;
  const extension = path.extname(expectedFilename);
  const stem = extension ? expectedFilename.slice(0, -extension.length) : expectedFilename;
  const collisionPattern = new RegExp(
    `^${escapeRegExp(stem)} ?\\([1-9]\\d*\\)${escapeRegExp(extension)}$`,
  );
  return collisionPattern.test(suggestedFilename);
}

async function findManifestSourceTurn(page) {
  const sourceTurn = page.locator(`[data-message-id="${manifest.sourceTurnId}"]`);
  const findUnique = async () => {
    const count = await sourceTurn.count();
    if (count > 1) throw new Error("The manifest-bound source turn is not unique");
    return count === 1;
  };

  if (await findUnique()) return sourceTurn;

  // ChatGPT virtualizes long conversations and preserves the tab's previous
  // scroll position. A manifest-bound result can therefore be the newest turn
  // while its exact data-message-id is not attached to the DOM. Move to the
  // newest edge first, then perform one bounded backwards scan. Correlation is
  // still fail-closed on the exact source-turn UUID; scrolling never substitutes
  // text similarity or a neighbouring attachment card for source identity.
  await page.keyboard.press("Escape");
  await page.keyboard.press("End");
  await page.waitForTimeout(500);
  if (await findUnique()) return sourceTurn;

  for (let scan = 0; scan < 40; scan += 1) {
    await page.keyboard.press("PageUp");
    await page.waitForTimeout(250);
    if (await findUnique()) return sourceTurn;
  }

  throw new Error(
    `Manifest-bound source turn ${manifest.sourceTurnId} was not found in the bounded conversation scan`,
  );
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
  const conversationPayload = await captureConversationMetadata(page, context, threadUrl);
  await page.waitForLoadState("domcontentloaded", { timeout: timeoutMs });
  const current = new URL(page.url());
  if (current.origin !== "https://chatgpt.com" || current.pathname !== `/c/${threadId}`) {
    throw new Error("Dedicated ChatGPT profile did not remain on the manifest-bound conversation");
  }
  if (await page.getByRole("button", { name: "Log in", exact: true }).count()) {
    throw new Error("Dedicated ChatGPT profile is not authenticated");
  }

  const sourceTurn = await findManifestSourceTurn(page);
  const sourceText = await sourceTurn.innerText({ timeout: timeoutMs });
  for (const marker of manifest.requiredText) {
    if (!sourceText.includes(marker)) throw new Error(`Required correlation text was not found: ${marker}`);
  }

  // ChatGPT renders attachment cards as siblings of the element carrying the
  // assistant message ID. Select the nearest ancestor that owns download
  // controls so a content-reference index remains local to this exact turn.
  const conversationTurn = sourceTurn.locator(
    "xpath=ancestor::*[starts-with(@data-testid, 'conversation-turn-')][1]",
  );
  const sourceArticle = sourceTurn.locator("xpath=ancestor::article[1]");
  const sourceContainer = await conversationTurn.count()
    ? conversationTurn
    : await sourceArticle.count()
      ? sourceArticle
      : sourceTurn.locator('xpath=ancestor::*[.//button[@aria-label="Download file"]][1]');
  const attachmentControlSelector = [
    'button[aria-label="Download file"]',
    'button:has-text("Download")',
    "a[download]",
    'a[href^="sandbox:"]',
    'a[href*="/backend-api/estuary/content"]',
    'a[href*="/backend-api/files/"]',
  ].join(", ");
  const sourceDownloads = sourceContainer.locator(attachmentControlSelector);
  const contentReference = manifest.attachmentReference.match(
    /^:chatgpt-content-reference\{index=["']?(\d+)["']?\}$/,
  );
  let candidates = [];
  if (contentReference) {
    const attachmentIndex = Number(contentReference[1]);
    const sourceDownloadCount = await sourceDownloads.count();
    if (!Number.isSafeInteger(attachmentIndex) || sourceDownloadCount > 8) {
      throw new Error(
        `Expected at most 8 download controls in the referenced source turn, found ${sourceDownloadCount}`,
      );
    }
    // content-reference indices are stable evidence identifiers but are not
    // guaranteed to equal the DOM order of attachment cards. Use the index as
    // a bounded priority hint when possible, then verify every remaining card
    // owned by this exact source turn by filename, byte count, and SHA-256.
    const candidateOrder = [];
    if (attachmentIndex < sourceDownloadCount) candidateOrder.push(attachmentIndex);
    for (let index = 0; index < sourceDownloadCount; index += 1) {
      if (index !== attachmentIndex) candidateOrder.push(index);
    }
    candidates = candidateOrder.map((index) => sourceDownloads.nth(index));
  } else {
    const downloads = page.locator(attachmentControlSelector);
    const candidateMatches = [];
    for (let scan = 0; scan < 40; scan += 1) {
      const count = await downloads.count();
      for (let index = 0; index < count; index += 1) {
        const candidate = downloads.nth(index);
        const correlationDepth = await candidate.evaluate((button, filename) => {
          let cursor = button;
          for (let depth = 0; cursor && depth < 12; depth += 1, cursor = cursor.parentElement) {
            if ((cursor.textContent ?? "").includes(filename)) return depth;
          }
          return -1;
        }, attachmentName);
        if (correlationDepth >= 0) candidateMatches.push({ candidate, correlationDepth });
      }
      if (candidateMatches.length) break;
      await page.keyboard.press("PageUp");
      await page.waitForTimeout(250);
    }
    const nearestDepth = candidateMatches.length
      ? Math.min(...candidateMatches.map(({ correlationDepth }) => correlationDepth))
      : -1;
    candidates = candidateMatches
      .filter(({ correlationDepth }) => correlationDepth === nearestDepth)
      .map(({ candidate }) => candidate);
  }
  if (candidates.length > 8) throw new Error(`Expected at most 8 exact attachment download controls, found ${candidates.length}`);

  const mismatches = [];
  for (const candidate of candidates) {
    const attemptRoot = path.join(outputRoot, "collection-attempts", collectionId, crypto.randomUUID());
    await fs.mkdir(attemptRoot, { recursive: true });
    try {
      const eventTimeout = Math.min(timeoutMs, 30_000);
      const signedRequestPromise = page.waitForRequest((request) => {
        try {
          const url = new URL(request.url());
          return url.origin === "https://chatgpt.com" &&
            url.pathname === "/backend-api/estuary/content" &&
            url.searchParams.get("fn") === attachmentName;
        } catch { return false; }
      }, { timeout: eventTimeout })
        .then((request) => ({ kind: "request", request }))
        .catch((error) => ({ kind: "request-error", error }));
      const browserDownloadPromise = page.waitForEvent("download", { timeout: eventTimeout })
        .then((download) => ({ kind: "download", download }))
        .catch((error) => ({ kind: "download-error", error }));

      // Playwright's locator click is a trusted browser input. ChatGPT currently
      // uses two attachment transports: smaller/generated files expose the
      // signed estuary request, while larger ZIPs may hand the response straight
      // to Chrome's download manager without a page-observable request.
      await candidate.click({ force: true, timeout: eventTimeout });
      let attachmentEvent = await Promise.race([signedRequestPromise, browserDownloadPromise]);
      if (attachmentEvent.kind === "request-error") attachmentEvent = await browserDownloadPromise;
      else if (attachmentEvent.kind === "download-error") attachmentEvent = await signedRequestPromise;

      const temporaryPath = path.join(attemptRoot, attachmentName);
      if (attachmentEvent.kind === "request") {
        const signedUrl = new URL(attachmentEvent.request.url());
        if (signedUrl.searchParams.get("fn") !== attachmentName) {
          throw new Error("Signed attachment filename changed");
        }
        const response = await context.request.get(signedUrl.href, {
          headers: { referer: threadUrl },
          timeout: eventTimeout,
        });
        if (!response.ok()) throw new Error(`Authenticated attachment fetch returned HTTP ${response.status()}`);
        await fs.writeFile(temporaryPath, await response.body());
      } else if (attachmentEvent.kind === "download") {
        const suggestedFilename = attachmentEvent.download.suggestedFilename();
        // Chrome may add a local collision suffix even when ChatGPT's exact
        // attachment and Content-Disposition filename are correct. Accept only
        // that mechanical basename variant; source-turn correlation plus the
        // manifest-bound byte count and SHA-256 remain mandatory below.
        if (!isChromeCollisionFilename(suggestedFilename, attachmentName)) {
          throw new Error(`Browser download filename changed to ${suggestedFilename}`);
        }
        await attachmentEvent.download.saveAs(temporaryPath);
      } else {
        const requestError = String(attachmentEvent?.error?.message ?? attachmentEvent?.error ?? "unknown error");
        throw new Error(`No supported attachment transport completed: ${requestError}`);
      }
      const retained = await retainVerifiedCollection(temporaryPath, attemptRoot, "IN_THREAD_CONTROL", {
        matchingControls: candidates.length,
      });
      let conversationRestored = true;
      if (page.url() !== threadUrl) {
        try { await page.goto(threadUrl, { waitUntil: "domcontentloaded", timeout: timeoutMs }); }
        catch { conversationRestored = false; }
      }
      console.log(JSON.stringify({
        ok: true,
        ...retained,
        conversationRestored,
      }));
      await browser.close();
      return;
    } catch (error) {
      mismatches.push(String(error?.message ?? error));
      await removeAttempt(attemptRoot);
      if (page.isClosed()) page = await context.newPage();
      if (page.url() !== threadUrl) {
        try { await page.goto(threadUrl, { waitUntil: "domcontentloaded", timeout: timeoutMs }); } catch { /* next candidate fails closed */ }
      }
    }
  }
  const libraryCollection = await collectFromChatGptLibrary(context, conversationPayload);
  console.log(JSON.stringify({
    ok: true,
    ...libraryCollection,
    matchingControls: candidates.length,
    directControlFailures: mismatches.length,
    conversationRestored: page.url() === threadUrl,
  }));
  await browser.close();
}

try {
  await main();
} catch (error) {
  const failure = publicCollectionFailure(error);
  console.error(JSON.stringify({
    ok: false,
    manifestPath,
    collectionId: collectionId ?? null,
    threadId: threadId ?? null,
    attachmentName: attachmentName ?? null,
    ...failure,
  }));
  process.exit(1);
}
