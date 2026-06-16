// Browser.js
const Signer = require("./index");

var url = process.argv[2];
var userAgent = process.argv[3];
var verifyFp = process.argv[4]; // optional: reuse a stable per-account verifyFp

(async function main() {
  let signer;
  try {
    signer = new Signer(url, userAgent);
    await signer.init();

    const sign = await signer.sign(url, verifyFp);
    const navigator = await signer.navigator();

    let output = JSON.stringify({
      status: "ok",
      data: {
        ...sign,
        navigator: navigator,
      },
    });
    console.log(output);
  } catch (err) {
    console.error(err);
    process.exitCode = 1;
  } finally {
    if (signer) {
      await signer.close();
    }
  }
})();
