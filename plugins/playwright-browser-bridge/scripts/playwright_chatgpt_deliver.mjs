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

const endpoint = String(values.get("--endpoint") ?? "http://127.0.0.1:8931/mcp");
const timeoutMs = Number(values.get("--timeout-ms") ?? 120_000);
const manifestPath = path.resolve(required("--manifest"));
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const idPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/;
const maxPromptBytes = 65_536;
const maxAttachmentBytes = 268_435_456;
const maxAttachments = 16;
let sessionId;
let nextId = 1;
let manifest;
let threadUrl;

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
    const headers = { Accept: "application/json, text/event-stream", "Content-Type": "application/json" };
    if (sessionId) headers["Mcp-Session-Id"] = sessionId;
    const response = await fetch(endpoint, {
      method: "POST", headers, body: JSON.stringify(payload), signal: controller.signal,
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
    jsonrpc: "2.0", id: nextId++, method: "tools/call", params: { name, arguments: args },
  });
  if (response?.error || response?.result?.isError) {
    throw new Error(`${name} failed: ${JSON.stringify(response?.error ?? response?.result)}`);
  }
  return response?.result;
}

function resultText(result) {
  return (result?.content ?? []).filter((item) => item.type === "text").map((item) => item.text).join("\n");
}

function exactReference(text, role, names) {
  const candidates = [];
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*-\s+([a-z]+)\s+"([^"]+)"[^\n]*\[ref=([^\]\s]+)\]/i);
    if (!match || match[1].toLowerCase() !== role) continue;
    if (names.some((name) => match[2].toLowerCase() === name.toLowerCase())) candidates.push(match[3]);
  }
  return [...new Set(candidates)];
}

function enabledReference(text, role, names) {
  const candidates = [];
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*-\s+([a-z]+)\s+"([^"]+)"[^\n]*\[ref=([^\]\s]+)\]/i);
    if (!match || match[1].toLowerCase() !== role || /\bdisabled\b/i.test(line)) continue;
    if (names.some((name) => match[2].toLowerCase() === name.toLowerCase())) candidates.push(match[3]);
  }
  return [...new Set(candidates)];
}

function nearestClickableReferenceBefore(text, needle) {
  const lines = text.split(/\r?\n/);
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (!/^\s*-\s+/i.test(line) || !line.toLowerCase().includes(needle.toLowerCase())) continue;
    for (let ancestor = index; ancestor >= 0; ancestor -= 1) {
      const clickable = lines[ancestor].match(
        /^\s*-\s+[a-z]+(?:\s+"[^"]+")?[^\n]*\[ref=([^\]\s]+)\][^\n]*\[cursor=pointer\]/i,
      );
      if (clickable) return clickable[1];
    }
  }
  return undefined;
}

const attachmentControlNames = [
  "Add files and more",
  "Attach files",
  "Upload files",
  "Add photos & files",
];
const composerNames = ["Message ChatGPT", "Message", "Chat with ChatGPT", "Ask ChatGPT"];

