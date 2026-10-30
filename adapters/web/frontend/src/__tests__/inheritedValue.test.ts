// 会话最终用哪个模型、地址、渠道：顺序和 engine/model.py 的 resolve_provider 一致，
// 哪一层还没读到就停在那一层，不拿下一层的值冒充；读失败和还在读分得开。
import { beforeEach, describe, expect, it } from 'vitest';

import type { AdminNode, ProvidersResponse } from '../api/supervisorClient';
import {
  inheritanceSourcesOf,
  LOADED,
  nodeRowOf,
  sessionLayerOf,
  sessionNodeLabel,
  sessionNodeOf,
  sessionNodeRowOf,
  sessionOverrideOf,
  UNREAD,
  useSettingsStore,
} from '../store/settingsStore';
import { pickEntryNode, rootEntryNodes } from '../utils/entryNodes';
import { inheritedField, inheritedProvider, resolveInherited, type InheritanceSources } from '../utils/inheritedValue';

const block = (model: string, base_url = '') => ({
  model, base_url, model_raw: model, base_url_raw: base_url,
  api_key_present: false, api_key_redacted: '', supports_vision: null, type: '', type_explicit: false, label: '',
});

const sources = (over: Partial<InheritanceSources> = {}): InheritanceSources => ({
  session: { status: 'loaded', fields: null },
  node: { status: 'loaded', id: 'main', fields: null },
  channels: { status: 'loaded', table: {}, active: 'openai' },
  global: { status: 'loaded', model: 'global-model', baseUrl: 'https://global.example/v1' },
  ...over,
});

describe('继承链的顺序', () => {
  it('会话覆盖、节点、节点点名的渠道块、全局依次往下找', () => {
    expect(inheritedField(sources({ session: { status: 'loaded', fields: { model: 'session-model' } } }), 'model'))
      .toEqual({ known: true, source: 'session', value: 'session-model', origin: '' });
    expect(inheritedField(sources({ node: { status: 'loaded', id: 'main', fields: { model: 'node-model' } } }), 'model'))
      .toEqual({ known: true, source: 'node', value: 'node-model', origin: 'main' });
    expect(inheritedField(sources({
      node: { status: 'loaded', id: 'main', fields: { provider: 'relay' } },
      channels: { status: 'loaded', table: { relay: block('relay-model', 'https://relay.example/v1') }, active: 'openai' },
    }), 'base_url')).toEqual({ known: true, source: 'channel', value: 'https://relay.example/v1', origin: 'relay' });
    expect(inheritedField(sources(), 'model')).toEqual({ known: true, source: 'global', value: 'global-model', origin: '' });
    expect(inheritedField(sources({ global: { status: 'loaded', model: '', baseUrl: '' } }), 'model'))
      .toEqual({ known: true, source: 'default', value: '', origin: '' });
  });

  // engine 读节点时把 $ENV{} 展开成空串，和没写一样；会话覆盖原样送上游，那里的 $ENV{} 就是它本身。
  it('节点写的 $ENV{} 当没写，往下一层找；会话覆盖里的照原样算', () => {
    expect(inheritedField(sources({ node: { status: 'loaded', id: 'main', fields: { model: '$ENV{}' } } }), 'model'))
      .toEqual({ known: true, source: 'global', value: 'global-model', origin: '' });
    expect(inheritedField(sources({ node: { status: 'loaded', id: 'main', fields: { model: '$ENV{MAIN_MODEL}' } } }), 'model'))
      .toEqual({ known: true, source: 'node', value: '$ENV{MAIN_MODEL}', origin: 'main' });
    expect(inheritedField(sources({ session: { status: 'loaded', fields: { model: '$ENV{}' } } }), 'model'))
      .toEqual({ known: true, source: 'session', value: '$ENV{}', origin: '' });
  });

  it('节点自己写了模型就轮不到它点名的渠道块', () => {
    const resolved = inheritedField(sources({
      node: { status: 'loaded', id: 'main', fields: { model: 'node-model', provider: 'relay' } },
      channels: { status: 'loaded', table: { relay: block('relay-model') }, active: 'openai' },
    }), 'model');

    expect(resolved).toMatchObject({ known: true, source: 'node', value: 'node-model' });
  });

  it('会话点名的渠道比节点点名的优先，旧写法 provider_type 也认', () => {
    const resolved = inheritedField(sources({
      session: { status: 'loaded', fields: { provider_type: 'relay' } },
      node: { status: 'loaded', id: 'main', fields: { provider: 'other' } },
      channels: { status: 'loaded', table: { relay: block('relay-model'), other: block('other-model') }, active: 'openai' },
    }), 'model');

    expect(resolved).toMatchObject({ known: true, source: 'channel', value: 'relay-model', origin: 'relay' });
  });

  it('名字底下没有渠道块时按线格式名处理，这一层什么也不给', () => {
    const resolved = inheritedField(sources({
      node: { status: 'loaded', id: 'main', fields: { provider: 'anthropic' } },
      channels: { status: 'loaded', table: { relay: block('relay-model') }, active: 'openai' },
    }), 'model');

    expect(resolved).toMatchObject({ known: true, source: 'global', value: 'global-model' });
  });

  it('不看会话自己填的这一项时，会话点名的渠道照样算数', () => {
    const resolved = inheritedField(sources({
      session: { status: 'loaded', fields: { model: 'session-model', provider: 'relay' } },
      channels: { status: 'loaded', table: { relay: block('relay-model') }, active: 'openai' },
    }), 'model', false);

    expect(resolved).toMatchObject({ known: true, source: 'channel', value: 'relay-model' });
  });

  it('不是字符串的字段当没写', () => {
    const resolved = inheritedField(sources({ node: { status: 'loaded', id: 'main', fields: { model: 42 } } }), 'model');

    expect(resolved).toMatchObject({ known: true, source: 'global' });
  });
});

