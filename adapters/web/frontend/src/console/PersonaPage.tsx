// 人格：人设、模型与补充 System。各块各自保存，写的是节点文件。
import { useEffect, useId, useState } from 'react';
import { usePageNotice } from '../hooks/usePageNotice';
import { useRequestGuard } from '../hooks/useRequestGuard';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';

import {
  getNodeFileRaw,
  getNodeFiles,
  getNodeRaw,
  getNodes,
  getProviders,
  isEditConflict,
  isNotFound,
  receipted,
  updateNodeFileRaw,
  updateNodeRaw,
  type RawText,
} from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import { mergeModelChoices, modelsFromNodes, modelsFromProviders } from '../utils/modelChoices';
import {
  Block,
  ErrorText,
  Facts,
  Footnote,
  Input,
  Panel,
  SaveBar,
  Textarea,
} from './components';
import { commitYamlKeys } from './yamlCommit';
import { parseYamlMapping, readYamlPath } from './yamlRewrite';
import { reloadBeforeEditing, savedMessage } from '../utils/sentence';
import { errorMessage } from '../api/request';

const NODE_ID = 'qq.orchestrator';

/** 节点文件里写的 model；读不出来（文件坏了、没写）都当没写。 */
function modelOf(content: string): string {
  try {
    const value = readYamlPath(parseYamlMapping(content), ['model']);
    return value === undefined || value === null ? '' : String(value);
  } catch {
    return '';
  }
}

interface PromptFragment {
  filename: string;
  label: string;
  placeholder: string;
  rows: number;
  savedNote: string;
  fallback?: string;
}

const PERSONA: PromptFragment = {
  filename: '_persona.md',
  label: '人设',
  placeholder: '写 bot 的性格、说话方式、忌讳。这段会原样拼进提示词。',
  rows: 14,
  savedNote: '已保存，下一条消息就会用新的人设。',
  fallback: '_persona.example.md',
};

const PERSONA_TAIL: PromptFragment = {
  filename: '_persona-tail.md',
  label: '补充 System',
  placeholder: '填写追加到系统提示词末尾的内容，留空关闭。',
  rows: 8,
  savedNote: '已保存，下一条消息就会使用新的补充 System。留空时不注入。',
};

// 先查列表，避免尚未创建的可选片段在浏览器控制台留下 404。
async function readPromptFragment(token: string, filename: string): Promise<RawText | null> {
  const files = await getNodeFiles(token);
  if (!files.some((file) => file.name === filename && !file.is_example)) return null;
  try {
    return await getNodeFileRaw(token, filename);
  } catch (error) {
    // 查完列表到读之间被删掉了，一样按还没建算。
    if (isNotFound(error)) return null;
    throw error;
  }
}

const PromptFragment = ({ fragment }: { fragment: PromptFragment }) => {
  const token = useSettingsStore(state => state.adminToken);
  const [generation, setGeneration] = useState(0);
  return <PromptFragmentEditor fragment={fragment} key={`${token || ''}:${fragment.filename}:${generation}`} onReload={() => setGeneration((value) => value + 1)} />;
};

