/**
 * 配置页辅助脚本：填入常用预设，并复制设备下载地址。
 *
 * 时间段解析和校验仍只由服务端负责，前端不复制业务规则。
 */
document.addEventListener('DOMContentLoaded', () => {
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
});