describe('哪一层没读到就停在哪一层', () => {
  it('会话覆盖还在读时不拿节点的模型顶上', () => {
    const resolved = inheritedField(sources({
      session: { status: 'loading', fields: null },
      node: { status: 'loaded', id: 'main', fields: { model: 'node-model' } },
    }), 'model');

    expect(resolved).toEqual({ known: false, source: 'session', status: 'loading', origin: '' });
  });

  it('读失败和还在读分得开', () => {
    expect(inheritedField(sources({ session: { status: 'failed', fields: null } }), 'model'))
      .toMatchObject({ known: false, source: 'session', status: 'failed' });
    expect(inheritedField(sources({ node: { status: 'failed', id: 'main', fields: null } }), 'model'))
      .toMatchObject({ known: false, source: 'node', status: 'failed', origin: 'main' });
  });

  it('渠道表没读到时，点名了渠道才停在渠道那一层', () => {
    const failedTable = { status: 'failed' as const, table: null, active: '' };

    expect(inheritedField(sources({
      node: { status: 'loaded', id: 'main', fields: { provider: 'relay' } },
      channels: failedTable,
    }), 'model')).toEqual({ known: false, source: 'channel', status: 'failed', origin: 'relay' });
    expect(inheritedField(sources({ channels: failedTable }), 'model'))
      .toMatchObject({ known: true, source: 'global', value: 'global-model' });
  });

  it('会话覆盖没读到时，渠道那一层也不知道该找哪个渠道', () => {
    const resolved = inheritedField(sources({ session: { status: 'loading', fields: null } }), 'model', false);

    expect(resolved).toMatchObject({ known: false, source: 'channel', status: 'loading' });
  });

  it('全局配置没读到时停在全局那一层', () => {
    expect(inheritedField(sources({ global: { status: 'failed', model: '', baseUrl: '' } }), 'model'))
      .toMatchObject({ known: false, source: 'global', status: 'failed' });
  });

  it('节点表里没有会话的节点时停在节点那一层说找不到，渠道块和全局都不看', () => {
    const missing = sources({
      node: { status: 'missing', id: 'gone', fields: null },
      channels: { status: 'loaded', table: { relay: block('relay-model') }, active: 'openai' },
    });

    expect(inheritedField(missing, 'model')).toEqual({ known: false, source: 'node', status: 'missing', origin: 'gone' });
    expect(inheritedField(missing, 'base_url', false)).toMatchObject({ known: false, source: 'node', status: 'missing' });
    expect(inheritedProvider(missing, false)).toMatchObject({ known: false, source: 'node', status: 'missing' });
    // 会话自己写了的还是它：会话覆盖在节点上面
    expect(inheritedField({ ...missing, session: { status: 'loaded', fields: { model: 'session-model' } } }, 'model'))
      .toMatchObject({ known: true, source: 'session', value: 'session-model' });
  });

  it('resolveInherited 一层都没有时就是默认', () => {
    expect(resolveInherited([])).toEqual({ known: true, source: 'default', value: '', origin: '' });
  });
});

