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
const maxResponseBytes = Number(options.get("--max-response-bytes") ?? 4_194_304);
const maxPendingCalls = Number(options.get("--max-pending-calls") ?? 128);
const maxPendingBytes = Number(options.get("--max-pending-bytes") ?? 8 * 1_048_576);
const maxIngressRequests = Number(options.get("--max-ingress-requests") ?? 32);
const maxIngressBytes = Number(options.get("--max-ingress-bytes") ?? 8 * 1_048_576);
const shutdownToken = String(process.env.PON_BROWSER_BRIDGE_SHUTDOWN_TOKEN ?? "");
delete process.env.PON_BROWSER_BRIDGE_SHUTDOWN_TOKEN;
if (!upstreamEndpoint || !shutdownToken || !Number.isInteger(port) || port < 1024 || port > 65535
    || !Number.isInteger(maxResponseBytes) || maxResponseBytes < 65_536
    || !Number.isInteger(maxPendingCalls) || maxPendingCalls < 1
    || !Number.isInteger(maxPendingBytes) || maxPendingBytes < 1_048_576
    || !Number.isInteger(maxIngressRequests) || maxIngressRequests < 1
    || !Number.isInteger(maxIngressBytes) || maxIngressBytes < 65_536) {
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
const downstreamSessions = new Map();
const maxDownstreamSessions = 64;
const downstreamSessionTtlMs = 30 * 60 * 1_000;
let pendingCalls = 0;
let pendingBytes = 0;
let activeIngressRequests = 0;
let activeIngressBytes = 0;

class BrokerError extends Error {
  constructor(message, { httpStatus = 500, rpcCode = -32603 } = {}) {
    super(message);
    this.httpStatus = httpStatus;
    this.rpcCode = rpcCode;
  }
}

function pruneDownstreamSessions(now = Date.now()) {
  for (const [session, state] of downstreamSessions) {
    if (now - state.lastSeen > downstreamSessionTtlMs) downstreamSessions.delete(session);
  }
}

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

async function readCappedResponse(response) {
  const declared = Number(response.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > maxResponseBytes) {
    await response.body?.cancel();
    throw new BrokerError(`Upstream MCP response exceeds ${maxResponseBytes} bytes`, { httpStatus: 502, rpcCode: -32005 });
  }
  if (!response.body) return "";
  const reader = response.body.getReader();
  const chunks = [];
  let length = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > maxResponseBytes) {
        await reader.cancel();
        throw new BrokerError(`Upstream MCP response exceeds ${maxResponseBytes} bytes`, { httpStatus: 502, rpcCode: -32005 });
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  return Buffer.concat(chunks.map((chunk) => Buffer.from(chunk)), length).toString("utf8");
}

async function upstream(payload, operationTimeoutMs = timeoutMs) {
  const controller = new AbortController();
  const boundedTimeout = Math.max(1, Math.min(timeoutMs, operationTimeoutMs));
  const timer = setTimeout(() => controller.abort(), boundedTimeout);
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
    const body = await readCappedResponse(response);
    if (!response.ok) throw new Error(`Upstream MCP HTTP ${response.status}: ${body.slice(0, 500)}`);
    return decodePayload(body, response.headers.get("content-type") ?? "");
  } finally {
    clearTimeout(timer);
  }
}

