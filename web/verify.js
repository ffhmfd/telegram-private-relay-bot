(() => {
  'use strict';
  const raw = location.hash.slice(1);
  history.replaceState(null, '', location.pathname);
  const status = document.getElementById('status');
  const retry = document.getElementById('retry');
  let widget;
  let solved = false;

  function show(message, state = '') {
    status.textContent = message;
    status.dataset.state = state;
  }

  async function post(path, data) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(path, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data), signal: controller.signal,
        credentials: 'omit', cache: 'no-store', redirect: 'error',
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.message || '验证服务暂不可用，请稍后重试。');
      return result;
    } finally {
      clearTimeout(timer);
    }
  }

  async function submit(token) {
    if (solved) return;
    retry.hidden = true;
    show('正在确认验证结果…');
    try {
      const result = await post('/api/verify', {challenge: raw, token});
      solved = true;
      window.turnstile.remove(widget);
      show(result.message, 'success');
    } catch (error) {
      show(error.name === 'AbortError' ? '请求超时，请稍后重新验证。' : error.message, 'error');
      retry.hidden = false;
    }
  }

  retry.addEventListener('click', () => {
    if (solved) return;
    retry.hidden = true;
    show('请完成下方验证。');
    window.turnstile.reset(widget);
  });

  async function start() {
    if (!/^[A-Za-z0-9_-]{43}$/.test(raw)) {
      show('请从 Telegram 机器人消息中的“点击验证”按钮进入。', 'error');
      return;
    }
    try {
      const challenge = await post('/api/challenge', {challenge: raw});
      const script = document.createElement('script');
      script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
      script.async = true;
      script.onload = () => {
        show('请完成下方验证。');
        widget = window.turnstile.render('#challenge', {
          sitekey: document.querySelector('main').dataset.sitekey,
          action: 'private_relay', cData: challenge.cdata,
          theme: 'light', size: 'flexible', language: 'zh-cn',
          callback: submit,
          'expired-callback': () => {
            if (solved) return;
            show('验证已过期，请重新验证。', 'error');
            retry.hidden = false;
          },
          'error-callback': () => {
            if (solved) return;
            show('无法加载验证，请检查网络，或用系统浏览器打开原验证链接。', 'error');
            retry.hidden = false;
          },
        });
      };
      script.onerror = () => show('无法连接 Cloudflare，请检查网络后重新打开原验证链接。', 'error');
      document.head.appendChild(script);
    } catch (error) {
      show(error.name === 'AbortError' ? '请求超时，请重新打开原验证链接。' : error.message, 'error');
    }
  }
  start();
})();