describe('走哪个渠道', () => {
  it('会话点名的、节点点名的，都没点名就是在用的那个', () => {
    expect(inheritedProvider(sources({ session: { status: 'loaded', fields: { provider: 'relay' } } })))
      .toMatchObject({ known: true, source: 'session', value: 'relay' });
    expect(inheritedProvider(sources({ node: { status: 'loaded', id: 'main', fields: { provider: 'other' } } })))
      .toMatchObject({ known: true, source: 'node', value: 'other' });
    expect(inheritedProvider(sources())).toMatchObject({ known: true, source: 'global', value: 'openai' });
  });

  it('谁都没点名而渠道表没读到时说不出在用哪个', () => {
    expect(inheritedProvider(sources({ channels: { status: 'failed', table: null, active: '' } })))
      .toMatchObject({ known: false, source: 'global', status: 'failed' });
  });
});

describe('根入口', () => {
  const ai = (id: string, extra: Partial<AdminNode> = {}): AdminNode => ({ id, type: 'ai', ...extra });

  it('只有能当入口的节点委派出去的才算子节点', () => {
    const nodes = [
      ai('main'),
      ai('worker'),
      ai('reviewer'),
      ai('_template', { active: false, delegate_targets: ['worker'] }),
      ai('chat.intent', { internal: true, delegate_targets: ['reviewer'] }),
    ];

    expect(rootEntryNodes(nodes).map((node) => node.id)).toEqual(['main', 'worker', 'reviewer']);
  });

  it('入口节点委派下去的子节点不算根，委派给自己不算', () => {
    const nodes = [
      ai('bootstrap.cmd_reviewer'),
      ai('bootstrap.shell_orchestrator', { delegate_targets: ['bootstrap.cmd_reviewer'] }),
      ai('bootstrap.coder', { delegate_targets: ['bootstrap.coder'] }),
    ];

    expect(rootEntryNodes(nodes).map((node) => node.id)).toEqual(['bootstrap.shell_orchestrator', 'bootstrap.coder']);
  });

  it('一个根都没有时把入口节点全列出来', () => {
    const nodes = [ai('a', { delegate_targets: ['b'] }), ai('b', { delegate_targets: ['a'] }), ai('system.compactor')];

    expect(rootEntryNodes(nodes).map((node) => node.id)).toEqual(['a', 'b']);
  });
});

describe('替用户挑入口', () => {
  const roots = [{ id: 'bootstrap.coder' }, { id: 'bootstrap.shell_orchestrator' }];

  it('后端配的默认入口在根入口里就挑它，不在就挑第一个根入口', () => {
    expect(pickEntryNode(roots, 'bootstrap.shell_orchestrator')).toBe('bootstrap.shell_orchestrator');
    expect(pickEntryNode(roots, 'bootstrap.cmd_reviewer')).toBe('bootstrap.coder');
    expect(pickEntryNode(roots, '')).toBe('bootstrap.coder');
  });

  it('一个根入口都没有时挑不出来', () => {
    expect(pickEntryNode([], 'bootstrap.shell_orchestrator')).toBe('');
  });
});

