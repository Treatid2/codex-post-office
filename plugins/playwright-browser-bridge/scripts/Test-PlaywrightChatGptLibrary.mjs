#!/usr/bin/env node
// SPDX-License-Identifier: MPL-2.0

import assert from "node:assert/strict";
import {
  CollectionError,
  correlateOriginMessages,
  isChromeCollisionFilename,
  messageText,
  publicCollectionFailure,
  selectLibraryCandidates,
} from "./playwright_chatgpt_library.mjs";

const sourceId = "11111111-1111-4111-8111-111111111111";
const originId = "22222222-2222-4222-8222-222222222222";
const threadId = "33333333-3333-4333-8333-333333333333";
const turnId = "44444444-4444-4444-8444-444444444444";
const attachmentName = "RESULT.zip";
const manifest = { attachmentName, expectedBytes: 1234, threadId };
const conversation = {
  messages: [
    {
      id: sourceId,
      author: { role: "user" },
      content: { content_type: "text", parts: ["Review ID: R-1"] },
      metadata: { turn_exchange_id: turnId },
    },
    {
      id: originId,
      author: { role: "assistant" },
      content: { content_type: "text", parts: [`Result [${attachmentName}](sandbox:/mnt/data/${attachmentName})`] },
      metadata: { turn_exchange_id: turnId },
    },
    {
      id: "55555555-5555-4555-8555-555555555555",
      author: { role: "assistant" },
      content: { content_type: "text", parts: [attachmentName] },
      metadata: { turn_exchange_id: "66666666-6666-4666-8666-666666666666" },
    },
  ],
};

assert.equal(messageText(conversation.messages[1]).includes("sandbox:/mnt/data"), true);
const correlated = correlateOriginMessages(conversation, sourceId, attachmentName, ["Review ID: R-1"]);
assert.deepEqual([...correlated.originMessageIds], [originId]);

const library = {
  items: [
    {
      id: "libfile_abc123",
      file_id: "file_def456",
      file_name: attachmentName,
      file_size_bytes: 1234,
      origination_thread_id: threadId,
      origination_message_id: originId,
      trashed_at: null,
    },
    {
      id: "libfile_wrongthread",
      file_id: "file_wrongthread",
      file_name: attachmentName,
      file_size_bytes: 1234,
      origination_thread_id: "77777777-7777-4777-8777-777777777777",
      origination_message_id: originId,
      trashed_at: null,
    },
  ],
};
assert.deepEqual(selectLibraryCandidates(library, manifest, correlated.originMessageIds).map((item) => item.id), [
  "libfile_abc123",
]);

const directConversation = {
  messages: [{
    id: originId,
    author: { role: "assistant" },
    content: { content_type: "text", parts: [`Review ID: R-2\n${attachmentName}`] },
    metadata: {},
  }],
};
assert.deepEqual(
  [...correlateOriginMessages(directConversation, originId, attachmentName, ["Review ID: R-2"]).originMessageIds],
  [originId],
);

assert.throws(
  () => correlateOriginMessages(conversation, "88888888-8888-4888-8888-888888888888", attachmentName),
  (error) => error instanceof CollectionError && error.code === "SOURCE_TURN_CORRELATION_FAILED",
);
assert.equal(
  publicCollectionFailure(new CollectionError("EXACT_CODE", "bounded failure")).errorCode,
  "EXACT_CODE",
);
assert.equal(publicCollectionFailure(new Error("unexpected")).errorCode, "ATTACHMENT_COLLECTION_FAILED");
assert.equal(isChromeCollisionFilename("RESULT.zip", "RESULT.zip"), true);
assert.equal(isChromeCollisionFilename("RESULT(1).zip", "RESULT.zip"), true);
assert.equal(isChromeCollisionFilename("RESULT (12).zip", "RESULT.zip"), true);
assert.equal(isChromeCollisionFilename("OTHER(1).zip", "RESULT.zip"), false);
assert.equal(isChromeCollisionFilename("folder/RESULT(1).zip", "RESULT.zip"), false);

console.log(JSON.stringify({
  ok: true,
  exactSourceCorrelation: true,
  sameTurnCorrelation: true,
  exactLibrarySelection: true,
  failClosedErrors: true,
  chromeCollisionFilenames: true,
}));
