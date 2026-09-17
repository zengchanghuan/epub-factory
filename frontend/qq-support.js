(() => {
  'use strict';
  const english = document.documentElement.lang.toLowerCase().startsWith('en');
  const mobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  document.querySelectorAll('.qq-support-open').forEach((link) => {
    link.href = mobile
      ? 'mqqapi://card/show_pslcard?src_type=internal&version=1&uin=249998620'
      : 'tencent://ContactInfo/?subcmd=ViewInfo&puin=0&uin=249998620';
    link.addEventListener('click', () => {
      const status = link.closest('.qq-support').querySelector('.qq-support-status');
      status.textContent = english
        ? 'Opening the QQ profile. Choose Add Friend there. If nothing opens, copy 249998620 and search in QQ.'
        : '正在尝试打开 QQ 资料页，请在 QQ 中点击“加好友”。若无反应，请复制 249998620，在 QQ 中搜索添加。';
    });
  });
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
