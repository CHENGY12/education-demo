const v2Dom = {
  title: document.getElementById("v2Title"),
  subtitle: document.getElementById("v2Subtitle"),
  search: document.getElementById("v2Search"),
  reset: document.getElementById("v2Reset"),
  progress: document.getElementById("v2Progress"),
  progressText: document.getElementById("v2ProgressText"),
  meter: document.getElementById("v2Meter"),
  availableCount: document.getElementById("v2AvailableCount"),
  lockedCount: document.getElementById("v2LockedCount"),
  domains: document.getElementById("v2Domains"),
  viewport: document.getElementById("v2Viewport"),
  world: document.getElementById("v2World"),
  tiers: document.getElementById("v2TierLayer"),
  connections: document.getElementById("v2ConnectionLayer"),
  nodes: document.getElementById("v2NodeLayer"),
  detail: document.getElementById("v2Detail"),
  emptyDetail: document.getElementById("v2EmptyDetail")
};

const v2Palette = ["#39d5d8", "#ffd166", "#70d36b", "#f06f7f", "#a889ff", "#65a8ff", "#f49b4d"];
const v2Glyphs = {
  "基础": "基",
  "数据": "数",
  "工程": "工",
  "模型": "模",
  "应用": "用",
  "进阶": "阶",
  "产出": "冠"
};

const v2State = {
  data: null,
  nodes: [],
  byId: new Map(),
  positions: new Map(),
  completed: new Set(),
  domains: new Map(),
  activeDomain: "all",
  query: "",
  selectedId: null,
  storageKey: "",
  maxLevel: 0,
  worldWidth: 1240,
  worldHeight: 1040,
  hasInitialScroll: false
};

v2Init();