async function waitForChatReady() {
  const deadline = Date.now() + Math.min(timeoutMs, 30_000);
  let snapshot = "";
  while (Date.now() < deadline) {
    snapshot = resultText(await callTool("browser_snapshot", { depth: 12 }));
    if (/\bLog in\b|\bSign up\b/i.test(snapshot)) {
      throw new Error("Dedicated ChatGPT profile is not authenticated");
    }
    const addRefs = exactReference(snapshot, "button", attachmentControlNames);
    const composerRefs = exactReference(snapshot, "textbox", composerNames);
    if (addRefs.length === 1 && composerRefs.length === 1) return snapshot;
    await callTool("browser_wait_for", { time: 0.5 });
  }
  throw new Error("ChatGPT conversation did not expose one attachment control and one message composer within 30 seconds");
}

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
  const manifestStat = await fs.stat(manifestPath);
  if (!manifestStat.isFile() || manifestStat.size < 2 || manifestStat.size > 1_048_576) {
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
  if (typeof manifest.prompt !== "string" || !manifest.prompt.trim()) throw new Error("Manifest prompt is required");
  if (Buffer.byteLength(manifest.prompt, "utf8") > maxPromptBytes) throw new Error("Manifest prompt exceeds 65536 bytes");
  if (!Array.isArray(manifest.attachments) || manifest.attachments.length < 1 || manifest.attachments.length > maxAttachments) {
    throw new Error(`Manifest must contain 1..${maxAttachments} attachments`);
  }
  const seenPaths = new Set();
  let totalBytes = 0;
  for (const attachment of manifest.attachments) {
    if (!attachment || typeof attachment.path !== "string" || !path.isAbsolute(attachment.path)) {
      throw new Error("Every attachment path must be absolute");
    }
    const resolvedPath = path.resolve(attachment.path);
    if (seenPaths.has(resolvedPath.toLowerCase())) throw new Error("Duplicate attachment path");
    seenPaths.add(resolvedPath.toLowerCase());
    if (typeof attachment.sourceName !== "string" || path.basename(attachment.sourceName) !== attachment.sourceName ||
        attachment.sourceName.length > 240 || path.basename(resolvedPath) !== attachment.sourceName) {
      throw new Error("Attachment sourceName must exactly match the path leaf");
    }
    if (!Number.isSafeInteger(attachment.sizeBytes) || attachment.sizeBytes < 1) throw new Error("Invalid attachment sizeBytes");
    if (!/^[0-9a-f]{64}$/.test(attachment.sha256 ?? "")) throw new Error("Invalid attachment sha256");
    const stat = await fs.stat(resolvedPath);
    if (!stat.isFile() || stat.size !== attachment.sizeBytes) throw new Error(`Attachment size mismatch: ${attachment.sourceName}`);
    if (await sha256File(resolvedPath) !== attachment.sha256) throw new Error(`Attachment SHA-256 mismatch: ${attachment.sourceName}`);
    totalBytes += stat.size;
  }
  if (totalBytes > maxAttachmentBytes) throw new Error("Combined attachment size exceeds 268435456 bytes");
  threadUrl = `https://chatgpt.com/c/${manifest.threadId}`;
}

function marker() {
  return `POST-OFFICE-PLAYWRIGHT-DISPATCH ${manifest.dispatchId}`;
}

async function markerExists() {
  const found = resultText(await callTool("browser_find", { text: marker() }));
  return found.split(/\r?\n/).some((line) =>
    line.includes(marker()) && /\[ref=[^\]\s]+\]/.test(line) &&
    !/^\s*-\s+(?:textbox|button)\b/i.test(line)
  );
}

