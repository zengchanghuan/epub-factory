/**
 * auth.js — FixEpub 前端认证模块
 * 职责：
 *   - 存取 JWT token (localStorage)
 *   - 解析 OAuth 回调 fragment 中的 token
 *   - 提供 isLoggedIn / getCurrentUser / logout 等工具函数
 *   - 渲染 topbar 登录按钮 / 用户头像下拉
 *   - 注入登录弹窗 HTML 并绑定事件
 */
(function () {
  'use strict';

  // ─── 常量 ───────────────────────────────────────────────────────────────────
  const TOKEN_KEY = 'fixepub_auth_token';
  const USER_KEY  = 'fixepub_auth_user';
  const API       = window.FIXEPUB_API || '';
  // 短信未真正接入前，暂时隐藏登录/注册入口（改回 true 即可恢复）
  const AUTH_UI_ENABLED = false;

  // ─── token 存取 ────────────────────────────────────────────────────────────
  function saveToken(token) {
    localStorage.setItem(TOKEN_KEY, token);
  }

  function getToken() {
    return localStorage.getItem(TOKEN_KEY) || '';
  }

  function clearAuth() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
  }

  // ─── 解析 OAuth 回调 fragment（#access_token=...） ────────────────────────
  function extractFragmentToken() {
    const hash = window.location.hash;
    if (!hash) return null;
    const params = new URLSearchParams(hash.slice(1));
    return params.get('access_token') || null;
  }

  // ─── 向后端拉取当前用户信息 ─────────────────────────────────────────────────
  async function fetchMe(token) {
    try {
      const resp = await fetch(`${API}/api/v2/auth/me`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      const data = await resp.json();
      return data.user || null;
    } catch {
      return null;
    }
  }

  // ─── 归属匿名任务 ───────────────────────────────────────────────────────────
  async function claimAnonJobs(token) {
    const sessionId = getClientSession();
    if (!sessionId) return;
    try {
      await fetch(`${API}/api/v2/auth/claim-jobs`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ session_id: sessionId }),
      });
    } catch {}
  }

  function getClientSession() {
    return sessionStorage.getItem('client_session_id') ||
           localStorage.getItem('client_session_id') || '';
  }

  // ─── 初始化：处理 OAuth fragment & 验证已有 token ─────────────────────────
  function hideAuthUi() {
    const container = document.getElementById('authTopbarArea');
    if (container) {
      container.innerHTML = '';
      container.style.display = 'none';
    }
  }

  async function init() {
    if (!AUTH_UI_ENABLED) {
      hideAuthUi();
      window.__fixepubUser = null;
      return;
    }

    // OAuth 回调：URL fragment 中可能带 access_token
    const fragmentToken = extractFragmentToken();
    if (fragmentToken) {
      saveToken(fragmentToken);
      // 清除 fragment，避免刷新后重复处理
      history.replaceState(null, '', window.location.pathname + window.location.search);
      await claimAnonJobs(fragmentToken);
    }

    const token = getToken();
    if (!token) {
      window.__fixepubUser = null;
      renderTopbarUser(null);
      return;
    }

    const user = await fetchMe(token);
    if (!user) {
      clearAuth();
      window.__fixepubUser = null;
    } else {
      localStorage.setItem(USER_KEY, JSON.stringify(user));
      window.__fixepubUser = user;
    }
    renderTopbarUser(window.__fixepubUser);
  }

  // ─── 登出 ─────────────────────────────────────────────────────────────────
  function logout() {
    clearAuth();
    window.__fixepubUser = null;
    window.location.reload();
  }

  // ─── 渲染 topbar 用户区域 ──────────────────────────────────────────────────
  function renderTopbarUser(user) {
    if (!AUTH_UI_ENABLED) { hideAuthUi(); return; }
    const container = document.getElementById('authTopbarArea');
    if (!container) return;

    if (!user) {
      container.innerHTML = `
        <button id="loginBtn" onclick="window.fixepubAuth.openLoginModal()" style="
          background: var(--primary);
          color: #fff;
          border: none;
          padding: 6px 14px;
          border-radius: 6px;
          font-size: 13px;
          font-weight: 500;
          cursor: pointer;
          font-family: inherit;
        ">登录 / 注册</button>`;
      return;
    }

    const displayName = user.display_name || (user.phone ? user.phone.replace(/(\d{3})\d{4}(\d{4})/, '$1****$2') : '用户');
    const avatarHtml = user.avatar_url
      ? `<img src="${user.avatar_url}" style="width:28px;height:28px;border-radius:50%;object-fit:cover;">`
      : `<span style="width:28px;height:28px;border-radius:50%;background:var(--primary);color:#fff;font-size:12px;display:flex;align-items:center;justify-content:center;font-weight:600;">${displayName[0] || 'U'}</span>`;

    container.innerHTML = `
      <div style="position:relative;" id="userMenuWrap">
        <button onclick="document.getElementById('userDropdown').classList.toggle('open')" style="
          display:flex;align-items:center;gap:6px;background:none;border:1px solid var(--border);
          padding:4px 10px 4px 6px;border-radius:20px;cursor:pointer;font-family:inherit;
        ">
          ${avatarHtml}
          <span style="font-size:13px;font-weight:500;color:var(--text);">${displayName}</span>
          <span style="font-size:10px;color:var(--subtle);">▾</span>
        </button>
        <div id="userDropdown" style="
          display:none;position:absolute;right:0;top:calc(100% + 6px);
          background:#fff;border:1px solid var(--border);border-radius:8px;
          box-shadow:0 4px 16px rgba(0,0,0,.08);min-width:140px;z-index:1000;overflow:hidden;
        ">
          <a href="#" id="navTasksBtn" style="display:block;padding:10px 16px;font-size:13px;color:var(--text);text-decoration:none;" onmouseover="this.style.background='var(--surface)'" onmouseout="this.style.background='none'">我的任务</a>
          <hr style="margin:0;border:none;border-top:1px solid var(--border);">
          <button onclick="window.fixepubAuth.logout()" style="
            display:block;width:100%;text-align:left;padding:10px 16px;font-size:13px;
            color:var(--error);background:none;border:none;cursor:pointer;font-family:inherit;
          " onmouseover="this.style.background='var(--surface)'" onmouseout="this.style.background='none'">退出登录</button>
        </div>
      </div>`;

    // 点击外部关闭下拉
    document.addEventListener('click', (e) => {
      const wrap = document.getElementById('userMenuWrap');
      if (wrap && !wrap.contains(e.target)) {
        const dd = document.getElementById('userDropdown');
        if (dd) dd.classList.remove('open');
      }
    });

    // open class 控制显示
    const style = document.createElement('style');
    style.textContent = '#userDropdown.open { display: block !important; }';
    document.head.appendChild(style);

    // 绑定"我的任务"跳转
    const navTasksBtn = document.getElementById('navTasksBtn');
    if (navTasksBtn) {
      navTasksBtn.addEventListener('click', (e) => {
        e.preventDefault();
        const navTasks = document.getElementById('navTasks');
        if (navTasks) navTasks.click();
        document.getElementById('userDropdown').classList.remove('open');
      });
    }
  }

  // ─── 登录弹窗 ─────────────────────────────────────────────────────────────
  function injectLoginModal() {
    if (document.getElementById('authModal')) return;
    const modal = document.createElement('div');
    modal.id = 'authModal';
    modal.style.cssText = `
      display:none;position:fixed;inset:0;z-index:9999;
      background:rgba(0,0,0,.45);backdrop-filter:blur(4px);
      justify-content:center;align-items:center;
    `;
    modal.innerHTML = `
      <div style="
        background:#fff;border-radius:12px;width:100%;max-width:380px;margin:20px;
        position:relative;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.2);
      ">
        <button onclick="window.fixepubAuth.closeLoginModal()" style="
          position:absolute;top:12px;right:14px;border:none;background:none;
          font-size:20px;cursor:pointer;color:#999;line-height:1;
        ">✕</button>

        <div style="padding:28px 28px 0;">
          <h2 style="font-size:18px;font-weight:700;color:#0f0f0f;margin-bottom:4px;">登录 / 注册</h2>
          <p style="font-size:13px;color:#737373;margin-bottom:20px;">首次登录自动注册账号</p>

          <!-- Tab 切换 -->
          <div style="display:flex;gap:0;border-bottom:1px solid #e8e8e8;margin-bottom:20px;">
            <button class="auth-tab active" data-tab="phone" style="flex:1;padding:8px;border:none;background:none;font-size:13px;font-weight:600;color:#0f0f0f;cursor:pointer;border-bottom:2px solid #0f0f0f;font-family:inherit;">手机号</button>
            <button class="auth-tab" data-tab="google" style="flex:1;padding:8px;border:none;background:none;font-size:13px;color:#737373;cursor:pointer;border-bottom:2px solid transparent;font-family:inherit;">Google</button>
            <button class="auth-tab" data-tab="wechat" style="flex:1;padding:8px;border:none;background:none;font-size:13px;color:#737373;cursor:pointer;border-bottom:2px solid transparent;font-family:inherit;">微信</button>
          </div>
        </div>

        <!-- 手机号面板 -->
        <div class="auth-panel" id="panel-phone" style="padding:0 28px 28px;">
          <div style="margin-bottom:12px;">
            <label style="font-size:12px;font-weight:500;color:#737373;display:block;margin-bottom:6px;">手机号</label>
            <div style="display:flex;gap:8px;">
              <select id="phoneCountry" style="padding:10px 8px;border:1px solid #e8e8e8;border-radius:8px;font-size:13px;background:#fff;color:#0f0f0f;font-family:inherit;">
                <option value="+86">🇨🇳 +86</option>
                <option value="+1">🇺🇸 +1</option>
                <option value="+44">🇬🇧 +44</option>
                <option value="+81">🇯🇵 +81</option>
                <option value="+82">🇰🇷 +82</option>
                <option value="+852">🇭🇰 +852</option>
                <option value="+886">🇹🇼 +886</option>
              </select>
              <input id="phoneInput" type="tel" placeholder="请输入手机号" style="
                flex:1;padding:10px 12px;border:1px solid #e8e8e8;border-radius:8px;
                font-size:13px;outline:none;font-family:inherit;
              ">
            </div>
          </div>
          <div id="codeRow" style="display:none;margin-bottom:12px;">
            <label style="font-size:12px;font-weight:500;color:#737373;display:block;margin-bottom:6px;">验证码</label>
            <div style="display:flex;gap:8px;">
              <input id="codeInput" type="text" placeholder="6 位验证码" maxlength="6" style="
                flex:1;padding:10px 12px;border:1px solid #e8e8e8;border-radius:8px;
                font-size:13px;outline:none;letter-spacing:4px;font-family:inherit;
              ">
              <button id="resendBtn" disabled style="
                padding:10px 12px;border:1px solid #e8e8e8;border-radius:8px;
                font-size:12px;background:#f7f7f7;color:#737373;cursor:not-allowed;
                font-family:inherit;white-space:nowrap;
              ">60s</button>
            </div>
          </div>
          <p id="authMsg" style="font-size:12px;color:#dc2626;min-height:16px;margin-bottom:10px;"></p>
          <button id="sendCodeBtn" onclick="window.fixepubAuth.sendCode()" style="
            width:100%;padding:12px;background:#0f0f0f;color:#fff;border:none;
            border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit;
          ">获取验证码</button>
          <button id="verifyBtn" style="display:none;width:100%;padding:12px;background:#0f0f0f;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit;" onclick="window.fixepubAuth.verifyCode()">登录 / 注册</button>
        </div>

        <!-- Google 面板 -->
        <div class="auth-panel" id="panel-google" style="display:none;padding:0 28px 28px;">
          <p style="font-size:13px;color:#737373;margin-bottom:16px;line-height:1.6;">点击下方按钮，通过 Google 账号一键登录。</p>
          <button onclick="window.fixepubAuth.googleLogin()" style="
            width:100%;padding:12px;background:#fff;color:#0f0f0f;border:1px solid #e8e8e8;
            border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit;
            display:flex;align-items:center;justify-content:center;gap:10px;
          ">
            <svg width="18" height="18" viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.3 9.1 3.4l6.8-6.8C35.6 2.4 30.1 0 24 0 14.7 0 6.8 5.4 2.9 13.3l7.9 6.1C12.5 13.1 17.8 9.5 24 9.5z"/><path fill="#4285F4" d="M46.5 24.5c0-1.6-.1-3.1-.4-4.5H24v8.5h12.7c-.6 3-2.3 5.5-4.9 7.2l7.5 5.8c4.4-4.1 7.2-10.1 7.2-17z"/><path fill="#FBBC05" d="M10.8 28.6A14.5 14.5 0 0 1 9.5 24c0-1.6.3-3.1.8-4.6l-7.9-6.1A24 24 0 0 0 0 24c0 3.9.9 7.5 2.5 10.7l8.3-6.1z"/><path fill="#34A853" d="M24 48c6.1 0 11.3-2 15.1-5.4l-7.5-5.8c-2 1.4-4.6 2.2-7.6 2.2-6.2 0-11.5-4.2-13.4-9.8l-8.3 6.1C6.8 42.6 14.7 48 24 48z"/></svg>
            使用 Google 登录
          </button>
        </div>

        <!-- 微信面板 -->
        <div class="auth-panel" id="panel-wechat" style="display:none;padding:0 28px 28px;">
          <p style="font-size:13px;color:#737373;margin-bottom:16px;line-height:1.6;">点击下方按钮，通过微信公众号授权登录。</p>
          <button onclick="window.fixepubAuth.wechatLogin()" style="
            width:100%;padding:12px;background:#07C160;color:#fff;border:none;
            border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit;
            display:flex;align-items:center;justify-content:center;gap:10px;
          ">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="white"><path d="M8.691 2.188C3.891 2.188 0 5.476 0 9.53c0 2.212 1.17 4.203 3.002 5.55a.59.59 0 0 1 .213.665l-.39 1.48c-.019.07-.048.141-.048.213 0 .163.13.295.29.295a.326.326 0 0 0 .167-.054l1.903-1.114a.864.864 0 0 1 .717-.098 10.16 10.16 0 0 0 2.837.403c.276 0 .543-.027.811-.05-.857-2.578.157-4.972 1.932-6.446 1.703-1.415 3.882-1.98 5.853-1.838-.576-3.583-4.196-6.348-8.596-6.348zM5.785 5.991c.642 0 1.162.529 1.162 1.18a1.17 1.17 0 0 1-1.162 1.178A1.17 1.17 0 0 1 4.623 7.17c0-.651.52-1.18 1.162-1.18zm5.813 0c.642 0 1.162.529 1.162 1.18a1.17 1.17 0 0 1-1.162 1.178 1.17 1.17 0 0 1-1.162-1.178c0-.651.52-1.18 1.162-1.18zm5.34 2.867c-1.797-.052-3.746.512-5.28 1.786-1.72 1.428-2.687 3.72-1.78 6.22.942 2.453 3.666 4.229 6.884 4.229.826 0 1.622-.12 2.361-.336a.722.722 0 0 1 .598.082l1.584.926a.272.272 0 0 0 .14.047c.134 0 .24-.111.24-.247 0-.06-.023-.12-.038-.177l-.327-1.233a.582.582 0 0 1-.023-.156.49.49 0 0 1 .201-.398C23.024 18.48 24 16.82 24 14.98c0-3.21-2.931-5.837-7.062-6.122zm-3.74 2.719c.524 0 .949.435.949.972a.96.96 0 0 1-.949.972.96.96 0 0 1-.949-.972c0-.537.425-.972.949-.972zm3.965 0c.524 0 .949.435.949.972a.96.96 0 0 1-.949.972.96.96 0 0 1-.949-.972c0-.537.425-.972.949-.972z"/></svg>
            使用微信登录
          </button>
        </div>
      </div>
    `;
    document.body.appendChild(modal);

    // 隐藏未启用的登录方式：Google 和微信暂未开放
    const googleTab = modal.querySelector('[data-tab="google"]');
    if (googleTab) googleTab.style.display = 'none';
    const wechatTab = modal.querySelector('[data-tab="wechat"]');
    if (wechatTab) wechatTab.style.display = 'none';

    // Tab 切换逻辑
    modal.querySelectorAll('.auth-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        modal.querySelectorAll('.auth-tab').forEach(t => {
          t.style.color = '#737373';
          t.style.fontWeight = '500';
          t.style.borderBottomColor = 'transparent';
        });
        btn.style.color = '#0f0f0f';
        btn.style.fontWeight = '600';
        btn.style.borderBottomColor = '#0f0f0f';

        modal.querySelectorAll('.auth-panel').forEach(p => p.style.display = 'none');
        const panel = document.getElementById(`panel-${btn.dataset.tab}`);
        if (panel) panel.style.display = 'block';
      });
    });

    // 点击遮罩关闭
    modal.addEventListener('click', (e) => {
      if (e.target === modal) window.fixepubAuth.closeLoginModal();
    });
  }

  function openLoginModal(callback) {
    if (!AUTH_UI_ENABLED) return;
    window.__loginCallback = callback || null;
    injectLoginModal();
    const modal = document.getElementById('authModal');
    if (modal) modal.style.display = 'flex';
  }

  function closeLoginModal() {
    const modal = document.getElementById('authModal');
    if (modal) modal.style.display = 'none';
    window.__loginCallback = null;
  }

  // ─── 手机号登录流程 ────────────────────────────────────────────────────────
  let _resendTimer = null;

  async function sendCode() {
    const phoneEl = document.getElementById('phoneInput');
    const countryEl = document.getElementById('phoneCountry');
    const msgEl = document.getElementById('authMsg');
    const sendBtn = document.getElementById('sendCodeBtn');
    const codeRow = document.getElementById('codeRow');
    const verifyBtn = document.getElementById('verifyBtn');

    const phone = (countryEl.value + phoneEl.value.trim()).replace(/\s/g, '');
    if (!phone || phoneEl.value.trim().length < 5) {
      msgEl.textContent = '请输入正确的手机号';
      return;
    }

    sendBtn.disabled = true;
    sendBtn.textContent = '发送中...';
    msgEl.textContent = '';

    try {
      const resp = await fetch(`${API}/api/v2/auth/sms/send`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || '发送失败');

      codeRow.style.display = 'block';
      sendBtn.style.display = 'none';
      verifyBtn.style.display = 'block';
      msgEl.style.color = '#16a34a';
      msgEl.textContent = '验证码已发送，请查收短信';

      // 60s 倒计时
      const resendBtn = document.getElementById('resendBtn');
      let countdown = 60;
      resendBtn.textContent = `${countdown}s`;
      _resendTimer = setInterval(() => {
        countdown--;
        resendBtn.textContent = `${countdown}s`;
        if (countdown <= 0) {
          clearInterval(_resendTimer);
          resendBtn.textContent = '重新发送';
          resendBtn.disabled = false;
          resendBtn.style.cursor = 'pointer';
          resendBtn.style.color = '#0f0f0f';
          resendBtn.onclick = sendCode;
        }
      }, 1000);
    } catch (err) {
      msgEl.style.color = '#dc2626';
      msgEl.textContent = err.message;
      sendBtn.disabled = false;
      sendBtn.textContent = '获取验证码';
    }
  }

  async function verifyCode() {
    const phoneEl = document.getElementById('phoneInput');
    const countryEl = document.getElementById('phoneCountry');
    const codeEl = document.getElementById('codeInput');
    const msgEl = document.getElementById('authMsg');
    const verifyBtn = document.getElementById('verifyBtn');

    const phone = (countryEl.value + phoneEl.value.trim()).replace(/\s/g, '');
    const code = codeEl.value.trim();
    if (code.length !== 6) {
      msgEl.textContent = '请输入 6 位验证码';
      msgEl.style.color = '#dc2626';
      return;
    }

    verifyBtn.disabled = true;
    verifyBtn.textContent = '验证中...';
    msgEl.textContent = '';

    try {
      const resp = await fetch(`${API}/api/v2/auth/sms/verify`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone, code, session_id: getClientSession() }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || '验证失败');

      saveToken(data.access_token);
      window.__fixepubUser = data.user;
      closeLoginModal();
      renderTopbarUser(data.user);

      if (window.__loginCallback) {
        window.__loginCallback(data.user);
        window.__loginCallback = null;
      }
    } catch (err) {
      msgEl.style.color = '#dc2626';
      msgEl.textContent = err.message;
      verifyBtn.disabled = false;
      verifyBtn.textContent = '登录 / 注册';
    }
  }

  // ─── OAuth 登录 ────────────────────────────────────────────────────────────
  function googleLogin() {
    // Google 登录暂未开放，配置 GOOGLE_CLIENT_ID 后后端自动激活
    alert('Google 登录暂未开放，请使用手机号或微信登录');
  }

  function wechatLogin() {
    // 微信登录暂未开放，配置 WECHAT_APP_ID 后后端自动激活
    alert('微信登录暂未开放，请使用手机号登录');
  }

  // ─── 外部 API ─────────────────────────────────────────────────────────────
  window.fixepubAuth = {
    isLoggedIn: () => !!(window.__fixepubUser),
    getUser: () => window.__fixepubUser || null,
    getToken,
    logout,
    openLoginModal,
    closeLoginModal,
    sendCode,
    verifyCode,
    googleLogin,
    wechatLogin,
  };

  // ─── 页面加载时初始化 ──────────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