async function v2Init() {
  v2BindEvents();

  try {
    const response = await fetch("knowledge-tree.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    v2LoadTree(await response.json());
  } catch (error) {
    v2ShowToast("无法读取 knowledge-tree.json");
  }
}

function v2BindEvents() {
  v2Dom.search.addEventListener("input", () => {
    v2State.query = v2Dom.search.value.trim().toLowerCase();
    v2RenderMap();
  });

  v2Dom.reset.addEventListener("click", () => {
    v2State.completed = new Set(v2State.nodes.filter((node) => node.completed).map((node) => node.id));
    v2SaveProgress();
    v2PickSelection();
    v2RenderAll();
    v2ShowToast("进度已重置");
  });
}

function v2LoadTree(raw) {
  const data = v2Normalize(raw);
  v2State.data = data;
  v2State.nodes = data.nodes;
  v2State.byId = new Map(data.nodes.map((node) => [node.id, node]));
  v2State.maxLevel = Math.max(...data.nodes.map((node) => node.level), 0);
  v2State.domains = v2AssignDomainColors(data.nodes);
  v2State.storageKey = `knowledge-skill-tree-v2:${v2Slug(`${data.title}-${data.version}`)}`;
  v2State.completed = v2LoadProgress() || new Set(data.nodes.filter((node) => node.completed).map((node) => node.id));
  v2State.positions = v2CalculateLayout(data.nodes);
  v2PickSelection();
  v2RenderAll();

  requestAnimationFrame(() => {
    v2ScrollToFoundation();
  });
}

function v2Normalize(raw) {
  const source = raw && typeof raw === "object" ? raw : { nodes: [] };
  const nodes = (Array.isArray(source.nodes) ? source.nodes : []).map((node, index) => ({
    id: String(node.id || `node-${index + 1}`),
    title: String(node.title || node.name || `知识点 ${index + 1}`),
    domain: String(node.domain || node.category || "通用"),
    level: Number.isFinite(Number(node.level)) ? Number(node.level) : 0,
    lane: Number.isFinite(Number(node.lane)) ? Number(node.lane) : index,
    difficulty: v2Clamp(Number(node.difficulty || 3), 1, 5),
    mastery: v2Clamp(Number(node.mastery || 0), 0, 100),
    estimatedHours: Math.max(0, Number(node.estimatedHours || node.hours || 0)),
    completed: Boolean(node.completed || node.defaultCompleted || node.status === "completed"),
    dependencies: Array.isArray(node.dependencies) ? node.dependencies.map(String) : [],
    description: String(node.description || node.summary || "暂无描述。"),
    tags: Array.isArray(node.tags) ? node.tags.map(String) : [],
    resources: Array.isArray(node.resources) ? node.resources : []
  }));

  return {
    title: String(source.title || "知识技能树"),
    subtitle: String(source.subtitle || source.description || "Learning path map"),
    version: String(source.version || "local"),
    nodes
  };
}

function v2AssignDomainColors(nodes) {
  const colors = new Map();
  [...new Set(nodes.map((node) => node.domain))].forEach((domain, index) => {
    colors.set(domain, v2Palette[index % v2Palette.length]);
  });
  return colors;
}

function v2CalculateLayout(nodes) {
  const positions = new Map();
  const byLevel = new Map();
  const tierGap = 148;
  const topMargin = 102;
  const sideMargin = 150;
  const levels = [...new Set(nodes.map((node) => node.level))].sort((a, b) => a - b);
  const largestTier = Math.max(...levels.map((level) => nodes.filter((node) => node.level === level).length), 1);

  v2State.worldWidth = Math.max(1040, sideMargin * 2 + largestTier * 190);
  v2State.worldHeight = Math.max(980, topMargin * 2 + (v2State.maxLevel + 1) * tierGap);

  nodes.forEach((node) => {
    if (!byLevel.has(node.level)) byLevel.set(node.level, []);
    byLevel.get(node.level).push(node);
  });

  for (const level of levels) {
    const tierNodes = byLevel.get(level).sort((a, b) => a.lane - b.lane || a.title.localeCompare(b.title));
    const y = topMargin + (v2State.maxLevel - level) * tierGap;
    const gap = Math.min(215, (v2State.worldWidth - sideMargin * 2) / Math.max(tierNodes.length, 1));
    const start = v2State.worldWidth / 2 - ((tierNodes.length - 1) * gap) / 2;

    tierNodes.forEach((node, index) => {
      positions.set(node.id, { x: start + index * gap, y });
    });
  }

  return positions;
}

function v2RenderAll() {
  v2Dom.title.textContent = v2State.data.title;
  v2Dom.subtitle.textContent = v2State.data.subtitle;
  v2RenderProgress();
  v2RenderDomains();
  v2RenderMap();
  v2RenderDetail();
}

function v2RenderProgress() {
  const total = v2State.nodes.length;
  const done = v2State.nodes.filter((node) => v2State.completed.has(node.id)).length;
  const available = v2State.nodes.filter((node) => v2GetNodeState(node) === "ready").length;
  const locked = v2State.nodes.filter((node) => v2GetNodeState(node) === "locked").length;
  const percent = total ? Math.round((done / total) * 100) : 0;

  v2Dom.progress.textContent = `${percent}%`;
  v2Dom.progressText.textContent = `${done} / ${total} 已掌握`;
  v2Dom.meter.style.width = `${percent}%`;
  v2Dom.availableCount.textContent = String(available);
  v2Dom.lockedCount.textContent = String(locked);
}

function v2RenderDomains() {
  v2Dom.domains.replaceChildren();
  v2Dom.domains.appendChild(v2DomainButton("all", "全部", v2State.nodes.length, "var(--gold)"));

  for (const [domain, color] of v2State.domains.entries()) {
    const count = v2State.nodes.filter((node) => node.domain === domain).length;
    v2Dom.domains.appendChild(v2DomainButton(domain, domain, count, color));
  }
}

function v2DomainButton(value, label, count, color) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `v2-domain-button${v2State.activeDomain === value ? " active" : ""}`;
  button.style.setProperty("--domain-color", color);

  const dot = document.createElement("i");
  const text = document.createElement("span");
  const number = document.createElement("small");
  text.textContent = label;
  number.textContent = String(count);
  button.append(dot, text, number);
  button.addEventListener("click", () => {
    v2State.activeDomain = value;
    v2RenderDomains();
    v2RenderMap();
  });

  return button;
}

