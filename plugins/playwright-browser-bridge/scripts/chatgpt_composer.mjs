// SPDX-License-Identifier: MPL-2.0

const pollIntervalMs = 250;

async function firstUsable(locator) {
  const count = await locator.count();
  for (let index = 0; index < count; index += 1) {
    const candidate = locator.nth(index);
    if (await candidate.isVisible().catch(() => false) &&
        await candidate.isEditable().catch(() => false) &&
        await candidate.isEnabled().catch(() => false)) {
      return candidate;
    }
  }
  return null;
}

/**
 * Discover the composer by browser-exposed capabilities. Accessible textbox
 * semantics are authoritative; native/editable elements are bounded fallbacks.
 * Placeholder text is deliberately irrelevant because it is localized and has
 * repeatedly changed independently of the control's behavior.
 */
export async function discoverComposer(page, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const semantic = await firstUsable(page.getByRole("textbox"));
    if (semantic) return { locator: semantic, strategy: "accessible-textbox" };

    const native = await firstUsable(page.locator("textarea:not([disabled])"));
    if (native) return { locator: native, strategy: "native-textarea" };

    const editable = await firstUsable(page.locator(
      '[contenteditable="true"][role="textbox"], [contenteditable="true"]'));
    if (editable) return { locator: editable, strategy: "editable-surface" };

    await page.waitForTimeout(pollIntervalMs);
  }
  throw new Error("ChatGPT did not expose an enabled editable textbox capability");
}

async function firstVisible(locator) {
  const count = await locator.count();
  for (let index = 0; index < count; index += 1) {
    const candidate = locator.nth(index);
    if (await candidate.isVisible().catch(() => false)) return candidate;
  }
  return null;
}

/** Locate the submit action in the composer's own form before global fallbacks. */
export async function discoverSendAction(page, composer, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const form = composer.locator("xpath=ancestor::form[1]");
    if (await form.count()) {
      const nativeSubmit = await firstVisible(form.locator(
        'button[type="submit"], input[type="submit"]'));
      if (nativeSubmit) return { locator: nativeSubmit, strategy: "composer-form-submit" };
    }

    const accessible = await firstVisible(page.getByRole("button", { name: /send/i }));
    if (accessible) return { locator: accessible, strategy: "accessible-send-action" };

    const stableHook = await firstVisible(page.locator('button[data-testid="send-button"]'));
    if (stableHook) return { locator: stableHook, strategy: "stable-test-hook" };

    await page.waitForTimeout(pollIntervalMs);
  }
  throw new Error("ChatGPT did not expose a submit action for the discovered composer");
}

