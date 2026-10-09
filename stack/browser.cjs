const { chromium } = require("playwright");
const fs = require("node:fs");

(async () => {
  const port = Number(process.env.CHROME_PORT || 9223);
  const browser = await chromium.launch({
    headless: true,
    chromiumSandbox: true,
    args: ["--remote-debugging-port=" + port, "--remote-debugging-address=127.0.0.1"],
  });
  const metadata = await (await fetch("http://127.0.0.1:" + port + "/json/version")).json();
  fs.writeFileSync(process.env.BROWSER_READY_FILE || "browser-ready.json", JSON.stringify({
    endpoint: metadata.webSocketDebuggerUrl,
  }));
  browser.on("disconnected", () => process.exit(0));
  process.on("SIGTERM", async () => {
    await browser.close();
    process.exit(0);
  });
})();