function v2RenderMap() {
  v2Dom.world.style.width = `${v2State.worldWidth}px`;
  v2Dom.world.style.height = `${v2State.worldHeight}px`;
  v2Dom.connections.setAttribute("width", String(v2State.worldWidth));
  v2Dom.connections.setAttribute("height", String(v2State.worldHeight));
  v2Dom.connections.setAttribute("viewBox", `0 0 ${v2State.worldWidth} ${v2State.worldHeight}`);

  v2RenderTiers();
  v2RenderConnections();
  v2RenderNodes();
}

function v2RenderTiers() {
  v2Dom.tiers.replaceChildren();

  for (let level = 0; level <= v2State.maxLevel; level += 1) {
    const sample = v2State.nodes.find((node) => node.level === level);
    const y = 102 + (v2State.maxLevel - level) * 148;
    const band = document.createElement("div");
    band.className = "v2-tier-band";
    band.style.top = `${y - 50}px`;
    band.style.setProperty("--tier-color", sample ? v2State.domains.get(sample.domain) : "var(--cyan)");

    const label = document.createElement("div");
    label.className = "v2-tier-label";
    const strong = document.createElement("strong");
    const sub = document.createElement("span");
    strong.textContent = `Tier ${String(level + 1).padStart(2, "0")}`;
    sub.textContent = v2GetTierLabel(level);
    label.append(strong, sub);
    band.appendChild(label);
    v2Dom.tiers.appendChild(band);
  }
}

function v2RenderConnections() {
  v2Dom.connections.replaceChildren();

  for (const target of v2State.nodes) {
    for (const dependencyId of target.dependencies) {
      const source = v2State.byId.get(dependencyId);
      if (!source) continue;

      const from = v2State.positions.get(source.id);
      const to = v2State.positions.get(target.id);
      if (!from || !to) continue;

      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      const sourceY = from.y - 42;
      const targetY = to.y + 42;
      const midY = sourceY - Math.max(42, (sourceY - targetY) * 0.45);
      path.setAttribute("d", `M ${from.x} ${sourceY} C ${from.x} ${midY}, ${to.x} ${midY}, ${to.x} ${targetY}`);

      const classes = [v2GetEdgeState(source, target)];
      if (v2State.selectedId === source.id || v2State.selectedId === target.id) classes.push("active");
      if (!v2Matches(source) || !v2Matches(target)) classes.push("dimmed");
      path.setAttribute("class", classes.filter(Boolean).join(" "));
      v2Dom.connections.appendChild(path);
    }
  }
}

function v2RenderNodes() {
  v2Dom.nodes.replaceChildren();

  for (const node of v2State.nodes) {
    const position = v2State.positions.get(node.id);
    if (!position) continue;

    const status = v2GetNodeState(node);
    const wrapper = document.createElement("div");
    wrapper.className = `v2-node ${status}${v2State.selectedId === node.id ? " selected" : ""}${v2Matches(node) ? "" : " dimmed"}`;
    wrapper.style.left = `${position.x}px`;
    wrapper.style.top = `${position.y}px`;
    wrapper.style.setProperty("--domain-color", v2State.domains.get(node.domain));

    const button = document.createElement("button");
    button.type = "button";
    button.className = "v2-node-button";
    button.setAttribute("aria-label", `${node.title}，${v2StatusLabel(status)}`);
    button.addEventListener("click", () => {
      v2State.selectedId = node.id;
      v2RenderMap();
      v2RenderDetail();
    });

    const orb = document.createElement("span");
    orb.className = "v2-node-orb";
    const glyph = document.createElement("span");
    glyph.className = "v2-node-glyph";
    glyph.textContent = v2Glyphs[node.domain] || Array.from(node.domain).slice(0, 1).join("") || "技";
    orb.appendChild(glyph);

    const label = document.createElement("span");
    label.className = "v2-node-label";
    const title = document.createElement("span");
    title.className = "v2-node-title";
    title.textContent = node.title;
    const meta = document.createElement("span");
    meta.className = "v2-node-meta";
    meta.textContent = `Lv ${node.level + 1} · 难度 ${node.difficulty}`;
    label.append(title, meta);

    button.append(orb, label);
    wrapper.appendChild(button);
    v2Dom.nodes.appendChild(wrapper);
  }
}

