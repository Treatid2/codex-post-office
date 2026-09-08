#!/usr/bin/env node
// SPDX-License-Identifier: MPL-2.0

import http from "node:http";

const index = process.argv.indexOf("--port");
const port = Number(process.argv[index + 1]);
if (!Number.isInteger(port)) throw new Error("--port is required");
const server = http.createServer((_request, response) => {
  response.writeHead(503, { "Content-Type": "application/json" });
  response.end('{"testStub":true}');
});
server.listen(port, "127.0.0.1");
process.on("SIGTERM", () => server.close(() => process.exit(0)));
process.on("SIGINT", () => server.close(() => process.exit(0)));