async function uploadAttachments(snapshot) {
  let addRefs = exactReference(snapshot, "button", attachmentControlNames);
  if (addRefs.length !== 1) {
    const visibleButtonLabels = snapshot.split(/\r?\n/)
      .map((line) => line.match(/^\s*-\s+button\s+"([^"]+)"[^\n]*\[ref=([^\]\s]+)\]/i))
      .filter(Boolean)
      .map((match) => match[1])
      .filter((label) => /\b(add|attach|upload|file|photo)\b/i.test(label))
      .slice(0, 12);
    throw new Error(
      `Expected one attachment control, found ${addRefs.length}; ` +
      `visible file-related buttons: ${JSON.stringify(visibleButtonLabels)}`,
    );
  }
  const uploadNames = ["Upload from computer", "Upload files", "Add photos & files"];
  try {
    await callTool("browser_click", { target: addRefs[0], element: "ChatGPT attachment control" });
  } catch (error) {
    // ChatGPT can reflow the composer after a long conversation hydrates. A
    // Playwright accessibility ref can therefore become unstable during the
    // click even though the control remains visible. First check whether the
    // timed-out click opened the menu; otherwise refresh the snapshot and make
    // exactly one retry against the new ref.
    await callTool("browser_wait_for", { time: 0.5 });
    const afterFailure = resultText(await callTool("browser_snapshot", { depth: 10 }));
    const visibleUploadActions = [
      ...exactReference(afterFailure, "button", uploadNames),
      ...exactReference(afterFailure, "menuitem", uploadNames),
    ];
    if (visibleUploadActions.length === 0) {
      addRefs = exactReference(afterFailure, "button", attachmentControlNames);
      if (addRefs.length !== 1) throw error;
      await callTool("browser_click", { target: addRefs[0], element: "ChatGPT attachment control" });
    }
  }
  await callTool("browser_wait_for", { time: 0.25 });

  const menu = resultText(await callTool("browser_snapshot", { depth: 10 }));
  const uploadRefs = [
    ...exactReference(menu, "button", uploadNames),
    ...exactReference(menu, "menuitem", uploadNames),
  ];
  const uniqueUploadRefs = [...new Set(uploadRefs)];
  if (uniqueUploadRefs.length > 1) throw new Error(`Expected at most one upload menu action, found ${uniqueUploadRefs.length}`);
  if (uniqueUploadRefs.length === 1) {
    await callTool("browser_click", { target: uniqueUploadRefs[0], element: "Upload files from computer" });
  } else {
    const fallbackUploadRefs = [];
    for (const uploadName of uploadNames) {
      const uploadFind = resultText(await callTool("browser_find", { text: uploadName }));
      const clickableReference = nearestClickableReferenceBefore(uploadFind, uploadName);
      if (clickableReference) fallbackUploadRefs.push(clickableReference);
    }
    const uniqueFallbackRefs = [...new Set(fallbackUploadRefs)];
    if (uniqueFallbackRefs.length === 1) {
      await callTool("browser_click", {
        target: uniqueFallbackRefs[0],
        element: "Upload files from computer",
      });
    } else {
    const visibleMenuActions = menu.split(/\r?\n/)
      .map((line) => line.match(/^\s*-\s+(?:button|menuitem)\s+"([^"]+)"[^\n]*\[ref=([^\]\s]+)\]/i))
      .filter(Boolean)
      .map((match) => match[1])
      .filter((label) => /\b(add|attach|upload|file|photo|computer)\b/i.test(label))
      .slice(0, 12);
    throw new Error(
      `Expected one upload menu action, found ${uniqueFallbackRefs.length}; ` +
      `visible file-related actions: ${JSON.stringify(visibleMenuActions)}`,
    );
    }
  }
  await callTool("browser_file_upload", { paths: manifest.attachments.map((item) => path.resolve(item.path)) });
  for (const attachment of manifest.attachments) {
    try {
      await callTool("browser_wait_for", { text: attachment.sourceName });
    } catch {
      const visible = resultText(await callTool("browser_find", { text: attachment.sourceName }));
      if (!visible.toLowerCase().includes(attachment.sourceName.toLowerCase())) {
        throw new Error(`Uploaded attachment did not become visible: ${attachment.sourceName}`);
      }
    }
  }
}

async function closeSession() {
  if (!sessionId) return;
  try {
    await fetch(endpoint, {
      method: "DELETE", headers: { "Mcp-Session-Id": sessionId },
      signal: AbortSignal.timeout(Math.min(timeoutMs, 2_000)),
    });
  } catch {
    // Best effort. Cleanup must not hide the delivery result.
  }
}

