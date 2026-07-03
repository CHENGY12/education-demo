#!/usr/bin/env node

const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");

function read(file) {
  return fs.readFileSync(path.join(root, file), "utf8");
}

function fail(message) {
  console.error(`FAIL ${message}`);
  process.exitCode = 1;
}

function pass(message) {
  console.log(`PASS ${message}`);
}

function assert(condition, message) {
  if (condition) pass(message);
  else fail(message);
}

const html = read("index.html");
const css = read("styles.css");
const app = read("app.js");
const v2Html = read("skill-tree-v2.html");
const v2Css = read("skill-tree-v2.css");
const v2App = read("skill-tree-v2.js");
const data = JSON.parse(read("knowledge-tree.json"));

const requiredIds = [
  "treeTitle",
  "treeSubtitle",
  "searchInput",
  "fileInput",
  "fitButton",
  "resetButton",
  "zoomInButton",
  "zoomOutButton",
  "domainFilters",
  "progressArc",
  "progressPercent",
  "progressLabel",
  "currentLabel",
  "treeStage",
  "treeWorld",
  "connectionLayer",
  "nodeLayer",
  "detailPanel",
  "emptyDetailTemplate"
];

for (const id of requiredIds) {
  assert(html.includes(`id="${id}"`), `HTML contains #${id}`);
}

assert(html.includes('href="styles.css"'), "HTML links styles.css");
assert(html.includes('src="app.js"'), "HTML loads app.js");
assert(css.includes(".skill-node"), "CSS defines skill nodes");
assert(css.includes(".connection-layer path"), "CSS defines dependency connections");
assert(app.includes('fetch("knowledge-tree.json"'), "App fetches offline JSON data");

const v2RequiredIds = [
  "v2Title",
  "v2Subtitle",
  "v2Search",
  "v2Reset",
  "v2Progress",
  "v2Meter",
  "v2ProgressText",
  "v2AvailableCount",
  "v2LockedCount",
  "v2Domains",
  "v2Viewport",
  "v2World",
  "v2TierLayer",
  "v2ConnectionLayer",
  "v2NodeLayer",
  "v2Detail",
  "v2EmptyDetail"
];

for (const id of v2RequiredIds) {
  assert(v2Html.includes(`id="${id}"`), `V2 HTML contains #${id}`);
}

assert(v2Html.includes('href="skill-tree-v2.css"'), "V2 HTML links skill-tree-v2.css");
assert(v2Html.includes('src="skill-tree-v2.js"'), "V2 HTML loads skill-tree-v2.js");
assert(v2Css.includes(".v2-tier-band"), "V2 CSS defines visible tier bands");
assert(v2Css.includes(".v2-node-orb"), "V2 CSS defines orb skill nodes");
assert(v2Css.includes(".v2-connection-layer path"), "V2 CSS defines dependency lines");
assert(v2App.includes('fetch("knowledge-tree.json"'), "V2 app fetches offline JSON data");
assert(v2App.includes("v2ScrollToFoundation"), "V2 app scrolls to the foundation layer");

const nodes = Array.isArray(data.nodes) ? data.nodes : [];
assert(nodes.length >= 8, "Knowledge tree has enough demo nodes");

const ids = new Set();
for (const node of nodes) {
  assert(typeof node.id === "string" && node.id.length > 0, `Node has id: ${node.title || node.id}`);
  assert(!ids.has(node.id), `Node id is unique: ${node.id}`);
  ids.add(node.id);
}

let edgeCount = 0;
for (const node of nodes) {
  const dependencies = Array.isArray(node.dependencies) ? node.dependencies : [];
  edgeCount += dependencies.length;
  assert(typeof node.title === "string" && node.title.length > 0, `Node ${node.id} has title`);
  assert(Number.isFinite(Number(node.level)), `Node ${node.id} has numeric level`);
  assert(Number.isFinite(Number(node.lane)), `Node ${node.id} has numeric lane`);
  for (const dependency of dependencies) {
    assert(ids.has(dependency), `Dependency ${dependency} exists for ${node.id}`);
    assert(dependency !== node.id, `Node ${node.id} does not depend on itself`);
  }
}
assert(edgeCount >= nodes.length - 2, "Tree has visible dependency links");

const byId = new Map(nodes.map((node) => [node.id, node]));
const visiting = new Set();
const visited = new Set();

function visit(id, stack = []) {
  if (visiting.has(id)) {
    fail(`Cycle detected: ${[...stack, id].join(" -> ")}`);
    return;
  }
  if (visited.has(id)) return;
  visiting.add(id);
  const node = byId.get(id);
  for (const dependency of node.dependencies || []) {
    visit(dependency, [...stack, id]);
  }
  visiting.delete(id);
  visited.add(id);
}

for (const node of nodes) visit(node.id);
assert(process.exitCode !== 1, "Dependency graph is acyclic");

const occupied = new Set();
for (const node of nodes) {
  const key = `${node.level}:${node.lane}`;
  assert(!occupied.has(key), `Layout slot is unique for ${node.id}`);
  occupied.add(key);
}

const completed = new Set(nodes.filter((node) => node.completed).map((node) => node.id));
function getState(node) {
  if (completed.has(node.id)) return "mastered";
  const dependencies = (node.dependencies || []).filter((id) => byId.has(id));
  return dependencies.every((id) => completed.has(id)) ? "available" : "locked";
}

for (const node of nodes) {
  if (!completed.has(node.id)) continue;
  const missing = (node.dependencies || []).filter((id) => !completed.has(id));
  assert(missing.length === 0, `Completed node ${node.id} has completed prerequisites`);
}

const available = nodes.filter((node) => getState(node) === "available");
const locked = nodes.filter((node) => getState(node) === "locked");
const mastered = nodes.filter((node) => getState(node) === "mastered");
assert(mastered.length > 0, "Default state has mastered nodes");
assert(available.length > 0, "Default state has learnable nodes");
assert(locked.length > 0, "Default state has locked nodes");

const maxLevelNode = nodes.reduce((best, node) => (Number(node.level) > Number(best.level) ? node : best), nodes[0]);
assert((maxLevelNode.dependencies || []).length > 0, "Final demo node has prerequisites");

const minLevel = Math.min(...nodes.map((node) => Number(node.level)));
const maxLevel = Math.max(...nodes.map((node) => Number(node.level)));
assert(minLevel === 0, "Tree starts at foundation level 0");
assert(maxLevel > minLevel, "Tree has multiple vertical tiers");

if (process.exitCode === 1) {
  process.exit(1);
}

console.log(`OK ${nodes.length} nodes, ${edgeCount} links, ${available.length} learnable nodes`);
