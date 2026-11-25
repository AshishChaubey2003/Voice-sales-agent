// Real-Chromium check of the demo page against a running runner.
// Not part of the default pytest or CI run: it needs Playwright and Chromium.
//
// Prerequisites: Node, Playwright with a Chromium build (not installed by this repo), and the
// stub server, which uses the real runner and auth gate with a stub bot (no external calls):
//   1. .\.venv313\Scripts\python.exe tests\js\stub_runner.py        (prints URL and token)
//   2. In a second terminal, set the two printed values and run this file:
//        PowerShell: $env:VOICE_E2E_URL="..."; $env:VOICE_E2E_TOKEN="..."; node tests/js/voice-client.browser.mjs
//        bash:       VOICE_E2E_URL=... VOICE_E2E_TOKEN=... node tests/js/voice-client.browser.mjs
// If Playwright is not resolvable from this folder, set PLAYWRIGHT_ENTRY to its entry file.
//
// Pass criteria: the demo page reports "signalling-complete" (the real SDP answer was
// applied in Chromium). Exit 0 on pass, 1 on fail, 2 when the environment is incomplete.
const url = process.env.VOICE_E2E_URL;
const token = process.env.VOICE_E2E_TOKEN;
if (!url || !token) {
  console.error("Set VOICE_E2E_URL and VOICE_E2E_TOKEN to run the browser check.");
  process.exit(2);
}

const { chromium } = await import(process.env.PLAYWRIGHT_ENTRY || "playwright");
const browser = await chromium.launch({
  headless: true,
  args: [
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
  ],
});

let exitCode = 1;
try {
  const page = await browser.newPage();
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });

  await page.goto(`${url.replace(/\/+$/, "")}/voice-client/demo.html`);
  await page.fill("#token", token);
  await page.click("#connect");

  const status = page.locator("#status");
  await status.waitFor({ timeout: 60000 });
  await page.waitForFunction(
    () => ["signalling-complete", "error"].includes(document.getElementById("status").dataset.state),
    null,
    { timeout: 60000 },
  );
  const state = await status.getAttribute("data-state");
  const text = await status.textContent();
  console.log(JSON.stringify({ state, text, consoleErrorCount: consoleErrors.length }));
  exitCode = state === "signalling-complete" ? 0 : 1;
} finally {
  await browser.close();
}
process.exit(exitCode);
