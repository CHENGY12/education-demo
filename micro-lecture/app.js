const DEFAULT_TREE_DATA = {
  title: "AI 学习路线技能树",
  subtitle: "从基础能力到可上线的智能应用 Demo",
  version: "demo-0.1",
  nodes: [
    {
      id: "math",
      title: "数学基础",
      domain: "基础",
      level: 0,
      lane: 1,
      difficulty: 3,
      mastery: 84,
      estimatedHours: 12,
      completed: true,
      description: "向量、矩阵、概率分布、导数和常见优化目标，是理解模型训练过程的底层语言。",
      tags: ["线性代数", "概率", "优化"],
      resources: [{ label: "线性代数速查", url: "#" }]
    },
    {
      id: "programming",
      title: "Python 工程基础",
      domain: "基础",
      level: 0,
      lane: 3,
      difficulty: 2,
      mastery: 92,
      estimatedHours: 10,
      completed: true,
      description: "掌握 Python、虚拟环境、包管理、模块拆分和基础调试能力。",
      tags: ["Python", "调试", "环境"],
      resources: [{ label: "项目脚手架清单", url: "#" }]
    },
    {
      id: "probability",
      title: "统计与不确定性",
      domain: "基础",
      level: 1,
      lane: 0,
      difficulty: 3,
      mastery: 74,
      estimatedHours: 8,
      completed: true,
      dependencies: ["math"],
      description: "用随机变量、估计、置信区间和假设检验描述数据与模型输出的不确定性。",
      tags: ["统计", "抽样", "估计"]
    },
    {
      id: "data-pipeline",
      title: "数据处理管线",
      domain: "数据",
      level: 1,
      lane: 2,
      difficulty: 3,
      mastery: 78,
      estimatedHours: 14,
      completed: true,
      dependencies: ["programming"],
      description: "将原始数据清洗、切分、校验和版本化，保证训练和评估输入稳定可追踪。",
      tags: ["清洗", "版本", "特征"]
    },
    {
      id: "systems",
      title: "服务与系统常识",
      domain: "工程",
      level: 1,
      lane: 4,
      difficulty: 2,
      mastery: 56,
      estimatedHours: 8,
      dependencies: ["programming"],
      description: "理解 HTTP、进程、日志、配置和资源限制，为后续部署模型服务打基础。",
      tags: ["HTTP", "日志", "配置"]
    },
    {
      id: "ml-basics",
      title: "机器学习核心概念",
      domain: "模型",
      level: 2,
      lane: 1,
      difficulty: 3,
      mastery: 48,
      estimatedHours: 16,
      dependencies: ["probability", "data-pipeline"],
      description: "理解训练集、验证集、损失函数、泛化、过拟合、正则化和交叉验证。",
      tags: ["损失函数", "泛化", "验证"]
    },
    {
      id: "visualization",
      title: "数据可视化诊断",
      domain: "数据",
      level: 2,
      lane: 3,
      difficulty: 2,
      mastery: 62,
      estimatedHours: 7,
      dependencies: ["data-pipeline"],
      description: "通过分布、异常值、相关性和切片指标发现数据问题与模型偏差。",
      tags: ["分布", "切片", "异常"]
    },
    {
      id: "supervised",
      title: "监督学习任务",
      domain: "模型",
      level: 3,
      lane: 0,
      difficulty: 3,
      mastery: 32,
      estimatedHours: 15,
      dependencies: ["ml-basics"],
      description: "处理分类、回归、排序等任务，选择合适的基线和指标。",
      tags: ["分类", "回归", "排序"]
    },
    {
      id: "deep-learning",
      title: "深度学习基础",
      domain: "模型",
      level: 3,
      lane: 2,
      difficulty: 4,
      mastery: 26,
      estimatedHours: 20,
      dependencies: ["ml-basics", "math"],
      description: "理解神经网络、反向传播、优化器、归一化和常见训练技巧。",
      tags: ["神经网络", "反传", "优化器"]
    },
    {
      id: "experiments",
      title: "实验设计与追踪",
      domain: "工程",
      level: 3,
      lane: 4,
      difficulty: 3,
      mastery: 28,
      estimatedHours: 12,
      dependencies: ["ml-basics", "visualization"],
      description: "定义对照组、记录配置和指标，避免一次实验只留下不可复现的截图。",
      tags: ["A/B", "指标", "复现"]
    },
    {
      id: "nlp",
      title: "自然语言处理",
      domain: "应用",
      level: 4,
      lane: 1,
      difficulty: 4,
      mastery: 18,
      estimatedHours: 18,
      dependencies: ["deep-learning"],
      description: "理解分词、文本表示、Transformer、生成式模型和常见文本任务。",
      tags: ["Transformer", "生成", "文本"]
    },
    {
      id: "vision",
      title: "计算机视觉",
      domain: "应用",
      level: 4,
      lane: 2,
      difficulty: 4,
      mastery: 12,
      estimatedHours: 18,
      dependencies: ["deep-learning"],
      description: "掌握图像分类、检测、分割和视觉表征的基本任务关系。",
      tags: ["分类", "检测", "分割"]
    },
    {
      id: "causal",
      title: "因果推断入门",
      domain: "进阶",
      level: 4,
      lane: 3,
      difficulty: 5,
      mastery: 10,
      estimatedHours: 22,
      dependencies: ["probability", "experiments"],
      description: "用干预、混杂、DAG 和反事实框架分析相关性之外的问题。",
      tags: ["DAG", "干预", "反事实"]
    },
    {
      id: "evaluation",
      title: "模型评估体系",
      domain: "工程",
      level: 4,
      lane: 5,
      difficulty: 4,
      mastery: 16,
      estimatedHours: 14,
      dependencies: ["supervised", "experiments"],
      description: "建立离线指标、人工评测、线上监控和失败样例回流机制。",
      tags: ["评测", "监控", "回流"]
    },
    {
      id: "rag",
      title: "RAG 知识应用",
      domain: "应用",
      level: 5,
      lane: 1,
      difficulty: 4,
      mastery: 8,
      estimatedHours: 16,
      dependencies: ["nlp", "evaluation"],
      description: "将文档解析、向量检索、重排、上下文构造和生成评估组合成可用链路。",
      tags: ["检索", "重排", "问答"]
    },
    {
      id: "deployment",
      title: "模型服务部署",
      domain: "工程",
      level: 5,
      lane: 4,
      difficulty: 4,
      mastery: 6,
      estimatedHours: 16,
      dependencies: ["systems", "evaluation"],
      description: "把模型封装成服务，处理配置、密钥、日志、限流、回滚和资源监控。",
      tags: ["API", "监控", "回滚"]
    },
    {
      id: "demo-product",
      title: "可展示 Demo 产品",
      domain: "产出",
      level: 6,
      lane: 2,
      difficulty: 5,
      mastery: 0,
      estimatedHours: 20,
      dependencies: ["rag", "deployment", "causal"],
      description: "将技能树、模型能力、数据输入和网页交互整合成可以被外部访问的完整演示。",
      tags: ["前端", "部署", "演示"]
    }
  ]
};

