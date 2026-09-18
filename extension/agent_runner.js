(() => {
  function bounded(promise, ms, signal) {
    return new Promise((resolve, reject) => {
      const abort = () => done(reject, Object.assign(new Error('任务已停止'), { code: 'cancelled' }));
      let timer;
      const done = (fn, value) => { clearTimeout(timer); signal?.removeEventListener('abort', abort); fn(value); };
      timer = setTimeout(() => done(reject, Object.assign(new Error('等待超时'), { code: 'timeout' })), Math.max(1, ms));
      Promise.resolve(promise).then(value => done(resolve, value), error => done(reject, error));
      signal?.addEventListener('abort', abort, { once: true });
      if (signal?.aborted) abort();
    });
  }

  class Run {
    constructor(sessionId, tabId, io, limits = {}) {
      this.sessionId = sessionId; this.tabId = tabId; this.io = io;
      this.limits = { total: 3600000, observe: 30000, recovery: 90000, action: 120000,
        settleAction: 30000, decision: 180000, poll: 1000, ...limits };
      this.deadline = Date.now() + this.limits.total;
      this.abort = new AbortController(); this.sequence = 0;
      this.startSubmitted = false; this.startAcknowledged = false;
      this.pendingActionId = null; this.backendSubmitted = false;
    }
    check() {
      if (this.abort.signal.aborted) throw Object.assign(new Error('任务已停止'), { code: 'cancelled' });
      if (Date.now() >= this.deadline) throw new Error('任务超过总时间预算，未完成');
    }
    id(prefix) { return `${this.sessionId}:${prefix}:${++this.sequence}`; }
    async wait(promise, ms) {
      this.check();
      const result = await bounded(promise, Math.min(ms, this.deadline - Date.now()), this.abort.signal);
      this.check(); return result;
    }
    async message(type, data = {}, timeout = 5000, stopping = false) {
      if (!stopping) this.check();
      const promise = this.io.message({ ...data, type, tabId: this.tabId, sessionId: this.sessionId });
      const response = stopping ? await bounded(promise, timeout) : await this.wait(promise, timeout);
      if (!response?.ok) throw Object.assign(new Error(response?.error || '浏览器后台无响应'), { code: response?.code });
      return response;
    }
    async control(command, data = {}, stopping = false) {
      return (await this.message('AGENT_CONTROL', { command, ...data }, 5000, stopping)).result;
    }
    async start() {
      try {
        const probe = await this.control('probe');
        if (probe?.protocol_version !== 2) throw new Error('协议不匹配');
      } catch (error) {
        if (error.code === 'cancelled') throw error;
        throw Object.assign(new Error('无法确认扩展后台通信或协议版本，请在扩展管理页重新加载扩展，并关闭后重新打开侧边栏'), { code: 'protocol_mismatch' });
      }
      this.check();
      this.startSubmitted = true;
      const response = await this.control('start');
      if (response?.protocol_version !== 2) throw new Error('请重新加载扩展以启用自动化协议 v2');
      this.startAcknowledged = true;
    }
    async stop() {
      this.abort.abort();
      const results = await Promise.allSettled([
        ...(this.startSubmitted ? [this.control('cancel', {}, true)] : []),
        ...(this.backendSubmitted ? [bounded(this.io.api('/v1/agent/cancel', { session_id: this.sessionId }), 5000)] : []),
      ]);
      return results;
    }
    async sleep(ms) { return this.wait(new Promise(r => setTimeout(r, ms)), ms + 1000); }

    async observe() {
      const deadline = Math.min(this.deadline, Date.now() + this.limits.recovery);
      let attempt = 0;
      while (Date.now() < deadline) {
        this.check();
        const observationId = this.id('observe');
        try {
          const response = await this.message('AGENT_OBSERVE', { observationId, includeScreenshot: true },
            Math.min(this.limits.observe, deadline - Date.now()));
          const page = response.pageState;
          if (page?.observation_id !== observationId || page.tab_id !== this.tabId || !page.document_epoch) {
            throw Object.assign(new Error('观察协议不匹配，请同时更新后端和扩展'), { code: 'protocol_mismatch' });
          }
          const annotated = await this.wait(this.io.annotate(page), Math.max(1, deadline - Date.now()));
          this.io.status('running', '页面状态已更新');
          return annotated;
        } catch (error) {
          this.check();
          if (['cancelled', 'session_lost', 'protocol_mismatch', 'execution_unknown'].includes(error.code)) throw error;
          await this.control('invalidate_observation', { observationId });
          this.io.status('recovering', `正在重新获取页面状态（第 ${++attempt} 次恢复）`);
          const remaining = deadline - Date.now();
          if (remaining > 0) await this.sleep(Math.min(this.limits.poll * Math.min(attempt, 4), remaining));
        }
      }
      throw new Error('页面观察持续失败，已达到自动恢复时间预算；未使用旧观察继续操作');
    }

    async decide(path, body) {
      const deadline = Math.min(this.deadline, Date.now() + this.limits.decision);
      const request = { ...body, session_id: this.sessionId, protocol_version: 2,
        request_id: this.id('decision'), budget_ms: Math.max(1, deadline - Date.now()) };
      let submit = true;
      while (Date.now() < deadline) {
        this.check();
        try {
          if (submit) this.backendSubmitted = true;
          const result = await this.wait(this.io.api(submit ? path : '/v1/agent/status', submit ? request :
            { session_id: this.sessionId, request_id: request.request_id }), Math.min(30000, deadline - Date.now()));
          if (result?.protocol_version !== 2 || result.session_id !== this.sessionId) throw Object.assign(new Error('自动化协议不匹配，请更新后端'), { status: 409 });
          if (result.status !== 'processing') return result;
          submit = false;
        } catch (error) {
          this.check();
          if (error.status >= 400 && error.status < 500 && !(error.status === 404 && !submit)) throw error;
          submit = error.status === 404;
          this.io.status('recovering', '正在确认后端决策状态');
        }
        await this.sleep(Math.min(this.limits.poll, Math.max(1, deadline - Date.now())));
      }
      throw new Error('模型决策超过时间预算，任务未完成');
    }

    async execute(action) {
      this.check();
      // 先记可能已送达；回复丢失不能被当成没有执行。
      this.pendingActionId = action.action_id;
      try {
        const response = await this.message('AGENT_EXECUTE', { action,
          timeoutMs: Math.min(this.limits.action, this.deadline - Date.now()) }, this.limits.action);
        if (!['completed', 'partial', 'not_dispatched'].includes(response.result?.execution_state)) throw new Error('动作结果未知，禁止自动重试');
        this.pendingActionId = null;
        return response.result;
      } catch (error) {
        this.check();
        if (error.code && error.code !== 'timeout') throw error;
        this.io.status('recovering', '正在确认上一动作是否结束，不重复执行');
        await this.control('abort_action', { actionId: action.action_id });
        const deadline = Math.min(this.deadline, Date.now() + this.limits.settleAction);
        while (Date.now() < deadline) {
          const state = await this.control('status', { actionId: action.action_id });
          if (state.state === 'finished') { this.pendingActionId = null; return state.result; }
          if (state.state === 'not_received') throw new Error('动作是否被接收无法确认，任务结束且不重放');
          if (state.state === 'unknown') break;
          await this.sleep(Math.min(this.limits.poll, Math.max(1, deadline - Date.now())));
        }
        throw new Error('旧动作执行结果仍未知，已阻止后续输入');
      }
    }

    async finish() {
      this.abort.abort();
      if (!this.startSubmitted) return { state: 'not_started', safe: true };
      try {
        // 先登记关闭意图，即使侧边栏结束等待，迟到动作也会继续收尾。
        let closed = await this.control('end', {}, true);
        const deadline = Date.now() + 5000;
        while (closed?.reason === 'action_pending' && Date.now() < deadline) {
          await new Promise(resolve => setTimeout(resolve, 100));
          const state = await this.control('status', {}, true);
          if (state.safe || state.state === 'unknown') break;
        }
        if (closed?.reason === 'action_pending') closed = await this.control('end', {}, true);
        if (typeof closed?.safe !== 'boolean') throw new Error('收尾响应无效');
        return closed;
      } catch {
        return { state: 'unconfirmed', safe: false, reason: this.pendingActionId ? 'execution_unknown' :
          this.startAcknowledged ? 'cleanup_unconfirmed' : 'startup_unconfirmed' };
      }
    }
  }
  globalThis.AgentRunner = { Run, bounded };
  if (typeof module !== 'undefined') module.exports = globalThis.AgentRunner;
})();
