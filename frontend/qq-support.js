(() => {
  'use strict';
  const english = document.documentElement.lang.toLowerCase().startsWith('en');
  document.querySelectorAll('[data-copy-qq]').forEach((button) => {
    button.addEventListener('click', async () => {
      const status = button.closest('.qq-support').querySelector('.qq-support-status');
      try {
        await navigator.clipboard.writeText('249998620');
        status.textContent = english
          ? 'Copied: 249998620. Search for this number in QQ to add support.'
          : '已复制 QQ 号：249998620，请在 QQ 中搜索并添加客服。';
      } catch (_) {
        status.textContent = english
          ? 'Could not copy automatically. Select and copy QQ number: 249998620.'
          : '自动复制未成功，请长按或选中 QQ 号手动复制：249998620。';
      }
    });
  });
})();
