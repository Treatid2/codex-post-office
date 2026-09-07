#!/usr/bin/env node
// SPDX-License-Identifier: MPL-2.0

const argumentsMap = new Map();
for (let index = 2; index < process.argv.length; index += 1) {
  const argument = process.argv[index];
  if (!argument.startsWith("--")) {
    throw new Error(`Unexpected argument: ${argument}`);
  }
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
const timeoutMs = Number(argumentsMap.get("--timeout-ms") ?? 10_000);
const browserCheck = argumentsMap.has("--browser-check");
let sessionId;
let nextId = 1;

function decodePayload(responseText, contentType) {
  if (!responseText.trim()) return null;
  if (!contentType.includes("text/event-stream")) return JSON.parse(responseText);

  const messages = [];
  let dataLines = [];
  for (const line of responseText.split(/\r?\n/)) {
    if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    } else if (line === "" && dataLines.length) {
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
    if (!response.ok) {
      throw new Error(`MCP HTTP ${response.status}: ${body.slice(0, 500)}`);
    }
    return decodePayload(body, response.headers.get("content-type") ?? "");
  } finally {
    clearTimeout(timeout);
  }
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
    // Session cleanup is best effort and must not hide the probe result.
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
      clientInfo: { name: "post-office-next-probe", version: "0.1.0" },
    },
  });
  if (initialize?.error) throw new Error(JSON.stringify(initialize.error));

  await request({ jsonrpc: "2.0", method: "notifications/initialized", params: {} });
  const toolsResponse = await request({
    jsonrpc: "2.0",
    id: nextId++,
    method: "tools/list",
    params: {},
  });
  if (toolsResponse?.error) throw new Error(JSON.stringify(toolsResponse.error));
  const tools = toolsResponse?.result?.tools ?? [];
  const toolNames = tools.map((tool) => tool.name).sort();
  const forbiddenTools = ["browser_evaluate", "browser_network_request", "browser_run_code_unsafe"];
  const exposedForbiddenTools = forbiddenTools.filter((name) => toolNames.includes(name));
  if (exposedForbiddenTools.length) {
    throw new Error(`Broker exposed forbidden tools: ${exposedForbiddenTools.join(", ")}`);
  }
  const blockedResponse = await request({
    jsonrpc: "2.0",
    id: nextId++,
    method: "tools/call",
    params: { name: "browser_run_code_unsafe", arguments: {} },
  });
  if (blockedResponse?.error?.code !== -32602) {
    throw new Error("Broker did not reject a direct call to browser_run_code_unsafe");
  }

  let browserCheckResult = null;
  if (browserCheck) {
    if (!toolNames.includes("browser_tabs")) {
      throw new Error("Playwright MCP did not expose browser_tabs");
    }
    const tabsResponse = await request({
      jsonrpc: "2.0",
      id: nextId++,
      method: "tools/call",
      params: { name: "browser_tabs", arguments: { action: "list" } },
    });
    if (tabsResponse?.error || tabsResponse?.result?.isError) {
      throw new Error(JSON.stringify(tabsResponse?.error ?? tabsResponse?.result));
    }
    browserCheckResult = { ok: true, tool: "browser_tabs" };
  }

  console.log(JSON.stringify({
    ok: true,
    endpoint,
    protocolVersion: initialize?.result?.protocolVersion ?? null,
    serverInfo: initialize?.result?.serverInfo ?? null,
    toolCount: toolNames.length,
    tools: toolNames,
    securityCheck: { ok: true, blockedTool: "browser_run_code_unsafe" },
    browserCheck: browserCheckResult,
  }));
} catch (error) {
  const message = error?.name === "AbortError"
    ? `MCP probe timed out after ${timeoutMs}ms`
    : String(error?.message ?? error);
  console.error(JSON.stringify({ ok: false, endpoint, error: message }));
  process.exitCode = 1;
} finally {
  await closeSession();
}