function serialize(operation, { bodyBytes, session, deadline }) {
  if (pendingCalls >= maxPendingCalls || pendingBytes + bodyBytes > maxPendingBytes) {
    throw new BrokerError("Browser broker pending-call capacity is exhausted", { httpStatus: 429, rpcCode: -32003 });
  }
  pendingCalls += 1;
  pendingBytes += bodyBytes;
  const admitted = async () => {
    const state = downstreamSessions.get(session);
    if (!state) throw new BrokerError("MCP session retired before its queued call started", { httpStatus: 409, rpcCode: -32004 });
    const remaining = deadline - Date.now();
    if (remaining <= 0) throw new BrokerError("MCP call expired while waiting in the broker queue", { httpStatus: 504, rpcCode: -32000 });
    return operation(remaining);
  };
  const result = queue.then(admitted, admitted);
  queue = result.catch(() => undefined);
  return result.finally(() => {
    pendingCalls -= 1;
    pendingBytes -= bodyBytes;
  });
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

async function route(message, context) {
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
  return serialize(async (remainingMs) => {
    const mappedId = upstreamId++;
    const response = await upstream({ ...message, id: mappedId }, remainingMs);
    return { ...response, id };
  }, context);
}

function sendJson(response, status, value, sessionId) {
  const headers = { "Content-Type": "application/json" };
  if (sessionId) headers["Mcp-Session-Id"] = sessionId;
  response.writeHead(status, headers);
  response.end(value === null ? "" : JSON.stringify(value));
}

async function readRequestBody(request) {
  if (activeIngressRequests >= maxIngressRequests) {
    throw new BrokerError("Browser broker ingress-request capacity is exhausted", { httpStatus: 429, rpcCode: -32006 });
  }
  const declaredText = request.headers["content-length"];
  const declared = declaredText === undefined ? null : Number(declaredText);
  if (declared !== null && (!Number.isInteger(declared) || declared < 0 || declared > 1_048_576)) {
    throw new BrokerError("MCP request exceeds 1 MiB", { httpStatus: 413, rpcCode: -32007 });
  }
  if (declared !== null && activeIngressBytes + declared > maxIngressBytes) {
    throw new BrokerError("Browser broker ingress-byte capacity is exhausted", { httpStatus: 429, rpcCode: -32006 });
  }

  activeIngressRequests += 1;
  let retainedBytes = 0;
  const chunks = [];
  request.setTimeout(timeoutMs, () => {
    request.destroy(new BrokerError("MCP request body timed out", { httpStatus: 408, rpcCode: -32008 }));
  });
  try {
    for await (const chunk of request) {
      if (retainedBytes + chunk.length > 1_048_576) {
        throw new BrokerError("MCP request exceeds 1 MiB", { httpStatus: 413, rpcCode: -32007 });
      }
      if (activeIngressBytes + chunk.length > maxIngressBytes) {
        throw new BrokerError("Browser broker ingress-byte capacity is exhausted", { httpStatus: 429, rpcCode: -32006 });
      }
      retainedBytes += chunk.length;
      activeIngressBytes += chunk.length;
      chunks.push(chunk);
    }
    return { bytes: retainedBytes, text: Buffer.concat(chunks, retainedBytes).toString("utf8") };
  } finally {
    request.setTimeout(0);
    activeIngressRequests -= 1;
    activeIngressBytes -= retainedBytes;
  }
}

const server = http.createServer(async (request, response) => {
  const acceptedAt = Date.now();
  try {
    const requestHost = request.headers.host;
    if (requestHost !== `${host}:${port}` && requestHost !== `localhost:${port}`) {
      return sendJson(response, 421, { error: "Host is not allowlisted" });
    }
    const url = new URL(request.url ?? "/", `http://${host}:${port}`);
    pruneDownstreamSessions();
    if (request.method === "GET" && url.pathname === "/health") {
      return sendJson(response, 200, {
        ok: true,
        upstreamConnected: Boolean(upstreamSessionId),
        downstreamSessions: downstreamSessions.size,
        pendingCalls,
        pendingBytes,
        activeIngressRequests,
        activeIngressBytes,
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

    const body = await readRequestBody(request);
    const message = JSON.parse(body.text);
    if (Array.isArray(message)) return sendJson(response, 400, jsonRpcError(null, -32600, "Batches are not supported"));

    let session = request.headers["mcp-session-id"];
    if (message.method === "initialize") {
      if (downstreamSessions.size >= maxDownstreamSessions) {
        return sendJson(response, 429, jsonRpcError(message.id, -32002, "Too many MCP sessions"));
      }
      session = crypto.randomUUID();
      downstreamSessions.set(session, { lastSeen: Date.now() });
    } else if (typeof session !== "string" || !downstreamSessions.has(session)) {
      return sendJson(response, 404, jsonRpcError(message.id, -32001, "Unknown MCP session"));
    } else {
      downstreamSessions.get(session).lastSeen = Date.now();
    }
    const result = await route(message, {
      bodyBytes: body.bytes,
      session,
      deadline: acceptedAt + timeoutMs,
    });
    if (result === null) {
      response.writeHead(202, { "Mcp-Session-Id": session });
      return response.end();
    }
    return sendJson(response, 200, result, session);
  } catch (error) {
    const status = error instanceof BrokerError ? error.httpStatus : 500;
    const code = error instanceof BrokerError ? error.rpcCode : -32603;
    return sendJson(response, status, jsonRpcError(null, code, String(error?.message ?? error)));
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
