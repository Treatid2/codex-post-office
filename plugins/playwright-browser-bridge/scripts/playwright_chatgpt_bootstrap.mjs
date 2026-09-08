#!/usr/bin/env node
// SPDX-License-Identifier: MPL-2.0

import fs from "node:fs/promises";

const argumentsMap = new Map();
for (let index = 2; index < process.argv.length; index += 1) {
  const argument = process.argv[index];
  if (!argument.startsWith("--")) throw new Error(`Unexpected argument: ${argument}`);
  const [key, inlineValue] = argument.split("=", 2);
  if (inlineValue !== undefined) {
    argumentsMap.set(key, inlineValue);
  } else if (process.argv[index + 1] && !process.argv[index + 1].startsWith("--")) {
    argumentsMap.set(key, process.argv[index + 1]);
    index += 1;
  } else {
    argumentsMap.set(key, true);
  }
}

const endpoint = String(argumentsMap.get("--endpoint") ?? "http://127.0.0.1:8931/mcp");
const timeoutMs = Number(argumentsMap.get("--timeout-ms") ?? 30_000);
const keepOpen = argumentsMap.has("--keep-open");
const sessionStatePath = argumentsMap.get("--session-state");
const target = "https://chatgpt.com/";
let sessionId;
let nextId = 1;
let preserveSession = false;

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
    throw new Error(JSON.stringify(response?.error ?? response?.result));
  }
  return response?.result;
}

function resultText(result) {
  return (result?.content ?? [])
    .filter((item) => item.type === "text")
    .map((item) => item.text)
    .join("\n");
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
    // Best effort. Session cleanup must not hide the activation result.
  }
}

async function closeNamedSession(id) {
  if (!id) return;
  try {
    await fetch(endpoint, {
      method: "DELETE",
      headers: { "Mcp-Session-Id": id },
      signal: AbortSignal.timeout(Math.min(timeoutMs, 2_000)),
    });
  } catch {
    // A previous retained authentication session is best-effort cleanup only.
  }
}

try {
  const initialize = await request({
    jsonrpc: "2.0",
    id: nextId++,
    method: "initialize",
    params: {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "post-office-chatgpt-bootstrap", version: "0.1.0" },
    },
  });
  if (initialize?.error) throw new Error(JSON.stringify(initialize.error));
  await request({ jsonrpc: "2.0", method: "notifications/initialized", params: {} });

  await callTool("browser_navigate", { url: target });
  await callTool("browser_wait_for", { time: 1 });
  const snapshot = resultText(await callTool("browser_snapshot", { depth: 6 }));
  const tabs = resultText(await callTool("browser_tabs", { action: "list" }));

  const authenticationRequired = /\bLog in\b|\bSign up\b/i.test(snapshot);
  const authenticated = !authenticationRequired &&
    (/\bNew chat\b|Message ChatGPT|Start a new chat/i.test(snapshot));
  const pageUrl = tabs.match(/\]\((https:\/\/[^)]+)\)/)?.[1] ?? target;
  const state = authenticationRequired
    ? "AUTHENTICATION_REQUIRED"
    : authenticated
      ? "AUTHENTICATED"
      : "UNKNOWN";
  preserveSession = keepOpen && state === "AUTHENTICATION_REQUIRED";

  let previousSessionId;
  if (typeof sessionStatePath === "string" && sessionStatePath) {
    try {
      previousSessionId = JSON.parse(await fs.readFile(sessionStatePath, "utf8")).sessionId;
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    if (previousSessionId && previousSessionId !== sessionId) {
      await closeNamedSession(previousSessionId);
    }
    if (preserveSession) {
      await fs.writeFile(
        sessionStatePath,
        JSON.stringify({ sessionId }) + "\n",
        { mode: 0o600 },
      );
    } else {
      await fs.rm(sessionStatePath, { force: true });
    }
  }

  console.log(JSON.stringify({
    ok: true,
    endpoint,
    target,
    pageUrl,
    state,
    headedSignInRequired: state !== "AUTHENTICATED",
    authenticationTabRetained: preserveSession,
  }));
  if (state === "UNKNOWN") process.exitCode = 2;
} catch (error) {
  const message = error?.name === "AbortError"
    ? `ChatGPT bootstrap timed out after ${timeoutMs}ms`
    : String(error?.message ?? error);
  console.error(JSON.stringify({ ok: false, endpoint, target, error: message }));
  process.exitCode = 1;
} finally {
  if (!preserveSession) await closeSession();
}