try {
  await validateManifest();
  const initialize = await request({
    jsonrpc: "2.0", id: nextId++, method: "initialize",
    params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "post-office-chatgpt-delivery", version: "0.1.0" } },
  });
  if (initialize?.error) throw new Error(JSON.stringify(initialize.error));
  await request({ jsonrpc: "2.0", method: "notifications/initialized", params: {} });

  try {
    await callTool("browser_file_upload", { paths: [] });
  } catch (error) {
    if (!/only be used when there is related modal state present/i.test(String(error?.message ?? error))) {
      throw error;
    }
  }
  const navigation = resultText(await callTool("browser_navigate", { url: threadUrl }));
  if (/\bLog in\b|\bSign up\b/i.test(navigation)) throw new Error("Dedicated ChatGPT profile is not authenticated");
  try {
    await callTool("browser_press_key", { key: "Escape" });
  } catch (error) {
    if (!/File chooser/i.test(String(error?.message ?? error))) throw error;
    await callTool("browser_file_upload", { paths: [] });
    await callTool("browser_press_key", { key: "Escape" });
  }
  await callTool("browser_wait_for", { time: 0.25 });
  let snapshot = await waitForChatReady();
  if (await markerExists()) {
    console.log(JSON.stringify({
      ok: true, endpoint, threadId: manifest.threadId, messageId: manifest.messageId,
      dispatchId: manifest.dispatchId, marker: marker(), attachmentCount: manifest.attachments.length,
      receiptReference: `playwright-chatgpt:${manifest.dispatchId}:${manifest.threadId}`,
      attachments: manifest.attachments.map(({ sourceName, sizeBytes, sha256 }) => ({ sourceName, sizeBytes, sha256 })),
      replayed: true,
    }));
  } else {
    await uploadAttachments(snapshot);
    snapshot = resultText(await callTool("browser_snapshot", { depth: 12 }));
    const composerRefs = exactReference(snapshot, "textbox", composerNames);
    if (composerRefs.length !== 1) throw new Error(`Expected one ChatGPT message composer, found ${composerRefs.length}`);
    const prompt = `${marker()}\n\n${manifest.prompt}`;
    await callTool("browser_type", {
      target: composerRefs[0], element: "ChatGPT message composer", text: prompt, submit: false,
    });
    const sendDeadline = Date.now() + timeoutMs;
    let sendRef;
    while (Date.now() < sendDeadline) {
      const ready = resultText(await callTool("browser_snapshot", { depth: 10 }));
      const sendRefs = enabledReference(ready, "button", ["Send prompt", "Send"]);
      if (sendRefs.length > 1) throw new Error(`Expected at most one enabled send action, found ${sendRefs.length}`);
      if (sendRefs.length === 1) {
        sendRef = sendRefs[0];
        break;
      }
      const fallbackSendRefs = [];
      for (const sendName of ["Send prompt", "Send"]) {
        const sendFind = resultText(await callTool("browser_find", { text: sendName }));
        const clickableReference = nearestClickableReferenceBefore(sendFind, sendName);
        if (clickableReference) fallbackSendRefs.push(clickableReference);
      }
      const uniqueFallbackSendRefs = [...new Set(fallbackSendRefs)];
      if (uniqueFallbackSendRefs.length > 1) {
        throw new Error(`Expected at most one fallback send action, found ${uniqueFallbackSendRefs.length}`);
      }
      if (uniqueFallbackSendRefs.length === 1) {
        sendRef = uniqueFallbackSendRefs[0];
        break;
      }
      await callTool("browser_wait_for", { time: 0.5 });
    }
    if (!sendRef) throw new Error("ChatGPT send action did not become enabled after attachment upload");
    await callTool("browser_click", { target: sendRef, element: "Send ChatGPT delivery" });
    try { await callTool("browser_wait_for", { text: marker() }); } catch { /* exact check below */ }
    if (!await markerExists()) throw new Error("Submitted delivery marker was not observed in the target conversation");
    console.log(JSON.stringify({
      ok: true, endpoint, threadId: manifest.threadId, messageId: manifest.messageId,
      dispatchId: manifest.dispatchId, marker: marker(), attachmentCount: manifest.attachments.length,
      receiptReference: `playwright-chatgpt:${manifest.dispatchId}:${manifest.threadId}`,
      attachments: manifest.attachments.map(({ sourceName, sizeBytes, sha256 }) => ({ sourceName, sizeBytes, sha256 })),
      replayed: false,
    }));
  }
} catch (error) {
  const message = error?.name === "AbortError"
    ? `ChatGPT delivery timed out after ${timeoutMs}ms`
    : String(error?.message ?? error);
  console.error(JSON.stringify({
    ok: false, endpoint, manifestPath, dispatchId: manifest?.dispatchId ?? null,
    threadId: manifest?.threadId ?? null, error: message,
  }));
  process.exitCode = 1;
} finally {
  await closeSession();
}
