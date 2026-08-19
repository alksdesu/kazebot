// 人格与能力。这一页有两套保存路径：人设和节点配置各自就地存盘，
// 下半页的开关走 qq.yaml，由顶部那个「应用」统一提交。
import { useEffect, useId, useState } from 'react';

import {
  getAllToolNames,
  getNodeFileRaw,
  getNodeRaw,
  getNodes,
  getProviders,
  updateNodeFileRaw,
  updateNodeRaw,
} from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import { mergeModelChoices, modelsFromNodes, modelsFromProviders } from '../utils/modelChoices';
import { Block, Footnote, Grid, Option, SaveBar } from './components';
import { BoolOption, NumberField, SubOption } from './fields';
import { WordList } from './WordList';
import {
  NodeYamlShapeError,
  readYamlList,
  readYamlScalar,
  replaceYamlList,
  upsertYamlScalar,
} from './nodeYaml';

const NODE_ID = 'qq.orchestrator';
const PERSONA_FILE = '_persona.md';

const say = (error: unknown): string => (error instanceof Error ? error.message : '出错了');

/** 一块独立存盘的区域共用的底栏。草稿在这一块自己手里，不进顶部的「N 项待应用」。 */
const Persona = () => {
  const token = useSettingsStore((state) => state.adminToken);
  const [saved, setSaved] = useState('');
  const [text, setText] = useState('');
  /** 文件不存在时 bot 读的是 _persona.example.md，第一次保存会把这层回退关掉。 */
  const [onlyExample, setOnlyExample] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('正在读取…');

  useEffect(() => {
    if (!token) return;
    void (async () => {
      try {
        const body = await getNodeFileRaw(token, PERSONA_FILE);
        setSaved(body);
        setText(body);
        setOnlyExample(false);
        setNote('');
      } catch {
        // 读不到就是还没建过这个文件，example 正在顶班。
        setOnlyExample(true);
        setNote('');
      }
    })();
  }, [token]);

  const save = async () => {
    if (!token) return;
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
        <p className="qc-facts qc-halt-text">
          还没有 <code>_persona.md</code>，bot 现在读的是 <code>_persona.example.md</code>。
          在这里保存一次就会创建它，示例从此不再生效。
        </p>
      )}
      <textarea
        className="qc-area"
        onChange={(event) => setText(event.target.value)}
        placeholder="写 bot 的性格、说话方式、忌讳。这段会原样拼进提示词。"
        rows={14}
        value={text}
      />
      <SaveBar
        busy={busy}
        dirty={text !== saved}
        note={note}
        onReset={() => setText(saved)}
        onSave={() => void save()}
      />
    </>
  );
};

