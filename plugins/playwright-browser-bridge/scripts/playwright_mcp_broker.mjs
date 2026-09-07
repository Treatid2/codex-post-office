#!/usr/bin/env node
// SPDX-License-Identifier: MPL-2.0

import http from "node:http";
import crypto from "node:crypto";

const options = new Map();
for (let index = 2; index < process.argv.length; index += 1) {
  const key = process.argv[index];
  const value = process.argv[index + 1];
  if (!key.startsWith("--") || !value || value.startsWith("--")) {
    throw new Error(`Expected --name value, received: ${key}`);
  }
  options.set(key, value);
  index += 1;
}

const host = "127.0.0.1";
const port = Number(options.get("--listen-port") ?? 8931);
const upstreamEndpoint = String(options.get("--upstream"));
const timeoutMs = Number(options.get("--timeout-ms") ?? 120_000);
const shutdownToken = String(options.get("--shutdown-token") ?? "");
if (!upstreamEndpoint || !shutdownToken || !Number.isInteger(port) || port < 1024 || port > 65535) {
  throw new Error("A valid --listen-port, --upstream endpoint, and --shutdown-token are required.");
}

const allowedTools = new Set([
  "browser_click",
  "browser_close",
  "browser_console_messages",
  "browser_file_upload",
  "browser_fill_form",
  "browser_find",
  "browser_handle_dialog",
  "browser_hover",
  "browser_navigate",
  "browser_navigate_back",
  "browser_network_requests",
  "browser_press_key",
  "browser_select_option",
  "browser_snapshot",
  "browser_tabs",
  "browser_type",
  "browser_wait_for",
]);

let upstreamSessionId;
let upstreamId = 1;
let initializeResult;
let tools = [];
let queue = Promise.resolve();
const downstreamSessions = new Set();

function decodePayload(text, contentType) {
  if (!text.trim()) return null;
  if (!contentType.includes("text/event-stream")) return JSON.parse(text);
  const messages = [];
  let data = [];
  for (const line of text.split(/\r?\n/)) {
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    else if (line === "" && data.length) {
      messages.push(JSON.parse(data.join("\n")));
      data = [];
    }
  }
  if (data.length) messages.push(JSON.parse(data.join("\n")));
  return messages.at(-1) ?? null;
}

async function upstream(payload) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = {
      Accept: "application/json, text/event-stream",
      "Content-Type": "application/json",
    };
    if (upstreamSessionId) headers["Mcp-Session-Id"] = upstreamSessionId;
    const response = await fetch(upstreamEndpoint, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    upstreamSessionId = response.headers.get("mcp-session-id") ?? upstreamSessionId;
    const body = await response.text();
    if (!response.ok) throw new Error(`Upstream MCP HTTP ${response.status}: ${body.slice(0, 500)}`);
    return decodePayload(body, response.headers.get("content-type") ?? "");
  } finally {
    clearTimeout(timer);
  }
}

function serialize(operation) {
  const result = queue.then(operation, operation);
  queue = result.catch(() => undefined);
  return result;
}

function jsonRpcError(id, code, message) {
  return { jsonrpc: "2.0", id: id ?? null, error: { code, message } };
}

async function initializeUpstream() {
  const initialized = await upstream({
    jsonrpc: "2.0",
    id: upstreamId++,
    method: "initialize",
    params: {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "post-office-next-browser-broker", version: "0.1.0" },
    },
  });
  if (initialized?.error) throw new Error(JSON.stringify(initialized.error));
  initializeResult = initialized.result;
  await upstream({ jsonrpc: "2.0", method: "notifications/initialized", params: {} });
  const listed = await upstream({ jsonrpc: "2.0", id: upstreamId++, method: "tools/list", params: {} });
  if (listed?.error) throw new Error(JSON.stringify(listed.error));
  tools = (listed?.result?.tools ?? []).filter((tool) => allowedTools.has(tool.name));
  const missing = [...allowedTools].filter((name) => !tools.some((tool) => tool.name === name));
  if (missing.length) throw new Error(`Upstream Playwright MCP is missing tools: ${missing.join(", ")}`);
}

