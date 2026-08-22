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
import { Button, Check, ErrorText, Input, Tag } from './components';
import { useConsoleStore } from './consoleStore';
import { blockerLabel, showSignalTag, signalLabel } from './signals';

const VERDICT =
  'mt-1.5 ml-[34px] flex flex-wrap items-baseline gap-1.5 text-xs max-md:ml-0'
  + ' motion-safe:animate-[verdict-flip_120ms_ease-out]';

const SIG = 'bg-[var(--duties-muted)] px-1.5 font-mono text-[0.65rem]';

const FLAG =
  'w-6 flex-none border border-[var(--duties-border)] bg-[var(--duties-panel)] py-0.5'
  + ' font-mono text-xs leading-tight text-[var(--duties-secondary)] transition-colors';

const FLAG_TOGGLE =
  `${FLAG} hover:border-[var(--duties-text)] hover:text-[var(--duties-text)]`
  + ' aria-pressed:border-[var(--duties-text)] aria-pressed:bg-[var(--duties-text)]'
  + ' aria-pressed:text-[var(--duties-bg)] aria-pressed:hover:text-[var(--duties-bg)]';

/** 三种结局各有各的读法：会回 / 不会回 / 要到运行时才知道。 */
const verdictOf = (v: QqDryRunVerdict) => {
  if (v.undetermined) return { mark: '看运行时', tone: 'text-[var(--duties-text)]' };
  if (v.triggered) return { mark: '会回应', tone: 'text-[var(--duties-live)]' };
  return { mark: '不回应', tone: 'text-[var(--duties-secondary)]' };
};

const Verdict = ({ verdict }: { verdict: QqDryRunVerdict | undefined }) => {
  if (!verdict) return <p className={`${VERDICT} text-[var(--duties-secondary)]`}>↳ 等待判定</p>;
  const { mark, tone } = verdictOf(verdict);
  const blocked = verdict.blocked_by ? `${blockerLabel(verdict.blocked_by)}拦下` : '';
  return (
    // key 让判定翻转时节点重建，动画因此从头播 —— 「改开关 → 判定变了」这个因果要看得见。
    <p className={VERDICT} key={`${mark}${verdict.signal}${verdict.blocked_by}`}>
      <span aria-hidden="true">↳ </span>
      <span className={`font-semibold ${tone}`}>{mark}</span>
      {showSignalTag(verdict.signal, verdict.reason) && (
        <span className={SIG}>{signalLabel(verdict.signal)}</span>
      )}
      {blocked && <span className={`${SIG} text-[var(--duties-danger)]`}>{blocked}</span>}
      <span className="text-[var(--duties-secondary)]">{verdict.reason}</span>
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
    <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)]">
      <div className="flex flex-wrap items-center gap-2.5 border-b border-[var(--duties-border)] px-3.5 py-2.5">
        <Tag>{AUDITION_GROUP.alias}</Tag>
        <span className="text-xs text-[var(--duties-secondary)]">
          在听：
          {listening.length
            ? listening.map((name) => signalLabel(name)).join(' · ')
            : '没有开着的信号'}
        </span>
        <span className="flex-1" />
        {loading && (
          <span className="font-mono text-[0.65rem] text-[var(--duties-secondary)]">重算中…</span>
        )}
        <Check checked={cooldown} onChange={setCooldown}>算上冷却</Check>
        <label className="flex items-center gap-1.5 text-xs">
          间隔
          <Input
            min={0}
            onChange={(e) => setIntervalSec(Number.parseFloat(e.target.value))}
            step={1}
            type="number"
            value={String(intervalSec)}
            width="tiny"
          />
          秒
        </label>
      </div>

      {error && <ErrorText>{error}</ErrorText>}

      <ol className="py-1.5">
        {messages.map((message) => (
          <li className="px-3.5 py-2" key={message.id}>
            <div className="flex items-center gap-1.5 max-md:flex-wrap">
              <button
                className="flex-none"
                onClick={() => patch(message.id, { sender: message.sender === 'a' ? 'b' : 'a' })}
                title="换一个发言人。每人冷却是按人计的，两个人才看得出区别"
                type="button"
              >
                <Tag className="cursor-pointer hover:text-[var(--duties-text)]">
                  {SENDERS[message.sender].alias}
                </Tag>
              </button>
              <Input
                className="max-md:order-first max-md:basis-full"
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
                width="wide"
              />
              <button
                aria-pressed={message.atMe}
                className={FLAG_TOGGLE}
                onClick={() => patch(message.id, { atMe: !message.atMe })}
                title="这条消息 @ 了 bot"
                type="button"
              >
                @
              </button>
              <button
                aria-pressed={message.replyToBot}
                className={FLAG_TOGGLE}
                onClick={() => patch(message.id, { replyToBot: !message.replyToBot })}
                title="这条消息引用了 bot 说过的话"
                type="button"
              >
                ↩
              </button>
              <button
                aria-label="删掉这条"
                className={`${FLAG} hover:border-[var(--duties-danger)] hover:text-[var(--duties-danger)]`}
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

      <div className="flex items-center gap-2 border-t border-[var(--duties-border)] px-3.5 py-2.5">
        <Button disabled={messages.length >= MAX_MESSAGES} onClick={add} tone="quiet">
          加一条
        </Button>
        <Button onClick={reset} tone="quiet">恢复样例</Button>
        <span className="font-mono text-[0.65rem] text-[var(--duties-secondary)]">
          {messages.length >= MAX_MESSAGES ? `已到 ${MAX_MESSAGES} 条上限` : `${messages.length} 条`}
        </span>
      </div>
    </div>
  );
};