describe('settings store 里的共用读取状态', () => {
  beforeEach(() => {
    useSettingsStore.getState().clearLoginData();
    useSettingsStore.setState({ adminToken: 'token', entryNodeId: '' });
  });

  it('读成过以后再读不成不记失败', () => {
    const store = useSettingsStore.getState();
    store.setNodeList([{ id: 'main', type: 'ai' }]);
    store.failNodeList('后读的那次没读成');
    store.setProviderTable({ active_provider: 'openai', providers: {} } as unknown as ProvidersResponse);
    store.failProviderTable('后读的那次没读成');
    store.setGlobalConfig('m', '');
    store.failGlobalConfig('后读的那次没读成');

    const state = useSettingsStore.getState();
    expect(state.nodeListRead).toBe(LOADED);
    expect(state.providerTableRead).toBe(LOADED);
    expect(state.globalConfigRead).toBe(LOADED);
  });

  it('还没读成时记下原因，同一个原因不换对象', () => {
    useSettingsStore.getState().failNodeList('boom');
    const first = useSettingsStore.getState().nodeListRead;
    expect(first).toEqual({ loaded: false, error: 'boom' });

    useSettingsStore.getState().failNodeList('boom');
    expect(useSettingsStore.getState().nodeListRead).toBe(first);

    useSettingsStore.getState().failNodeList('another');
    expect(useSettingsStore.getState().nodeListRead).toEqual({ loaded: false, error: 'another' });
  });

  it('节点表一份给下拉只列入口，一份留着查模型', () => {
    const nodes: AdminNode[] = [
      { id: 'main', type: 'ai' },
      { id: '_template', type: 'ai', active: false },
      { id: 'chat.intent', type: 'ai', internal: true },
      { id: 'system.compactor', type: 'ai' },
    ];
    useSettingsStore.getState().setNodeList(nodes);

    const state = useSettingsStore.getState();
    expect(state.availableNodes.map((node) => node.id)).toEqual(['main']);
    expect(state.allNodes).toBe(nodes);
    expect(state.nodeListRead).toBe(LOADED);
  });

  it('退出登录时读取状态一起回到没读过', () => {
    const store = useSettingsStore.getState();
    store.setNodeList([{ id: 'main', type: 'ai' }]);
    store.failProviderTable('boom');
    store.setGlobalConfig('m', 'u', 'main');
    store.clearLoginData();

    const state = useSettingsStore.getState();
    expect(state.nodeListRead).toBe(UNREAD);
    expect(state.providerTableRead).toBe(UNREAD);
    expect(state.globalConfigRead).toBe(UNREAD);
    expect(state.allNodes).toEqual([]);
    expect(state.providerTable).toBeNull();
    expect(state.defaultNodeId).toBe('');
  });

  it('全局配置带了默认入口就记下，没带就不动', () => {
    useSettingsStore.getState().setGlobalConfig('m', '', 'bootstrap.shell_orchestrator');
    expect(useSettingsStore.getState().defaultNodeId).toBe('bootstrap.shell_orchestrator');

    useSettingsStore.getState().setGlobalConfig('m2', '');
    expect(useSettingsStore.getState()).toMatchObject({ globalModel: 'm2', defaultNodeId: 'bootstrap.shell_orchestrator' });
  });

  it('当前节点每写一次序号加一，退出登录也算一次', () => {
    const before = useSettingsStore.getState().activeNodeWrites;

    useSettingsStore.getState().setActiveNode('main', false, 'main', 'session-a');
    useSettingsStore.getState().setActiveNode('main', false, 'main', 'session-a');
    expect(useSettingsStore.getState().activeNodeWrites).toBe(before + 2);

    useSettingsStore.getState().clearLoginData();
    expect(useSettingsStore.getState().activeNodeWrites).toBe(before + 3);
  });
});

