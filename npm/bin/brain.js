#!/usr/bin/env node
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const PACKAGE = process.env.BRAIN_PACKAGE || "ai-computer-brain";
const windows = process.platform === "win32";
const toolBin = process.env.UV_TOOL_BIN_DIR || path.join(os.homedir(), ".local", "bin");
const exe = (name) => path.join(toolBin, windows ? `${name}.exe` : name);

function say(message) {
  process.stderr.write(`[brain] ${message}\n`);
}

function run(command, args, options = {}) {
  return spawnSync(command, args, { stdio: "inherit", ...options });
}

function onPath(command) {
  const probe = spawnSync(command, ["--version"], { stdio: "ignore" });
  return !probe.error && probe.status === 0;
}

function uvCommand() {
  if (onPath("uv")) return "uv";
  if (fs.existsSync(exe("uv"))) return exe("uv");
  say("installing uv, the Python manager (one time)");
  const result = windows
    ? run("powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "irm https://astral.sh/uv/install.ps1 | iex"])
    : run("sh", ["-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"]);
  if (result.status !== 0 || !fs.existsSync(exe("uv"))) {
    say("uv could not be installed; see https://docs.astral.sh/uv/getting-started/installation/");
    return null;
  }
  return exe("uv");
}

function installBrain() {
  const uv = uvCommand();
  if (!uv) return false;
  say("installing AI Computer Brain (Python 3.12 is downloaded if needed)");
  const result = run(uv, ["tool", "install", "--python", "3.12", "--force", PACKAGE]);
  if (result.status !== 0) return false;
  run(uv, ["tool", "update-shell"], { stdio: "ignore" });
  return true;
}

function brainCommand() {
  if (fs.existsSync(exe("brain"))) return exe("brain");
  return onPath("brain") ? "brain" : null;
}

const args = process.argv.slice(2);
if (args[0] === "--install-only") {
  if (!brainCommand() && !installBrain()) say("the brain was not installed yet; run `brain` once to retry");
  process.exit(0);
}
if (args[0] === "--update") {
  process.exit(installBrain() ? 0 : 1);
}
let brain = brainCommand();
if (!brain && installBrain()) brain = brainCommand();
if (!brain) {
  say("brain is not installed; see https://github.com/Cognio-so/AI_computer_brain#quick-install");
  process.exit(1);
}
const env = { ...process.env, PATH: `${toolBin}${path.delimiter}${process.env.PATH || ""}` };
const result = run(brain, args, { env });
process.exit(result.status === null ? 1 : result.status);
