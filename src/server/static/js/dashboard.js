/**
 * dashboard 模板右侧信息栏
 *
 * 只负责信息栏；照片的加载、切换、屏幕常亮、界面自动隐藏都由 display.js 处理
 * （两个模板共用同一套 DOM id，所以 display.js 无需改动）。
 *
 * 数据来源分两类：
 * - 时钟：纯前端本地时间，每秒更新，不发任何请求
 * - 农历节气 / 历史上的今天：服务端 /api/panel，服务端已做缓存与降级
 */

// 面板数据刷新间隔：内容按天变化，30 分钟一次足够，兼顾跨零点自动更新
const PANEL_REFRESH_MS = 30 * 60 * 1000;

let panelLastDate = null;   // 上次拿到的日期，用于检测跨零点

document.addEventListener('DOMContentLoaded', () => {
  try {
    startClock();
  } catch (e) {
    console.error('[dashboard] 时钟启动失败', e);
  }

  try {
    refreshPanel();
    setInterval(refreshPanel, PANEL_REFRESH_MS);
  } catch (e) {
    console.error('[dashboard] 信息面板初始化失败', e);
  }
});

/* ==================== 时钟 ==================== */

function startClock() {
  tickClock();
  // 对齐到下一秒再开始按秒走，避免显示的秒数与系统时间差半秒
  setTimeout(() => {
    tickClock();
    setInterval(tickClock, 1000);
  }, 1000 - (Date.now() % 1000));
}

function tickClock() {
  const now = new Date();
  const pad = n => String(n).padStart(2, '0');

  setText('dash-hhmm', `${pad(now.getHours())}:${pad(now.getMinutes())}`);
  setText('dash-ss', pad(now.getSeconds()));

  // 跨零点时日期和农历都要变，立即拉一次面板数据
  const iso = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  if (panelLastDate && panelLastDate !== iso) {
    console.log('[dashboard] 检测到跨日，刷新信息面板');
    panelLastDate = iso;
    refreshPanel();
  }
}

/* ==================== 信息面板 ==================== */

async function refreshPanel() {
  let payload;
  try {
    const resp = await fetch('/api/panel');
    payload = await resp.json();
  } catch (e) {
    console.warn('[dashboard] 获取信息面板数据失败', e);
    setText('dash-panel-status', '信息面板离线');
    return;
  }

  if (!payload || payload.status !== 'ok' || !payload.data) {
    console.warn('[dashboard] 信息面板返回异常', payload);
    setText('dash-panel-status', '信息面板异常');
    return;
  }

  const d = payload.data;
  panelLastDate = d.date && d.date.iso;

  renderDate(d.date);
  renderLunar(d.lunar);
  renderHistory(d.onthisday);

  setText('dash-panel-status', '');
}

function renderDate(date) {
  if (!date) return;
  setText('dash-date', `${date.year} 年 ${date.month} 月 ${date.day} 日`);
  setText('dash-weekday', date.weekday || '');
}

function renderLunar(lunar) {
  const block = document.getElementById('dash-lunar-block');
  if (!lunar || !lunar.available) {
    // 农历是离线计算的，走到这里通常是依赖缺失
    if (block) block.hidden = true;
    if (lunar && lunar.error) console.warn('[dashboard] 农历不可用：' + lunar.error);
    return;
  }
  if (block) block.hidden = false;

  setText('dash-lunar-text', lunar.text || '');

  // 当日节气
  toggleBadge('dash-jieqi', lunar.jieqi);

  // 传统节日（春节、端午等）
  const festival = (lunar.festivals && lunar.festivals.length) ? lunar.festivals[0] : '';
  toggleBadge('dash-festival', festival);

  // 下一个节气还有几天
  const nj = lunar.next_jieqi;
  setText('dash-next-jieqi',
    nj ? `距 ${nj.name} 还有 ${nj.days_left} 天` : '');
}

function renderHistory(onthisday) {
  const block = document.getElementById('dash-history-block');
  const list = document.getElementById('dash-history-list');
  if (!block || !list) return;

  const items = (onthisday && onthisday.items) || [];
  if (!onthisday || !onthisday.available || items.length === 0) {
    // 外部数据源失败时整块隐藏，不留空标题
    block.hidden = true;
    if (onthisday && onthisday.error) {
      console.warn('[dashboard] 历史上的今天不可用：' + onthisday.error);
    }
    return;
  }

  block.hidden = false;
  list.innerHTML = '';
  items.forEach(it => {
    const li = document.createElement('li');
    const year = document.createElement('span');
    year.className = 'history-year';
    year.textContent = it.year != null ? it.year : '—';
    // 用 textContent 而非 innerHTML，避免外部文本里的标记被当成 HTML 解析
    const text = document.createTextNode(it.text || '');
    li.appendChild(year);
    li.appendChild(text);
    list.appendChild(li);
  });

  if (onthisday.stale) {
    setText('dash-panel-status', '历史数据来自缓存');
  }
}

/* ==================== 工具 ==================== */

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function toggleBadge(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  if (value) {
    el.textContent = value;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}