const dom = {
  treeTitle: document.getElementById("treeTitle"),
  treeSubtitle: document.getElementById("treeSubtitle"),
  searchInput: document.getElementById("searchInput"),
  fileInput: document.getElementById("fileInput"),
  fitButton: document.getElementById("fitButton"),
  resetButton: document.getElementById("resetButton"),
  zoomInButton: document.getElementById("zoomInButton"),
  zoomOutButton: document.getElementById("zoomOutButton"),
  domainFilters: document.getElementById("domainFilters"),
  progressArc: document.getElementById("progressArc"),
  progressPercent: document.getElementById("progressPercent"),
  progressLabel: document.getElementById("progressLabel"),
  currentLabel: document.getElementById("currentLabel"),
  treeStage: document.getElementById("treeStage"),
  treeWorld: document.getElementById("treeWorld"),
  connectionLayer: document.getElementById("connectionLayer"),
  nodeLayer: document.getElementById("nodeLayer"),
  detailPanel: document.getElementById("detailPanel"),
  emptyDetailTemplate: document.getElementById("emptyDetailTemplate")
};

const layout = {
  nodeWidth: 190,
  nodeHeight: 86,
  colGap: 274,
  rowGap: 124,
  marginX: 112,
  marginY: 86
};

const domainPalette = ["#e5b94e", "#37d4c0", "#72d36d", "#f06f7f", "#9a88ff", "#68a8ff", "#f19848"];
const storagePrefix = "knowledge-skill-tree:";