function v2RenderDetail() {
  v2Dom.detail.replaceChildren();
  const node = v2State.byId.get(v2State.selectedId);
  if (!node) {
    v2Dom.detail.appendChild(v2Dom.emptyDetail.content.cloneNode(true));
    return;
  }

  const status = v2GetNodeState(node);
  const fragment = document.createDocumentFragment();
  const top = document.createElement("div");
  top.className = "v2-detail-top";
  const token = document.createElement("span");
  token.className = "v2-token";
  token.style.setProperty("--domain-color", v2State.domains.get(node.domain));
  token.textContent = node.domain;
  const statusText = document.createElement("span");
  statusText.className = "v2-status";
  statusText.textContent = v2StatusLabel(status);
  top.append(token, statusText);

  const title = document.createElement("h2");
  title.textContent = node.title;

  const description = document.createElement("p");
  description.textContent = node.description;

  const grid = document.createElement("div");
  grid.className = "v2-detail-grid";
  grid.append(
    v2Metric("等级", `Tier ${node.level + 1}`),
    v2Metric("难度", `${node.difficulty} / 5`),
    v2Metric("预计", node.estimatedHours ? `${node.estimatedHours}h` : "-"),
    v2Metric("依赖", String(node.dependencies.length))
  );

  fragment.append(top, title, description, grid);
  if (node.tags.length) fragment.appendChild(v2TagSection("标签", node.tags));
  if (node.dependencies.length) fragment.appendChild(v2DependencySection("前置技能", node.dependencies));

  const unlocks = v2State.nodes.filter((candidate) => candidate.dependencies.includes(node.id)).map((candidate) => candidate.id);
  if (unlocks.length) fragment.appendChild(v2DependencySection("解锁后续", unlocks));

  const action = document.createElement("button");
  action.type = "button";
  action.className = "v2-primary";
  action.disabled = status === "locked";
  action.textContent = status === "done" ? "取消掌握" : status === "ready" ? "点亮技能" : "前置未完成";
  action.addEventListener("click", () => {
    if (v2State.completed.has(node.id)) {
      v2State.completed.delete(node.id);
      v2RemoveInvalidDescendants();
    } else {
      v2State.completed.add(node.id);
    }
    v2SaveProgress();
    v2RenderAll();
  });
  fragment.appendChild(action);

  v2Dom.detail.appendChild(fragment);
}

function v2Metric(label, value) {
  const metric = document.createElement("div");
  metric.className = "v2-metric";
  const key = document.createElement("span");
  key.textContent = label;
  const val = document.createElement("strong");
  val.textContent = value;
  metric.append(key, val);
  return metric;
}

function v2TagSection(title, tags) {
  const section = document.createElement("section");
  section.className = "v2-section";
  const heading = document.createElement("h3");
  heading.textContent = title;
  const list = document.createElement("div");
  list.className = "v2-tags";
  tags.forEach((tag) => {
    const item = document.createElement("span");
    item.textContent = tag;
    list.appendChild(item);
  });
  section.append(heading, list);
  return section;
}

function v2DependencySection(title, ids) {
  const section = document.createElement("section");
  section.className = "v2-section";
  const heading = document.createElement("h3");
  heading.textContent = title;
  const list = document.createElement("div");
  list.className = "v2-deps";

  ids.forEach((id) => {
    const node = v2State.byId.get(id);
    if (!node) return;

    const button = document.createElement("button");
    button.type = "button";
    button.className = v2State.completed.has(id) ? "done" : "missing";
    button.textContent = node.title;
    button.addEventListener("click", () => {
      v2State.selectedId = id;
      v2RenderMap();
      v2RenderDetail();
      v2ScrollToNode(id);
    });
    list.appendChild(button);
  });

  section.append(heading, list);
  return section;
}

