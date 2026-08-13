const state = {
  view: "seed",
  reviewBucket: "seed",
  items: [],
  detail: null,
  index: -1,
  summary: null,
  completedJobs: new Set(),
  optimisticJobs: new Map(),
  searchTimer: null,
  total: 0,
  offset: 0,
  pageSize: 100,
  skills: [],
  skillDetail: null,
  skillIndex: -1,
  skillTotal: 0,
  skillOffset: 0,
  skillPageSize: 50,
  skillSearchTimer: null,
  skillSelectedVersion: null,
  skillsLoaded: false,
  polling: false,
};

const labels = {
  pending: "未处理",
  running: "解题中",
  final: "已定稿",
  disagreement: "不一致",
  invalid: "题目无效",
  error: "运行错误",
};

const skillStatusLabels = {
  active: "已激活",
  verified: "已验证",
  passed: "已通过",
  completed: "已完成",
  pending: "待验证",
  queued: "排队中",
  running: "验证中",
  proposed: "待审核",
  failed: "验证失败",
  rejected: "未通过",
  superseded: "已被替代",
  archived: "已归档",
};

const annotationStatusLabels = {
  valid: "题面有效",
  invalid: "题面无效",
  uncertain: "有效性待定",
  unreviewed: "尚未审阅",
};

const retryDispositionLabels = {
  none: "无需重试",
  retry: "历史自动重解（新流程已停用）",
  question_revision: "建议修订题面",
  human_review: "转人工复核",
};

const terminalStatuses = new Set(["completed", "failed"]);
const activeStatuses = new Set(["queued", "running"]);
const defaultReviewStatuses = {
  seed: "disagreement,invalid,error",
  candidate: "disagreement",
};
const $ = (id) => document.getElementById(id);

function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = text;
  return node;
}

function text(value, fallback = "") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

const mathRenderOptions = Object.freeze({
  delimiters: [
    { left: "$$", right: "$$", display: true },
    { left: "$", right: "$", display: false },
    { left: "\\[", right: "\\]", display: true },
    { left: "\\(", right: "\\)", display: false },
  ],
  throwOnError: false,
  trust: false,
  strict: "warn",
  ignoredClasses: ["katex", "mono", "enum-code"],
});

function renderMath(root) {
  if (!root || typeof window.renderMathInElement !== "function") return;
  window.renderMathInElement(root, mathRenderOptions);
}

function asNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function progressValue(value) {
  return Math.max(0, Math.min(100, asNumber(value, 0)));
}

function formatDate(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return text(value, "—");
  return parsed.toLocaleString("zh-CN", { hour12: false });
}