const Capabilities = () => {
  const token = useSettingsStore((state) => state.adminToken);
  const modelListId = useId();
  const [raw, setRaw] = useState('');
  const [tools, setTools] = useState<string[]>([]);
  const [targets, setTargets] = useState<string[]>([]);
  const [allTools, setAllTools] = useState<string[]>([]);
  const [allNodes, setAllNodes] = useState<string[]>([]);
  // 配过的名字只增不减。插件注册的工具（stocktool_* 之类）不在 all-tool-names 里，
  // 取消勾选后要是从候选表里消失，就再也勾不回来了。
  const [known, setKnown] = useState<{ tools: string[]; targets: string[] }>({ tools: [], targets: [] });
  const [model, setModel] = useState('');
  const [mode, setMode] = useState('');
  const [providerModels, setProviderModels] = useState<string[]>([]);
  const [nodeModels, setNodeModels] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('正在读取…');

  const adopt = (body: string) => {
    const allow = readYamlList(body, ['tool_access', 'allow']);
    const delegates = readYamlList(body, ['delegate_targets']);
    setRaw(body);
    setTools(allow);
    setTargets(delegates);
    setModel(readYamlScalar(body, ['model']));
    setMode(readYamlScalar(body, ['tool_access', 'mode']));
    setKnown((prev) => ({
      tools: [...new Set([...prev.tools, ...allow])],
      targets: [...new Set([...prev.targets, ...delegates])],
    }));
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
      // 候选表拉不到不影响改已有的项，所以各自吞掉失败。
      void getAllToolNames(token).then(setAllTools).catch(() => undefined);
      void getNodes(token)
        .then((nodes) => {
          setAllNodes(
            nodes.filter((n: any) => n.id !== NODE_ID && !String(n.id).startsWith('system.'))
              .map((n: any) => String(n.id)),
          );
          // 系统节点也算进来：它们写的多半是 $ENV{...}，正好把这种写法摆到候选里。
          setNodeModels(modelsFromNodes(nodes, NODE_ID));
        })
        .catch(() => undefined);
      void getProviders(token)
        .then((data) => setProviderModels(modelsFromProviders(data)))
        .catch(() => undefined);
    })();
  }, [token]);

  const current = raw
    ? {
      tools: readYamlList(raw, ['tool_access', 'allow']),
      targets: readYamlList(raw, ['delegate_targets']),
      model: readYamlScalar(raw, ['model']),
    }
    : { tools: [], targets: [], model: '' };
  const dirty = JSON.stringify([tools, targets, model])
    !== JSON.stringify([current.tools, current.targets, current.model]);

  const save = async () => {
    if (!token) return;
    setBusy(true);
    try {
      let next = replaceYamlList(raw, ['tool_access', 'allow'], tools);
      next = replaceYamlList(next, ['delegate_targets'], targets);
      next = upsertYamlScalar(next, 'model', model.trim(), 'type');
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

  const toggle = (list: string[], set: (next: string[]) => void, name: string) => {
    set(list.includes(name) ? list.filter((item) => item !== name) : [...list, name]);
  };

  // 候选表拉不到时至少要能看到已配的项，否则界面会显得像被清空了。
  const toolChoices = [...new Set([...known.tools, ...tools, ...allTools])].sort();
  const nodeChoices = [...new Set([...known.targets, ...targets, ...allNodes])].sort();
  const modelChoices = mergeModelChoices(providerModels, nodeModels);

  return (
    <>
      {/* allow 只在 allowlist 模式下被读，别的模式勾了也不生效，界面上看不出来。 */}
      {mode && mode !== 'allowlist' && (
        <p className="qc-mismatch">
          这个节点的 tool_access.mode 是 {mode}，下面的勾选不生效
          {mode === 'all'
            ? '：all 模式读的是 deny 列表，所有工具都放行。'
            : '：none 模式一个工具都不给。'}
          改模式请到设置的「工具与权限」页。
        </p>
      )}
      <div className="qc-checks">
        {toolChoices.length === 0 && <span className="qc-chip-empty">没有可选工具</span>}
        {toolChoices.map((name) => (
          <label className="qc-check" key={name}>
            <input checked={tools.includes(name)} onChange={() => toggle(tools, setTools, name)} type="checkbox" />
            <code>{name}</code>
          </label>
        ))}
      </div>

      <p className="qc-words-label">可以委派给这些节点</p>
      <div className="qc-checks">
        {nodeChoices.length === 0 && <span className="qc-chip-empty">没有其他节点</span>}
        {nodeChoices.map((id) => (
          <label className="qc-check" key={id}>
            <input checked={targets.includes(id)} onChange={() => toggle(targets, setTargets, id)} type="checkbox" />
            <code>{id}</code>
          </label>
        ))}
      </div>

      <p className="qc-words-label">模型</p>
      <input
        className="qc-inp qc-inp-wide"
        list={modelChoices.length ? modelListId : undefined}
        onChange={(event) => setModel(event.target.value)}
        placeholder="留空 = 跟随全局默认模型"
        value={model}
      />
      {modelChoices.length > 0 && (
        <datalist id={modelListId}>
          {modelChoices.map((name) => <option key={name} value={name} />)}
        </datalist>
      )}
      <p className="qc-facts">
        任意模型 id 都能填；也可以写 <code>$ENV{'{VAR}'}</code>，从环境变量取。
      </p>

      <SaveBar busy={busy} dirty={dirty} note={note} onReset={() => adopt(raw)} onSave={() => void save()} />
    </>
  );
};

export const PersonaPage = () => (
  <>
    <Block hint="拼进提示词的那段人设，改完下一条消息就生效" title="人设">
      <div className="qc-panel">
        <Persona />
      </div>
    </Block>

    <Block hint="综合入口节点能调哪些工具、能把活派给谁" title="能力">
      <div className="qc-panel">
        <p className="qc-words-label">可以调用这些工具</p>
        <Capabilities />
      </div>
    </Block>

    <Block hint="消息发出去之前的处理" title="回复格式">
      <Grid>
        <BoolOption
          configKey="reply_to_trigger"
          desc="回复时引用触发它的那条消息，群里热闹时能看清在回谁。"
          fallback
          label="引用原消息"
        />
        <BoolOption
          configKey="strip_asterisk_styles"
          desc="QQ 不渲染 Markdown，*强调* 会原样显示成星号。"
          fallback
          label="去掉星号强调"
        />
        <BoolOption
          configKey="strip_underscore_styles"
          desc="下划线同理。但代码和文件名里常有下划线，默认不动。"
          label="去掉下划线强调"
        />
        <Option checked disabled name="长度上限" onChange={() => undefined}>
          <p className="qc-opt-desc">超长的回复会被截断，避免被 QQ 拒收。</p>
          <NumberField configKey="message_limit" label="单条消息" step={100} unit="字" />
          <NumberField configKey="history_text_limit" label="历史里每条" step={50} unit="字" />
        </Option>
        <BoolOption
          configKey="enable_image_forward_merge"
          desc="多张图合并成一条转发发出，而不是一张张发。"
          label="合并发图"
        >
          <NumberField configKey="image_forward_merge_threshold" label="至少" unit="张才合并" />
          <p className="qc-opt-desc">
            默认关是有原因的：合并转发会长时间占住那条唯一的反向 WS，期间发消息、贴表情全部排队。发现卡顿就关掉，即时生效。
          </p>
        </BoolOption>
      </Grid>
    </Block>

    <Block hint="除了说话之外还能做什么" title="扩展能力">
      <Grid>
        <BoolOption
          configKey="enable_reactions"
          desc="给群消息贴表情回应。"
          fallback
          label="表情回应"
        />
        <BoolOption configKey="enable_auto_like" desc="给群友的资料卡点赞。" label="自动点赞">
          <NumberField configKey="auto_like_times" label="每人每天" unit="次" />
        </BoolOption>
        <BoolOption
          configKey="enable_preempt"
          desc="上一条还没答完就来了新消息时，放弃旧的去答新的。"
          label="抢答"
        />
        <Option checked disabled name="表情提示" onChange={() => undefined}>
          <p className="qc-opt-desc">告诉模型有哪些收藏表情可用。0 表示不告诉它。</p>
          <NumberField configKey="face_prompt_limit" label="最多列出" unit="个" />
        </Option>
      </Grid>
    </Block>

    <Block hint="群友发来的东西里，哪些交给模型看" title="输入能力">
      <Grid>
        <BoolOption configKey="enable_image_input" desc="把图片交给视觉节点识别。" fallback label="看图片">
          <NumberField configKey="max_images_per_turn" label="每轮最多" unit="张" />
          <NumberField configKey="image_max_bytes" label="单图上限" scale={1024 * 1024} step={1} unit="MB" />
          <NumberField configKey="image_wait_after_text_sec" label="等图窗口" step={0.5} unit="秒" />
          <p className="qc-opt-desc">QQ 常把「文字＋图」拆成两条发过来，收到纯文本后等这么久看有没有图跟上。</p>
        </BoolOption>
        <BoolOption configKey="enable_file_input" desc="读取群文件的内容。" fallback label="读文件">
          <NumberField configKey="max_files_per_turn" label="每轮最多" unit="个" />
          <NumberField configKey="file_max_bytes" label="单文件上限" scale={1024 * 1024} step={1} unit="MB" />
          <WordList
            configKey="file_extra_allowed_extensions"
            empty="只收内置白名单里的类型"
            label="额外放行"
            placeholder="输入一个扩展名，回车添加"
          />
          <p className="qc-opt-desc">可执行文件与脚本类型不受这里影响，填了也不会收。</p>
        </BoolOption>
        <BoolOption
          configKey="enable_forward_msg_input"
          desc="展开合并转发的聊天记录。"
          fallback
          label="读合并转发"
        >
          <NumberField configKey="forward_msg_max_depth" label="嵌套深度" unit="层" />
          <NumberField configKey="forward_msg_max_messages" label="最多" step={10} unit="条" />
          <NumberField configKey="forward_msg_text_limit" label="总字数" step={1000} unit="字" />
          <NumberField configKey="forward_msg_timeout_sec" label="展开超时" step={0.5} unit="秒" />
          <SubOption configKey="enable_forward_msg_media" fallback label="里面的图片和文件也收" />
          <SubOption configKey="forward_msg_expand_for_trigger" fallback label="群里靠关键词也能触发" />
          <p className="qc-opt-desc">后一项关掉，转发消息就只有 @ 或回复才理。</p>
        </BoolOption>
      </Grid>
    </Block>

    <Footnote>
      上面两块各自存盘，和顶部的「应用」无关 —— 它们写的是节点文件，不是 qq.yaml。
      下面三块才走 qq.yaml，改完要点顶部的「应用」。
    </Footnote>
  </>
);
