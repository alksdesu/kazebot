// 人格：bot 是谁、用哪个模型。两块各自就地保存，写的是节点文件，不经过任何平台配置。
import { useEffect, useId, useState } from 'react';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';

import {
  getNodeFileRaw,
  getNodeFiles,
  getNodeRaw,
  getNodes,
  getProviders,
  isNotFound,
  updateNodeFileRaw,
  updateNodeRaw,
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
import {
  NodeYamlShapeError,
  readYamlScalar,
  upsertYamlScalar,
  yamlString,
} from './nodeYaml';

const NODE_ID = 'qq.orchestrator';
const PERSONA_FILE = '_persona.md';

const say = (error: unknown): string => (error instanceof Error ? error.message : '出错了');

/** 读人设文件，还没建是 null。先查列表再读：直接去读不存在的文件，浏览器控制台会留一条 404 报错。 */
async function readPersona(token: string): Promise<string | null> {
  const files = await getNodeFiles(token);
  if (!files.some((file) => file.name === PERSONA_FILE && !file.is_example)) return null;
  try {
    return await getNodeFileRaw(token, PERSONA_FILE);
  } catch (error) {
    // 查完列表到读之间被删掉了，一样按还没建算。
    if (isNotFound(error)) return null;
    throw error;
  }
}

/** 一块独立保存的区域共用的底栏。草稿在这一块自己手里，不进顶部的「N 项待应用」。 */
const Persona = () => {
  const token = useSettingsStore(state => state.adminToken);
  return <PersonaEditor key={token || ""} />;
};

const PersonaEditor = () => {
  const token = useSettingsStore((state) => state.adminToken);
  const [saved, setSaved] = useState('');
  const [text, setText] = useState('');
  /** 文件不存在时 bot 读的是 _persona.example.md，第一次保存会把这层回退关掉。 */
  const [onlyExample, setOnlyExample] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [note, setNote] = useState('正在读取…');
  const confirmDiscard = useUnsavedChanges(text !== saved, '人设有未保存修改，确定离开或丢弃吗？');

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    setLoaded(false);
    void (async () => {
      try {
        const body = await readPersona(token);
        if (cancelled) return;
        setSaved(body ?? '');
        setText(body ?? '');
        setOnlyExample(body === null);
        setNote('');
        setLoaded(true);
      } catch (error) {
        if (cancelled) return;
        setOnlyExample(false);
        setLoaded(false);
        setNote(say(error));
      }
    })();
    return () => { cancelled = true; };
  }, [token]);

  const save = async () => {
    if (!token || busy || !loaded) return;
    setBusy(true);
    try {
      await updateNodeFileRaw(token, PERSONA_FILE, text);
      setSaved(text);
      setOnlyExample(false);
      setNote('已保存，下一条消息就会用新的人设。');
    } catch (error) {
      setNote(say(error));
    }
    setBusy(false);
  };

  return (
    <>
      {onlyExample && (
        <ErrorText>
          还没有 <code>_persona.md</code>，bot 现在读的是 <code>_persona.example.md</code>。
          在这里保存一次就会创建它，示例从此不再生效。
        </ErrorText>
      )}
      <Textarea
        aria-label="人设内容"
        disabled={busy || !loaded}
        onChange={(event) => setText(event.target.value)}
        placeholder="写 bot 的性格、说话方式、忌讳。这段会原样拼进提示词。"
        rows={14}
        value={text}
      />
      <SaveBar
        busy={busy}
        dirty={loaded && text !== saved}
        label="保存人设"
        note={note}
        onReset={() => { if (confirmDiscard()) setText(saved); }}
        onSave={() => void save()}
      />
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
  const [note, setNote] = useState('正在读取…');

  const adopt = (body: string) => {
    const currentModel = readYamlScalar(body, ['model']);
    setRaw(body);
    setModel(currentModel);
  };

  useEffect(() => {
    if (!token) return;
    void (async () => {
      try {
        adopt(await getNodeRaw(token, NODE_ID));
        setNote('');
      } catch (error) {
        setNote(say(error));
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
  }, [token]);

  const dirty = model !== (raw ? readYamlScalar(raw, ['model']) : '');
  const confirmDiscard = useUnsavedChanges(dirty, '节点模型还有未保存修改，确定离开或丢弃吗？');

  const save = async () => {
    if (!token || !raw || busy) return;
    setBusy(true);
    try {
      const value = model.trim();
      const next = upsertYamlScalar(raw, 'model', value && yamlString(value), 'type');
      await updateNodeRaw(token, NODE_ID, next);
      adopt(next);
      setNote('已保存。节点文件每次任务都重新读，立刻生效。');
    } catch (error) {
      setNote(error instanceof NodeYamlShapeError
        ? `${error.message}（没有写入，文件原样保留）`
        : say(error));
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

      <SaveBar busy={busy} dirty={dirty && Boolean(raw)} label="保存模型" note={note} onReset={() => { if (confirmDiscard()) adopt(raw); }} onSave={() => void save()} />
    </>
  );
};

export const PersonaPage = () => (
  <>
    <Block hint="拼进提示词的那段人设，改完下一条消息就生效" title="人设">
      <Panel>
        <Persona />
      </Panel>
    </Block>

    <Block hint="综合入口节点用哪个模型。能调哪些工具在设置的「节点」页改节点 YAML 的 tool_access" title="模型">
      <Panel>
        <p className="mb-1.5 text-xs text-[var(--duties-secondary)]">可以调用这些工具</p>
        <ModelChoice />
      </Panel>
    </Block>

    <Footnote>
      两块各自保存，写的是这个实例的节点文件。平台自己的发送处理在「平台」组里改。
    </Footnote>
  </>
);
