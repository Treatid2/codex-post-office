#!/usr/bin/env node
// SPDX-License-Identifier: MPL-2.0

import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";

const values = new Map();
for (let index = 2; index < process.argv.length; index += 1) {
  const argument = process.argv[index];
  if (!argument.startsWith("--")) throw new Error(`Unexpected argument: ${argument}`);
  const [key, inlineValue] = argument.split("=", 2);
  if (inlineValue !== undefined) {
    values.set(key, inlineValue);
  } else if (process.argv[index + 1] && !process.argv[index + 1].startsWith("--")) {
    values.set(key, process.argv[index + 1]);
    index += 1;
  } else {
    values.set(key, true);
  }
}

function required(name) {
  const value = values.get(name);
  if (typeof value !== "string" || !value) throw new Error(`${name} is required`);
  return value;
}

const endpoint = String(values.get("--endpoint") ?? "http://127.0.0.1:8931/mcp");
const timeoutMs = Number(values.get("--timeout-ms") ?? 120_000);
const manifestPath = path.resolve(required("--manifest"));
const outputRoot = path.resolve(required("--output-root"));
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const idPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/;
let manifest;
let collectionId;
let threadId;
let attachmentName;
let expectedSha256;
let expectedBytes;
let requiredTexts;
let threadUrl;

let sessionId;
let nextId = 1;

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
  if (typeof manifest.sourceTurnId !== "string" || !manifest.sourceTurnId || manifest.sourceTurnId.length > 512) {
    throw new Error("Invalid sourceTurnId");
  }
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
  expectedSha256 = manifest.expectedSha256;
  expectedBytes = manifest.expectedBytes;
  requiredTexts = manifest.requiredText;
  threadUrl = `https://chatgpt.com/c/${threadId}`;
}

function decodePayload(responseText, contentType) {
  if (!responseText.trim()) return null;
  if (!contentType.includes("text/event-stream")) return JSON.parse(responseText);
  const messages = [];
  let dataLines = [];
  for (const line of responseText.split(/\r?\n/)) {
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    else if (line === "" && dataLines.length) {
      messages.push(JSON.parse(dataLines.join("\n")));
      dataLines = [];
    }
  }
  if (dataLines.length) messages.push(JSON.parse(dataLines.join("\n")));
  return messages.at(-1) ?? null;
}

async function request(payload) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = {
      Accept: "application/json, text/event-stream",
      "Content-Type": "application/json",
    };
    if (sessionId) headers["Mcp-Session-Id"] = sessionId;
    const response = await fetch(endpoint, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    sessionId = response.headers.get("mcp-session-id") ?? sessionId;
    const body = await response.text();
    if (!response.ok) throw new Error(`MCP HTTP ${response.status}: ${body.slice(0, 500)}`);
    return decodePayload(body, response.headers.get("content-type") ?? "");
  } finally {
    clearTimeout(timeout);
  }
}

async function callTool(name, args) {
  const response = await request({
    jsonrpc: "2.0",
    id: nextId++,
    method: "tools/call",
    params: { name, arguments: args },
  });
  if (response?.error || response?.result?.isError) {
    throw new Error(`${name} failed: ${JSON.stringify(response?.error ?? response?.result)}`);
  }
  return response?.result;
}

function resultText(result) {
  return (result?.content ?? [])
    .filter((item) => item.type === "text")
    .map((item) => item.text)
    .join("\n");
}

async function collectFiles(directory) {
  const collected = [];
  const entries = await fs.readdir(directory, { withFileTypes: true });
  for (const entry of entries) {
    const candidate = path.join(directory, entry.name);
    if (entry.isDirectory()) collected.push(...await collectFiles(candidate));
    else if (entry.isFile()) collected.push(candidate);
  }
  return collected;
}

async function hashFile(filePath) {
  const bytes = await fs.readFile(filePath);
  return {
    bytes: bytes.length,
    sha256: crypto.createHash("sha256").update(bytes).digest("hex"),
  };
}

async function snapshotFiles() {
  const snapshot = new Map();
  for (const filePath of await collectFiles(outputRoot)) {
    const stat = await fs.stat(filePath);
    snapshot.set(filePath, { size: stat.size, mtimeMs: stat.mtimeMs });
  }
  return snapshot;
}

