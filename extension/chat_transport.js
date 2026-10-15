/* 展示连接可重建；只有取消接口能停止后端请求。 */
globalThis.ChatTransport = (() => {
  async function follow({ stateUrl, submitUrl, body, headers = {}, signal, onEvent, onConnection = () => {} }) {
    let attempt = null, cursor = 0, first = Boolean(body), failures = 0;
    const wait = ms => new Promise(resolve => {
      const finish = () => { clearTimeout(timer); signal?.removeEventListener('abort', finish); resolve(); };
      const timer = setTimeout(finish, ms);
      signal?.addEventListener('abort', finish, { once: true });
    });
    while (!signal?.aborted) {
      const controller = new AbortController();
      const abort = () => controller.abort();
      signal?.addEventListener('abort', abort, { once: true });
      let timer;
      const touch = () => { clearTimeout(timer); timer = setTimeout(abort, 45000); };
      touch();
      try {
        let response;
        if (first) {
          first = false;
          response = await fetch(submitUrl, { method: 'POST', headers, body: JSON.stringify(body),
            signal: controller.signal, credentials: 'omit', redirect: 'error' });
        } else {
          const check = await fetch(stateUrl, { headers, signal: controller.signal, credentials: 'omit', redirect: 'error' });
          if (!check.ok) {
            if (check.status === 404) throw new Error('请求尚未保存或已不可访问，请检查状态后重试');
            throw new Error(`查询请求状态失败 (${check.status})`);
          }
          const state = await check.json();
          if (attempt !== null && state.attempt !== attempt || state.status !== 'running') {
            await onEvent({ request_snapshot: state });
            return;
          }
          attempt = state.attempt;
          response = await fetch(`${stateUrl}/events?attempt=${attempt}&after=${cursor}`, {
            headers, signal: controller.signal, credentials: 'omit', redirect: 'error' });
        }
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          if (response.status >= 400 && response.status < 500 && response.status !== 409) {
            await onEvent({ error: data.error || { code: data.detail || `http_${response.status}` } });
            return;
          }
          throw new Error(data.error?.code || data.detail || `http_${response.status}`);
        }
        onConnection('connected');
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '', terminal = false;
        try {
          while (!signal?.aborted) {
            const { done, value } = await reader.read();
            if (done) break;
            touch();
            buffer += decoder.decode(value, { stream: true });
            let index;
            while ((index = buffer.indexOf('\n\n')) >= 0) {
              const frame = buffer.slice(0, index); buffer = buffer.slice(index + 2);
              const data = frame.split('\n').filter(s => s.startsWith('data:')).map(s => s.slice(5).trimStart()).join('\n');
              if (!data || data === '[DONE]') continue;
              const event = JSON.parse(data);
              const position = event.request_event;
              if (position) {
                if (attempt !== null && position.attempt !== attempt) throw new Error('执行版本已改变，请恢复请求状态');
                attempt = position.attempt;
                if (position.id <= cursor) continue;
              }
              if (event.heartbeat) attempt = event.heartbeat.attempt;
              await onEvent(event);
              if (position) cursor = position.id;
              failures = 0;
              terminal = Boolean(event.request_snapshot && event.request_snapshot.status !== 'running');
              if (terminal) return;
            }
          }
        } finally { await reader.cancel().catch(() => {}); }
        if (terminal || signal?.aborted) return;
        throw new Error('连接结束但未收到任务最终状态');
      } catch (error) {
        if (signal?.aborted) return;
        failures += 1;
        if (failures >= 5) throw new Error('连接暂未恢复；后端任务不会因此取消，请检查处理状态');
        onConnection('reconnecting');
        await wait(Math.min(1000 * 2 ** (failures - 1), 8000));
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener('abort', abort);
      }
    }
  }
  return { follow };
})();
