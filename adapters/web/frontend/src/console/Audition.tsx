// 试听：一段模拟群消息流，每条旁边显示 bot 会不会回、命中哪条规则。
// 判定全部来自后端 /qq/trigger/dry-run，前端不重写一份布尔组合。
import { useEffect } from 'react';

import type { QqDryRunVerdict } from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import {
  alignVerdicts,
  AUDITION_GROUP,
  MAX_MESSAGES,
  SENDERS,
  useAuditionStore,
} from './auditionStore';
import { useConsoleStore } from './consoleStore';
import { blockerLabel, showSignalTag, signalLabel } from './signals';

/** 三种结局各有各的读法：会回 / 不会回 / 要到运行时才知道。 */
const verdictOf = (v: QqDryRunVerdict) => {
  if (v.undetermined) return { mark: '看运行时', tone: 'qc-aud-mark-maybe' };
  if (v.triggered) return { mark: '会回应', tone: 'qc-aud-mark-yes' };
  return { mark: '不回应', tone: 'qc-aud-mark-no' };
};

const Verdict = ({ verdict }: { verdict: QqDryRunVerdict | undefined }) => {
  if (!verdict) return <p className="qc-aud-verdict qc-aud-verdict-void">↳ 等待判定</p>;
  const { mark, tone } = verdictOf(verdict);
  const blocked = verdict.blocked_by ? `${blockerLabel(verdict.blocked_by)}拦下` : '';
  return (
    // key 让判定翻转时节点重建，动画因此从头播 —— 「改开关 → 判定变了」这个因果要看得见。
    <p className="qc-aud-verdict" key={`${mark}${verdict.signal}${verdict.blocked_by}`}>
      <span aria-hidden="true">↳ </span>
      <span className={`qc-aud-mark ${tone}`}>{mark}</span>
      {showSignalTag(verdict.signal, verdict.reason) && (
        <span className="qc-aud-sig">{signalLabel(verdict.signal)}</span>
      )}
      {blocked && <span className="qc-aud-sig qc-aud-sig-halt">{blocked}</span>}
      <span className="qc-aud-why">{verdict.reason}</span>
    </p>
  );
};

export const Audition = () => {
  const token = useSettingsStore((state) => state.adminToken);
  const draft = useConsoleStore((state) => state.draft);
  const live = useConsoleStore((state) => state.live);
  const {
    messages, intervalSec, cooldown, result, resultIds, loading, error,
    patch, explode, add, remove, reset, setInterval: setIntervalSec, setCooldown, schedule,
  } = useAuditionStore();

  // draft / live 也是依赖：改开关必须让整列判定跟着重算，否则试听显示的是上一份配置。
  useEffect(() => {
    if (token) schedule(token);
  }, [token, messages, intervalSec, cooldown, draft, live, schedule]);

  const byId = alignVerdicts(resultIds, result?.results);
  const listening = result?.enabled_signals || [];

  return (
    <div className="qc-aud">
      <div className="qc-aud-head">
        <span className="qc-tag">{AUDITION_GROUP.alias}</span>
        <span className="qc-aud-listen">
          在听：
          {listening.length
            ? listening.map((name) => signalLabel(name)).join(' · ')
            : '没有开着的信号'}
        </span>
        <span className="qc-bar-spacer" />
        {loading && <span className="qc-aud-busy">重算中…</span>}
        <label className="qc-aud-toggle">
          <input checked={cooldown} onChange={(e) => setCooldown(e.target.checked)} type="checkbox" />
          算上冷却
        </label>
        <label className="qc-aud-toggle">
          间隔
          <input
            className="qc-inp qc-inp-tiny"
            min={0}
            onChange={(e) => setIntervalSec(Number.parseFloat(e.target.value))}
            step={1}
            type="number"
            value={String(intervalSec)}
          />
          秒
        </label>
      </div>

      {error && <p className="qc-login-error">{error}</p>}

      <ol className="qc-aud-list">
        {messages.map((message) => (
          <li className="qc-aud-item" key={message.id}>
            <div className="qc-aud-line">
              <button
                className="qc-tag qc-tag-btn"
                onClick={() => patch(message.id, { sender: message.sender === 'a' ? 'b' : 'a' })}
                title="换一个发言人。每人冷却是按人计的，两个人才看得出区别"
                type="button"
              >
                {SENDERS[message.sender].alias}
              </button>
              <input
                className="qc-inp qc-inp-wide"
                onChange={(e) => patch(message.id, { text: e.target.value })}
                onPaste={(e) => {
                  const lines = e.clipboardData.getData('text').split(/\r?\n/)
                    .map((line) => line.trim()).filter(Boolean);
                  if (lines.length < 2) return;
                  e.preventDefault();
                  explode(message.id, lines);
                }}
                placeholder="群里的一句话，可以直接粘贴多行"
                value={message.text}
              />
              <button
                aria-pressed={message.atMe}
                className="qc-flag"
                onClick={() => patch(message.id, { atMe: !message.atMe })}
                title="这条消息 @ 了 bot"
                type="button"
              >
                @
              </button>
              <button
                aria-pressed={message.replyToBot}
                className="qc-flag"
                onClick={() => patch(message.id, { replyToBot: !message.replyToBot })}
                title="这条消息引用了 bot 说过的话"
                type="button"
              >
                ↩
              </button>
              <button
                aria-label="删掉这条"
                className="qc-flag qc-flag-x"
                onClick={() => remove(message.id)}
                type="button"
              >
                ×
              </button>
            </div>
            <Verdict verdict={byId.get(message.id)} />
          </li>
        ))}
      </ol>

      <div className="qc-aud-foot">
        <button
          className="qc-btn qc-btn-quiet"
          disabled={messages.length >= MAX_MESSAGES}
          onClick={add}
          type="button"
        >
          加一条
        </button>
        <button className="qc-btn qc-btn-quiet" onClick={reset} type="button">
          恢复样例
        </button>
        <span className="qc-aud-count">
          {messages.length >= MAX_MESSAGES ? `已到 ${MAX_MESSAGES} 条上限` : `${messages.length} 条`}
        </span>
      </div>
    </div>
  );
};
