// Lets `node --test tests/hooks/` work on every Node version. Node 20 runs the test files of a directory argument; Node
// 22 loads the directory as a module instead, so this entry point imports the suites itself.
const { readdirSync } = require("node:fs");

(async () => {
  for (const name of readdirSync(__dirname).sort()) {
    if (name.endsWith(".test.mjs")) await import(`./${name}`);
  }
})();