async function findVerifiedDownload(baseline, clickStartedAt) {
  const files = await collectFiles(outputRoot);
  const matches = [];
  for (const filePath of files) {
    const stat = await fs.stat(filePath);
    const before = baseline.get(filePath);
    const changed = !before || before.size !== stat.size || before.mtimeMs !== stat.mtimeMs;
    if (!changed || stat.mtimeMs < clickStartedAt - 2_000 || stat.size !== expectedBytes) continue;
    const identity = await hashFile(filePath);
    if (identity.sha256 === expectedSha256) matches.push({ filePath, ...identity, mtimeMs: stat.mtimeMs });
  }
  matches.sort((left, right) => right.mtimeMs - left.mtimeMs);
  return matches[0] ?? null;
}

async function normalizeVerifiedDownload(verified) {
  const attemptRoot = path.join(outputRoot, "collections", collectionId, crypto.randomUUID());
  await fs.mkdir(attemptRoot, { recursive: true });
  const retainedName = path.join(attemptRoot, attachmentName);
  await fs.rename(verified.filePath, retainedName);
  const identity = await hashFile(retainedName);
  if (identity.bytes !== expectedBytes || identity.sha256 !== expectedSha256) {
    throw new Error("Normalized attachment identity differs from the verified download");
  }
  return { filePath: retainedName, ...identity };
}

async function closeSession() {
  if (!sessionId) return;
  try {
    await fetch(endpoint, {
      method: "DELETE",
      headers: { "Mcp-Session-Id": sessionId },
      signal: AbortSignal.timeout(Math.min(timeoutMs, 2_000)),
    });
  } catch {
    // Best effort. Session cleanup must not hide the collection result.
  }
}

