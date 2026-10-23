(/** 封装并导出标签页执行控制器及受控异常。 */ () => {
  class ControlError extends Error {
    /** 创建带机器可读错误码的执行控制异常。 */
    constructor(code, message) { super(message); this.code = code; }
  }

  class Controller {
    /** 初始化持久化接口、各标签页的运行状态、串行队列及取消记录。 */
    constructor(store) { this.store = store; this.tabs = new Map(); this.queues = new Map(); this.cancelledSessions = new Set(); }

    /** 将状态变更加入指定标签页的串行队列，隔离此前任务失败的影响。 */
    serial(tabId, fn) {
      // 每个标签页独立串行化状态变更；前一项失败不能阻塞后续取消或清理。
      const previous = this.queues.get(tabId) || Promise.resolve();
      const next = previous.catch(/** 吸收前序失败，使串行队列继续处理后续请求。 */ () => {}).then(fn);
      this.queues.set(tabId, next);
      next.finally(/** 仅移除仍指向本任务的队列记录。 */ () => { if (this.queues.get(tabId) === next) this.queues.delete(tabId); }).catch(/** 消费清理链的拒绝，保留原任务结果。 */ () => {});
      return next;
    }

    /** 检查旧任务和持久化状态，确认可安全启动后登记新会话。 */
    async start(tabId, sessionId) {
      return this.serial(tabId, /** 串行检查旧状态并登记新会话。 */ async () => {
        if (this.cancelledSessions.has(sessionId)) throw new ControlError('cancelled', '任务已停止');
        const old = this.tabs.get(tabId);
        if (old?.sessionId === sessionId) { this.assertRun(old); return { protocol_version: 2 }; }
        if (old?.closeRequested && !old.current && !old.ended) {
          const closed = await this.closeRun(old);
          if (!closed.safe) throw new ControlError('cleanup_failed', '上个任务的资源清理尚未完成，请稍后重试');
        }
        if (old && (!old.ended || old.current)) throw new ControlError('tab_busy', '该标签页仍有任务或未确认的动作');
        const saved = await this.store.read(tabId);
        if (this.cancelledSessions.has(sessionId)) throw new ControlError('cancelled', '任务已停止');
        if (saved?.state === 'closing') {
          throw new ControlError('cleanup_unconfirmed', '后台在任务收尾时中断，资源释放状态未确认；请检查页面后重新打开测试标签页');
        }
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

    /** 返回指定标签页的当前会话，拒绝已丢失或被替换的会话标识。 */
    getRun(tabId, sessionId) {
      const run = this.tabs.get(tabId);
      if (!run || run.sessionId !== sessionId) throw new ControlError('session_lost', '任务已失效，请重新启动');
      return run;
    }

    /** 确认运行记录仍是当前任务且尚未取消或结束。 */
    assertRun(run) {
      if (this.tabs.get(run.tabId) !== run || run.cancelled || run.ended) throw new ControlError('cancelled', '任务已停止');
    }

    /** 登记会话取消，标记当前观察和动作失效，并返回停止进度。 */
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

    /** 取消指定未完成动作；动作未登记时取消会话，再返回当前状态。 */
    abortAction(tabId, sessionId, actionId) {
      const run = this.getRun(tabId, sessionId);
      const record = run.actions.get(actionId);
      if (record && !record.settled) record.cancelled = true;
      if (!record) this.cancel(tabId, sessionId);
      return this.status(tabId, sessionId, actionId);
    }

    /** 报告动作接收、执行与完成状态，并注明是否可以安全继续。 */
    status(tabId, sessionId, actionId) {
      const run = this.getRun(tabId, sessionId);
      const record = actionId ? run.actions.get(actionId) : run.current;
      if (!record) return { state: actionId ? 'not_received' : 'stopped', safe: !run.current };
      return { state: record.settled ? 'finished' : record.unknown ? 'unknown' : record.pending.size ? 'running' : 'stopping',
        safe: record.settled, result: record.settled ? record.result : null };
    }

    /** 在无未完成动作时创建观察令牌，并使上一轮观察失效。 */
    observeToken(tabId, sessionId, observationId) {
      const run = this.getRun(tabId, sessionId);
      this.assertRun(run);
      if (run.current) throw new ControlError('action_busy', '旧动作尚未结束，稍后重新观察');
      if (run.observation) run.observation.cancelled = true;
      const token = { id: observationId, cancelled: false, run,
        /** 检查会话与观察令牌是否仍有效，阻止发布被替换的观察。 */
        check: () => { this.assertRun(run); if (token.cancelled || run.observation !== token) throw new ControlError('stale_observation', '观察已被替换'); } };
      run.observation = token;
      return token;
    }

    /** 仅使标识匹配的当前观察失效，避免误取消新观察。 */
    invalidateObservation(tabId, sessionId, observationId) {
      const run = this.getRun(tabId, sessionId);
      if (run.observation?.id === observationId) run.observation.cancelled = true;
    }

    /** 在标签页串行队列内发布观察，并在发布前后检查令牌有效性。 */
    publishObservation(token, publish) {
      return this.serial(token.run.tabId, /** 在发布前后验证观察令牌。 */ async () => {
        token.check();
        await publish();
        token.check();
      });
    }

    /** 校验观察和动作标识，持久化占用后执行单步动作，并复用重复请求的原结果。 */
    async execute(tabId, sessionId, action, perform, timeoutMs = 120000) {
      const prepared = await this.serial(tabId, /** 串行校验动作身份并建立唯一执行记录。 */ async () => {
        const run = this.getRun(tabId, sessionId);
        const digest = JSON.stringify(action);
        const old = run.actions.get(action.action_id);
        // 重复动作复用原来的完成结果，且同一标识不能携带不同输入。
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
        /** 在派发操作前检查会话、动作取消、输入异常和截止时间。 */
        record.check = () => {
          this.assertRun(run);
          if (record.cancelled || record.effectError || Date.now() >= record.deadline) throw new ControlError('action_cancelled', '动作已停止或超时');
        };
        /** 跟踪尚未结束的异步操作；记录副作用失败并在操作结束时尝试收尾。 */
        record.track = (promise, effect = false) => {
          record.pending.add(promise);
          promise.catch(/** 把副作用失败标记为执行结果不可确认。 */ () => { if (effect) record.effectError = true; });
          promise.finally(/** 移除已完成操作并尝试结束动作。 */ () => { record.pending.delete(promise); this.finish(record); }).catch(/** 消费跟踪链的拒绝，错误由动作状态记录。 */ () => {});
        };
        run.actions.set(action.action_id, record);
        run.current = record;
        let resolve;
        record.promise = new Promise(/** 保存动作结果的完成回调供收尾时调用。 */ r => { resolve = r; });
        record.resolve = resolve;
        try {
          // 在任何输入派发前保存占用；后台重启不能把未确认动作视为未执行。
          await this.store.write(tabId, { sessionId, actionId: action.action_id, state: 'running' });
          record.check();
          run.observation.cancelled = true;
          Promise.resolve().then(/** 再次检查动作有效性后执行动作主体。 */ () => { record.check(); return perform(record); }).then(/** 保存动作主体返回的结果。 */ result => { record.result = result; }, /** 将动作主体异常转换为失败结果。 */ error => {
            record.result = { success: false, action_type: action.type, error: error.message };
          }).finally(/** 标记主体已结束并尝试统一收尾。 */ () => { record.bodyDone = true; this.finish(record); });
        } catch (error) {
          record.result = { success: false, action_type: action.type, error: error.message };
          record.bodyDone = true;
          this.finish(record);
        }
        return { promise: record.promise };
      });
      return prepared.promise;
    }

    /** 等待动作主体及所有已派发操作结束，持久化结果后才解除标签页占用。 */
    finish(record) {
      // 动作主体返回后，仍要等已派发的输入及按键释放全部结束，才能解除标签页占用。
      if (!record.bodyDone || record.pending.size || record.finishing) return;
      record.finishing = true;
      this.serial(record.run.tabId, /** 串行持久化动作结果并处理待关闭会话。 */ async () => {
        const result = { ...record.result, action_id: record.action.action_id,
          ...(record.action.type === 'click' ? { click_effects: { press_sent: false, release_confirmed: false, ...record.clickEffects } } : {}),
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
        if (record.run.closeRequested) await this.closeRun(record.run);
      }).catch(/** 消费收尾队列的拒绝，避免未处理的异步错误。 */ () => {});
    }

    // 仅从标签页串行队列内部调用，避免收尾重入队列后等待自身。
    /** 从串行队列内部清理任务资源；仍有动作或清理失败时保留未完成状态。 */
    async closeRun(run) {
      if (this.tabs.get(run.tabId) !== run) return { state: 'superseded', safe: true };
      if (run.current) return { state: run.current.unknown ? 'unknown' : 'stopping', safe: false,
        reason: run.current.unknown ? 'execution_unknown' : 'action_pending' };
      if (run.ended) return { state: 'stopped', safe: true };
      try {
        if (!run.cleaned) {
          await this.store.write(run.tabId, { sessionId: run.sessionId, state: 'closing' });
          await run.cleanup();
          run.cleaned = true;
        }
        await this.store.write(run.tabId, { sessionId: run.sessionId, state: 'finished' });
        run.ended = true;
        return { state: 'stopped', safe: true };
      } catch {
        return { state: 'cleanup_failed', safe: false, reason: 'cleanup_failed' };
      }
    }

    /** 登记取消和关闭意图，待动作收尾后执行资源清理并返回安全状态。 */
    async end(tabId, sessionId, cleanup) {
      await this.cancel(tabId, sessionId);
      return this.serial(tabId, /** 串行登记清理函数并尝试关闭当前会话。 */ async () => {
        const run = this.tabs.get(tabId);
        if (!run || run.sessionId !== sessionId) return { state: 'superseded', safe: true };
        run.closeRequested = true;
        run.cleanup ||= cleanup;
        return this.closeRun(run);
      });
    }
  }
  globalThis.AgentExecution = { Controller, ControlError };
  if (typeof module !== 'undefined') module.exports = globalThis.AgentExecution;
})();
