/**
 * 配置页辅助脚本
 *
 * 目前只做一件事：把「常用预设」按钮的值填进对应的配置输入框。
 * 刻意不做任何解析或校验——解析规则只有服务端一份（parse_time_windows），
 * 前端再写一份必然与之漂移，跨零点归属与区间合并这类边界尤其容易不一致。
 */
document.addEventListener('DOMContentLoaded', () => {
  const buttons = document.querySelectorAll('.settings-preset');
  if (!buttons.length) return;

  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const target = document.getElementById(button.dataset.target || '');
      if (!target) {
        console.warn('[settings] 找不到预设对应的输入框', button.dataset.target);
        return;
      }
      target.value = button.dataset.value || '';
      // 提示使用者「填好了但还没保存」，避免以为点一下就生效
      target.classList.add('is-preset-filled');
      target.focus();
      target.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
  });
});