describe('某个会话的节点和覆盖', () => {
  beforeEach(() => {
    useSettingsStore.getState().clearLoginData();
    useSettingsStore.setState({ adminToken: 'token', entryNodeId: 'entry' });
  });

  it('有会话时只认读到的那份，没读到不拿入口节点顶上', () => {
    useSettingsStore.getState().setActiveNode('other', true, 'default', 'session-a');
    const state = useSettingsStore.getState();

    expect(sessionNodeOf(state, 'session-b')).toEqual({ known: false, nodeId: '', isOverride: false });
    expect(sessionNodeOf(state, 'session-a')).toEqual({ known: true, nodeId: 'other', isOverride: true });
  });

  it('新对话依次是刚选的、入口节点、后端默认入口', () => {
    useSettingsStore.getState().setActiveNode('picked', true, 'default', '');
    expect(sessionNodeOf(useSettingsStore.getState(), '')).toEqual({ known: true, nodeId: 'picked', isOverride: true });

    useSettingsStore.getState().setActiveNode('elsewhere', true, 'default', 'session-a');
    expect(sessionNodeOf(useSettingsStore.getState(), '')).toEqual({ known: true, nodeId: 'entry', isOverride: false });

    useSettingsStore.setState({ entryNodeId: '' });
    expect(sessionNodeOf(useSettingsStore.getState(), '')).toMatchObject({ known: true, nodeId: 'default' });
  });

  it('有会话时 store 里存的不是它的覆盖就是还没读到', () => {
    useSettingsStore.getState().setSessionProviderOverride({ model: 'm' }, 'session-a');
    const state = useSettingsStore.getState();

    expect(sessionOverrideOf(state, 'session-b')).toEqual({ known: false, override: null });
    expect(sessionOverrideOf(state, 'session-a')).toEqual({ known: true, override: { model: 'm' } });
    expect(sessionOverrideOf(state, '')).toEqual({ known: true, override: null });
    expect(sessionLayerOf(state, 'session-b', '')).toEqual({ status: 'loading', fields: null });
    expect(sessionLayerOf(state, 'session-b', 'boom')).toEqual({ status: 'failed', fields: null });
  });

  it('会话的节点读到了但节点表没读到时停在节点那一层', () => {
    useSettingsStore.getState().setActiveNode('main', false, 'main', 'session-a');
    useSettingsStore.getState().failNodeList('boom');
    const state = useSettingsStore.getState();
    const resolved = inheritedField(inheritanceSourcesOf(state, 'session-a', { status: 'loaded', fields: null }, ''), 'model');

    expect(resolved).toEqual({ known: false, source: 'node', status: 'failed', origin: 'main' });
  });

  it('会话的节点没读到时，调用方给的原因决定是读失败还是还在读', () => {
    const state = useSettingsStore.getState();
    const session = { status: 'loaded' as const, fields: null };

    expect(inheritanceSourcesOf(state, 'session-x', session, '').node).toEqual({ status: 'loading', id: '', fields: null });
    expect(inheritanceSourcesOf(state, 'session-x', session, 'boom').node).toEqual({ status: 'failed', id: '', fields: null });
  });

  it('节点表里查得到会话的节点时用它那一行', () => {
    useSettingsStore.getState().setNodeList([{ id: 'worker', type: 'ai', model: 'worker-model' }]);
    useSettingsStore.getState().setActiveNode('worker', true, 'main', 'session-a');
    const state = useSettingsStore.getState();
    const resolved = inheritedField(inheritanceSourcesOf(state, 'session-a', { status: 'loaded', fields: null }, ''), 'model');

    expect(resolved).toEqual({ known: true, source: 'node', value: 'worker-model', origin: 'worker' });
  });

  it('会话记着节点的旧 id 时认出 aliases 里有它的那一行，先认文件名，yaml 里写的 id 不算数', () => {
    useSettingsStore.getState().setNodeList([
      { id: 'renamed', type: 'ai', name: 'Renamed', aliases: ['old.id'], model: 'renamed-model' },
      { id: 'shadow', type: 'ai', aliases: ['worker'], model: 'shadow-model' },
      { id: 'worker', type: 'ai', model: 'worker-model' },
      { id: 'decorated', type: 'ai', declared_id: 'yaml.only', model: 'decorated-model' },
    ]);
    useSettingsStore.getState().setActiveNode('old.id', true, 'main', 'session-a');
    const state = useSettingsStore.getState();
    const node = sessionNodeRowOf(state, 'session-a');

    expect(node).toMatchObject({ known: true, nodeId: 'old.id', missing: false });
    expect(node.row?.id).toBe('renamed');
    expect(sessionNodeLabel(node)).toBe('Renamed');
    expect(inheritedField(inheritanceSourcesOf(state, 'session-a', { status: 'loaded', fields: null }, ''), 'model'))
      .toEqual({ known: true, source: 'node', value: 'renamed-model', origin: 'renamed' });
    expect(nodeRowOf(state, 'worker')?.model).toBe('worker-model');
    // engine 不按 yaml 里写的 id 找节点
    expect(nodeRowOf(state, 'yaml.only')).toBeNull();
    useSettingsStore.getState().setActiveNode('yaml.only', true, 'main', 'session-a');
    expect(sessionNodeRowOf(useSettingsStore.getState(), 'session-a')).toMatchObject({ row: null, missing: true });
  });

  it('节点表读成了却认不出会话的节点：说找不到，不往下一层找，也不算读失败', () => {
    useSettingsStore.getState().setNodeList([{ id: 'main', type: 'ai', model: 'main-model' }]);
    useSettingsStore.getState().setGlobalConfig('global-model', '', 'main');
    useSettingsStore.getState().setActiveNode('gone', true, 'main', 'session-a');
    const state = useSettingsStore.getState();
    const node = sessionNodeRowOf(state, 'session-a');
    const chain = inheritanceSourcesOf(state, 'session-a', { status: 'loaded', fields: null }, '');

    expect(node).toMatchObject({ known: true, nodeId: 'gone', row: null, missing: true });
    expect(sessionNodeLabel(node)).toBe('gone（找不到这个节点）');
    expect(chain.node).toEqual({ status: 'missing', id: 'gone', fields: null });
    expect(inheritedField(chain, 'model')).toEqual({ known: false, source: 'node', status: 'missing', origin: 'gone' });
  });

  it('节点表还没读成时认不出不算找不到', () => {
    useSettingsStore.getState().setActiveNode('gone', true, 'main', 'session-a');
    useSettingsStore.getState().failNodeList('boom');
    const node = sessionNodeRowOf(useSettingsStore.getState(), 'session-a');

    expect(node.missing).toBe(false);
    expect(sessionNodeLabel(node)).toBe('gone');
  });
});
