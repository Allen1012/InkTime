/**
 * 配置页交互：分类标签切换、配置项搜索、常用预设填入、设备地址与配置键名复制。
 *
 * 全部配置项始终位于同一个表单内，标签切换只做显隐，因此无论当前看的是哪个分类，
 * 保存都会提交所有可编辑项。时间段解析与配置校验仍只由服务端负责，前端不复制业务规则。
 * 说明与键名放在纯 CSS 悬停层里（`:hover` 与 `:focus-within`），脚本只负责复制，
 * 因此无脚本环境下仍能看到说明和键名，只是不能一键复制。
 */
document.addEventListener('DOMContentLoaded', () => {
  const shell = document.getElementById('settings-shell');
  if (shell) {
    setupTabs(shell);
    setupSearch(shell);
  }
  setupPresets();
  setupCopyButton();
  setupKeyCopy();
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
    if (await copyText(target.value)) {
      if (copyStatus) copyStatus.textContent = '已复制';
      return;
    }
    target.focus();
    target.select();
    if (copyStatus) copyStatus.textContent = '无法自动复制，请手动复制已选中的地址';
  });
}

/**
 * 绑定悬停层里的键名复制。用事件委托而不是逐项绑定：注册表有七十项，
 * 每项一个监听器纯属浪费，且悬停层内容始终在 DOM 里，委托一次就够。
 */
function setupKeyCopy() {
  const status = document.getElementById('settings-copy-status');
  document.addEventListener('click', async (event) => {
    const button = event.target.closest('.settings-field-key');
    if (!button) return;
    // 键名按钮位于 <label> 内部，阻止默认行为避免顺带聚焦并滚动到对应控件。
    event.preventDefault();
    const key = button.dataset.copyText || button.textContent.trim();
    const copied = await copyText(key);
    button.dataset.copied = copied ? 'true' : 'false';
    if (status) {
      status.textContent = copied
        ? `已复制配置键名 ${key}`
        : `无法自动复制，已选中 ${key}，请按 Ctrl+C`;
    }
    if (!copied) selectElementText(button);
    window.setTimeout(() => { delete button.dataset.copied; }, 1800);
  });
}

/**
 * 复制文本，优先用异步剪贴板接口，失败时退回旧的执行命令方式。
 *
 * 家庭局域网通常是普通 HTTP，此时 navigator.clipboard 在非安全上下文里不存在，
 * 只用它会让复制按钮在最常见的部署形态下静默失效，因此必须保留兜底路径。
 *
 * @param {string} text 待复制文本。
 * @returns {Promise<boolean>} 是否复制成功。
 */
async function copyText(text) {
  if (!text) return false;
  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (error) {
      // 用户拒绝授权或接口被策略禁用，继续走兜底路径。
    }
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', 'readonly');
  area.style.position = 'fixed';
  area.style.top = '-1000px';
  area.style.opacity = '0';
  document.body.appendChild(area);
  area.select();
  let copied = false;
  try {
    copied = document.execCommand('copy');
  } catch (error) {
    copied = false;
  }
  area.remove();
  return copied;
}

/**
 * 选中某个元素内的文本，供复制彻底失败时让用户自己按 Ctrl+C。
 *
 * @param {HTMLElement} element 目标元素。
 */
function selectElementText(element) {
  const selection = window.getSelection();
  if (!selection) return;
  const range = document.createRange();
  range.selectNodeContents(element);
  selection.removeAllRanges();
  selection.addRange(range);
}