const state = {
  data: null,
  nodes: [],
  nodeById: new Map(),
  positions: new Map(),
  completed: new Set(),
  selectedId: null,
  activeDomain: "all",
  query: "",
  storageKey: "",
  domainColors: new Map(),
  worldWidth: 1200,
  worldHeight: 800,
  view: { x: 0, y: 0, scale: 1 },
  drag: null,
  hasFitOnce: false
};

init();

async function init() {
  bindEvents();

  let data = DEFAULT_TREE_DATA;
  try {
    const response = await fetch("knowledge-tree.json", { cache: "no-store" });
    if (response.ok) {
      data = await response.json();
    }
  } catch (error) {
    showToast("使用内置示例数据");
  }

  loadTree(data, { keepProgress: true });
}

function bindEvents() {
  dom.searchInput.addEventListener("input", () => {
    state.query = dom.searchInput.value.trim().toLowerCase();
    renderTree();
  });

  dom.fileInput.addEventListener("change", async (event) => {
    const [file] = event.target.files;
    if (!file) return;

    try {
      const text = await file.text();
      const data = JSON.parse(text);
      loadTree(data, { keepProgress: false });
      showToast(`已导入 ${file.name}`);
    } catch (error) {
      showToast("JSON 文件无法解析");
    } finally {
      dom.fileInput.value = "";
    }
  });

  dom.fitButton.addEventListener("click", () => fitView());
  dom.zoomInButton.addEventListener("click", () => zoomBy(1.16));
  dom.zoomOutButton.addEventListener("click", () => zoomBy(0.86));

  dom.resetButton.addEventListener("click", () => {
    state.completed = getDefaultCompleted(state.nodes);
    saveProgress();
    pickDefaultSelection();
    renderAll();
    showToast("进度已重置");
  });

  dom.treeStage.addEventListener("pointerdown", handlePointerDown);
  dom.treeStage.addEventListener("pointermove", handlePointerMove);
  dom.treeStage.addEventListener("pointerup", handlePointerUp);
  dom.treeStage.addEventListener("pointercancel", handlePointerUp);
  dom.treeStage.addEventListener("wheel", handleWheel, { passive: false });

  const resizeObserver = new ResizeObserver(() => {
    if (!state.hasFitOnce) return;
    fitView();
  });
  resizeObserver.observe(dom.treeStage);
}

function loadTree(rawData, options = {}) {
  const data = normalizeTree(rawData);
  state.data = data;
  state.nodes = data.nodes;
  state.nodeById = new Map(data.nodes.map((node) => [node.id, node]));
  state.positions = calculatePositions(data.nodes);
  state.domainColors = assignDomainColors(data.nodes);
  state.storageKey = `${storagePrefix}${slugify(`${data.title}-${data.version || "local"}`)}`;
  state.completed = options.keepProgress ? loadProgress() || getDefaultCompleted(data.nodes) : getDefaultCompleted(data.nodes);
  state.activeDomain = "all";
  state.query = "";
  dom.searchInput.value = "";
  pickDefaultSelection();
  renderAll();
  requestAnimationFrame(() => fitView());
}

function normalizeTree(rawData) {
  const source = rawData && typeof rawData === "object" ? rawData : DEFAULT_TREE_DATA;
  const rawNodes = Array.isArray(source.nodes) ? source.nodes : [];
  const nodes = rawNodes.map((node, index) => {
    const id = String(node.id || `node-${index + 1}`);
    const dependencies = normalizeArray(node.dependencies || node.prerequisites || node.requires).map(String);
    const level = Number.isFinite(Number(node.level)) ? Number(node.level) : null;
    const lane = Number.isFinite(Number(node.lane)) ? Number(node.lane) : index;

    return {
      id,
      title: String(node.title || node.name || id),
      domain: String(node.domain || node.category || "通用"),
      level,
      lane,
      difficulty: clamp(Number(node.difficulty || node.rank || 3), 1, 5),
      mastery: clamp(Number(node.mastery || 0), 0, 100),
      estimatedHours: Math.max(0, Number(node.estimatedHours || node.hours || 0)),
      completed: Boolean(node.completed || node.defaultCompleted || node.status === "completed"),
      dependencies,
      description: String(node.description || node.summary || "暂无描述。"),
      tags: normalizeArray(node.tags).map(String),
      resources: normalizeResources(node.resources)
    };
  });

  inferMissingLevels(nodes);

  return {
    title: String(source.title || "知识技能树"),
    subtitle: String(source.subtitle || source.description || "Learning path map"),
    version: String(source.version || "local"),
    nodes
  };
}

