// SPDX-License-Identifier: MPL-2.0

const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const libraryFilePattern = /^libfile_[A-Za-z0-9]+$/;
const filePattern = /^file_[A-Za-z0-9]+$/;

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function isChromeCollisionFilename(observedFilename, expectedFilename) {
  if (observedFilename === expectedFilename) return true;
  if (typeof observedFilename !== "string" || pathBasename(observedFilename) !== observedFilename) return false;
  const extensionIndex = expectedFilename.lastIndexOf(".");
  const extension = extensionIndex > 0 ? expectedFilename.slice(extensionIndex) : "";
  const stem = extension ? expectedFilename.slice(0, -extension.length) : expectedFilename;
  const collisionPattern = new RegExp(
    `^${escapeRegExp(stem)} ?\\([1-9]\\d*\\)${escapeRegExp(extension)}$`,
  );
  return collisionPattern.test(observedFilename);
}

function pathBasename(value) {
  return value.split(/[\\/]/).at(-1);
}

export class CollectionError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "CollectionError";
    this.code = code;
    this.details = details;
  }
}

export function messageText(message) {
  if (!message || typeof message !== "object") return "";
  const content = message.content;
  if (!content || typeof content !== "object") return "";
  const values = [];
  if (typeof content.text === "string") values.push(content.text);
  if (Array.isArray(content.parts)) {
    for (const part of content.parts) {
      if (typeof part === "string") values.push(part);
      else if (part && typeof part === "object" && typeof part.text === "string") values.push(part.text);
    }
  }
  return values.join("\n");
}

export function conversationMessages(payload) {
  if (!payload || typeof payload !== "object" || !Array.isArray(payload.messages)) {
    throw new CollectionError(
      "CONVERSATION_METADATA_UNAVAILABLE",
      "ChatGPT conversation metadata did not contain a message list",
    );
  }
  return payload.messages.filter((message) => message && typeof message === "object");
}

function stableTurnKeys(message) {
  const metadata = message?.metadata;
  if (!metadata || typeof metadata !== "object") return [];
  return [metadata.turn_exchange_id, metadata.working_turn_id]
    .filter((value) => typeof value === "string" && uuidPattern.test(value));
}

export function correlateOriginMessages(payload, sourceTurnId, attachmentName, requiredText = []) {
  const messages = conversationMessages(payload);
  const sourceMatches = messages.filter((message) => message.id === sourceTurnId);
  if (sourceMatches.length !== 1) {
    throw new CollectionError(
      "SOURCE_TURN_CORRELATION_FAILED",
      `Expected one manifest-bound source message, found ${sourceMatches.length}`,
      { sourceTurnId },
    );
  }
  const source = sourceMatches[0];
  const sourceKeys = new Set(stableTurnKeys(source));
  const correlated = messages.filter((message) => {
    if (message.id === sourceTurnId) return true;
    if (message?.author?.role !== "assistant" || sourceKeys.size === 0) return false;
    return stableTurnKeys(message).some((value) => sourceKeys.has(value));
  });
  const attachmentMessages = correlated.filter((message) => {
    const text = messageText(message);
    return text.includes(attachmentName) && requiredText.every((marker) => {
      return text.includes(marker) || messageText(source).includes(marker);
    });
  });
  if (attachmentMessages.length < 1 || attachmentMessages.length > 8) {
    throw new CollectionError(
      "ATTACHMENT_ORIGIN_CORRELATION_FAILED",
      `Expected 1..8 attachment-bearing messages in the exact source turn, found ${attachmentMessages.length}`,
      { sourceTurnId },
    );
  }
  return {
    sourceMessage: source,
    originMessageIds: new Set(attachmentMessages.map((message) => message.id)),
  };
}

export function selectLibraryCandidates(payload, manifest, originMessageIds) {
  if (!payload || typeof payload !== "object" || !Array.isArray(payload.items)) {
    throw new CollectionError(
      "ATTACHMENT_LIBRARY_UNAVAILABLE",
      "ChatGPT Library metadata did not contain an item list",
    );
  }
  const matches = payload.items.filter((item) => {
    if (!item || typeof item !== "object") return false;
    return item.file_name === manifest.attachmentName &&
      item.file_size_bytes === manifest.expectedBytes &&
      item.origination_thread_id === manifest.threadId &&
      originMessageIds.has(item.origination_message_id) &&
      libraryFilePattern.test(item.id ?? "") &&
      filePattern.test(item.file_id ?? "") &&
      item.trashed_at == null;
  });
  if (matches.length > 8) {
    throw new CollectionError(
      "ATTACHMENT_LIBRARY_AMBIGUOUS",
      `ChatGPT Library returned ${matches.length} manifest-correlated candidates`,
    );
  }
  return matches;
}

export function publicCollectionFailure(error) {
  if (error instanceof CollectionError) {
    return { errorCode: error.code, error: error.message, details: error.details };
  }
  return {
    errorCode: "ATTACHMENT_COLLECTION_FAILED",
    error: String(error?.message ?? error),
    details: {},
  };
}