try {
  await validateManifest();
  await fs.mkdir(outputRoot, { recursive: true });
  const initialize = await request({
    jsonrpc: "2.0",
    id: nextId++,
    method: "initialize",
    params: {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "post-office-chatgpt-collector", version: "0.1.0" },
    },
  });
  if (initialize?.error) throw new Error(JSON.stringify(initialize.error));
  await request({ jsonrpc: "2.0", method: "notifications/initialized", params: {} });

  const navigation = resultText(await callTool("browser_navigate", { url: threadUrl }));
  if (/\bLog in\b|\bSign up\b/i.test(navigation)) {
    throw new Error("Dedicated ChatGPT profile is not authenticated");
  }
  await callTool("browser_press_key", { key: "Escape" });
  await callTool("browser_wait_for", { time: 0.25 });
  try {
    await callTool("browser_wait_for", { text: attachmentName });
  } catch {
    // Playwright MCP's text wait has a shorter internal deadline than this
    // transaction. The correlation and exact-control checks below remain the
    // authoritative fail-closed tests.
  }
  for (const marker of requiredTexts) {
    const found = resultText(await callTool("browser_find", { text: marker }));
    const matchedReference = found.split(/\r?\n/).some((line) =>
      line.toLowerCase().includes(marker.toLowerCase()) && /\[ref=[^\]\s]+\]/.test(line)
    );
    if (!matchedReference) {
      throw new Error(`Required correlation text was not found: ${marker}`);
    }
  }

  await callTool("browser_find", { text: attachmentName });
  let attachmentCandidates = [];
  for (let page = 0; page < 40; page += 1) {
    const attachment = resultText(await callTool("browser_snapshot", { depth: 16 }));
    const lines = attachment.split(/\r?\n/);
    const candidates = [];
    for (let index = 0; index < lines.length; index += 1) {
      const nameMatch = lines[index].match(/^\s*-\s+(?:link|button)\s+"([^"]+)"/i);
      if (nameMatch?.[1] !== attachmentName) continue;
      const reference = lines[index].match(/\[ref=([^\]\s]+)\]/)?.[1];
      if (!reference) continue;
      const indentation = lines[index].match(/^\s*/)?.[0].length ?? 0;
      const descendants = [];
      for (let child = index + 1; child < lines.length; child += 1) {
        if (!lines[child].trim()) continue;
        const childIndentation = lines[child].match(/^\s*/)?.[0].length ?? 0;
        if (childIndentation <= indentation) break;
        descendants.push(lines[child]);
      }
      const context = descendants.join("\n");
      const score = /:\s*Document\b/i.test(context) ? 2 : /:\s*File\b/i.test(context) ? 1 : 0;
      candidates.push({ reference, score, index });
    }
    // ChatGPT commonly exposes both an inline exact-filename download control
    // and a richer document card for the same attachment. Prefer the inline
    // control: the card launches the artifact preview and can invalidate the
    // browser target while that preview is still being prepared. The scored
    // card remains the bounded fallback when no direct control succeeds.
    attachmentCandidates = [...new Map(candidates.map((candidate) => [candidate.reference, candidate])).values()]
      .sort((left, right) => left.score - right.score || left.index - right.index);
    if (attachmentCandidates.length > 8) {
      throw new Error(`Exact attachment control count exceeds the bounded maximum: ${attachmentCandidates.length}`);
    }
    if (attachmentCandidates.length >= 1) break;
    await callTool("browser_press_key", { key: "PageUp" });
    await callTool("browser_wait_for", { time: 0.25 });
  }
  if (attachmentCandidates.length === 0) {
    throw new Error("Expected an exact actionable attachment control, found none in the bounded conversation scan");
  }

  const baseline = await snapshotFiles();
  const clickStartedAt = Date.now();
  const deadline = Date.now() + timeoutMs;
  let verified;
  let lastActionError = "No candidate was actionable";
  for (const candidate of attachmentCandidates) {
    try {
      await callTool("browser_click", {
        target: candidate.reference,
        element: `attachment ${attachmentName}`,
      });
    } catch (error) {
      lastActionError = String(error?.message ?? error);
      continue;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
    verified = await findVerifiedDownload(baseline, clickStartedAt);
    if (verified) break;

    try {
      const downloadMatches = resultText(await callTool("browser_find", { text: "Download" }));
      const downloadReferences = downloadMatches.split(/\r?\n/).flatMap((line) => {
        if (!/^\s*-\s+button\s+"Download"\s+\[ref=/i.test(line)) return [];
        return [...line.matchAll(/\[ref=([^\]\s]+)\]/g)].map((match) => match[1]);
      });
      const uniqueDownloadReferences = [...new Set(downloadReferences)];
      if (uniqueDownloadReferences.length !== 1) {
        throw new Error(`Expected one exact preview Download action, found ${uniqueDownloadReferences.length}`);
      }
      await callTool("browser_click", {
        target: uniqueDownloadReferences[0],
        element: `Download ${attachmentName}`,
      });
      const candidateDeadline = Math.min(deadline, Date.now() + 15_000);
      while (Date.now() < candidateDeadline) {
        verified = await findVerifiedDownload(baseline, clickStartedAt);
        if (verified) break;
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
      if (verified) break;
      lastActionError = "Candidate download did not match the declared content identity";
    } catch (error) {
      lastActionError = String(error?.message ?? error);
    }
    try {
      await callTool("browser_press_key", { key: "Escape" });
    } catch {
      // Continue only with references from the same unchanged page snapshot.
    }
  }
  if (!verified) {
    throw new Error(`Downloaded attachment did not reach the expected byte and SHA-256 identity: ${lastActionError}`);
  }
  verified = await normalizeVerifiedDownload(verified);

  console.log(JSON.stringify({
    ok: true,
    endpoint,
    collectionId,
    threadId,
    attachmentName,
    path: verified.filePath,
    bytes: verified.bytes,
    sha256: verified.sha256,
    evidenceMarkers: requiredTexts.length,
    matchingControls: attachmentCandidates.length,
    receiptReference: `playwright-chatgpt-collection:${collectionId}:${threadId}:${verified.sha256}`,
  }));
} catch (error) {
  const message = error?.name === "AbortError"
    ? `ChatGPT attachment collection timed out after ${timeoutMs}ms`
    : String(error?.message ?? error);
  console.error(JSON.stringify({
    ok: false,
    endpoint,
    manifestPath,
    collectionId: collectionId ?? null,
    threadId: threadId ?? null,
    attachmentName: attachmentName ?? null,
    error: message,
  }));
  process.exitCode = 1;
} finally {
  await closeSession();
}
