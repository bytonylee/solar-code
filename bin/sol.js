#!/usr/bin/env node
"use strict";

const { spawnSync } = require("node:child_process");
const { resolve } = require("node:path");

function main() {
  const launcher = resolve(__dirname, "sol");
  const args = process.argv.slice(2);
  const candidates = process.platform === "win32"
    ? [
        { command: "py", args: ["-3"] },
        { command: "python", args: [] },
        { command: "python3", args: [] },
      ]
    : [
        { command: "python3", args: [] },
        { command: "python", args: [] },
      ];

  for (const candidate of candidates) {
    const result = spawnSync(
      candidate.command,
      [...candidate.args, launcher, ...args],
      { stdio: "inherit" },
    );
    if (result.error?.code === "ENOENT") {
      continue;
    }
    if (result.error) {
      console.error(`sol could not start Python: ${result.error.message}`);
      process.exit(1);
    }
    if (result.signal) {
      process.exit(1);
    }
    process.exit(result.status ?? 1);
  }

  console.error(
    "sol requires Python 3.10+; install Python and run sol again.",
  );
  process.exit(1);
}

main();