async function route(message) {
  const id = message?.id;
  if (message?.jsonrpc !== "2.0" || typeof message?.method !== "string") {
    return jsonRpcError(id, -32600, "Invalid JSON-RPC request");
  }
  if (message.method === "initialize") {
    return { jsonrpc: "2.0", id, result: initializeResult };
  }
  if (message.method.startsWith("notifications/")) return null;
  if (message.method === "ping") return { jsonrpc: "2.0", id, result: {} };
  if (message.method === "tools/list") return { jsonrpc: "2.0", id, result: { tools } };
  if (message.method !== "tools/call") {
    return jsonRpcError(id, -32601, `Method not exposed by browser broker: ${message.method}`);
  }
  const toolName = message?.params?.name;
  if (!allowedTools.has(toolName)) {
    return jsonRpcError(id, -32602, `Browser tool is not allowlisted: ${toolName}`);
  }
  return serialize(async () => {
    const mappedId = upstreamId++;
    const response = await upstream({ ...message, id: mappedId });
    return { ...response, id };
  });
}

function sendJson(response, status, value, sessionId) {
  const headers = { "Content-Type": "application/json" };
  if (sessionId) headers["Mcp-Session-Id"] = sessionId;
  response.writeHead(status, headers);
  response.end(value === null ? "" : JSON.stringify(value));
}

const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url ?? "/", `http://${host}:${port}`);
    if (request.method === "GET" && url.pathname === "/health") {
      return sendJson(response, 200, {
        ok: true,
        upstreamConnected: Boolean(upstreamSessionId),
        downstreamSessions: downstreamSessions.size,
        toolCount: tools.length,
      });
    }
    if (request.method === "POST" && url.pathname === "/shutdown") {
      if (request.headers["x-bridge-shutdown"] !== shutdownToken) {
        return sendJson(response, 403, { error: "Forbidden" });
      }
      sendJson(response, 202, { ok: true });
      setImmediate(() => shutdown().finally(() => process.exit(0)));
      return;
    }
    if (url.pathname !== "/mcp") return sendJson(response, 404, { error: "Not found" });
    if (request.method === "DELETE") {
      const session = request.headers["mcp-session-id"];
      if (typeof session === "string") downstreamSessions.delete(session);
      response.writeHead(204);
      return response.end();
    }
    if (request.method !== "POST") return sendJson(response, 405, { error: "Method not allowed" });

    const chunks = [];
    let length = 0;
    for await (const chunk of request) {
      length += chunk.length;
      if (length > 1_048_576) throw new Error("MCP request exceeds 1 MiB");
      chunks.push(chunk);
    }
    const message = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    if (Array.isArray(message)) return sendJson(response, 400, jsonRpcError(null, -32600, "Batches are not supported"));

    let session = request.headers["mcp-session-id"];
    if (message.method === "initialize") {
      session = crypto.randomUUID();
      downstreamSessions.add(session);
    } else if (typeof session !== "string" || !downstreamSessions.has(session)) {
      return sendJson(response, 404, jsonRpcError(message.id, -32001, "Unknown MCP session"));
    }
    const result = await route(message);
    if (result === null) {
      response.writeHead(202, { "Mcp-Session-Id": session });
      return response.end();
    }
    return sendJson(response, 200, result, session);
  } catch (error) {
    return sendJson(response, 500, jsonRpcError(null, -32603, String(error?.message ?? error)));
  }
});

async function shutdown() {
  if (server.listening) server.close();
  if (upstreamSessionId) {
    try {
      await fetch(upstreamEndpoint, {
        method: "DELETE",
        headers: { "Mcp-Session-Id": upstreamSessionId },
        signal: AbortSignal.timeout(2_000),
      });
    } catch {
      // Best effort during process shutdown.
    }
  }
}

process.on("SIGINT", () => shutdown().finally(() => process.exit(0)));
process.on("SIGTERM", () => shutdown().finally(() => process.exit(0)));

await initializeUpstream();
server.listen(port, host);