function inferMissingLevels(nodes) {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const visiting = new Set();

  function resolve(node) {
    if (Number.isFinite(node.level)) return node.level;
    if (visiting.has(node.id)) return 0;
    visiting.add(node.id);
    const depLevels = node.dependencies
      .map((id) => byId.get(id))
      .filter(Boolean)
      .map((dep) => resolve(dep));
    visiting.delete(node.id);
    node.level = depLevels.length ? Math.max(...depLevels) + 1 : 0;
    return node.level;
  }

  nodes.forEach(resolve);
}

function normalizeArray(value) {
  if (Array.isArray(value)) return value;
  if (typeof value === "string" && value.trim()) return value.split(",").map((item) => item.trim()).filter(Boolean);
  return [];
}

function normalizeResources(resources) {
  return normalizeArray(resources).map((resource, index) => {
    if (typeof resource === "string") {
      return { label: resource, url: "#" };
    }

    return {
      label: String(resource.label || resource.title || `资料 ${index + 1}`),
      url: String(resource.url || "#")
    };
  });
}

function calculatePositions(nodes) {
  const positions = new Map();
  let maxLevel = 0;
  let maxLane = 0;

  nodes.forEach((node) => {
    maxLevel = Math.max(maxLevel, node.level);
    maxLane = Math.max(maxLane, node.lane);
  });

  state.worldWidth = layout.marginX * 2 + maxLevel * layout.colGap + layout.nodeWidth;
  state.worldHeight = layout.marginY * 2 + maxLane * layout.rowGap + layout.nodeHeight;

  nodes.forEach((node) => {
    const yOffset = node.level % 2 === 0 ? 0 : 18;
    positions.set(node.id, {
      x: layout.marginX + node.level * layout.colGap,
      y: layout.marginY + node.lane * layout.rowGap + yOffset
    });
  });

  return positions;
}

function assignDomainColors(nodes) {
  const colors = new Map();
  [...new Set(nodes.map((node) => node.domain))].forEach((domain, index) => {
    colors.set(domain, domainPalette[index % domainPalette.length]);
  });
  return colors;
}

function loadProgress() {
  try {
    const saved = JSON.parse(localStorage.getItem(state.storageKey) || "null");
    if (!Array.isArray(saved)) return null;
    const validIds = new Set(state.nodes.map((node) => node.id));
    return new Set(saved.filter((id) => validIds.has(id)));
  } catch (error) {
    return null;
  }
}

function saveProgress() {
  localStorage.setItem(state.storageKey, JSON.stringify([...state.completed]));
}

function getDefaultCompleted(nodes) {
  return new Set(nodes.filter((node) => node.completed).map((node) => node.id));
}

function pickDefaultSelection() {
  const available = state.nodes.find((node) => getNodeState(node) === "available");
  state.selectedId = (available || state.nodes[0] || {}).id || null;
}

function renderAll() {
  dom.treeTitle.textContent = state.data.title;
  dom.treeSubtitle.textContent = state.data.subtitle;
  renderProgress();
  renderFilters();
  renderTree();
  renderDetail();
}

function renderProgress() {
  const total = state.nodes.length;
  const completed = state.nodes.filter((node) => state.completed.has(node.id)).length;
  const available = state.nodes.filter((node) => getNodeState(node) === "available").length;
  const percent = total ? Math.round((completed / total) * 100) : 0;
  const circumference = 301.59;

  dom.progressArc.style.strokeDashoffset = String(circumference - (circumference * percent) / 100);
  dom.progressPercent.textContent = `${percent}%`;
  dom.progressLabel.textContent = `${completed} / ${total}`;
  dom.currentLabel.textContent = `${available} 个可学习`;
}

function renderFilters() {
  const domains = [...new Set(state.nodes.map((node) => node.domain))];
  dom.domainFilters.replaceChildren();

  dom.domainFilters.appendChild(createFilterButton("all", "全部", state.nodes.length, "var(--gold)"));

  domains.forEach((domain) => {
    const count = state.nodes.filter((node) => node.domain === domain).length;
    dom.domainFilters.appendChild(createFilterButton(domain, domain, count, state.domainColors.get(domain)));
  });
}

