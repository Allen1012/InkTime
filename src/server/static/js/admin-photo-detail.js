(() => {
  "use strict";

  const root = document.querySelector("[data-photo-detail]");
  const form = document.getElementById("photo-edit-form");
  const saveButton = document.getElementById("photo-save-button");
  const status = document.getElementById("analysis-draft-status");
  if (!root || !form || !saveButton || !status) return;

  // curation 属于可编辑字段但不属于「生成结果」：重新分析不会覆盖人工收录判断，
  // 所以它只进 editableNames，不进 generatedNames。漏掉它会导致改了收录状态而
  // 保存按钮仍然禁用。
  const editableNames = [
    "category", "analysis_status", "curation", "caption", "side_caption",
    "memory_score", "beauty_score", "reason", "date_taken", "exif_city"
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

  // ===== 照片预览：全屏、箭头自动隐藏、全屏内翻页 =====
  //
  // 全屏容器取整个预览框而不是图片按钮：箭头是按钮的兄弟节点，对按钮请求全屏
  // 会把箭头留在全屏之外，全屏里就没法翻页了。
  //
  // 全屏内翻页不能走链接跳转——导航会让浏览器退出全屏，「全屏连续看图」就断了。
  // 所以全屏态下拦截点击，改为换 img.src 加 history.replaceState，并向
  // /api/admin/photos/<id>/adjacent 取新的邻居；退出全屏时若照片已经变了就
  // 整页刷新一次，让表单、EXIF、生命周期这些区域与地址栏里的照片对齐。
  const viewer = document.querySelector("[data-detail-viewer]");
  if (viewer) {
    const image = viewer.querySelector("[data-detail-image]");
    const trigger = viewer.querySelector("[data-fullscreen-trigger]");
    const prevLink = viewer.querySelector("[data-nav-prev]");
    const nextLink = viewer.querySelector("[data-nav-next]");
    const adjacentTemplate = viewer.dataset.adjacentUrl || "";
    const returnQuery = viewer.dataset.returnQuery || "";
    // 静置隐藏延迟与展示页保持一致的量级：太短会在移动鼠标的间隙里闪
    const HIDE_DELAY_MS = 2500;
    let hideTimer = null;
    let initialPhotoId = currentPhotoId();
    let navigating = false;
    // 邻居查询未返回时排队的点击方向与次数，避免连续点击被静默丢弃
    let pending = null;

    function currentPhotoId() {
      const matched = window.location.pathname.match(/\/admin\/photos\/(\d+)/);
      return matched ? matched[1] : "";
    }

    function scheduleHide() {
      window.clearTimeout(hideTimer);
      viewer.classList.remove("ui-hidden");
      hideTimer = window.setTimeout(() => {
        viewer.classList.add("ui-hidden");
      }, HIDE_DELAY_MS);
    }

    ["mousemove", "mousedown", "keydown", "touchstart"].forEach((name) => {
      viewer.addEventListener(name, scheduleHide, { passive: true });
    });
    viewer.addEventListener("mouseleave", () => {
      window.clearTimeout(hideTimer);
      viewer.classList.add("ui-hidden");
    });

    /** 把某一侧的箭头设为可用或禁用，全屏内换图后同步邻居可用性。 */
    function applyNeighbour(link, photoId) {
      if (!link) return;
      if (photoId) {
        link.dataset.photoId = String(photoId);
        link.classList.remove("is-disabled");
        link.removeAttribute("aria-disabled");
        link.setAttribute(
          "href",
          "/admin/photos/" + photoId + (returnQuery ? "?return_query=" + encodeURIComponent(returnQuery) : "")
        );
      } else {
        delete link.dataset.photoId;
        link.classList.add("is-disabled");
        link.setAttribute("aria-disabled", "true");
        // 移除属性而不是置空：href="" 在部分浏览器等价于当前页地址，
        // 一旦哪条防护失效就会变成重载当前页
        link.removeAttribute("href");
      }
    }

    /** 读出某一侧当前可用的邻居编号，禁用或到端时返回空串。 */
    function neighbourId(direction) {
      const link = direction === "prev" ? prevLink : nextLink;
      if (!link || link.classList.contains("is-disabled")) return "";
      const fromData = link.dataset.photoId;
      if (fromData) return fromData;
      const matched = (link.getAttribute("href") || "").match(/\/photos\/(\d+)/);
      return matched ? matched[1] : "";
    }

    /**
     * 请求在全屏内朝某个方向翻一张。
     *
     * 上一次的邻居请求还没回来时把这次点击排队而不是丢弃：邻居查询要一次网络往返，
     * 而连续点击的间隔常常更短，直接 return 会让「点了三下只前进一张」。排队深度
     * 设上限，避免按住不放堆积出一长串跳转。
     */
    function navigate(direction) {
      const photoId = neighbourId(direction);
      if (!photoId) {
        pending = null;
        return;
      }
      if (navigating) {
        if (pending && pending.direction === direction) {
          pending.remaining = Math.min(pending.remaining + 1, 5);
        } else {
          pending = { direction, remaining: 1 };
        }
        return;
      }
      showInFullscreen(photoId);
    }

    /** 在全屏内切到指定照片：换图、改地址、再取新的邻居。 */
    async function showInFullscreen(photoId) {
      navigating = true;
      try {
        // 换图与地址先做完，不等邻居查询：用户要的是图立刻变
        image.src = "/admin/photos/" + photoId + "/full";
        const target =
          "/admin/photos/" + photoId + (returnQuery ? "?return_query=" + encodeURIComponent(returnQuery) : "");
        window.history.replaceState({}, "", target);
        const url = adjacentTemplate.replace(/\/\d+\/adjacent$/, "/" + photoId + "/adjacent");
        const response = await fetch(
          url + (returnQuery ? "?return_query=" + encodeURIComponent(returnQuery) : ""),
          { credentials: "same-origin", headers: { Accept: "application/json" } }
        );
        if (!response.ok) throw new Error(String(response.status));
        const body = await response.json();
        applyNeighbour(prevLink, (body.data || {}).previous_id);
        applyNeighbour(nextLink, (body.data || {}).next_id);
      } catch (error) {
        // 取邻居失败不影响已经换好的图；两侧按钮保持原样，退出全屏后整页刷新会纠正
        console.warn("[photo-detail] 全屏内取邻居失败：" + (error && error.message));
      } finally {
        navigating = false;
      }
      // 邻居已更新，继续消费排队中的点击
      if (pending) {
        const direction = pending.direction;
        pending.remaining -= 1;
        if (pending.remaining <= 0) pending = null;
        navigate(direction);
      }
    }

    [prevLink, nextLink].forEach((link) => {
      if (!link) return;
      link.addEventListener("click", (event) => {
        // 双击/三击已经建立的选区会在图片上留下一层系统选中色，这里兜底清掉。
        // CSS 的 user-select: none 是主防线，这条是防止其他祖先节点产生的选区。
        const selection = window.getSelection();
        if (selection && selection.rangeCount) selection.removeAllRanges();
        if (link.classList.contains("is-disabled")) {
          event.preventDefault();
          return;
        }
        // 非全屏时保持普通链接语义：整页跳转，表单与元数据自然跟着更新
        if (!document.fullscreenElement) return;
        event.preventDefault();
        navigate(link === prevLink ? "prev" : "next");
      });
    });

    if (trigger) {
      trigger.addEventListener("click", () => {
        if (document.fullscreenElement) {
          document.exitFullscreen().catch(() => {});
          return;
        }
        const request = viewer.requestFullscreen || viewer.webkitRequestFullscreen;
        // 不支持时什么都不做：图片本来就正常显示在页面上
        if (typeof request !== "function") return;
        const result = request.call(viewer);
        if (result && typeof result.catch === "function") {
          result.catch((error) => {
            console.warn("[photo-detail] 无法进入全屏：" + (error && error.message));
          });
        }
      });
    }

    document.addEventListener("fullscreenchange", () => {
      if (document.fullscreenElement) {
        scheduleHide();
        return;
      }
      // 退出全屏后页面其余部分仍是进入时那张照片的数据，地址已变则刷新对齐
      if (currentPhotoId() !== initialPhotoId) window.location.reload();
    });

    scheduleHide();
  }

  updateDirty();
  loadLatest();
})();