// 丢弃时重建编辑器，读回磁盘最新版，也隔离旧凭证下尚未返回的请求。
const PromptFragmentEditor = ({ fragment, onReload }: { fragment: PromptFragment; onReload: () => void }) => {
  const token = useSettingsStore((state) => state.adminToken);
  const [saved, setSaved] = useState('');
  const [text, setText] = useState('');
  // 保存时交回的指纹。文件还没建是空串：第一次保存要求它仍然不存在，被别处抢先建了就报冲突。
  const [base, setBase] = useState<string | null>(null);
  const [missing, setMissing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const { notice, error, setMessage, setError, setLoadError } = usePageNotice();
  const requests = useRequestGuard(fragment.filename);
  const confirmDiscard = useUnsavedChanges(text !== saved, `${fragment.label}有未保存修改，确定离开或丢弃吗？`);

  useEffect(() => {
    if (!token) return;
    const request = requests.begin('fragment');
    setLoaded(false);
    void (async () => {
      try {
        const file = await readPromptFragment(token, fragment.filename);
        if (!request.isCurrent()) return;
        setSaved(file?.content ?? '');
        setText(file?.content ?? '');
        setBase(file ? file.hash : '');
        setMissing(file === null);
        setLoadError('');
        setLoaded(true);
      } catch (caught) {
        if (!request.isCurrent()) return;
        setMissing(false);
        setLoaded(false);
        setLoadError(errorMessage(caught));
      }
    })();
  }, [requests, token, fragment.filename, setLoadError]);

  const save = async () => {
    if (!token || busy || !loaded) return;
    setBusy(true);
    try {
      const { result, receiptProblem } = await receipted(updateNodeFileRaw(token, fragment.filename, text, base));
      setSaved(text);
      // 回执没读懂就留着旧指纹：下次保存撞 409，不会把这次存上的整份盖掉。没改动时「丢弃」点不了，只能刷新页面重读。
      if (result) setBase(result.hash);
      setMissing(false);
      setMessage(savedMessage(fragment.savedNote, '', result?.warnings), result ? '' : reloadBeforeEditing(receiptProblem, '刷新页面读入最新内容'));
    } catch (caught) {
      setError(isEditConflict(caught)
        ? `${fragment.label}在打开之后被别处改过，没有保存：点「丢弃」读入最新内容，再把改动重做一遍。`
        : errorMessage(caught));
    }
    setBusy(false);
  };

  return (
    <>
      {missing && fragment.fallback && (
        <ErrorText>
          还没有 <code>{fragment.filename}</code>，bot 现在读的是 <code>{fragment.fallback}</code>。
          在这里保存一次就会创建它，示例从此不再生效。
        </ErrorText>
      )}
      <Textarea
        aria-label={`${fragment.label}内容`}
        disabled={busy || !loaded}
        onChange={(event) => setText(event.target.value)}
        placeholder={fragment.placeholder}
        rows={fragment.rows}
        value={text}
      />
      <SaveBar
        busy={busy}
        dirty={loaded && text !== saved}
        label={`保存${fragment.label}`}
        note={notice || (loaded || error ? '' : '正在读取…')}
        onReset={() => { if (confirmDiscard()) onReload(); }}
        onSave={() => void save()}
      />
      {error && <ErrorText role="alert">{error}</ErrorText>}
    </>
  );
};

const ModelChoice = () => {
  const token = useSettingsStore(state => state.adminToken);
  return <ModelChoiceEditor key={token || ""} />;
};

const ModelChoiceEditor = () => {
  const token = useSettingsStore((state) => state.adminToken);
  const modelListId = useId();
  const [raw, setRaw] = useState('');
  const [model, setModel] = useState('');
  const [providerModels, setProviderModels] = useState<string[]>([]);
  const [nodeModels, setNodeModels] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const { notice, error, setMessage, setError, setLoadError } = usePageNotice();

  const adopt = (body: string) => {
    setRaw(body);
    setModel(modelOf(body));
  };

  useEffect(() => {
    if (!token) return;
    void (async () => {
      try {
        adopt((await getNodeRaw(token, NODE_ID)).content);
        setLoadError('');
        setLoaded(true);
      } catch (caught) {
        setLoadError(errorMessage(caught));
      }
      // 候选表拉不到不影响改模型，所以各自吞掉失败。
      void getNodes(token)
        // 系统节点也算进来：它们写的多半是 $ENV{...}，正好把这种写法摆到候选里。
        .then((nodes) => setNodeModels(modelsFromNodes(nodes, NODE_ID)))
        .catch(() => undefined);
      void getProviders(token)
        .then((data) => setProviderModels(modelsFromProviders(data)))
        .catch(() => undefined);
    })();
  }, [token, setLoadError]);

  const dirty = model !== (raw ? modelOf(raw) : '');
  const confirmDiscard = useUnsavedChanges(dirty, '节点模型还有未保存修改，确定离开或丢弃吗？');

  const save = async () => {
    if (!token || !raw || busy) return;
    setBusy(true);
    try {
      const value = model.trim();
      // 只改 model 这一个键，合并到现读的这一版上：节点文件的其他改动留着。留空就删掉这一行，跟随全局默认。
      const { content, note: rewriteNote, result, receiptProblem } = await commitYamlKeys({
        read: () => getNodeRaw(token, NODE_ID),
        write: (next, hash) => updateNodeRaw(token, NODE_ID, next, hash),
        edits: [{ path: ['model'], value: value || undefined }],
      });
      adopt(content);
      const saved = rewriteNote
        ? `已保存。${rewriteNote}。节点文件每次任务都重新读，立刻生效。`
        : '已保存。节点文件每次任务都重新读，立刻生效。';
      setMessage(savedMessage(saved, '', result?.warnings), receiptProblem);
    } catch (caught) {
      setError(errorMessage(caught));
    }
    setBusy(false);
  };

  const modelChoices = mergeModelChoices(providerModels, nodeModels);

  return (
    <>
      <Input
        aria-label="综合入口模型"
        disabled={busy || !raw}
        list={modelChoices.length ? modelListId : undefined}
        onChange={(event) => setModel(event.target.value)}
        placeholder="留空 = 跟随全局默认模型"
        value={model}
        width="wide"
      />
      {modelChoices.length > 0 && (
        <datalist id={modelListId}>
          {modelChoices.map((name) => <option key={name} value={name} />)}
        </datalist>
      )}
      <Facts>
        任意模型 id 都能填；也可以写 <code>$ENV{'{VAR}'}</code>，从环境变量取。
      </Facts>

      <SaveBar busy={busy} dirty={dirty && Boolean(raw)} label="保存模型" note={notice || (loaded || error ? '' : '正在读取…')} onReset={() => { if (confirmDiscard()) adopt(raw); }} onSave={() => void save()} />
      {error && <ErrorText role="alert">{error}</ErrorText>}
    </>
  );
};

export const PersonaPage = () => (
  <>
    <Block hint="拼进提示词的那段人设，改完下一条消息就生效" title="人设">
      <Panel>
        <PromptFragment fragment={PERSONA} />
      </Panel>
    </Block>

    <Block hint="综合入口节点用哪个模型。能调哪些工具在设置的「节点」页改节点 YAML 的 tool_access" title="模型">
      <Panel>
        <p className="mb-1.5 text-xs text-[var(--duties-secondary)]">可以调用这些工具</p>
        <ModelChoice />
      </Panel>
    </Block>

    <Block hint="追加在系统提示词末尾，保持原生 System；留空关闭。仅对引用人格片段的入口生效。" title="补充 System">
      <Panel>
        <PromptFragment fragment={PERSONA_TAIL} />
      </Panel>
    </Block>

    <Footnote>
      各块各自保存，写入当前节点配置；加入共享组后，这些文件跟随组内共享配置。平台自己的发送处理在「平台」组里改。
    </Footnote>
  </>
);