function createFilterButton(value, label, count, color) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `filter-pill${state.activeDomain === value ? " active" : ""}`;
  button.style.setProperty("--domain-color", color);
  button.dataset.value = value;

  const dot = document.createElement("i");
  const text = document.createElement("span");
  const number = document.createElement("small");
  text.textContent = label;
  number.textContent = String(count);

  button.append(dot, text, number);
  button.addEventListener("click", () => {
    state.activeDomain = value;
    renderFilters();
    renderTree();
  });

  return button;
}

function renderTree() {
  dom.treeWorld.style.width = `${state.worldWidth}px`;
  dom.treeWorld.style.height = `${state.worldHeight}px`;
  dom.connectionLayer.setAttribute("width", String(state.worldWidth));
  dom.connectionLayer.setAttribute("height", String(state.worldHeight));
  dom.connectionLayer.setAttribute("viewBox", `0 0 ${state.worldWidth} ${state.worldHeight}`);
  renderConnections();
  renderNodes();
  applyTransform();
}

function renderConnections() {
  dom.connectionLayer.replaceChildren();

  state.nodes.forEach((node) => {
    node.dependencies.forEach((dependencyId) => {
      const source = state.nodeById.get(dependencyId);
      if (!source) return;

      const sourcePosition = state.positions.get(source.id);
      const targetPosition = state.positions.get(node.id);
      if (!sourcePosition || !targetPosition) return;

      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      const sourceX = sourcePosition.x + layout.nodeWidth;
      const sourceY = sourcePosition.y + layout.nodeHeight / 2;
      const targetX = targetPosition.x;
      const targetY = targetPosition.y + layout.nodeHeight / 2;
      const curve = Math.max(80, (targetX - sourceX) * 0.46);

      path.setAttribute(
        "d",
        `M ${sourceX} ${sourceY} C ${sourceX + curve} ${sourceY}, ${targetX - curve} ${targetY}, ${targetX} ${targetY}`
      );

      const classes = [getEdgeState(source, node)];
      if (state.selectedId === source.id || state.selectedId === node.id) classes.push("active");
      if (!matchesView(source) || !matchesView(node)) classes.push("dimmed");
      path.setAttribute("class", classes.filter(Boolean).join(" "));
      dom.connectionLayer.appendChild(path);
    });
  });
}

function renderNodes() {
  dom.nodeLayer.replaceChildren();

  state.nodes.forEach((node) => {
    const position = state.positions.get(node.id);
    const nodeState = getNodeState(node);
    const wrapper = document.createElement("div");
    wrapper.className = `skill-node ${nodeState}${state.selectedId === node.id ? " selected" : ""}${matchesView(node) ? "" : " dimmed"}`;
    wrapper.style.left = `${position.x}px`;
    wrapper.style.top = `${position.y}px`;
    wrapper.style.setProperty("--domain-color", state.domainColors.get(node.domain));

    const button = document.createElement("button");
    button.type = "button";
    button.className = "node-button";
    button.setAttribute("aria-label", `${node.title}，${getStatusLabel(nodeState)}`);
    button.addEventListener("pointerdown", (event) => event.stopPropagation());
    button.addEventListener("click", () => {
      state.selectedId = node.id;
      renderTree();
      renderDetail();
    });

    const main = document.createElement("div");
    main.className = "node-main";

    const badge = document.createElement("span");
    badge.className = "node-badge";
    badge.textContent = getDomainAbbr(node.domain);

    const title = document.createElement("h3");
    title.className = "node-title";
    title.textContent = node.title;
    main.append(badge, title);

    const meta = document.createElement("div");
    meta.className = "node-meta";
    const level = document.createElement("span");
    level.textContent = `Lv ${node.level + 1} · ${node.estimatedHours || "-"}h`;
    const pips = document.createElement("span");
    pips.className = "node-pips";
    for (let index = 1; index <= 5; index += 1) {
      const pip = document.createElement("i");
      if (index <= node.difficulty) pip.className = "on";
      pips.appendChild(pip);
    }
    meta.append(level, pips);

    button.append(main, meta);
    wrapper.appendChild(button);
    dom.nodeLayer.appendChild(wrapper);
  });
}

