(() => {
  class ControlError extends Error {
    constructor(code, message) { super(message); this.code = code; }
  }

  class Controller {
    constructor(store) { this.store = store; this.tabs = new Map(); this.queues = new Map(); this.cancelledSessions = new Set(); }

    serial(tabId, fn) {
      const previous = this.queues.get(tabId) || Promise.resolve();
      const next = previous.catch(() => {}).then(fn);
      this.queues.set(tabId, next);
      next.finally(() => { if (this.queues.get(tabId) === next) this.queues.delete(tabId); }).catch(() => {});
      return next;
    }

    async start(tabId, sessionId) {
      return this.serial(tabId, async () => {
        if (this.cancelledSessions.has(sessionId)) throw new ControlError('cancelled', '任务已停止');
        const old = this.tabs.get(tabId);
        if (old?.sessionId === sessionId) { this.assertRun(old); return { protocol_version: 2 }; }
        if (old && (!old.ended || old.current)) throw new ControlError('tab_busy', '该标签页仍有任务或未确认的动作');
        const saved = await this.store.read(tabId);
        if (this.cancelledSessions.has(sessionId)) throw new ControlError('cancelled', '任务已停止');
        if (saved && ['running', 'unknown'].includes(saved.state)) {
          throw new ControlError('execution_unknown', '后台曾中断，旧动作结果未知；请检查页面，不能自动重放');
        }
        const run = { tabId, sessionId, cancelled: false, ended: false, current: null, actions: new Map(), observation: null };
        this.tabs.set(tabId, run);
        await this.store.write(tabId, { sessionId, state: 'idle' });
        this.assertRun(run);
        return { protocol_version: 2 };
      });
    }

    getRun(tabId, sessionId) {
      const run = this.tabs.get(tabId);
      if (!run || run.sessionId !== sessionId) throw new ControlError('session_lost', '任务已失效，请重新启动');
      return run;
    }

    assertRun(run) {
      if (this.tabs.get(run.tabId) !== run || run.cancelled || run.ended) throw new ControlError('cancelled', '任务已停止');
    }

    async cancel(tabId, sessionId) {
      this.cancelledSessions.add(sessionId);
      if (this.cancelledSessions.size > 512) this.cancelledSessions.delete(this.cancelledSessions.values().next().value);
      const run = this.tabs.get(tabId);
      if (!run || run.sessionId !== sessionId) return { state: 'stopped' };
      run.cancelled = true;
      if (run.observation) run.observation.cancelled = true;
      if (run.current) run.current.cancelled = true;
      return { state: run.current ? 'stopping' : 'stopped' };
    }

    abortAction(tabId, sessionId, actionId) {
      const run = this.getRun(tabId, sessionId);
      const record = run.actions.get(actionId);
      if (record && !record.settled) record.cancelled = true;
      if (!record) this.cancel(tabId, sessionId);
      return this.status(tabId, sessionId, actionId);
    }

    status(tabId, sessionId, actionId) {
      const run = this.getRun(tabId, sessionId);
      const record = actionId ? run.actions.get(actionId) : run.current;
      if (!record) return { state: actionId ? 'not_received' : 'stopped', safe: !run.current };
      return { state: record.settled ? 'finished' : record.unknown ? 'unknown' : record.pending.size ? 'running' : 'stopping',
        safe: record.settled, result: record.settled ? record.result : null };
    }

    observeToken(tabId, sessionId, observationId) {
      const run = this.getRun(tabId, sessionId);
      this.assertRun(run);
      if (run.current) throw new ControlError('action_busy', '旧动作尚未结束，稍后重新观察');
      if (run.observation) run.observation.cancelled = true;
      const token = { id: observationId, cancelled: false, run,
        check: () => { this.assertRun(run); if (token.cancelled || run.observation !== token) throw new ControlError('stale_observation', '观察已被替换'); } };
      run.observation = token;
      return token;
    }

    invalidateObservation(tabId, sessionId, observationId) {
      const run = this.getRun(tabId, sessionId);
      if (run.observation?.id === observationId) run.observation.cancelled = true;
    }

    publishObservation(token, publish) {
      return this.serial(token.run.tabId, async () => {
        token.check();
        await publish();
        token.check();
      });
    }

    async execute(tabId, sessionId, action, perform, timeoutMs = 120000) {
      const prepared = await this.serial(tabId, async () => {
        const run = this.getRun(tabId, sessionId);
        const digest = JSON.stringify(action);
        const old = run.actions.get(action.action_id);
        if (old) {
          if (old.digest !== digest) throw new ControlError('action_conflict', '动作标识不能用于不同内容');
          return { promise: old.promise };
        }
        this.assertRun(run);
        if (run.current) throw new ControlError('action_busy', '旧动作尚未结束，禁止并发输入');
        if (!action.action_id || !action.observation_id || run.observation?.id !== action.observation_id || run.observation.cancelled) {
          return { promise: Promise.resolve({ success: false, stale: true, action_type: action.type,
            action_id: action.action_id, execution_state: 'not_dispatched', details: '观察已失效，请重新观察' }) };
        }
        if (run.actions.size >= 256) throw new ControlError('action_limit', '任务动作数量已达上限');
        const record = { action, digest, run, cancelled: false, dispatched: false, pending: new Set(),
          settled: false, bodyDone: false, deadline: Date.now() + Math.min(timeoutMs, 120000) };
        record.check = () => {
          this.assertRun(run);
          if (record.cancelled || record.effectError || Date.now() >= record.deadline) throw new ControlError('action_cancelled', '动作已停止或超时');
        };
        record.track = (promise, effect = false) => {
          record.pending.add(promise);
          promise.catch(() => { if (effect) record.effectError = true; });
          promise.finally(() => { record.pending.delete(promise); this.finish(record); }).catch(() => {});
        };
        run.actions.set(action.action_id, record);
        run.current = record;
        let resolve;
        record.promise = new Promise(r => { resolve = r; });
        record.resolve = resolve;
        try {
          // 在任何输入派发前保存占用；后台重启不能把未确认动作视为未执行。
          await this.store.write(tabId, { sessionId, actionId: action.action_id, state: 'running' });
          record.check();
          run.observation.cancelled = true;
          Promise.resolve().then(() => { record.check(); return perform(record); }).then(result => { record.result = result; }, error => {
            record.result = { success: false, action_type: action.type, error: error.message };
          }).finally(() => { record.bodyDone = true; this.finish(record); });
        } catch (error) {
          record.result = { success: false, action_type: action.type, error: error.message };
          record.bodyDone = true;
          this.finish(record);
        }
        return { promise: record.promise };
      });
      return prepared.promise;
    }

    finish(record) {
      if (!record.bodyDone || record.pending.size || record.finishing) return;
      record.finishing = true;
      this.serial(record.run.tabId, async () => {
        const result = { ...record.result, action_id: record.action.action_id,
          execution_state: record.result.success ? 'completed' : record.dispatched ? 'partial' : 'not_dispatched' };
        if (record.effectError) {
          record.unknown = true;
          record.resolve({ ...result, success: false, execution_state: 'unknown', error: '输入或释放按键失败，执行状态未知，禁止继续输入' });
          return;
        }
        try {
          await this.store.write(record.run.tabId, { sessionId: record.run.sessionId,
            actionId: record.action.action_id, state: 'finished' });
        } catch {
          record.unknown = true;
          record.resolve({ ...result, success: false, execution_state: 'unknown', error: '执行状态未能持久化，禁止自动重试' });
          return;
        }
        record.result = result;
        record.settled = true;
        if (record.run.current === record) record.run.current = null;
        record.resolve(result);
      }).catch(() => {});
    }

    async end(tabId, sessionId, cleanup) {
      await this.cancel(tabId, sessionId);
      return this.serial(tabId, async () => {
        const run = this.tabs.get(tabId);
        if (!run || run.sessionId !== sessionId) return { state: 'superseded' };
        if (run.current) return { state: 'unknown', safe: false };
        await cleanup();
        run.ended = true;
        await this.store.write(tabId, { sessionId, state: 'finished' });
        return { state: 'stopped', safe: true };
      });
    }
  }
  globalThis.AgentExecution = { Controller, ControlError };
  if (typeof module !== 'undefined') module.exports = globalThis.AgentExecution;
})();
