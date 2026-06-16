const fs = require("fs");
const { createCipheriv } = require("crypto");
const { devices, chromium } = require("playwright-chromium");
const Utils = require("./utils");
const iPhone11 = devices["iPhone 11 Pro"];

function resolveBrowserExecutable() {
  const candidates = [
    process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    process.env.CHROME_PATH,
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  ].filter(Boolean);

  return candidates.find((candidate) => fs.existsSync(candidate));
}

class Signer {
  userAgent =
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/98.0.4758.109 Safari/537.36";
  args = [
    "--disable-blink-features",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--window-size=1920,1080",
    "--start-maximized",
  ];
  // Default TikTok loading page
  default_url = "https://www.tiktok.com/@rihanna?lang=en";

  // Password for xttparams AES encryption
  password = "webapp1.0+202106";

  constructor(default_url, userAgent, browser) {
    if (default_url) {
      this.default_url = default_url;
    }

    // Use the provided userAgent or the default one
    this.userAgent = userAgent || this.userAgent;

    if (browser) {
      this.browser = browser;
      this.isExternalBrowser = true;
    }

    this.args.push(`--user-agent="${this.userAgent}"`);

    this.options = {
      headless: true,
      args: this.args,
      ignoreDefaultArgs: ["--mute-audio", "--hide-scrollbars"],
    };

    const executablePath = resolveBrowserExecutable();
    if (executablePath) {
      this.options.executablePath = executablePath;
    }
  }

  async init() {
    if (!this.browser) {
      this.browser = await chromium.launch(this.options);
    }

    // Deterministic desktop context so every signature comes from the SAME
    // device profile, matching the desktop User-Agent used for the request.
    // Randomizing these made each publish look like a brand-new device.
    let emulateTemplate = {
      ...iPhone11,
      locale: "en-US",
      deviceScaleFactor: 2,
      isMobile: false,
      hasTouch: false,
      userAgent: this.userAgent,
    };
    emulateTemplate.viewport = { width: 1280, height: 800 };

    this.context = await this.browser.newContext({
      bypassCSP: true,
      ignoreHTTPSErrors: true,
      ...emulateTemplate,
    });

    this.page = await this.context.newPage();

    await this.page.route("**/*", (route) => {
      return route.request().resourceType() === "script"
        ? route.abort()
        : route.continue();
    });

    await this.page.goto(this.default_url, {
      waitUntil: "networkidle",
    });

    const LOAD_SCRIPTS = ["signer.js", "webmssdk.js", "xbogus.js"];
    for (const script of LOAD_SCRIPTS) {
      await this.page.addScriptTag({
        path: `${__dirname}/javascript/${script}`,
      });
      // console.log("[+] " + script + " loaded");
    }

    await this.page.evaluate(() => {
      const originalGenerateBogus = window.generateBogus;

      window.generateSignature = function generateSignature(url) {
        if (typeof window.byted_acrawler.sign !== "function") {
          throw "No signature function found";
        }
        return window.byted_acrawler.sign({ url: url });
      };

      window.generateBogus = function generateBogus(params, userAgent) {
        if (typeof originalGenerateBogus !== "function") {
          throw "No X-Bogus function found";
        }
        return originalGenerateBogus(params, userAgent);
      };
      return this;
    });
  }

  async navigator() {
    // Get the "viewport" of the page, as reported by the page.
    const info = await this.page.evaluate(() => {
      return {
        deviceScaleFactor: window.devicePixelRatio,
        user_agent: window.navigator.userAgent,
        browser_language: window.navigator.language,
        browser_platform: window.navigator.platform,
        browser_name: window.navigator.appCodeName,
        browser_version: window.navigator.appVersion,
      };
    });
    return info;
  }
  async sign(link, verifyFp) {
    // Reuse a stable per-account verifyFp when provided, otherwise generate one.
    let verify_fp = verifyFp || Utils.generateVerifyFp();
    let newUrl = link + "&verifyFp=" + verify_fp;
    let token = await this.page.evaluate(`generateSignature("${newUrl}")`);
    let signed_url = newUrl + "&_signature=" + token;
    let queryString = new URL(signed_url).searchParams.toString();
    let bogus = await this.page.evaluate(`generateBogus("${queryString}","${this.userAgent}")`);
    signed_url += "&X-Bogus=" + bogus;


    return {
      signature: token,
      verify_fp: verify_fp,
      signed_url: signed_url,
      "x-tt-params": this.xttparams(queryString),
      "x-bogus": bogus,
    };
  }

  xttparams(query_str) {
    query_str += "&is_encryption=1";
    // Encrypt query string using aes-128-cbc
    const cipher = createCipheriv("aes-128-cbc", this.password, this.password);
    return Buffer.concat([cipher.update(query_str), cipher.final()]).toString(
      "base64"
    );
  }

  async close() {
    if (this.browser && !this.isExternalBrowser) {
      await this.browser.close();
      this.browser = null;
    }
    if (this.page) {
      this.page = null;
    }
  }
}

module.exports = Signer;