function renderDetail() {
  dom.detailPanel.replaceChildren();
  const node = state.nodeById.get(state.selectedId);
  if (!node) {
    dom.detailPanel.appendChild(dom.emptyDetailTemplate.content.cloneNode(true));
    return;
  }

  const nodeState = getNodeState(node);
  const root = document.createDocumentFragment();
  const color = state.domainColors.get(node.domain);

  const kicker = document.createElement("div");
  kicker.className = "detail-kicker";
  const domain = document.createElement("span");
  domain.className = "domain-token";
  domain.style.setProperty("--domain-color", color);
  domain.textContent = node.domain;
  const status = document.createElement("span");
  status.className = "status-token";
  status.textContent = getStatusLabel(nodeState);
  kicker.append(domain, status);

  const title = document.createElement("h2");
  title.textContent = node.title;

  const description = document.createElement("p");
  description.className = "detail-description";
  description.textContent = node.description;

  const mastery = document.createElement("div");
  mastery.className = "mastery-bar";
  mastery.style.setProperty("--mastery", `${node.mastery}%`);
  mastery.appendChild(document.createElement("i"));

  const metrics = document.createElement("div");
  metrics.className = "detail-grid";
  metrics.append(
    createMetric("等级", `Lv ${node.level + 1}`),
    createMetric("难度", `${node.difficulty} / 5`),
    createMetric("预计", node.estimatedHours ? `${node.estimatedHours}h` : "-"),
    createMetric("依赖", `${node.dependencies.length}`)
  );

  root.append(kicker, title, description, mastery, metrics);

  if (node.tags.length) {
    root.appendChild(createTagSection("标签", node.tags));
  }

  if (node.dependencies.length) {
    root.appendChild(createDependencySection("前置", node.dependencies));
  }

  const unlocks = state.nodes.filter((candidate) => candidate.dependencies.includes(node.id)).map((candidate) => candidate.id);
  if (unlocks.length) {
    root.appendChild(createDependencySection("后续", unlocks));
  }

  if (node.resources.length) {
    root.appendChild(createResourceSection(node.resources));
  }

  const action = document.createElement("button");
  action.type = "button";
  action.className = "primary-action";
  action.disabled = nodeState === "locked";
  action.textContent = nodeState === "mastered" ? "标记为未掌握" : nodeState === "available" ? "标记为已掌握" : "前置未完成";
  action.addEventListener("click", () => {
    if (state.completed.has(node.id)) {
      state.completed.delete(node.id);
      removeBlockedDescendants(node.id);
    } else {
      state.completed.add(node.id);
    }
    saveProgress();
    renderAll();
  });
  root.appendChild(action);

  dom.detailPanel.appendChild(root);
}

function createMetric(label, value) {
  const metric = document.createElement("div");
  metric.className = "metric";
  const small = document.createElement("small");
  small.textContent = label;
  const strong = document.createElement("strong");
  strong.textContent = value;
  metric.append(small, strong);
  return metric;
}

function createTagSection(title, tags) {
  const section = document.createElement("section");
  section.className = "detail-section";
  const heading = document.createElement("h3");
  heading.textContent = title;
  const list = document.createElement("div");
  list.className = "tag-list";
  tags.forEach((tag) => {
    const item = document.createElement("span");
    item.textContent = tag;
    list.appendChild(item);
  });
  section.append(heading, list);
  return section;
}

function createDependencySection(title, dependencyIds) {
  const section = document.createElement("section");
  section.className = "detail-section";
  const heading = document.createElement("h3");
  heading.textContent = title;
  const list = document.createElement("div");
  list.className = "dependency-list";

  dependencyIds.forEach((id) => {
    const dependency = state.nodeById.get(id);
    if (!dependency) return;

    const button = document.createElement("button");
    button.type = "button";
    button.className = state.completed.has(id) ? "done" : "missing";
    button.textContent = dependency.title;
    button.addEventListener("click", () => {
      state.selectedId = id;
      renderTree();
      renderDetail();
    });
    list.appendChild(button);
  });

  section.append(heading, list);
  return section;
}

function createResourceSection(resources) {
  const section = document.createElement("section");
  section.className = "detail-section";
  const heading = document.createElement("h3");
  heading.textContent = "资料";
  const list = document.createElement("ul");
  list.className = "resource-list";

  resources.forEach((resource) => {
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.href = resource.url;
    link.textContent = resource.label;
    if (resource.url !== "#") {
      link.target = "_blank";
      link.rel = "noreferrer";
    }
    item.appendChild(link);
    list.appendChild(item);
  });

  section.append(heading, list);
  return section;
}

function removeBlockedDescendants(rootId) {
  let changed = true;
  while (changed) {
    changed = false;
    state.nodes.forEach((node) => {
      if (!state.completed.has(node.id)) return;
      const hasMissingDependency = node.dependencies.some((id) => !state.completed.has(id));
      if (hasMissingDependency || node.dependencies.includes(rootId)) {
        state.completed.delete(node.id);
        changed = true;
      }
    });
  }
}

