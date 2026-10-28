// 带图消息交给主模型还是绕去视觉入口节点。写的是 runtime.yaml，不走顶部那条 qq.yaml 状态条。
import { useEffect, useState } from 'react';

import { getRuntimeFile } from '../api/supervisorClient';
import { useRequestGuard } from '../hooks/useRequestGuard';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';
import { useSettingsStore } from '../store/settingsStore';
import { Block, Desc, Empty, ErrorText, Facts, Field, SaveBar, Select } from './components';
import { commitRuntimeKeys } from './yamlCommit';
import { parseYamlMapping, readYamlPath } from './yamlRewrite';
import { savedMessage } from '../utils/sentence';
import { errorMessage } from '../api/request';

const PATH = ['routing', 'vision', 'enabled'];

const readMode = (content: string): string => {
  const value = readYamlPath(parseYamlMapping(content), PATH);
  return normalizeMode(value === undefined || value === null ? '' : String(value));
};

// 「自动」是字符串，另两档写成布尔，和手写 yaml 的读法一致。
const yamlValueOf = (mode: string): boolean | string => (mode === 'true' ? true : mode === 'false' ? false : 'auto');

const MODES = [
  { value: 'auto', label: '看不了图才绕' },
  { value: 'true', label: '总是绕过去' },
  { value: 'false', label: '从不绕' },
];

const NOTES: Record<string, string> = {
  auto: '主渠道自己收得下图就直接交给它，工具、委派、绘图都还在；收不下、且视觉节点确实配了自己的模型，才绕过去。',
  true: '带图消息一律绕去视觉节点，那里没有任何工具 —— 想把图片挡在工具外面就选这个。',
  false: '带图消息一律交给主渠道，哪怕它收不下图（那会直接报错，靠备选链兜）。',
};

/** yaml 里写过 true/yes/on 各种写法，都归到三档里。 */
export const normalizeMode = (raw: string): string => {
  const value = (raw || '').trim().toLowerCase().replace(/^["']|["']$/g, '');
  if (['false', '0', 'no', 'off'].includes(value)) return 'false';
  if (['true', '1', 'yes', 'on'].includes(value)) return 'true';
  return 'auto';
};

export const VisionRouting = () => {
  const token = useSettingsStore((state) => state.adminToken);
  const [raw, setRaw] = useState<string | null>(null);
  const [mode, setMode] = useState('auto');
  const [saved, setSaved] = useState('auto');
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [error, setError] = useState('');
  const confirmDiscard = useUnsavedChanges(mode !== saved, '带图路由尚未保存，确定离开或丢弃吗？');
  const requests = useRequestGuard();

  useEffect(() => {
    if (!token) return;
    const request = requests.begin('runtime');
    setRaw(null);
    void getRuntimeFile(token).then(({ content: text }) => {
      if (!request.isCurrent()) return;
      const current = readMode(text);
      setRaw(text);
      setMode(current);
      setSaved(current);
    }).catch((caught) => {
      if (!request.isCurrent()) return;
      setError(errorMessage(caught));
      setRaw(null);
    });
  }, [requests, token]);

  const save = async () => {
    if (!token || raw === null || busy) return;
    setBusy(true);
    setError('');
    try {
      // 只写这一个键，落在本实例那层；读和写之间被改过由后端回 409。
      const { content, note: rewriteNote, result, receiptProblem } = await commitRuntimeKeys(token, [{ path: PATH, value: yamlValueOf(mode) }]);
      setRaw(content);
      setSaved(mode);
      const saved = rewriteNote ? `已保存。${rewriteNote}。下一条带图消息就按新规矩走。` : '已保存。下一条带图消息就按新规矩走。';
      setNote(savedMessage(saved, '', result?.warnings));
      setError(receiptProblem);
    } catch (caught) {
      setError(errorMessage(caught));
    }
    setBusy(false);
  };

  if (raw === null) return <Empty>{error || '正在读取带图路由…'}</Empty>;

  return (
    <Block hint="带图消息交给主渠道还是绕去视觉入口节点" title="带图消息">
      <Field label="路由方式">
        <Select
          disabled={busy}
          aria-label="带图消息路由"
          onChange={(event) => setMode(event.target.value)}
          value={mode}
          width="wide"
        >
          {MODES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
        </Select>
      </Field>
      <Desc indent={false}>{NOTES[mode]}</Desc>
      {mode === 'auto' && (
        <Facts>
          按上面每个渠道的「带图消息」判定。绕过去还有一个前提：视觉入口节点得有自己的
          CLONOTH_VISION_MODEL 或 CLONOTH_VISION_BASE_URL（旧名 QQ_VISION_* 也认）。两个都空时它解析出来就是主渠道那个模型，
          绕过去只是白丢工具，所以自动档会留在主渠道并在日志里说一声。
        </Facts>
      )}
      {error && <ErrorText>{error}</ErrorText>}
      <SaveBar
        busy={busy}
        dirty={mode !== saved}
        label="保存路由"
        note={note}
        onReset={() => { if (confirmDiscard()) setMode(saved); }}
        onSave={() => void save()}
      />
    </Block>
  );
};
