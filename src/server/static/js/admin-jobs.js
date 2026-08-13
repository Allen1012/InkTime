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
  var lastEtag = '';
  var unchangedCount = 0;
  var currentInterval = POLL_FAST;
  var indicator = document.getElementById('poll-indicator');

  // -- 状态标签映射 --
  var STATUS_LABELS = {
    pending: 'pending', running: 'running', succeeded: 'succeeded',
    failed: 'failed', canceled: 'canceled'
  };
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

  function buildRow(job) {
    var progress = job.progress || 0;
    var canCancel = ACTIVE_STATUSES[job.status];
    var canRetry = TERMINAL_STATUSES[job.status] && job.status !== 'succeeded'
      && job.attempts < job.max_attempts;
    var barClass = 'progress-bar';
    if (job.status === 'failed') barClass += ' is-failed';
    else if (job.status === 'canceled') barClass += ' is-canceled';

    var photoCell = '';
    if (job.photo_id) {
      photoCell = '<a href="/admin/photos/' + job.photo_id + '">#' + job.photo_id + '</a>';
    }
    var resultCell = '';
    if (job.result_summary) {
      resultCell = escapeHtml(job.result_summary);
    } else if (job.result_json) {
      resultCell = '<details><summary>原始结果</summary><code>' + escapeHtml(String(job.result_json)) + '</code></details>';
    }
    var errorCell = '';
    if (job.error_code || job.error_summary) {
      errorCell = '<span class="status-error">' + escapeHtml((job.error_code || '') + ' ' + (job.error_summary || '')) + '</span>';
    }

    var queueLabel = QUEUE_LABELS[job.queue] || job.queue;
    var typeLabel = TYPE_LABELS[job.job_type] || job.job_type;
    var csrf = getCsrfToken();

    var tr = document.createElement('tr');
    tr.setAttribute('data-job-key', job.queue + ':' + job.id);
    tr.setAttribute('data-status', job.status);
    tr.setAttribute('data-progress', progress);
    tr.innerHTML =
      '<td class="job-id" title="队列标识 ' + job.queue + ':' + job.id + '">' + queueLabel + ' #' + job.id + '</td>' +
      '<td title="' + escapeHtml(job.job_type) + '">' + typeLabel + '</td>' +
      '<td><span class="status-badge status-' + job.status + '">' + STATUS_LABELS[job.status] + '</span></td>' +
      '<td><div class="progress-cell">' +
        '<span class="progress" role="progressbar" aria-valuenow="' + progress + '" aria-valuemin="0" aria-valuemax="100">' +
        '<span class="' + barClass + '" style="width: ' + progress + '%"></span></span>' +
        '<span class="progress-text">' + progress + '%</span></div></td>' +
      '<td>' + job.attempts + '/' + job.max_attempts + '</td>' +
      '<td>' + photoCell + '</td>' +
      '<td class="job-result">' + resultCell + '</td>' +
      '<td class="job-error">' + errorCell + '</td>' +
      '<td><div class="job-actions">' +
        '<form action="/admin/jobs/' + job.queue + '/' + job.id + '/cancel" method="post">' +
        '<input type="hidden" name="csrf_token" value="' + csrf + '">' +
        '<button class="button button-secondary button-small" type="submit"' +
        (canCancel ? '' : ' disabled title="仅等待中或执行中的任务可以取消"') + '>取消</button></form>' +
        '<form action="/admin/jobs/' + job.queue + '/' + job.id + '/retry" method="post">' +
        '<input type="hidden" name="csrf_token" value="' + csrf + '">' +
        '<button class="button button-primary button-small" type="submit"' +
        (canRetry ? '' : ' disabled title="仅失败或已取消、且尝试次数未用尽的任务可以重试"') + '>重试</button></form>' +
      '</div></td>';
    return tr;
  }

  function updateRow(tr, job) {
    var progress = job.progress || 0;
    var oldStatus = tr.getAttribute('data-status');
    var oldProgress = parseInt(tr.getAttribute('data-progress'), 10);
    if (oldStatus === job.status && oldProgress === progress) return false;

    tr.setAttribute('data-status', job.status);
    tr.setAttribute('data-progress', progress);

    // 状态 badge
    var badge = tr.querySelector('.status-badge');
    if (badge) {
      badge.className = 'status-badge status-' + job.status;
      badge.textContent = STATUS_LABELS[job.status];
    }
    // 进度条
    var bar = tr.querySelector('.progress-bar');
    if (bar) {
      bar.className = 'progress-bar' + (job.status === 'failed' ? ' is-failed' : job.status === 'canceled' ? ' is-canceled' : '');
      bar.style.width = progress + '%';
    }
    var pbar = tr.querySelector('.progress');
    if (pbar) pbar.setAttribute('aria-valuenow', progress);
    var ptext = tr.querySelector('.progress-text');
    if (ptext) ptext.textContent = progress + '%';
    // 尝试次数
    var cells = tr.querySelectorAll('td');
    if (cells[4]) cells[4].textContent = job.attempts + '/' + job.max_attempts;
    // 结果
    if (cells[6]) {
      if (job.result_summary) cells[6].innerHTML = escapeHtml(job.result_summary);
      else if (job.result_json) cells[6].innerHTML = '<details><summary>原始结果</summary><code>' + escapeHtml(String(job.result_json)) + '</code></details>';
    }
    // 错误
    if (cells[7]) {
      if (job.error_code || job.error_summary) {
        cells[7].innerHTML = '<span class="status-error">' + escapeHtml((job.error_code || '') + ' ' + (job.error_summary || '')) + '</span>';
      } else {
        cells[7].innerHTML = '';
      }
    }
    // 按钮状态
    var canCancel = ACTIVE_STATUSES[job.status];
    var canRetry = TERMINAL_STATUSES[job.status] && job.status !== 'succeeded' && job.attempts < job.max_attempts;
    var buttons = tr.querySelectorAll('.job-actions button');
    if (buttons[0]) buttons[0].disabled = !canCancel;
    if (buttons[1]) buttons[1].disabled = !canRetry;
    return true;
  }

  function renderJobs(jobs) {
    var container = document.getElementById('jobs-container');
    var tbody = document.getElementById('jobs-tbody');

    // 空 → 有任务：创建表格
    if (!tbody && jobs.length > 0) {
      container.innerHTML = buildTableHtml(jobs);
      return true;
    }
    // 有任务 → 空：显示空状态
    if (tbody && jobs.length === 0) {
      container.innerHTML = '<section class="empty-state" id="jobs-empty">' +
        '<h2>当前没有后台任务</h2>' +
        '<p class="muted">重新分析、上传或维护操作产生的任务会显示在这里。</p></section>';
      return true;
    }
    if (!tbody) return false;

    var changed = false;
    var existingKeys = {};
    var rows = tbody.querySelectorAll('tr[data-job-key]');
    for (var i = 0; i < rows.length; i++) {
      existingKeys[rows[i].getAttribute('data-job-key')] = rows[i];
    }

    // 更新/插入
    var newKeys = {};
    for (var j = 0; j < jobs.length; j++) {
      var job = jobs[j];
      var key = job.queue + ':' + job.id;
      newKeys[key] = true;
      var existing = existingKeys[key];
      if (existing) {
        if (updateRow(existing, job)) changed = true;
      } else {
        var newRow = buildRow(job);
        // 按位置插入（jobs 已经排好序）
        if (j === 0) {
          tbody.insertBefore(newRow, tbody.firstChild);
        } else {
          var prevKey = jobs[j - 1].queue + ':' + jobs[j - 1].id;
          var prevRow = existingKeys[prevKey] || tbody.querySelector('[data-job-key="' + prevKey + '"]');
          if (prevRow && prevRow.nextSibling) {
            tbody.insertBefore(newRow, prevRow.nextSibling);
          } else {
            tbody.appendChild(newRow);
          }
        }
        changed = true;
      }
    }

    // 移除已被清理的任务行
    for (var k in existingKeys) {
      if (!newKeys[k]) {
        existingKeys[k].remove();
        changed = true;
      }
    }
    return changed;
  }

  function buildTableHtml(jobs) {
    var container = document.getElementById('jobs-container');
    // 完整重建：利用已有模板结构的简化版
    var tmp = document.createElement('div');
    tmp.innerHTML = '<div class="table-wrap"><table class="photo-table job-table table-centered"><thead><tr>' +
      '<th>编号</th><th>类型</th><th>状态</th><th>进度</th><th>尝试</th><th>照片</th><th>结果</th><th>错误</th><th>操作</th>' +
      '</tr></thead><tbody id="jobs-tbody"></tbody></table></div>';
    var tbody = tmp.querySelector('#jobs-tbody');
    for (var i = 0; i < jobs.length; i++) {
      tbody.appendChild(buildRow(jobs[i]));
    }
    return tmp.innerHTML;
  }

  function poll() {
    timerId = null;
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
        // 网络错误：退避重试
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

  // 可见性控制
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) stop();
    else { poll(); } // 恢复时立即刷新一次
  });

  // 辅助函数
  function escapeHtml(str) {
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
  }

  function getCsrfToken() {
    var el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : '';
  }

  // 启动
  start();
})();