function v2GetNodeState(node) {
  if (v2State.completed.has(node.id)) return "done";
  const dependencies = node.dependencies.filter((id) => v2State.byId.has(id));
  return dependencies.every((id) => v2State.completed.has(id)) ? "ready" : "locked";
}

function v2GetEdgeState(source, target) {
  if (v2State.completed.has(source.id) && v2State.completed.has(target.id)) return "done";
  if (v2State.completed.has(source.id) && v2GetNodeState(target) === "ready") return "ready";
  return "";
}

function v2StatusLabel(status) {
  if (status === "done") return "已掌握";
  if (status === "ready") return "可学习";
  return "未解锁";
}

function v2Matches(node) {
  if (v2State.activeDomain !== "all" && node.domain !== v2State.activeDomain) return false;
  if (!v2State.query) return true;
  const haystack = [node.title, node.domain, node.description, ...node.tags].join(" ").toLowerCase();
  return haystack.includes(v2State.query);
}

function v2PickSelection() {
  const ready = v2State.nodes.find((node) => v2GetNodeState(node) === "ready");
  v2State.selectedId = (ready || v2State.nodes[0] || {}).id || null;
}

function v2RemoveInvalidDescendants() {
  let changed = true;
  while (changed) {
    changed = false;
    for (const node of v2State.nodes) {
      if (!v2State.completed.has(node.id)) continue;
      if (node.dependencies.some((id) => !v2State.completed.has(id))) {
        v2State.completed.delete(node.id);
        changed = true;
      }
    }
  }
}

function v2GetTierLabel(level) {
  if (level === 0) return "基础层";
  if (level === v2State.maxLevel) return "终局产出";
  if (level >= v2State.maxLevel - 1) return "高阶组合";
  if (level >= 3) return "专业路线";
  return "能力进阶";
}

function v2ScrollToFoundation() {
  if (v2State.hasInitialScroll) return;
  v2State.hasInitialScroll = true;
  v2Dom.viewport.scrollTop = Math.max(0, v2State.worldHeight - v2Dom.viewport.clientHeight);
  v2Dom.viewport.scrollLeft = Math.max(0, (v2State.worldWidth - v2Dom.viewport.clientWidth) / 2);
}

function v2ScrollToNode(id) {
  const position = v2State.positions.get(id);
  if (!position) return;
  v2Dom.viewport.scrollTo({
    left: Math.max(0, position.x - v2Dom.viewport.clientWidth / 2),
    top: Math.max(0, position.y - v2Dom.viewport.clientHeight / 2),
    behavior: "smooth"
  });
}

function v2LoadProgress() {
  try {
    const parsed = JSON.parse(localStorage.getItem(v2State.storageKey) || "null");
    if (!Array.isArray(parsed)) return null;
    const valid = new Set(v2State.nodes.map((node) => node.id));
    return new Set(parsed.filter((id) => valid.has(id)));
  } catch (error) {
    return null;
  }
}

function v2SaveProgress() {
  localStorage.setItem(v2State.storageKey, JSON.stringify([...v2State.completed]));
}

function v2ShowToast(message) {
  const oldToast = document.querySelector(".v2-toast");
  if (oldToast) oldToast.remove();

  const toast = document.createElement("div");
  toast.className = "v2-toast";
  toast.textContent = message;
  document.body.appendChild(toast);
  window.setTimeout(() => toast.remove(), 2200);
}

function v2Slug(value) {
  return String(value)
    .toLowerCase()
    .replace(/[^a-z0-9\u4e00-\u9fa5]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

function v2Clamp(value, min, max) {
  if (Number.isNaN(value)) return min;
  return Math.min(max, Math.max(min, value));
}
