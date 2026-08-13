/**
 * 后台任务页面自动轮询刷新。
 *
 * 策略：
 * - 有活跃任务（pending/running）时 3 秒轮询
 * - 全部终态时 30 秒轮询
 * - 页面不可见时暂停
 * - 连续无变化时逐步退避
 * - 支持 ETag 条件请求（304 不消耗带宽）
 */
(function () {
  'use strict';

  var POLL_FAST = 3000;
  var POLL_SLOW = 30000;
  var POLL_MAX = 60000;
  var ACTIVE_STATUSES = { pending: 1, running: 1 };
  var TERMINAL_STATUSES = { succeeded: 1, failed: 1, canceled: 1 };

  var timerId = null;
  var inFlight = false;
  var lastEtag = '';
  var unchangedCount = 0;
  var currentInterval = POLL_FAST;
  var indicator = document.getElementById('poll-indicator');

  var QUEUE_LABELS = { photo: '照片', maintenance: '维护' };
  var TYPE_LABELS = {
    analyze_photo: '照片分析', generate_narration: '重写旁白',
    backfill_content_hash: '摘要回填', render_display: '展示产物渲染',
    cleanup_expired_trash: '回收站过期清理'
  };

  function setIndicator(state) {
    if (!indicator) return;
    indicator.className = 'poll-indicator poll-' + state;
    indicator.title = state === 'active' ? '自动刷新中'
      : state === 'paused' ? '已暂停（页面不可见）'
      : state === 'idle' ? '任务均已完成，低频刷新' : '';
  }

  function hasActive(jobs) {
    for (var i = 0; i < jobs.length; i++) {
      if (ACTIVE_STATUSES[jobs[i].status]) return true;
    }
    return false;
  }

  function scheduleNext(jobs) {
    if (hasActive(jobs)) {
      unchangedCount = 0;
      currentInterval = POLL_FAST;
      setIndicator('active');
    } else {
      currentInterval = Math.min(POLL_MAX, POLL_SLOW + unchangedCount * 5000);
      setIndicator('idle');
    }
    timerId = setTimeout(poll, currentInterval);
  }

  /** 创建一个单元格并按需设置文本或 HTML。 */
  function cell(className) {
    var td = document.createElement('td');
    if (className) td.className = className;
    return td;
  }

  /** 构造取消/重试表单，action 用校验过的队列名与整数编号拼接。 */
  function actionForm(queue, id, action, label, buttonClass, enabled, disabledTitle) {
    var form = document.createElement('form');
    form.method = 'post';
    form.action = '/admin/jobs/' + encodeURIComponent(queue) + '/' + id + '/' + action;
    var token = document.createElement('input');
    token.type = 'hidden';
    token.name = 'csrf_token';
    token.value = getCsrfToken();
    var button = document.createElement('button');
    button.type = 'submit';
    button.className = 'button ' + buttonClass + ' button-small';
    button.textContent = label;
    if (!enabled) {
      button.disabled = true;
      button.title = disabledTitle;
    }
    form.appendChild(token);
    form.appendChild(button);
    return form;
  }

  function buildRow(job) {
    var queue = safeQueue(job.queue);
    var status = safeStatus(job.status);
    var id = safeInt(job.id);
    var progress = safeInt(job.progress);
    var attempts = safeInt(job.attempts);
    var maxAttempts = safeInt(job.max_attempts);
    var canCancel = !!ACTIVE_STATUSES[status];
    var canRetry = !!TERMINAL_STATUSES[status] && status !== 'succeeded' && attempts < maxAttempts;

    var tr = document.createElement('tr');
    tr.setAttribute('data-job-key', queue + ':' + id);
    tr.setAttribute('data-status', status);
    tr.setAttribute('data-progress', String(progress));

    // 编号
    var idCell = cell('job-id');
    idCell.title = '队列标识 ' + queue + ':' + id;
    idCell.textContent = (QUEUE_LABELS[queue] || queue) + ' #' + id;
    tr.appendChild(idCell);

    // 类型
    var typeCell = cell();
    typeCell.title = String(job.job_type || '');
    typeCell.textContent = TYPE_LABELS[job.job_type] || String(job.job_type || '');
    tr.appendChild(typeCell);

    // 状态
    var statusCell = cell();
    var badge = document.createElement('span');
    badge.className = 'status-badge status-' + status;
    badge.textContent = status;
    statusCell.appendChild(badge);
    tr.appendChild(statusCell);

    // 进度
    var progressCell = cell();
    var wrap = document.createElement('div');
    wrap.className = 'progress-cell';
    var track = document.createElement('span');
    track.className = 'progress';
    track.setAttribute('role', 'progressbar');
    track.setAttribute('aria-valuenow', String(progress));
    track.setAttribute('aria-valuemin', '0');
    track.setAttribute('aria-valuemax', '100');
    var bar = document.createElement('span');
    bar.className = barClassFor(status);
    bar.style.width = progress + '%';
    track.appendChild(bar);
    var text = document.createElement('span');
    text.className = 'progress-text';
    text.textContent = progress + '%';
    wrap.appendChild(track);
    wrap.appendChild(text);
    progressCell.appendChild(wrap);
    tr.appendChild(progressCell);

    // 尝试次数
    var attemptCell = cell('job-attempts');
    attemptCell.textContent = attempts + '/' + maxAttempts;
    tr.appendChild(attemptCell);

    // 照片
    var photoCell = cell('job-photo');
    if (job.photo_id) {
      var photoId = safeInt(job.photo_id);
      var link = document.createElement('a');
      link.href = '/admin/photos/' + photoId;
      link.textContent = '#' + photoId;
      photoCell.appendChild(link);
    }
    tr.appendChild(photoCell);

    // 结果
    var resultCell = cell('job-result');
    fillResultCell(resultCell, job);
    tr.appendChild(resultCell);

    // 错误
    var errorCell = cell('job-error');
    fillErrorCell(errorCell, job);
    tr.appendChild(errorCell);

    // 操作
    var actionCell = cell();
    var actions = document.createElement('div');
    actions.className = 'job-actions';
    actions.appendChild(actionForm(queue, id, 'cancel', '取消', 'button-secondary',
      canCancel, '仅等待中或执行中的任务可以取消'));
    actions.appendChild(actionForm(queue, id, 'retry', '重试', 'button-primary',
      canRetry, '仅失败或已取消、且尝试次数未用尽的任务可以重试'));
    actionCell.appendChild(actions);
    tr.appendChild(actionCell);

    return tr;
  }

  function barClassFor(status) {
    if (status === 'failed') return 'progress-bar is-failed';
    if (status === 'canceled') return 'progress-bar is-canceled';
    return 'progress-bar';
  }

  /** 填充结果单元格：优先中文摘要，其次可折叠的原始 JSON。 */
  function fillResultCell(td, job) {
    td.textContent = '';
    if (job.result_summary) {
      td.textContent = String(job.result_summary);
    } else if (job.result_json) {
      var details = document.createElement('details');
      var summary = document.createElement('summary');
      summary.textContent = '原始结果';
      var code = document.createElement('code');
      code.textContent = String(job.result_json);
      details.appendChild(summary);
      details.appendChild(code);
      td.appendChild(details);
    }
  }

  /** 填充错误单元格。 */
  function fillErrorCell(td, job) {
    td.textContent = '';
    if (job.error_code || job.error_summary) {
      var span = document.createElement('span');
      span.className = 'status-error';
      span.textContent = (job.error_code || '') + ' ' + (job.error_summary || '');
      td.appendChild(span);
    }
  }

  function updateRow(tr, job) {
    var status = safeStatus(job.status);
    var progress = safeInt(job.progress);
    var attempts = safeInt(job.attempts);
    var maxAttempts = safeInt(job.max_attempts);
    var oldStatus = tr.getAttribute('data-status');
    var oldProgress = parseInt(tr.getAttribute('data-progress'), 10);
    var oldAttempts = tr.getAttribute('data-attempts');
    if (oldStatus === status && oldProgress === progress
        && oldAttempts === String(attempts)) {
      return false;
    }

    tr.setAttribute('data-status', status);
    tr.setAttribute('data-progress', String(progress));
    tr.setAttribute('data-attempts', String(attempts));

    var badge = tr.querySelector('.status-badge');
    if (badge) {
      badge.className = 'status-badge status-' + status;
      badge.textContent = status;
    }
    var bar = tr.querySelector('.progress-bar');
    if (bar) {
      bar.className = barClassFor(status);
      bar.style.width = progress + '%';
    }
    var track = tr.querySelector('.progress');
    if (track) track.setAttribute('aria-valuenow', String(progress));
    var ptext = tr.querySelector('.progress-text');
    if (ptext) ptext.textContent = progress + '%';

    // 用 class 定位而非列索引：加列或调序不会静默错位
    var attemptCell = tr.querySelector('.job-attempts');
    if (attemptCell) attemptCell.textContent = attempts + '/' + maxAttempts;
    var resultCell = tr.querySelector('.job-result');
    if (resultCell) fillResultCell(resultCell, job);
    var errorCell = tr.querySelector('.job-error');
    if (errorCell) fillErrorCell(errorCell, job);

    var canCancel = !!ACTIVE_STATUSES[status];
    var canRetry = !!TERMINAL_STATUSES[status] && status !== 'succeeded' && attempts < maxAttempts;
    var buttons = tr.querySelectorAll('.job-actions button');
    if (buttons[0]) buttons[0].disabled = !canCancel;
    if (buttons[1]) buttons[1].disabled = !canRetry;
    return true;
  }

  function renderJobs(jobs) {
    var tbody = document.getElementById('jobs-tbody');
    var tableWrap = document.getElementById('jobs-table-wrap');
    var empty = document.getElementById('jobs-empty');
    if (!tbody) return false;

    // 表格骨架常驻，只切换空/非空的可见性
    if (tableWrap) tableWrap.hidden = jobs.length === 0;
    if (empty) empty.hidden = jobs.length > 0;

    var changed = false;
    var existingKeys = {};
    var rows = tbody.querySelectorAll('tr[data-job-key]');
    for (var i = 0; i < rows.length; i++) {
      existingKeys[rows[i].getAttribute('data-job-key')] = rows[i];
    }

    // 按服务端顺序重排并更新，避免逐行插入时的位置假设
    var newKeys = {};
    var previous = null;
    for (var j = 0; j < jobs.length; j++) {
      var job = jobs[j];
      var key = safeQueue(job.queue) + ':' + safeInt(job.id);
      newKeys[key] = true;
      var row = existingKeys[key];
      if (row) {
        if (updateRow(row, job)) changed = true;
      } else {
        row = buildRow(job);
        changed = true;
      }
      // 若当前位置不是期望位置，移动它（insertBefore 对已在文档中的节点是移动）
      var expectedNext = previous ? previous.nextSibling : tbody.firstChild;
      if (row !== expectedNext) {
        tbody.insertBefore(row, expectedNext);
        changed = true;
      }
      previous = row;
    }

    // 移除已被清理的任务行
    for (var k in existingKeys) {
      if (!Object.prototype.hasOwnProperty.call(existingKeys, k)) continue;
      if (!newKeys[k]) {
        existingKeys[k].remove();
        changed = true;
      }
    }
    return changed;
  }

  function poll() {
    if (inFlight) return;          // 防重入：避免可见性切换时叠加定时器
    inFlight = true;
    if (timerId) { clearTimeout(timerId); timerId = null; }

    var headers = { Accept: 'application/json' };
    if (lastEtag) headers['If-None-Match'] = lastEtag;

    fetch('/api/admin/jobs', { credentials: 'same-origin', headers: headers })
      .then(function (resp) {
        if (resp.status === 304) {
          unchangedCount++;
          return null;
        }
        if (!resp.ok) throw new Error(resp.status);
        var etag = resp.headers.get('ETag');
        if (etag) lastEtag = etag;
        return resp.json();
      })
      .then(function (body) {
        inFlight = false;
        if (!body) {
          scheduleNext(getCurrentStatusList());
          return;
        }
        var jobs = body.data || [];
        var changed = renderJobs(jobs);
        if (!changed) unchangedCount++;
        else unchangedCount = 0;
        scheduleNext(jobs);
      })
      .catch(function () {
        inFlight = false;
        // 网络错误：指数退避重试，成功后 scheduleNext 会重新计算间隔
        unchangedCount++;
        currentInterval = Math.min(POLL_MAX, currentInterval * 2);
        timerId = setTimeout(poll, currentInterval);
      });
  }

  function getCurrentStatusList() {
    var rows = document.querySelectorAll('#jobs-tbody tr[data-status]');
    var result = [];
    for (var i = 0; i < rows.length; i++) {
      result.push({ status: rows[i].getAttribute('data-status') });
    }
    return result;
  }

  function start() {
    if (timerId) return;
    var rows = getCurrentStatusList();
    if (hasActive(rows)) {
      currentInterval = POLL_FAST;
      setIndicator('active');
    } else {
      currentInterval = POLL_SLOW;
      setIndicator('idle');
    }
    timerId = setTimeout(poll, currentInterval);
  }

  function stop() {
    if (timerId) { clearTimeout(timerId); timerId = null; }
    setIndicator('paused');
  }

  // 可见性控制：切走暂停，切回立即刷新一次（poll 内有 inFlight 防重入）
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) stop();
    else poll();
  });

  // 辅助函数
  // 说明：本脚本全程用 textContent / setAttribute 构造 DOM，不做 HTML 字符串拼接，
  // 因此不需要 escapeHtml——留着反而容易被误用在属性上下文里。

  /** 白名单校验队列名，非法值返回空串。 */
  function safeQueue(value) {
    return QUEUE_LABELS[value] ? value : '';
  }

  /** 白名单校验状态名，非法值回退为 pending。 */
  function safeStatus(value) {
    return (ACTIVE_STATUSES[value] || TERMINAL_STATUSES[value]) ? value : 'pending';
  }

  /** 强制转为非负整数，非法值返回 0。 */
  function safeInt(value) {
    var n = parseInt(value, 10);
    return isFinite(n) && n >= 0 ? n : 0;
  }

  function getCsrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) return meta.getAttribute('content') || '';
    var el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : '';
  }

  // 启动
  start();
})();