function displayValue(value, fallback = "—") {
  if (value === null || value === undefined || value === "") return fallback;
  if (typeof value === "string") return value;
  try { return JSON.stringify(value, null, 2); }
  catch (_) { return String(value); }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function toast(title, message = "", type = "ok") {
  const node = el("div", `toast ${type === "error" ? "error" : ""}`);
  node.append(el("strong", "", title));
  if (message) node.append(el("p", "", message));
  $("toasts").append(node);
  setTimeout(() => node.remove(), 6000);
}

function jobId(job) {
  return text(job?.job_id || job?.id);
}

function isSkillJob(job, known = null) {
  return text(job?.kind || known?.kind) === "skill_revision"
    || Boolean(job?.skill_id || known?.skillId);
}

function skillTaskType(job, known = null) {
  const explicit = text(job?.task_type || known?.taskType);
  if (explicit) return explicit;
  const id = text(job?.skill_id || job?.item_id || known?.skillId || known?.itemId);
  return id.startsWith("extract-") ? "skill_extraction" : "skill_revision";
}

function jobLabel(job, known = null) {
  return text(
    known?.itemLabel
      || job?.item_label
      || job?.question_id
      || known?.questionId
      || known?.skillId
      || job?.item_id
      || job?.skill_id,
    jobId(job).slice(0, 10),
  );
}

function renderActiveJobs(activeJobs = []) {
  const root = $("active-jobs");
  const knownById = new Map(readKnownJobs().map((job) => [text(job.id), job]));
  const byId = new Map();
  activeJobs.forEach((job) => {
    if (jobId(job)) byId.set(jobId(job), job);
  });
  state.optimisticJobs.forEach((job, id) => {
    if (!byId.has(id)) byId.set(id, job);
  });
  const jobs = [...byId.values()];
  root.replaceChildren();
  if (!jobs.length) {
    root.classList.add("hidden");
    return;
  }
  root.classList.remove("hidden");
  const heading = el("div", "active-jobs-heading");
  heading.append(el("strong", "", `后台任务 · ${jobs.length}`), el("span", "", "可继续浏览"));
  root.append(heading);
  jobs.slice(0, 5).forEach((job) => {
    const known = knownById.get(jobId(job));
    const row = el("div", "active-job");
    const copy = el("div", "active-job-copy");
    const kind = isSkillJob(job, known)
      ? (skillTaskType(job, known) === "skill_extraction" ? "技能提炼" : "技能修订")
      : "题目重解";
    copy.append(
      el("strong", "", `${kind} · ${jobLabel(job, known)}`),
      el("span", "", text(job.message, text(job.status, "已排队"))),
    );
    const percent = progressValue(job.progress);
    row.append(copy, el("span", "active-job-percent", `${percent}%`));
    const bar = el("div", "progress compact-progress");
    const fill = el("span");
    fill.style.width = `${percent}%`;
    bar.append(fill);
    row.append(bar);
    root.append(row);
  });
  if (jobs.length > 5) root.append(el("p", "active-job-more", `另有 ${jobs.length - 5} 个后台任务`));
}

function renderSummary(data) {
  state.summary = data || {};
  const statusCounts = data.status || {};
  const root = $("summary");
  root.replaceChildren();
  const chips = [
    ["总题数", asNumber(data.total)],
    ["待复核", asNumber(statusCounts.disagreement) + asNumber(statusCounts.invalid) + asNumber(statusCounts.error)],
    ["解题中", asNumber(statusCounts.running)],
    ["已定稿", asNumber(statusCounts.final)],
  ];
  chips.forEach(([label, value]) => {
    const chip = el("div", "summary-chip");
    chip.append(el("strong", "", String(value)), el("span", "", label));
    root.append(chip);
  });
  const fixedView = Boolean(data.review_view?.fixed);
  const viewBanner = $("view-banner");
  const status = $("status-filter");
  if (fixedView) {
    viewBanner.textContent = `${data.review_view.title || "固定审阅清单"} · 共 ${asNumber(data.review_view.question_count)} 题`;
    viewBanner.classList.remove("hidden");
    status.value = "";
    status.disabled = true;
    $("empty-copy").textContent = "固定清单中没有符合搜索条件的题目。";
  } else {
    viewBanner.classList.add("hidden");
    status.disabled = false;
    $("empty-copy").textContent = "可切换到“全部”或“已定稿”查看完整记录。";
  }
  const subject = $("subject-filter");
  const selected = subject.value;
  subject.replaceChildren(new Option("全部", ""));
  Object.keys(data.subjects || {}).forEach((name) => {
    subject.append(new Option(`${name} · ${data.subjects[name]}`, name));
  });
  subject.value = selected;
  const terminalChanged = notifyCompletedJobs(data.active_jobs || [], data.recent_jobs || []);
  renderActiveJobs(data.active_jobs || []);
  return terminalChanged;
}

function readKnownJobs() {
  try {
    const value = JSON.parse(localStorage.getItem("knownReviewJobs") || "[]");
    return Array.isArray(value) ? value : [];
  } catch (_) {
    localStorage.removeItem("knownReviewJobs");
    return [];
  }
}

function notifyCompletedJobs(activeJobs, recentJobs) {
  const activeIds = new Set(activeJobs.map(jobId).filter(Boolean));
  const recentById = new Map(recentJobs.map((job) => [jobId(job), job]).filter(([id]) => id));
  const known = readKnownJobs();
  const changed = { any: false, question: false, skill: false, questionIds: [], skillIds: [] };
  known.forEach((knownJob) => {
    const id = text(knownJob.id);
    if (!id || activeIds.has(id) || state.completedJobs.has(id)) return;
    const terminal = recentById.get(id);
    if (!terminal || !terminalStatuses.has(terminal.status)) return;
    state.completedJobs.add(id);
    state.optimisticJobs.delete(id);
    const skillJob = isSkillJob(terminal, knownJob);
    const taskType = skillJob ? skillTaskType(terminal, knownJob) : "question_resolve";
    const label = jobLabel(terminal, knownJob);
    changed.any = true;
    changed.skill ||= skillJob;
    changed.question ||= !skillJob;
    if (skillJob) changed.skillIds.push(text(terminal.item_id || terminal.skill_id || knownJob.skillId || knownJob.itemId));
    else changed.questionIds.push(text(terminal.question_id || knownJob.questionId || knownJob.itemId));
    if (terminal.status === "completed") {
      if (taskType === "skill_extraction") {
        toast("解题技能提炼已完成", `${label} 的后台通用化提炼已完成，可在技能库查看结果。`);
        document.title = "● 技能提炼已完成｜题库共识审校台";
      } else if (skillJob) {
        toast("技能修订已完成", `${label} 已生成受审计的新版本，并完成结构与版本链校验。`);
        document.title = "● 技能修订已完成｜题库共识审校台";
      } else {
        toast("后台解题已完成", `题目 ${label} 已完成三次作答与 Teacher 核验。`);
        document.title = "● 解题已完成｜题库共识审校台";
      }
    } else if (skillJob) {
      const title = taskType === "skill_extraction" ? "解题技能提炼失败" : "技能修订失败";
      toast(title, terminal.error || terminal.message || `${label} 后台任务失败。`, "error");
      document.title = `! ${title}｜题库共识审校台`;
    } else {
      toast("后台解题失败", terminal.error || terminal.message || `题目 ${label} 运行失败。`, "error");
      document.title = "! 解题失败｜题库共识审校台";
    }
  });
  const knownById = new Map(known.map((job) => [text(job.id), job]));
  activeJobs.forEach((job) => state.optimisticJobs.delete(jobId(job)));
  const unresolvedKnown = known.filter((knownJob) => {
    const id = text(knownJob.id);
    return id
      && !activeIds.has(id)
      && !state.completedJobs.has(id)
      && !terminalStatuses.has(recentById.get(id)?.status);
  });
  const merged = activeJobs.map((job) => {
    const previous = knownById.get(jobId(job)) || {};
    const skillJob = isSkillJob(job, previous);
    const itemId = text(job.item_id || job.skill_id || previous.itemId || previous.skillId || job.question_id || previous.questionId);
    return {
      id: jobId(job),
      kind: skillJob ? "skill_revision" : text(job.kind, previous.kind || "question_resolve"),
      taskType: skillJob ? skillTaskType(job, previous) : undefined,
      itemId,
      itemLabel: jobLabel(job, previous),
      questionId: skillJob ? undefined : text(job.question_id || previous.questionId || itemId),
      skillId: skillJob ? text(job.item_id || job.skill_id || previous.skillId || itemId) : undefined,
    };
  }).concat(unresolvedKnown);
  localStorage.setItem("knownReviewJobs", JSON.stringify(merged));
  return changed;
}

function rememberStartedJob(job, fallbackLabel, metadata = {}) {
  const id = jobId(job);
  if (!id) throw new Error("后台任务响应缺少 job_id");
  const known = readKnownJobs();
  const skillJob = text(job.kind || metadata.kind) === "skill_revision" || Boolean(job.skill_id || metadata.skillId);
  const itemId = text(
    job.item_id || job.skill_id || job.question_id || metadata.itemId || metadata.skillId || metadata.questionId,
    fallbackLabel,
  );
  const itemLabel = text(job.item_label || metadata.itemLabel, fallbackLabel || itemId);
  const record = {
    id,
    kind: skillJob ? "skill_revision" : text(job.kind || metadata.kind, "question_resolve"),
    taskType: skillJob ? text(metadata.taskType, skillTaskType(job, metadata)) : undefined,
    itemId,
    itemLabel,
    questionId: skillJob ? undefined : text(job.question_id || metadata.questionId || itemId),
    skillId: skillJob ? text(job.skill_id || metadata.skillId || itemId) : undefined,
  };
  const next = known.filter((item) => text(item.id) !== id);
  next.push(record);
  localStorage.setItem("knownReviewJobs", JSON.stringify(next));
  state.optimisticJobs.set(id, {
    ...job,
    job_id: id,
    kind: record.kind,
    item_id: record.itemId,
    item_label: record.itemLabel,
    task_type: record.taskType,
    status: text(job.status, "queued"),
    progress: progressValue(job.progress),
    message: text(job.message, "任务已排队"),
  });
  renderActiveJobs(state.summary?.active_jobs || []);
}

async function loadSummary() {
  try { return renderSummary(await api("/api/summary")); }
  catch (error) {
    toast("无法读取统计", error.message, "error");
    return { any: false, question: false, skill: false, questionIds: [], skillIds: [] };
  }
}

function renderList() {
  const root = $("question-list");
  root.replaceChildren();
  const first = state.items.length ? state.offset + 1 : 0;
  const last = state.offset + state.items.length;
  $("list-count").textContent = `${first}–${last} / ${state.total || 0} 道题`;
  const pageCount = Math.max(1, Math.ceil((state.total || 0) / state.pageSize));
  const page = Math.floor(state.offset / state.pageSize) + 1;
  $("page-label").textContent = `第 ${page} / ${pageCount} 页`;
  $("page-prev").disabled = state.offset <= 0;
  $("page-next").disabled = state.offset + state.items.length >= state.total;
  state.items.forEach((item, index) => {
    const button = el("button", `question-item ${state.detail?.question_key === item.question_key ? "active" : ""}`);
    button.type = "button";
    const top = el("div", "item-top");
    top.append(el("span", "item-id", text(item.id, "未命名题目")), el("span", `mini-badge ${item.status}`, labels[item.status] || item.status));
    button.append(top, el("p", "item-prompt", item.prompt || "（无题干）"), el("div", "item-node", `${item.subject || "未分类"} · ${item.node_dir || "—"}`));
    button.addEventListener("click", () => selectQuestion(index));
    root.append(button);
  });
  renderMath(root);
}

async function loadQuestions({ keepSelection = true, resetOffset = false, edge = "first" } = {}) {
  if (resetOffset) state.offset = 0;
  const selectedKey = keepSelection ? state.detail?.question_key : null;
  const params = new URLSearchParams({
    status: $("status-filter").value,
    subject: $("subject-filter").value,
    q: $("search").value.trim(),
    review_bucket: state.reviewBucket,
    limit: String(state.pageSize),
    offset: String(state.offset),
  });
  try {
    let data = await api(`/api/questions?${params}`);
    if (!data.items.length && data.total > 0 && state.offset > 0) {
      state.offset = Math.floor((data.total - 1) / state.pageSize) * state.pageSize;
      params.set("offset", String(state.offset));
      data = await api(`/api/questions?${params}`);
    }
    state.items = Array.isArray(data.items) ? data.items : [];
    state.total = asNumber(data.total);
    let index = selectedKey ? state.items.findIndex((item) => item.question_key === selectedKey) : -1;
    if (index < 0 && state.items.length) index = edge === "last" ? state.items.length - 1 : 0;
    renderList();
    if (index >= 0) await selectQuestion(index);
    else showEmpty();
  } catch (error) {
    toast("无法读取题目", error.message, "error");
  }
}

function showEmpty() {
  state.detail = null;
  state.index = -1;
  $("detail").classList.add("hidden");
  $("empty-state").classList.remove("hidden");
  renderList();
}

async function selectQuestion(index) {
  if (!state.items.length) return showEmpty();
  state.index = Math.max(0, Math.min(index, state.items.length - 1));
  const item = state.items[state.index];
  try {
    const previousKey = state.detail?.question_key;
    state.detail = await api(`/api/questions/${item.question_key}`);
    if (previousKey !== state.detail.question_key) $("rounds-panel").open = false;
    $("empty-state").classList.add("hidden");
    $("detail").classList.remove("hidden");
    renderDetail();
    renderList();
    if (state.view !== "skills") document.title = `审校 ${state.detail.id}`;
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (error) {
    toast("无法读取详情", error.message, "error");
  }
}

async function selectAdjacent(delta) {
  const target = state.index + delta;
  if (target >= 0 && target < state.items.length) return selectQuestion(target);
  if (delta > 0 && state.offset + state.items.length < state.total) {
    state.offset += state.pageSize;
    return loadQuestions({ keepSelection: false, edge: "first" });
  }
  if (delta < 0 && state.offset > 0) {
    state.offset = Math.max(0, state.offset - state.pageSize);
    return loadQuestions({ keepSelection: false, edge: "last" });
  }
}

function renderDetail() {
  const d = state.detail;
  if (!d) return;
  $("prev").disabled = state.offset + state.index <= 0;
  $("next").disabled = state.offset + state.index >= state.total - 1;
  $("status-badge").textContent = labels[d.status] || d.status;
  $("status-badge").className = `badge ${d.status}`;
  $("subject-badge").textContent = d.subject || "未分类";
  $("question-id").textContent = d.id;
  $("question-prompt").textContent = d.question?.prompt || "（无题干）";
  $("node-path").textContent = d.node_dir;
  $("run-id").textContent = d.current_run_id ? `run · ${d.current_run_id}` : "尚无 run";
  const options = $("options");
  options.replaceChildren();
  (d.question?.options || []).forEach((option) => {
    const item = el("li", "option");
    item.append(el("span", "option-id", String(option.id || "")), el("span", "", String(option.text || "")));
    options.append(item);
  });
  renderAnnotations(d);
  renderJob(d.jobs || []);
  renderSolutions(d);
  renderRoundHistory(d);
  renderMath($("detail"));
}

function renderJob(jobs) {
  const job = jobs.find((item) => activeStatuses.has(item.status)) || jobs[0];
  const panel = $("job-panel");
  if (!job || !activeStatuses.has(job.status)) {
    panel.classList.add("hidden");
    return;
  }
  const percent = progressValue(job.progress);
  panel.classList.remove("hidden");
  $("job-message").textContent = text(job.message, "正在解题");
  $("job-id").textContent = `job · ${jobId(job)}`;
  $("job-progress-label").textContent = `${percent}%`;
  $("job-progress").style.width = `${percent}%`;
}

function solutionCard({ title, source, answer, solution, check, meta, teacher = false, feedback = [] }) {
  const card = el("article", `solution-card ${teacher ? "teacher" : ""}`);
  const head = el("div", "solution-head");
  const headCopy = el("div");
  headCopy.append(el("h3", "", title), el("span", "mono", meta || ""));
  head.append(headCopy, el("div", "answer-pill", answer || "—"));
  const body = el("div", "solution-body");
  body.append(el("p", "solution-label", teacher ? "TEACHER VERIFIED SOLUTION" : "SOLUTION"), el("p", "solution-text", solution || "暂无解题过程"));
  if (check) body.append(el("p", "solution-label", teacher ? "PROCESS REVIEW" : "INDEPENDENT CHECK"), el("p", "solution-text", check));
  if (feedback.length) {
    body.append(el("p", "solution-label", "AGENT FEEDBACK"));
    const list = el("ul", "feedback-list");
    feedback.forEach((item) => {
      const issues = (item.issues || []).join("；") || "无实质问题";
      list.append(el("li", "", `${item.agent_id}：${item.fully_correct ? "正确" : "需修正"} — ${issues}`));
    });
    body.append(list);
  }
  const footer = el("div", "solution-footer");
  footer.append(el("span", "muted", teacher ? "Teacher 独立核验" : "独立上下文输出"));
  const accept = el("button", "accept", "确认此过程并写入 final");
  accept.type = "button";
  accept.addEventListener("click", () => acceptSource(source));
  footer.append(accept);
  card.append(head, body, footer);
  return card;
}

function renderSolutions(detail) {
  const root = $("solutions");
  root.replaceChildren();
  const attempts = Array.isArray(detail.attempts) ? detail.attempts : [];
  ["solver1", "solver2", "solver3"].forEach((agentId, index) => {
    const attempt = attempts.find((item) => item.agent_id === agentId);
    if (!attempt) {
      const missing = solutionCard({ title: `解题 Agent ${index + 1}`, source: agentId, answer: "—", solution: "当前 run 尚无输出", meta: agentId });
      missing.querySelector("button").disabled = true;
      root.append(missing);
      return;
    }
    root.append(solutionCard({
      title: `解题 Agent ${index + 1}`,
      source: agentId,
      answer: attempt.answer,
      solution: attempt.solution,
      check: attempt.independent_check,
      meta: `${attempt.confidence} · ${attempt.question_valid ? "题目有效" : "题面存疑"}`,
    }));
  });
  if (detail.review) {
    root.append(solutionCard({
      title: "Teacher 核验结论",
      source: "teacher",
      answer: detail.review.teacher_answer,
      solution: detail.review.teacher_solution,
      check: detail.review.process_review,
      meta: `${detail.review.verdict} · ${detail.review.answer_consistent ? "答案一致" : "答案不一致"}`,
      teacher: true,
      feedback: detail.review.agent_feedback || [],
    }));
  } else {
    const missing = solutionCard({ title: "Teacher 核验结论", source: "teacher", answer: "—", solution: "尚未完成 Teacher 核验", teacher: true });
    missing.querySelector("button").disabled = true;
    root.append(missing);
  }
}

function renderCodeList(codes, emptyCopy = "无") {
  const root = el("div", "code-list");
  const values = Array.isArray(codes) ? codes.map((code) => text(code)).filter(Boolean) : [];
  if (!values.length) {
    root.append(el("span", "muted", emptyCopy));
    return root;
  }
  values.forEach((code) => root.append(el("code", "enum-code", code)));
  return root;
}

function renderAnnotations(detail) {
  const panel = $("annotation-panel");
  const root = $("annotations-list");
  const annotations = Array.isArray(detail.annotations) ? detail.annotations : [];
  root.replaceChildren();
  if (!annotations.length) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  $("annotation-count").textContent = `${annotations.length} 条审计记录`;
  annotations.forEach((annotation, index) => {
    const status = text(annotation.status, "unreviewed");
    const card = el("article", "annotation-card");
    const head = el("div", "annotation-head");
    const title = el("div");
    title.append(
      el("strong", "", annotationStatusLabels[status] || status),
      el("span", "mono", `记录 ${annotations.length - index} · ${formatDate(annotation.created_at)}`),
    );
    const badgeClass = status === "valid" ? "final" : status === "invalid" ? "invalid" : "neutral";
    head.append(title, el("span", `badge ${badgeClass}`, annotationStatusLabels[status] || status));
    card.append(head);
    if (annotation.summary) card.append(el("p", "annotation-summary", annotation.summary));
    const codes = el("div", "annotation-field");
    codes.append(el("span", "solution-label", "ANNOTATION CODES"), renderCodeList(annotation.issue_codes, "未给出 code"));
    card.append(codes);
    const proposed = annotation.proposed_revision;
    const hasProposed = proposed && typeof proposed === "object" && Object.keys(proposed).length > 0;
    if (hasProposed) {
      const revision = el("section", "proposed-revision");
      revision.append(
        el("p", "proposed-title", "暂存 proposed revision · 未自动改题"),
        el("p", "proposed-prompt", text(proposed.prompt, "（未提供新题干）")),
      );
      const options = Array.isArray(proposed.options) ? proposed.options : [];
      if (options.length) {
        const list = el("ol", "proposed-options");
        options.forEach((option) => {
          list.append(el("li", "", `${text(option.id)}. ${text(option.text)}`));
        });
        revision.append(list);
      }
      const revisionCodes = el("div", "annotation-field");
      revisionCodes.append(el("span", "solution-label", "REVISION CODES"), renderCodeList(proposed.revision_codes, "未给出修订 code"));
      revision.append(revisionCodes);
      card.append(revision);
    } else {
      card.append(el("p", "no-proposed-revision", "本条标注未提出题面修订；原题未被自动修改。"));
    }
    if (annotation.run_id) card.append(el("p", "mono annotation-run", `run · ${annotation.run_id}`));
    root.append(card);
  });
}

function roundLabel(round, index, rounds, currentRunId) {
  const current = text(round.run_id) === text(currentRunId);
  const fallback = /^postverify/i.test(text(round.run_id));
  const first = index === rounds.length - 1;
  if (current && fallback) return "当前轮 · 历史兜底轮";
  if (current && first) return "当前轮 · 首轮";
  if (current) return "当前轮";
  if (fallback) return "历史兜底轮";
  if (first) return "首轮";
  return "历史重解轮";
}

function roundActor(attempt, index) {
  const card = el("details", "round-actor");
  const summary = el("summary");
  const name = text(attempt.agent_id, `solver${index + 1}`);
  summary.append(
    el("strong", "", name),
    el("span", "round-answer", `答案 ${text(attempt.answer, "—")} · ${text(attempt.confidence, "—")} · ${attempt.question_valid ? "题面有效" : "题面存疑"}`),
  );
  const body = el("div", "round-actor-body");
  body.append(el("p", "solution-label", "SOLUTION"), el("p", "solution-text", text(attempt.solution, "暂无过程")));
  if (attempt.independent_check) {
    body.append(el("p", "solution-label", "INDEPENDENT CHECK"), el("p", "solution-text", attempt.independent_check));
  }
  card.append(summary, body);
  return card;
}

function roundTeacher(round) {
  const card = el("details", "round-actor round-teacher");
  const summary = el("summary");
  summary.append(
    el("strong", "", "Teacher"),
    el("span", "round-answer", `${text(round.verdict, "—")} · 答案 ${text(round.teacher_answer, "—")}`),
  );
  const body = el("div", "round-actor-body");
  body.append(el("p", "solution-label", "TEACHER SOLUTION"), el("p", "solution-text", text(round.teacher_solution, "暂无 Teacher 解法")));
  if (round.process_review) {
    body.append(el("p", "solution-label", "PROCESS REVIEW"), el("p", "solution-text", round.process_review));
  }
  card.append(summary, body);
  return card;
}

function renderRetryDiagnostics(round) {
  const retry = round.retry_feedback;
  const section = el("section", "retry-diagnostics");
  section.append(el("p", "solution-label", "TEACHER ROUTING DIAGNOSTICS · 不自动重解"));
  if (!retry || typeof retry !== "object") {
    section.append(el("p", "muted", "本轮没有 retry_feedback。"));
    return section;
  }
  const disposition = text(retry.disposition, "none");
  section.append(el("p", "retry-disposition", retryDispositionLabels[disposition] || disposition));
  const columns = el("div", "diagnostic-columns");
  const issues = el("div");
  issues.append(el("span", "solution-label", "ISSUE CODES"), renderCodeList(retry.issue_codes, "无 issue code"));
  const focus = el("div");
  focus.append(el("span", "solution-label", "FOCUS CODES"), renderCodeList(retry.focus_codes, "无 focus code"));
  columns.append(issues, focus);
  section.append(columns);
  return section;
}

function renderRoundHistory(detail) {
  const panel = $("rounds-panel");
  const root = $("rounds-list");
  const rounds = Array.isArray(detail.rounds) ? detail.rounds : [];
  root.replaceChildren();
  if (!rounds.length) {
    panel.classList.add("hidden");
    panel.open = false;
    return;
  }
  panel.classList.remove("hidden");
  $("rounds-count").textContent = `${rounds.length} 个审计轮次`;
  rounds.forEach((round, index) => {
    const isCurrent = text(round.run_id) === text(detail.current_run_id);
    const card = el("article", `round-card ${isCurrent ? "current" : ""}`);
    const head = el("div", "round-head");
    const title = el("div");
    title.append(
      el("span", "badge neutral", roundLabel(round, index, rounds, detail.current_run_id)),
      el("strong", "", `${text(round.verdict, "未知结论")} · ${round.answer_consistent ? "答案一致" : "答案不一致"}`),
    );
    head.append(
      title,
      el("span", "mono", `${formatDate(round.created_at)} · ${text(round.run_id)}`),
    );
    card.append(head);
    const actors = el("div", "round-actors");
    (round.attempts || []).forEach((attempt, attemptIndex) => actors.append(roundActor(attempt, attemptIndex)));
    actors.append(roundTeacher(round));
    card.append(actors, renderRetryDiagnostics(round));
    root.append(card);
  });
}

async function acceptSource(source) {
  const d = state.detail;
  if (!d) return;
  if (!window.confirm(`确认采用 ${source} 的答案与过程，并写入该节点的 answer_final.jsonl？`)) return;
  try {
    const result = await api(`/api/questions/${d.question_key}/accept`, {
      method: "POST",
      body: JSON.stringify({ source, run_id: d.current_run_id }),
    });
    const extraction = result.skill_extraction;
    if (jobId(extraction)) {
      rememberStartedJob(extraction, `题目 ${d.id} 的确认解法`, {
        kind: "skill_revision",
        taskType: "skill_extraction",
        skillId: extraction.skill_id,
        itemId: extraction.skill_id,
        itemLabel: `题目 ${d.id} 的确认解法`,
      });
    }
    toast(
      "已写入 answer_final",
      extraction ? `${d.id} 已标记为 final；通用解题技能正在后台提炼。` : `${d.id} 已标记为 final。`,
    );
    await Promise.all([loadSummary(), loadQuestions({ keepSelection: false })]);
  } catch (error) {
    toast("写入失败", error.message, "error");
  }
}

async function resolveAndMaybeNext(goNext) {
  const d = state.detail;
  if (!d) return;
  const guidance = $("guidance").value.trim();
  if (!guidance && !window.confirm("未输入任何提示。仍要启动三个全新 Agent 吗？")) return;
  try {
    const job = await api(`/api/questions/${d.question_key}/resolve`, {
      method: "POST",
      body: JSON.stringify({ guidance }),
    });
    rememberStartedJob(job, d.id, { kind: "question_resolve", itemId: d.question_key, itemLabel: d.id, questionId: d.id });
    toast("后台解题已启动", `job ${jobId(job).slice(0, 10)}；可以继续浏览。`);
    $("guidance").value = "";
    await loadSummary();
    if (goNext && state.offset + state.index < state.total - 1) {
      await selectAdjacent(1);
    } else {
      state.detail = await api(`/api/questions/${d.question_key}`);
      renderDetail();
    }
  } catch (error) {
    toast("无法启动重解", error.message, "error");
  }
}

function skillId(skill) {
  return text(skill?.skill_id || skill?.id);
}

function skillName(skill) {
  return text(skill?.name || skill?.title, skillId(skill) || "未命名技能");
}

function skillStatus(skill) {
  return text(skill?.status || skill?.verification_status, "pending");
}

function skillStatusLabel(status) {
  return skillStatusLabels[status] || text(status, "未知状态");
}

function skillStatusClass(status) {
  if (["verified", "passed", "active", "completed"].includes(status)) return "verified";
  if (["failed", "rejected", "error"].includes(status)) return "rejected";
  if (["running", "queued"].includes(status)) return "running";
  return "pending";
}

function versionNumber(version, detail = state.skillDetail) {
  return text(version?.version ?? version?.version_number, text(detail?.current_version, "—"));
}

function versionSha(version, detail = state.skillDetail) {
  return text(version?.sha256 || version?.content_sha256 || version?.skill_sha256, text(detail?.current_sha256 || detail?.sha256));
}

function versionKey(version, detail = state.skillDetail) {
  return versionSha(version, detail) || `version:${versionNumber(version, detail)}`;
}

function normalizeVersions(detail) {
  const versions = Array.isArray(detail?.versions) ? detail.versions.filter((item) => item && typeof item === "object") : [];
  const currentSha = text(detail?.current_sha256);
  const currentVersion = text(detail?.current_version);
  const hasCurrent = versions.some((item) => (
    (currentSha && versionSha(item, detail) === currentSha)
    || (currentVersion && versionNumber(item, detail) === currentVersion)
  ));
  if (!hasCurrent) {
    versions.unshift({
      version: detail?.current_version,
      sha256: detail?.current_sha256,
      status: detail?.status,
      source: detail?.metadata?.source,
      verification_run_id: detail?.metadata?.verification_run_id,
      created_at: detail?.updated_at,
      content: detail?.content,
      synthetic: true,
    });
  }
  if (!versions.length) versions.push({ content: detail?.content, synthetic: true });
  const seen = new Set();
  return versions.filter((item) => {
    const key = versionKey(item, detail);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function isCurrentSkillVersion(version, detail = state.skillDetail) {
  const currentSha = text(detail?.current_sha256);
  if (currentSha && versionSha(version, detail)) return versionSha(version, detail) === currentSha;
  return versionNumber(version, detail) === text(detail?.current_version);
}

function selectedSkillVersion(detail = state.skillDetail) {
  const versions = normalizeVersions(detail);
  const selected = versions.find((version) => versionKey(version, detail) === state.skillSelectedVersion);
  return selected || versions.find((version) => isCurrentSkillVersion(version, detail)) || versions[0];
}

function renderSkillList() {
  const root = $("skill-list");
  root.replaceChildren();
  const first = state.skills.length ? state.skillOffset + 1 : 0;
  const last = state.skillOffset + state.skills.length;
  $("skill-list-count").textContent = `${first}–${last} / ${state.skillTotal || 0} 个技能`;
  const pageCount = Math.max(1, Math.ceil((state.skillTotal || 0) / state.skillPageSize));
  const page = Math.floor(state.skillOffset / state.skillPageSize) + 1;
  $("skill-page-label").textContent = `第 ${page} / ${pageCount} 页`;
  $("skill-page-prev").disabled = state.skillOffset <= 0;
  $("skill-page-next").disabled = state.skillOffset + state.skills.length >= state.skillTotal;
  state.skills.forEach((item, index) => {
    const id = skillId(item);
    const status = skillStatus(item);
    const button = el("button", `question-item skill-item ${skillId(state.skillDetail) === id ? "active" : ""}`);
    button.type = "button";
    const top = el("div", "item-top");
    top.append(
      el("span", "item-id", skillName(item)),
      el("span", `mini-badge skill-${skillStatusClass(status)}`, skillStatusLabel(status)),
    );
    const version = text(item.current_version, "—");
    const sha = text(item.current_sha256).slice(0, 10);
    button.append(
      top,
      el("p", "item-prompt", text(item.description, "（暂无说明）")),
      el("div", "item-node", `v${version}${sha ? ` · ${sha}` : ""} · ${formatDate(item.updated_at)}`),
    );
    button.addEventListener("click", () => selectSkill(index));
    root.append(button);
  });
}

async function loadSkills({ keepSelection = true, resetOffset = false, edge = "first" } = {}) {
  if (resetOffset) state.skillOffset = 0;
  const selectedId = keepSelection ? skillId(state.skillDetail) : null;
  const params = new URLSearchParams({
    query: $("skill-search").value.trim(),
    limit: String(state.skillPageSize),
    offset: String(state.skillOffset),
  });
  try {
    let data = await api(`/api/skills?${params}`);
    if (!(data.items || []).length && asNumber(data.total) > 0 && state.skillOffset > 0) {
      const effectiveLimit = Math.max(1, asNumber(data.limit, state.skillPageSize));
      state.skillOffset = Math.floor((asNumber(data.total) - 1) / effectiveLimit) * effectiveLimit;
      params.set("offset", String(state.skillOffset));
      data = await api(`/api/skills?${params}`);
    }
    state.skills = Array.isArray(data.items) ? data.items : [];
    state.skillTotal = asNumber(data.total);
    state.skillPageSize = Math.max(1, asNumber(data.limit, state.skillPageSize));
    state.skillOffset = Math.max(0, asNumber(data.offset, state.skillOffset));
    state.skillsLoaded = true;
    let index = selectedId ? state.skills.findIndex((item) => skillId(item) === selectedId) : -1;
    if (index < 0 && state.skills.length) index = edge === "last" ? state.skills.length - 1 : 0;
    renderSkillList();
    if (index >= 0) await selectSkill(index, { preserveVersion: Boolean(selectedId) });
    else showSkillEmpty();
  } catch (error) {
    toast("无法读取技能库", error.message, "error");
  }
}

function showSkillEmpty() {
  state.skillDetail = null;
  state.skillIndex = -1;
  state.skillSelectedVersion = null;
  $("skill-detail").classList.add("hidden");
  $("skill-empty-state").classList.remove("hidden");
  renderSkillList();
}

async function selectSkill(index, { preserveVersion = false } = {}) {
  if (!state.skills.length) return showSkillEmpty();
  state.skillIndex = Math.max(0, Math.min(index, state.skills.length - 1));
  const item = state.skills[state.skillIndex];
  if (!preserveVersion || skillId(state.skillDetail) !== skillId(item)) state.skillSelectedVersion = null;
  try {
    state.skillDetail = await api(`/api/skills/${encodeURIComponent(skillId(item))}`);
    $("skill-empty-state").classList.add("hidden");
    $("skill-detail").classList.remove("hidden");
    renderSkillDetail();
    renderSkillList();
    if (state.view === "skills") document.title = `技能 ${skillName(state.skillDetail)}`;
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (error) {
    toast("无法读取技能详情", error.message, "error");
  }
}

async function selectAdjacentSkill(delta) {
  const target = state.skillIndex + delta;
  if (target >= 0 && target < state.skills.length) return selectSkill(target);
  if (delta > 0 && state.skillOffset + state.skills.length < state.skillTotal) {
    state.skillOffset += state.skillPageSize;
    return loadSkills({ keepSelection: false, edge: "first" });
  }
  if (delta < 0 && state.skillOffset > 0) {
    state.skillOffset = Math.max(0, state.skillOffset - state.skillPageSize);
    return loadSkills({ keepSelection: false, edge: "last" });
  }
}

function skillJobs(detail) {
  const id = skillId(detail);
  const candidates = [];
  (detail?.jobs || []).forEach((job) => candidates.push(job));
  (state.summary?.active_jobs || []).forEach((job) => {
    const itemId = text(job.item_id || job.skill_id);
    if (isSkillJob(job) && itemId === id) candidates.push(job);
  });
  const unique = new Map();
  candidates.forEach((job) => {
    const idValue = jobId(job);
    if (idValue) unique.set(idValue, job);
  });
  return [...unique.values()];
}

function renderSkillJob(detail) {
  const jobs = skillJobs(detail);
  const job = jobs.find((item) => activeStatuses.has(item.status));
  const panel = $("skill-job-panel");
  if (!job) {
    panel.classList.add("hidden");
    return;
  }
  const percent = progressValue(job.progress);
  panel.classList.remove("hidden");
  $("skill-job-message").textContent = text(job.message, job.status === "queued" ? "技能修订已排队" : "正在修订并验证技能");
  $("skill-job-id").textContent = `job · ${jobId(job)}`;
  $("skill-job-progress-label").textContent = `${percent}%`;
  $("skill-job-progress").style.width = `${percent}%`;
}

function verificationData(detail, version) {
  const metadata = detail?.metadata && typeof detail.metadata === "object" ? detail.metadata : {};
  const versionMetadata = version?.metadata && typeof version.metadata === "object" ? version.metadata : {};
  return version?.post_verification
    || version?.verification
    || versionMetadata.post_verification
    || versionMetadata.verification
    || metadata.post_verification
    || metadata.verification
    || detail?.post_verification
    || detail?.verification
    || null;
}

function appendVerificationAttempts(root, verification) {
  const attempts = Array.isArray(verification?.attempts)
    ? verification.attempts
    : Array.isArray(verification?.solutions) ? verification.solutions : [];
  if (!attempts.length) return;
  const grid = el("div", "verification-attempts");
  attempts.forEach((attempt, index) => {
    const card = el("article", "verification-attempt");
    card.append(
      el("strong", "", text(attempt.agent_id || attempt.agent, `Agent ${index + 1}`)),
      el("span", "answer-pill small-answer", text(attempt.answer, "—")),
      el("p", "solution-text", text(attempt.solution || attempt.reasoning, "暂无过程")),
    );
    grid.append(card);
  });
  root.append(grid);
}

function renderSkillVerification(detail, version) {
  const root = $("skill-verification-body");
  root.replaceChildren();
  const status = text(version?.status || detail?.status, "pending");
  const runId = text(version?.verification_run_id || detail?.metadata?.verification_run_id);
  $("skill-verification-run").textContent = runId ? `run · ${runId}` : "尚无 verification run";
  const summary = el("div", "verification-summary");
  summary.append(
    el("span", `badge skill-${skillStatusClass(status)}`, skillStatusLabel(status)),
    el("p", "", runId ? `校验 run ${runId}` : "此版本尚未返回详细校验记录。"),
  );
  root.append(summary);
  const verification = verificationData(detail, version);
  if (!verification) return;
  const message = verification.summary || verification.message || verification.process_review;
  if (message) root.append(el("p", "verification-message", text(message)));
  appendVerificationAttempts(root, verification);
  const teacher = verification.teacher || verification.review || verification.teacher_review;
  if (teacher && typeof teacher === "object") {
    const card = el("article", "verification-teacher");
    card.append(
      el("p", "solution-label", "TEACHER POST-VERIFICATION"),
      el("h3", "", text(teacher.verdict || teacher.status, "Teacher 核验")),
      el("p", "solution-text", text(teacher.teacher_solution || teacher.solution || teacher.process_review, "暂无 Teacher 说明")),
    );
    root.append(card);
  }
  const details = el("details", "verification-raw");
  details.append(el("summary", "", "查看原始验证记录"), el("pre", "skill-content", displayValue(verification)));
  root.append(details);
  renderMath(root);
}

function renderSkillVersionOptions(detail, selected) {
  const select = $("skill-version-select");
  const versions = normalizeVersions(detail);
  select.replaceChildren();
  versions.forEach((version) => {
    const number = versionNumber(version, detail);
    const status = skillStatusLabel(text(version.status || detail.status, "pending"));
    const current = isCurrentSkillVersion(version, detail) ? " · 当前" : "";
    const created = version.created_at ? ` · ${formatDate(version.created_at)}` : "";
    select.append(new Option(`v${number} · ${status}${current}${created}`, versionKey(version, detail)));
  });
  state.skillSelectedVersion = versionKey(selected, detail);
  select.value = state.skillSelectedVersion;
  select.disabled = versions.length <= 1;
}

function renderSkillDetail() {
  const detail = state.skillDetail;
  if (!detail) return;
  const selected = selectedSkillVersion(detail);
  const selectedStatus = text(selected?.status || detail.status, "pending");
  const selectedVersion = versionNumber(selected, detail);
  const selectedSha = versionSha(selected, detail);
  const current = isCurrentSkillVersion(selected, detail);
  const currentSha = text(detail.current_sha256 || detail.sha256);
  $("skill-prev").disabled = state.skillOffset + state.skillIndex <= 0;
  $("skill-next").disabled = state.skillOffset + state.skillIndex >= state.skillTotal - 1;
  $("skill-status-badge").textContent = skillStatusLabel(selectedStatus);
  $("skill-status-badge").className = `badge skill-${skillStatusClass(selectedStatus)}`;
  $("skill-version-badge").textContent = `v${selectedVersion}${current ? " · 当前" : " · 历史"}`;
  $("skill-version-badge").className = `badge ${current ? "final" : "neutral"}`;
  $("skill-id").textContent = skillId(detail);
  $("skill-title").textContent = skillName(detail);
  const tags = Array.isArray(detail.tags) && detail.tags.length ? `\n标签：${detail.tags.join(" · ")}` : "";
  $("skill-description").textContent = `${text(detail.description, "（暂无说明）")}${tags}`;
  renderSkillVersionOptions(detail, selected);
  $("skill-sha").textContent = selectedSha || "—";
  $("skill-updated").textContent = formatDate(selected?.created_at || detail.updated_at);
  $("skill-source").textContent = displayValue(selected?.source || detail.metadata?.source);
  $("skill-verification-status").textContent = skillStatusLabel(selectedStatus);
  $("skill-verification-status").className = `status-text skill-${skillStatusClass(selectedStatus)}`;
  $("skill-content-version").textContent = `v${selectedVersion}${current ? " · 当前版本" : " · 历史版本"}`;
  const content = text(selected?.content, current ? text(detail.content) : "");
  $("skill-content").textContent = content || "该历史版本的正文未由详情 API 返回；仍可核对 SHA、来源与验证记录。";
  $("skill-content").classList.toggle("empty-content", !content);
  $("skill-revision-base").textContent = currentSha
    ? `修订基线（当前版本）· ${currentSha}`
    : "当前版本缺少 SHA-256，暂不能提交受审计修订。";
  const hasActiveJob = skillJobs(detail).some((job) => activeStatuses.has(job.status));
  $("skill-revise").disabled = !currentSha || hasActiveJob;
  $("skill-revise-next").disabled = !currentSha || hasActiveJob;
  renderSkillJob(detail);
  renderSkillVerification(detail, selected);
}

async function reviseSkillAndMaybeNext(goNext) {
  const detail = state.skillDetail;
  if (!detail) return;
  const id = skillId(detail);
  const guidance = $("skill-guidance").value.trim();
  const baseSha = text(detail.current_sha256 || detail.sha256);
  if (!guidance) {
    toast("请输入修订提示", "说明需要补充、纠正或验证的内容后再提交。", "error");
    $("skill-guidance").focus();
    return;
  }
  if (!baseSha) {
    toast("无法建立修订基线", "技能详情缺少 current_sha256。", "error");
    return;
  }
  try {
    const job = await api(`/api/skills/${encodeURIComponent(id)}/revisions`, {
      method: "POST",
      body: JSON.stringify({ base_sha256: baseSha, guidance }),
    });
    rememberStartedJob(job, skillName(detail), {
      kind: "skill_revision",
      skillId: id,
      itemId: id,
      itemLabel: skillName(detail),
    });
    detail.jobs = [{
      ...job,
      kind: "skill_revision",
      skill_id: id,
      progress: progressValue(job.progress),
      message: text(job.message, "技能修订已排队"),
    }, ...(detail.jobs || [])];
    $("skill-guidance").value = "";
    toast("技能修订已启动", `job ${jobId(job).slice(0, 10)}；修订与结构、基线及版本链校验会在后台完成。`);
    renderSkillDetail();
    await loadSummary();
    if (goNext && state.skillOffset + state.skillIndex < state.skillTotal - 1) {
      await selectAdjacentSkill(1);
    } else {
      try {
        state.skillDetail = await api(`/api/skills/${encodeURIComponent(id)}`);
        renderSkillDetail();
      } catch (_) { /* optimistic queued state remains visible until polling succeeds */ }
    }
  } catch (error) {
    toast("无法启动技能修订", error.message, "error");
  }
}

async function setView(view) {
  const nextView = ["seed", "candidate", "skills"].includes(view) ? view : "seed";
  const previousView = state.view;
  state.view = nextView;
  if (nextView !== "skills") state.reviewBucket = nextView;
  const skills = state.view === "skills";
  $("questions-view").classList.toggle("hidden", skills);
  $("skills-view").classList.toggle("hidden", !skills);
  $("show-seed").classList.toggle("active", state.view === "seed");
  $("show-candidate").classList.toggle("active", state.view === "candidate");
  $("show-skills").classList.toggle("active", skills);
  $("show-seed").setAttribute("aria-pressed", String(state.view === "seed"));
  $("show-candidate").setAttribute("aria-pressed", String(state.view === "candidate"));
  $("show-skills").setAttribute("aria-pressed", String(skills));
  if (skills) {
    await loadSkills({ keepSelection: state.skillsLoaded });
    if (state.skillDetail) document.title = `技能 ${skillName(state.skillDetail)}`;
    else document.title = "解题技能库｜题库共识审校台";
  } else {
    const bucketChanged = previousView !== nextView;
    if (bucketChanged && !state.summary?.review_view?.fixed) {
      $("status-filter").value = defaultReviewStatuses[nextView];
    }
    await loadQuestions({ keepSelection: !bucketChanged, resetOffset: bucketChanged });
    document.title = state.detail ? `审校 ${state.detail.id}` : "题库共识审校台";
  }
}

$("prev").addEventListener("click", () => selectAdjacent(-1));
$("next").addEventListener("click", () => selectAdjacent(1));
$("page-prev").addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - state.pageSize);
  loadQuestions({ keepSelection: false, edge: "first" });
});
$("page-next").addEventListener("click", () => {
  if (state.offset + state.items.length >= state.total) return;
  state.offset += state.pageSize;
  loadQuestions({ keepSelection: false, edge: "first" });
});
$("refresh").addEventListener("click", () => Promise.all([loadSummary(), loadQuestions()]));
$("status-filter").addEventListener("change", () => loadQuestions({ keepSelection: false, resetOffset: true }));
$("subject-filter").addEventListener("change", () => loadQuestions({ keepSelection: false, resetOffset: true }));
$("search").addEventListener("input", () => {
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(() => loadQuestions({ keepSelection: false, resetOffset: true }), 280);
});
$("copy-id").addEventListener("click", async () => {
  if (!state.detail) return;
  try {
    await navigator.clipboard.writeText(state.detail.id);
    toast("题号已复制", state.detail.id);
  } catch (error) { toast("复制失败", error.message, "error"); }
});
$("resolve").addEventListener("click", () => resolveAndMaybeNext(false));
$("resolve-next").addEventListener("click", () => resolveAndMaybeNext(true));

$("show-seed").addEventListener("click", () => setView("seed"));
$("show-candidate").addEventListener("click", () => setView("candidate"));
$("show-skills").addEventListener("click", () => setView("skills"));
$("skill-prev").addEventListener("click", () => selectAdjacentSkill(-1));
$("skill-next").addEventListener("click", () => selectAdjacentSkill(1));
$("skill-page-prev").addEventListener("click", () => {
  state.skillOffset = Math.max(0, state.skillOffset - state.skillPageSize);
  loadSkills({ keepSelection: false, edge: "first" });
});
$("skill-page-next").addEventListener("click", () => {
  if (state.skillOffset + state.skills.length >= state.skillTotal) return;
  state.skillOffset += state.skillPageSize;
  loadSkills({ keepSelection: false, edge: "first" });
});
$("skill-refresh").addEventListener("click", () => Promise.all([loadSummary(), loadSkills()]));
$("skill-search").addEventListener("input", () => {
  clearTimeout(state.skillSearchTimer);
  state.skillSearchTimer = setTimeout(() => loadSkills({ keepSelection: false, resetOffset: true }), 280);
});
$("skill-version-select").addEventListener("change", (event) => {
  state.skillSelectedVersion = event.target.value;
  renderSkillDetail();
});
$("skill-copy-id").addEventListener("click", async () => {
  if (!state.skillDetail) return;
  try {
    await navigator.clipboard.writeText(skillId(state.skillDetail));
    toast("技能 ID 已复制", skillId(state.skillDetail));
  } catch (error) { toast("复制失败", error.message, "error"); }
});
$("skill-revise").addEventListener("click", () => reviseSkillAndMaybeNext(false));
$("skill-revise-next").addEventListener("click", () => reviseSkillAndMaybeNext(true));

async function poll() {
  if (state.polling) return;
  state.polling = true;
  try {
    const terminalChanged = await loadSummary();
    if (terminalChanged.question && state.view !== "skills") {
      await loadQuestions({ keepSelection: true });
    }
    if (terminalChanged.skill) {
      if (state.view === "skills") {
        if (terminalChanged.skillIds.includes(skillId(state.skillDetail))) state.skillSelectedVersion = null;
        await loadSkills({ keepSelection: true });
      }
      else state.skillsLoaded = false;
    }
    if (!terminalChanged.question && state.view !== "skills" && state.detail) {
      try {
        const latest = await api(`/api/questions/${state.detail.question_key}`);
        const changed = latest.updated_at !== state.detail.updated_at
          || latest.current_run_id !== state.detail.current_run_id
          || JSON.stringify(latest.jobs) !== JSON.stringify(state.detail.jobs)
          || JSON.stringify(latest.rounds) !== JSON.stringify(state.detail.rounds)
          || JSON.stringify(latest.annotations) !== JSON.stringify(state.detail.annotations);
        state.detail = latest;
        if (changed) renderDetail();
      } catch (_) { /* transient refresh errors are shown on the next full load */ }
    }
    if (!terminalChanged.skill && state.view === "skills" && state.skillDetail) {
      try {
        const id = skillId(state.skillDetail);
        const latest = await api(`/api/skills/${encodeURIComponent(id)}`);
        const changed = latest.updated_at !== state.skillDetail.updated_at
          || latest.current_sha256 !== state.skillDetail.current_sha256
          || JSON.stringify(latest.jobs) !== JSON.stringify(state.skillDetail.jobs)
          || JSON.stringify(latest.versions) !== JSON.stringify(state.skillDetail.versions);
        state.skillDetail = latest;
        if (changed) renderSkillDetail();
      } catch (_) { /* transient refresh errors are shown on the next full load */ }
    }
  } finally {
    state.polling = false;
  }
}

(async function init() {
  if (window.location.protocol === "file:") {
    $("file-protocol-warning").classList.remove("hidden");
    return;
  }
  await loadSummary();
  await loadQuestions({ keepSelection: false });
  setInterval(poll, 2500);
})();