function getNodeState(node) {
  if (state.completed.has(node.id)) return "mastered";
  const knownDependencies = node.dependencies.filter((id) => state.nodeById.has(id));
  return knownDependencies.every((id) => state.completed.has(id)) ? "available" : "locked";
}

function getEdgeState(source, target) {
  if (state.completed.has(source.id) && state.completed.has(target.id)) return "mastered";
  if (state.completed.has(source.id) && getNodeState(target) === "available") return "available";
  return "";
}

function getStatusLabel(nodeState) {
  if (nodeState === "mastered") return "已掌握";
  if (nodeState === "available") return "可学习";
  return "未解锁";
}

function matchesView(node) {
  const inDomain = state.activeDomain === "all" || node.domain === state.activeDomain;
  if (!inDomain) return false;
  if (!state.query) return true;

  const haystack = [node.title, node.domain, node.description, ...node.tags].join(" ").toLowerCase();
  return haystack.includes(state.query);
}

function getDomainAbbr(domain) {
  const text = String(domain).trim();
  return Array.from(text).slice(0, 2).join("");
}

function fitView() {
  const rect = dom.treeStage.getBoundingClientRect();
  if (!rect.width || !rect.height || !state.worldWidth || !state.worldHeight) return;

  const scale = clamp(Math.min((rect.width - 64) / state.worldWidth, (rect.height - 64) / state.worldHeight), 0.36, 1.1);
  state.view.scale = scale;
  state.view.x = (rect.width - state.worldWidth * scale) / 2;
  state.view.y = (rect.height - state.worldHeight * scale) / 2;
  state.hasFitOnce = true;
  applyTransform();
}

function zoomBy(multiplier) {
  const rect = dom.treeStage.getBoundingClientRect();
  zoomAt(rect.width / 2, rect.height / 2, state.view.scale * multiplier);
}

function handleWheel(event) {
  event.preventDefault();
  const rect = dom.treeStage.getBoundingClientRect();
  const multiplier = event.deltaY < 0 ? 1.08 : 0.92;
  zoomAt(event.clientX - rect.left, event.clientY - rect.top, state.view.scale * multiplier);
}

function zoomAt(stageX, stageY, nextScale) {
  const oldScale = state.view.scale;
  const scale = clamp(nextScale, 0.32, 1.55);
  const worldX = (stageX - state.view.x) / oldScale;
  const worldY = (stageY - state.view.y) / oldScale;
  state.view.scale = scale;
  state.view.x = stageX - worldX * scale;
  state.view.y = stageY - worldY * scale;
  applyTransform();
}

function applyTransform() {
  dom.treeWorld.style.transform = `translate(${state.view.x}px, ${state.view.y}px) scale(${state.view.scale})`;
}

function handlePointerDown(event) {
  if (event.button !== 0) return;
  dom.treeStage.setPointerCapture(event.pointerId);
  state.drag = {
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    originX: state.view.x,
    originY: state.view.y
  };
  dom.treeStage.classList.add("is-panning");
}

function handlePointerMove(event) {
  if (!state.drag || state.drag.pointerId !== event.pointerId) return;
  state.view.x = state.drag.originX + event.clientX - state.drag.startX;
  state.view.y = state.drag.originY + event.clientY - state.drag.startY;
  applyTransform();
}

function handlePointerUp(event) {
  if (!state.drag || state.drag.pointerId !== event.pointerId) return;
  state.drag = null;
  dom.treeStage.classList.remove("is-panning");
  if (dom.treeStage.hasPointerCapture(event.pointerId)) {
    dom.treeStage.releasePointerCapture(event.pointerId);
  }
}

function showToast(message) {
  const oldToast = document.querySelector(".toast");
  if (oldToast) oldToast.remove();

  const toast = document.createElement("div");
  toast.className = "toast";
  toast.textContent = message;
  document.body.appendChild(toast);
  window.setTimeout(() => toast.remove(), 2200);
}

function slugify(value) {
  return String(value)
    .toLowerCase()
    .replace(/[^a-z0-9\u4e00-\u9fa5]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

function clamp(value, min, max) {
  if (Number.isNaN(value)) return min;
  return Math.min(max, Math.max(min, value));
}
