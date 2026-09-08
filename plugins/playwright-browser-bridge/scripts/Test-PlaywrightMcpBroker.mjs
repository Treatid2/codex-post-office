#!/usr/bin/env node
// SPDX-License-Identifier: MPL-2.0

import assert from "node:assert/strict";
import http from "node:http";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { fileURLToPath } from "node:url";
import path from "node:path";

const allowedTools = [
  "browser_click", "browser_close", "browser_console_messages", "browser_file_upload",
  "browser_fill_form", "browser_find", "browser_handle_dialog", "browser_hover",
  "browser_navigate", "browser_navigate_back", "browser_network_requests", "browser_press_key",
  "browser_select_option", "browser_snapshot", "browser_tabs", "browser_type", "browser_wait_for",
];

async function listen(server) {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return server.address().port;
}

function readJson(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      try { resolve(JSON.parse(Buffer.concat(chunks).toString("utf8"))); }
      catch (error) { reject(error); }
    });
    request.on("error", reject);
  });
}

const upstreamCalls = [];
const upstreamServer = http.createServer(async (request, response) => {
  if (request.method === "DELETE") {
    response.writeHead(204);
    return response.end();
  }
  const message = await readJson(request);
  if (message.method === "tools/call") upstreamCalls.push(message.params.name);
  if (message.params?.name === "browser_wait_for") await new Promise((resolve) => setTimeout(resolve, 250));
  let result = {};
  if (message.method === "initialize") result = { protocolVersion: "2025-06-18", capabilities: {}, serverInfo: { name: "fake", version: "1" } };
  if (message.method === "tools/list") result = { tools: allowedTools.map((name) => ({ name, inputSchema: { type: "object" } })) };
  if (message.params?.name === "browser_snapshot") result = { content: [{ type: "text", text: "x".repeat(80_000) }] };
  const body = JSON.stringify({ jsonrpc: "2.0", id: message.id, result });
  response.writeHead(200, { "Content-Type": "application/json", "Mcp-Session-Id": "upstream-test", "Content-Length": Buffer.byteLength(body) });
  response.end(body);
});

const upstreamPort = await listen(upstreamServer);
const reserve = http.createServer();
const brokerPort = await listen(reserve);
await new Promise((resolve) => reserve.close(resolve));
const script = path.join(path.dirname(fileURLToPath(import.meta.url)), "playwright_mcp_broker.mjs");
const shutdownToken = "test-shutdown-token";
const broker = spawn(process.execPath, [
  script, "--listen-port", String(brokerPort), "--upstream", `http://127.0.0.1:${upstreamPort}/mcp`,
  "--timeout-ms", "2000", "--max-response-bytes", "65536", "--max-pending-calls", "2",
  "--max-pending-bytes", "1048576", "--max-ingress-requests", "2", "--max-ingress-bytes", "65536",
], {
  env: { ...process.env, PON_BROWSER_BRIDGE_SHUTDOWN_TOKEN: shutdownToken },
  stdio: ["ignore", "pipe", "pipe"],
});

const endpoint = `http://127.0.0.1:${brokerPort}`;
async function request(pathname, { method = "GET", host = `127.0.0.1:${brokerPort}`, session, message, shutdown } = {}) {
  const headers = { Host: host };
  if (session) headers["Mcp-Session-Id"] = session;
  if (message) headers["Content-Type"] = "application/json";
  if (shutdown) headers["X-Bridge-Shutdown"] = shutdown;
  const response = await fetch(endpoint + pathname, { method, headers, body: message ? JSON.stringify(message) : undefined });
  const text = await response.text();
  return { status: response.status, session: response.headers.get("mcp-session-id"), body: text ? JSON.parse(text) : null };
}

async function initialize() {
  const response = await request("/mcp", { method: "POST", message: { jsonrpc: "2.0", id: 1, method: "initialize", params: {} } });
  assert.equal(response.status, 200);
  return response.session;
}

function requestWithRawHost(host, message) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify(message);
    const outgoing = http.request({
      hostname: "127.0.0.1", port: brokerPort, path: "/mcp", method: "POST",
      headers: { Host: host, "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => resolve({ status: response.statusCode, body: JSON.parse(Buffer.concat(chunks).toString("utf8")) }));
    });
    outgoing.on("error", reject);
    outgoing.end(body);
  });
}

