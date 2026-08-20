/**
 * 配置页交互：分类标签切换、配置项搜索、常用预设填入与设备地址复制。
 *
 * 全部配置项始终位于同一个表单内，标签切换只做显隐，因此无论当前看的是哪个分类，
 * 保存都会提交所有可编辑项。时间段解析与配置校验仍只由服务端负责，前端不复制业务规则。
 */
document.addEventListener('DOMContentLoaded', () => {
  const shell = document.getElementById('settings-shell');
  if (shell) {
    setupTabs(shell);
    setupSearch(shell);
  }
  setupPresets();
  setupCopyButton();
});

const ACTIVE_TAB_STORAGE_KEY = 'inktime.settings.activeTab';

/**
 * 绑定分类标签：点击切换、左右方向键移动焦点，并记住上次所在分类。
 *
 * @param {HTMLElement} shell 配置页容器。
 */
function setupTabs(shell) {
  const tabs = Array.from(shell.querySelectorAll('.settings-tab'));
  const panels = Array.from(shell.querySelectorAll('.settings-panel'));
  if (!tabs.length || !panels.length) return;

  const activate = (tabId, { focus = false } = {}) => {
    let matched = false;
    tabs.forEach((tab) => {
      const isActive = tab.dataset.tab === tabId;
      if (isActive) matched = true;
      tab.classList.toggle('is-active', isActive);
      tab.setAttribute('aria-selected', isActive ? 'true' : 'false');
      tab.tabIndex = isActive ? 0 : -1;
      if (isActive && focus) tab.focus();
    });
    if (!matched) return false;
    panels.forEach((panel) => {
      panel.hidden = panel.dataset.tab !== tabId;
    });
    try {
      window.localStorage.setItem(ACTIVE_TAB_STORAGE_KEY, tabId);
    } catch (error) {
      // 隐私模式下写入会抛错，仅影响下次进入时的默认分类，不影响本次使用。
    }
    return true;
  };

  tabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      // 搜索状态下点击分类，视为放弃搜索回到该分类。
      clearSearch(shell);
      activate(tab.dataset.tab);
    });
    tab.addEventListener('keydown', (event) => {
      const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
      if (!step) return;
      event.preventDefault();
      const current = tabs.indexOf(tab);
      const next = tabs[(current + step + tabs.length) % tabs.length];
      clearSearch(shell);
      activate(next.dataset.tab, { focus: true });
    });
  });

  // 优先定位到校验失败的分类，其次是地址栏锚点，最后才是上次所在分类。
  const errored = tabs.find((tab) => tab.classList.contains('has-error'));
  if (errored) {
    activate(errored.dataset.tab);
    return;
  }
  const hash = window.location.hash.replace(/^#/, '');
  if (hash && activate(hash)) return;
  let remembered = null;
  try {
    remembered = window.localStorage.getItem(ACTIVE_TAB_STORAGE_KEY);
  } catch (error) {
    remembered = null;
  }
  if (remembered) activate(remembered);
}

/**
 * 绑定搜索框：跨全部分类过滤配置项，清空后回到标签模式。
 *
 * @param {HTMLElement} shell 配置页容器。
 */
function setupSearch(shell) {
  const input = document.getElementById('settings-search');
  if (!input) return;
  input.addEventListener('input', () => applySearch(shell, input.value));
  input.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    input.value = '';
    applySearch(shell, '');
  });
}

/**
 * 按关键词显隐配置项。关键词为空时恢复按分类展示。
 *
 * @param {HTMLElement} shell 配置页容器。
 * @param {string} rawQuery 用户输入的原始关键词。
 */
function applySearch(shell, rawQuery) {
  const query = rawQuery.trim().toLowerCase();
  const fields = Array.from(shell.querySelectorAll('.settings-field'));
  const sections = Array.from(shell.querySelectorAll('[data-section]'));
  const panels = Array.from(shell.querySelectorAll('.settings-panel'));
  const status = document.getElementById('settings-search-status');
  const empty = document.getElementById('settings-empty');

  if (!query) {
    shell.classList.remove('is-searching');
    fields.forEach((field) => { field.hidden = false; });
    sections.forEach((section) => { section.hidden = false; });
    const active = shell.querySelector('.settings-tab.is-active');
    const activeId = active ? active.dataset.tab : null;
    panels.forEach((panel) => { panel.hidden = activeId ? panel.dataset.tab !== activeId : false; });
    if (status) { status.hidden = true; status.textContent = ''; }
    if (empty) empty.hidden = true;
    return;
  }

  shell.classList.add('is-searching');
  let matches = 0;
  fields.forEach((field) => {
    const hit = (field.dataset.searchText || '').includes(query);
    field.hidden = !hit;
    if (hit) matches += 1;
  });
  sections.forEach((section) => {
    section.hidden = !section.querySelector('.settings-field:not([hidden])');
  });
  panels.forEach((panel) => {
    panel.hidden = !panel.querySelector('.settings-field:not([hidden])');
  });
  if (status) {
    status.hidden = false;
    status.textContent = matches
      ? `搜索命中 ${matches} 项，已跨全部分类显示；按 Esc 或清空搜索框返回分类浏览。`
      : '没有匹配的配置项。';
  }
  if (empty) empty.hidden = matches !== 0;
}

/**
 * 退出搜索状态，用于点击分类标签时放弃当前搜索。
 *
 * @param {HTMLElement} shell 配置页容器。
 */
function clearSearch(shell) {
  const input = document.getElementById('settings-search');
  if (!input || !input.value) return;
  input.value = '';
  applySearch(shell, '');
}

/**
 * 绑定常用预设按钮，把预设值填入对应输入框。
 */
function setupPresets() {
  document.querySelectorAll('.settings-preset').forEach((button) => {
    button.addEventListener('click', () => {
      const target = document.getElementById(button.dataset.target || '');
      if (!target) {
        console.warn('[settings] 找不到预设对应的输入框', button.dataset.target);
        return;
      }
      target.value = button.dataset.value || '';
      target.classList.add('is-preset-filled');
      target.focus();
      target.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
  });
}

/**
 * 绑定设备下载地址复制按钮，剪贴板不可用时退回手动选中。
 */
function setupCopyButton() {
  const copyButton = document.getElementById('copy-device-download-url');
  const copyStatus = document.getElementById('copy-device-download-status');
  if (!copyButton) return;

  copyButton.addEventListener('click', async () => {
    const target = document.getElementById(copyButton.dataset.copyTarget || '');
    if (!target) return;
    try {
      await navigator.clipboard.writeText(target.value);
      if (copyStatus) copyStatus.textContent = '已复制';
    } catch (error) {
      target.focus();
      target.select();
      if (copyStatus) copyStatus.textContent = '无法自动复制，请手动复制已选中的地址';
    }
  });
}
