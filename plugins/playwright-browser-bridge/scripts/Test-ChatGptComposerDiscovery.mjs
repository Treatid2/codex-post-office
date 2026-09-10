#!/usr/bin/env node
// SPDX-License-Identifier: MPL-2.0

import assert from "node:assert/strict";
import { discoverComposer, discoverSendAction } from "./chatgpt_composer.mjs";

class Locator {
  constructor(items = []) { this.items = items; }
  count() { return Promise.resolve(this.items.length); }
  nth(index) { return new Locator([this.items[index]]); }
  async isVisible() { return Boolean(this.items[0]?.visible); }
  async isEditable() { return Boolean(this.items[0]?.editable); }
  async isEnabled() { return this.items[0]?.enabled !== false; }
  locator(selector) {
    const item = this.items[0];
    if (selector === "xpath=ancestor::form[1]") return new Locator(item?.form ? [item.form] : []);
    if (selector.includes('type="submit"')) return new Locator(item?.submit ? [item.submit] : []);
    return new Locator([]);
  }
}

class Page {
  constructor({ semantic = [], native = [], editable = [], accessibleSend = [], stableSend = [] }) {
    Object.assign(this, { semantic, native, editable, accessibleSend, stableSend });
  }
  getByRole(role) {
    return new Locator(role === "textbox" ? this.semantic : this.accessibleSend);
  }
  locator(selector) {
    if (selector === "textarea:not([disabled])") return new Locator(this.native);
    if (selector.startsWith('[contenteditable="true"]')) return new Locator(this.editable);
    if (selector === 'button[data-testid="send-button"]') return new Locator(this.stableSend);
    return new Locator([]);
  }
  async waitForTimeout() {}
}

const usable = { visible: true, editable: true, enabled: true };
let page = new Page({ semantic: [usable] });
let found = await discoverComposer(page, 100);
assert.equal(found.strategy, "accessible-textbox");

page = new Page({ native: [usable] });
found = await discoverComposer(page, 100);
assert.equal(found.strategy, "native-textarea");

page = new Page({ editable: [usable] });
found = await discoverComposer(page, 100);
assert.equal(found.strategy, "editable-surface");

const nativeSubmit = { visible: true };
const composerWithForm = new Locator([{ ...usable, form: { submit: nativeSubmit } }]);
let send = await discoverSendAction(new Page({}), composerWithForm, 100);
assert.equal(send.strategy, "composer-form-submit");

send = await discoverSendAction(
  new Page({ accessibleSend: [{ visible: true }] }), new Locator([usable]), 100);
assert.equal(send.strategy, "accessible-send-action");

send = await discoverSendAction(
  new Page({ stableSend: [{ visible: true }] }), new Locator([usable]), 100);
assert.equal(send.strategy, "stable-test-hook");

console.log(JSON.stringify({
  ok: true,
  composerStrategies: ["accessible-textbox", "native-textarea", "editable-surface"],
  sendStrategies: ["composer-form-submit", "accessible-send-action", "stable-test-hook"],
}));