function openPartialBody(bytes) {
  const outgoing = http.request({
    hostname: "127.0.0.1", port: brokerPort, path: "/mcp", method: "POST",
    headers: { Host: `127.0.0.1:${brokerPort}`, "Content-Type": "application/json", "Transfer-Encoding": "chunked" },
  });
  const response = new Promise((resolve) => {
    outgoing.on("response", (incoming) => {
      const chunks = [];
      incoming.on("data", (chunk) => chunks.push(chunk));
      incoming.on("end", () => resolve({ status: incoming.statusCode, body: Buffer.concat(chunks).toString("utf8") }));
    });
    outgoing.on("error", (error) => resolve({ status: 0, body: String(error.message) }));
  });
  outgoing.write(Buffer.alloc(bytes, 0x20));
  return { outgoing, response };
}

function requestDeclaredBody(bytes) {
  return new Promise((resolve, reject) => {
    const outgoing = http.request({
      hostname: "127.0.0.1", port: brokerPort, path: "/mcp", method: "POST",
      headers: { Host: `127.0.0.1:${brokerPort}`, "Content-Type": "application/json", "Content-Length": bytes },
    }, (incoming) => {
      const chunks = [];
      incoming.on("data", (chunk) => chunks.push(chunk));
      incoming.on("end", () => resolve({ status: incoming.statusCode, body: JSON.parse(Buffer.concat(chunks).toString("utf8")) }));
    });
    outgoing.on("error", reject);
    outgoing.end(Buffer.alloc(bytes, 0x20));
  });
}

try {
  let health;
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      health = await request("/health");
      if (health.status === 200) break;
    } catch { /* startup */ }
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  assert.equal(health?.status, 200);

  const wrongHost = await requestWithRawHost(
    `evil.example:${brokerPort}`, { jsonrpc: "2.0", id: 2, method: "initialize", params: {} },
  );
  assert.equal(wrongHost.status, 421);
  assert.equal((await request("/health")).body.downstreamSessions, 0);

  const partial = openPartialBody(40_000);
  for (let attempt = 0; attempt < 40; attempt += 1) {
    health = await request("/health");
    if (health.body.activeIngressBytes >= 40_000) break;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.equal(health.body.activeIngressRequests, 1);
  assert.equal(health.body.activeIngressBytes, 40_000);
  const ingressRefused = await requestDeclaredBody(40_000);
  assert.equal(ingressRefused.status, 429);
  partial.outgoing.destroy();
  await partial.response;
  for (let attempt = 0; attempt < 40; attempt += 1) {
    health = await request("/health");
    if (health.body.activeIngressRequests === 0) break;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.equal(health.body.activeIngressRequests, 0);
  assert.equal(health.body.activeIngressBytes, 0);

  const session = await initialize();
  const slow = (id) => request("/mcp", {
    method: "POST", session,
    message: { jsonrpc: "2.0", id, method: "tools/call", params: { name: "browser_wait_for", arguments: {} } },
  });
  const first = slow(10);
  const second = slow(11);
  await new Promise((resolve) => setTimeout(resolve, 30));
  const saturated = await slow(12);
  assert.equal(saturated.status, 429);
  await Promise.all([first, second]);
  const recovered = await request("/mcp", {
    method: "POST", session,
    message: { jsonrpc: "2.0", id: 13, method: "tools/call", params: { name: "browser_tabs", arguments: {} } },
  });
  assert.equal(recovered.status, 200);

  const retiredSession = await initialize();
  const started = request("/mcp", {
    method: "POST", session: retiredSession,
    message: { jsonrpc: "2.0", id: 20, method: "tools/call", params: { name: "browser_wait_for", arguments: {} } },
  });
  const queued = request("/mcp", {
    method: "POST", session: retiredSession,
    message: { jsonrpc: "2.0", id: 21, method: "tools/call", params: { name: "browser_click", arguments: {} } },
  });
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.equal((await request("/mcp", { method: "DELETE", session: retiredSession })).status, 204);
  await started;
  const retired = await queued;
  assert.equal(retired.status, 409);
  assert.equal(upstreamCalls.filter((name) => name === "browser_click").length, 0);

  const oversized = await request("/mcp", {
    method: "POST", session,
    message: { jsonrpc: "2.0", id: 30, method: "tools/call", params: { name: "browser_snapshot", arguments: {} } },
  });
  assert.equal(oversized.status, 502);
  assert.equal((await request("/health")).status, 200);

  console.log(JSON.stringify({ ok: true, wrongHost: true, ingressBounded: true, saturationRecovery: true, retiredQueue: true, responseCap: true }));
} finally {
  try { await request("/shutdown", { method: "POST", shutdown: shutdownToken }); } catch { broker.kill("SIGKILL"); }
  await Promise.race([once(broker, "exit"), new Promise((resolve) => setTimeout(resolve, 2_000))]);
  if (broker.exitCode === null) broker.kill("SIGKILL");
  await new Promise((resolve) => upstreamServer.close(resolve));
}

