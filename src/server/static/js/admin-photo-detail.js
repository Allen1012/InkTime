(() => {
  "use strict";

  const root = document.querySelector("[data-photo-detail]");
  const form = document.getElementById("photo-edit-form");
  const saveButton = document.getElementById("photo-save-button");
  const status = document.getElementById("analysis-draft-status");
  if (!root || !form || !saveButton || !status) return;

  const editableNames = [
    "category", "analysis_status", "caption", "side_caption", "memory_score",
    "beauty_score", "reason", "date_taken", "exif_city"
  ];
  const generatedNames = new Set([
    "category", "analysis_status", "caption", "side_caption", "memory_score",
    "beauty_score", "reason"
  ]);
  const initial = new Map();
  let resultBaseline = new Map(initial);
  let pollTimer = null;
  let active = false;
  let requestGeneration = 0;

  const control = (name) => form.elements.namedItem(name);
  const serialize = () => editableNames.map((name) => {
    const field = control(name);
    return [name, field ? String(field.value) : ""];
  });
  const generatedValues = () => new Map(
    serialize().filter(([name]) => generatedNames.has(name))
  );
  serialize().forEach(([name, value]) => initial.set(name, value));
  resultBaseline = generatedValues();

  const updateDirty = () => {
    saveButton.disabled = !serialize().some(([name, value]) => initial.get(name) !== value);
  };
  form.addEventListener("input", updateDirty);
  form.addEventListener("change", updateDirty);

  const setStatus = (message, isError = false) => {
    status.textContent = message;
    status.classList.toggle("status-error", isError);
  };
  const setGenerateDisabled = (disabled) => {
    document.querySelectorAll("[data-analysis-draft-button]").forEach((button) => {
      button.disabled = disabled;
    });
  };
  const schedulePoll = () => {
    window.clearTimeout(pollTimer);
    if (active && !document.hidden) pollTimer = window.setTimeout(loadLatest, 2500);
  };
  const applyResult = (job) => {
    const result = job && job.result;
    const fields = result && result.fields;
    if (!fields || typeof fields !== "object") return 0;
    let conflicts = 0;
    Object.entries(fields).forEach(([name, value]) => {
      if (!generatedNames.has(name)) return;
      if (job.job_type === "generate_narration" && name !== "side_caption") return;
      const field = control(name);
      if (!field || !(typeof value === "string" || typeof value === "number" || value === null)) return;
      if (resultBaseline.has(name) && String(field.value) !== resultBaseline.get(name)) {
        conflicts += 1;
        return;
      }
      field.value = value === null ? "" : String(value);
    });
    updateDirty();
    return conflicts;
  };
  const handleJob = (job) => {
    if (!job) {
      active = false;
      setGenerateDisabled(false);
      setStatus("暂无待确认的生成结果");
      return;
    }
    active = job.status === "pending" || job.status === "running";
    setGenerateDisabled(active);
    if (job.status === "succeeded") {
      const conflicts = applyResult(job);
      setStatus(conflicts > 0
        ? `生成完成，已保留 ${conflicts} 个等待期间手工修改的字段，请确认后保存`
        : "生成完成，结果已填入表单，请确认后保存");
    } else if (job.status === "failed" || job.status === "canceled") {
      setStatus(job.error_summary || "生成任务未完成，请重试", true);
    } else {
      setStatus(job.status === "running" ? `正在生成（${job.progress || 0}%）` : "已排队，等待生成");
    }
    schedulePoll();
  };
  async function loadLatest() {
    const generation = ++requestGeneration;
    try {
      const response = await fetch(root.dataset.draftUrl, {headers: {"Accept": "application/json"}});
      if (!response.ok) throw new Error(`查询失败（HTTP ${response.status}）`);
      const payload = await response.json();
      if (generation === requestGeneration) handleJob(payload.data || null);
    } catch (error) {
      if (generation !== requestGeneration) return;
      active = false;
      setGenerateDisabled(false);
      setStatus(error instanceof Error ? error.message : "查询生成状态失败", true);
    }
  }
  async function enqueue(button) {
    const formId = button.getAttribute("form");
    const requestForm = formId ? document.getElementById(formId) : null;
    if (!requestForm) return;
    const generation = ++requestGeneration;
    resultBaseline = generatedValues();
    setGenerateDisabled(true);
    setStatus("正在提交生成任务");
    try {
      const response = await fetch(requestForm.action, {
        method: "POST",
        headers: {"Accept": "application/json"},
        body: new FormData(requestForm),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const message = (payload.error && payload.error.message) || payload.message || payload.error || `提交失败（HTTP ${response.status}）`;
        throw new Error(message);
      }
      if (generation !== requestGeneration) return;
      active = true;
      handleJob(payload.data || null);
    } catch (error) {
      if (generation !== requestGeneration) return;
      active = false;
      setGenerateDisabled(false);
      setStatus(error instanceof Error ? error.message : "提交生成任务失败", true);
    }
  }
  document.querySelectorAll("[data-analysis-draft-button]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.preventDefault();
      enqueue(button);
    });
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) window.clearTimeout(pollTimer);
    else if (active) loadLatest();
  });
  updateDirty();
  loadLatest();
})();
