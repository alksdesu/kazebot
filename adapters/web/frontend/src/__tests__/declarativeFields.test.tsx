// 声明驱动：字段按后端 schema 渲染、复杂件按分区交给页面、草稿与 API 按配置作用域分家。
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';

import * as api from '../api/supervisorClient';
import type { ConfigLiveState, ConsoleFieldSchema, ConsoleNode } from '../api/supervisorClient';
import { ConfigScopeProvider, FEATURES_SCOPE, platformScope } from '../console/configScope';
import { configStore, platformConsoleStore } from '../console/consoleStore';
import { SchemaFields } from '../console/FieldRenderer';
import { PlatformProvider } from '../console/platformContext';
import { textRuns } from '../console/RichText';
import { useSchemaStore } from '../console/schemaStore';
import { PartsProvider, type PageParts } from '../console/sectionParts';
import { QQ_NAPCAT, type PlatformDescriptor } from '../platforms/registry';
import { toDescriptor } from '../platforms/usePlatformRegistry';
import { clearConsoleSchemas, FEATURES_SCHEMA, QQ_SCHEMA } from './consoleSchemaFixture';

vi.mock('../api/supervisorClient', async importOriginal => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getPlatformRaw: vi.fn(), getPlatformState: vi.fn(), updatePlatformRaw: vi.fn(), getPlatformSchema: vi.fn(),
  getFeaturesRaw: vi.fn(), getFeaturesState: vi.fn(), updateFeaturesRaw: vi.fn(), getFeaturesSchema: vi.fn(),
}));

const TELEGRAM: PlatformDescriptor = {
  id: 'telegram',
  displayName: 'Telegram',
  shortName: 'TG',
  channels: [{ name: 'tg_group', kind: 'group', hashed: true }],
  hasConfig: true,
};

const live = (values: Record<string, unknown>, paths: Record<string, string>): ConfigLiveState => ({
  published: true,
  applied: true,
  file: { exists: true, mtime_ns: 1, size: 1 },
  state: { values, paths },
});

const field = (overrides: Partial<ConsoleFieldSchema> & Pick<ConsoleFieldSchema, 'kind' | 'label'>): ConsoleFieldSchema => ({
  path: 'demo.key',
  layers: ['platform'],
  default: null,
  ...overrides,
});

const FIELDS: Record<string, ConsoleFieldSchema> = {
  enable_echo: field({ kind: 'bool', label: '跟复读', desc: '连着几个人刷同一句时，也跟一条。', default: false }),
  echo_threshold: field({ kind: 'number', label: '连续', unit: '条才跟', default: 3 }),
  image_max_bytes: field({ kind: 'number', label: '单图上限', unit: 'MB', step: 1, scale: 1048576, default: 10485760 }),
  name_anywhere: field({ kind: 'bool', label: '不限句首，句中提到也算', default: true }),
  signal_prefix: field({
    kind: 'tristate', label: '前缀触发', desc: '以配置的前缀开头。', follows: 'group_trigger', follows_when: ['prefix'],
  }),
  signal_at: field({ kind: 'tristate', label: '被 @', desc: '消息里任何位置 @ 到 bot。', unset: true }),
  sticker_strategy: field({ kind: 'choice', label: '收多严', choices: [['strict', '严格'], ['loose', '宽松']], default: 'loose' }),
  trigger_prefixes: field({ kind: 'words', label: '触发前缀', empty: '没有前缀', placeholder: '输入一个前缀' }),
};

const renderNodes = (
  nodes: ConsoleNode[],
  { parts = {}, platform = QQ_NAPCAT, wrap }: { parts?: PageParts; platform?: PlatformDescriptor; wrap?: (child: ReactNode) => ReactNode } = {},
) => {
  const body = <SchemaFields fields={FIELDS} nodes={nodes} sectionId="demo" />;
  return render(
    <PlatformProvider platform={platform}>
      <PartsProvider parts={parts}>{wrap ? wrap(body) : body}</PartsProvider>
    </PlatformProvider>,
  );
};

beforeEach(() => {
  platformConsoleStore('qq').setState({
    live: live(
      { enable_echo: true, echo_threshold: 3, image_max_bytes: 5242880, group_trigger: 'prefix', sticker_strategy: 'strict' },
      { enable_echo: 'extensions.echo.enabled' },
    ),
    draft: {},
  });
  platformConsoleStore('telegram').setState({ live: null, draft: {} });
  configStore(FEATURES_SCOPE).setState({ live: null, draft: {} });
  clearConsoleSchemas();
});

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('按 schema 渲染字段', () => {
  it('块、开关、数字、说明按布局摆出来，改动写进当前平台的草稿', () => {
    renderNodes([{
      kind: 'block',
      title: '群内互动',
      hint: '除了说话之外还能做什么',
      children: [{
        kind: 'grid',
        children: [{
          kind: 'field',
          key: 'enable_echo',
          children: [{ kind: 'field', key: 'echo_threshold' }, { kind: 'desc', text: '同一个人连刷不算。' }],
        }],
      }],
    }]);

    expect(screen.getByText('群内互动')).toBeInTheDocument();
    expect(screen.getByText('除了说话之外还能做什么')).toBeInTheDocument();
    expect(screen.getByText('同一个人连刷不算。')).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /跟复读/ })).toBeChecked();
    expect(screen.getByLabelText('连续')).toHaveValue(3);

    fireEvent.change(screen.getByLabelText('连续'), { target: { value: '5' } });
    expect(platformConsoleStore('qq').getState().draft).toEqual({ echo_threshold: 5 });
  });

  it('数字按 schema 的倍数换算，写回时还原成配置单位', () => {
    renderNodes([{ kind: 'field', key: 'image_max_bytes' }]);

    expect(screen.getByLabelText('单图上限')).toHaveValue(5);
    fireEvent.change(screen.getByLabelText('单图上限'), { target: { value: '8' } });
    expect(platformConsoleStore('qq').getState().draft).toEqual({ image_max_bytes: 8 * 1048576 });
  });

  it('标了 sub 的开关缩成一行小勾选，没有说明', () => {
    renderNodes([{ kind: 'field', key: 'name_anywhere', sub: true }]);

    expect(screen.getByRole('checkbox', { name: '不限句首，句中提到也算' })).toBeChecked();
  });

  it('跟随别的键的三态按 follows_when 推导，并说明当前在跟随', () => {
    renderNodes([{ kind: 'field', key: 'signal_prefix' }]);

    expect(screen.getByRole('checkbox', { name: /前缀触发/ })).toBeChecked();
    expect(screen.getByText(/当前跟随旧的触发模式/)).toBeInTheDocument();
  });

  it('不跟任何键的三态没配时取 unset', () => {
    renderNodes([{ kind: 'field', key: 'signal_at' }]);

    expect(screen.getByRole('checkbox', { name: /被 @/ })).toBeChecked();
  });

  it('单选按 schema 给的选项画，写回选中的值', () => {
    renderNodes([{ kind: 'field', key: 'sticker_strategy' }]);

    fireEvent.click(screen.getByRole('button', { name: '宽松' }));
    expect(platformConsoleStore('qq').getState().draft).toEqual({ sticker_strategy: 'loose' });
  });

  it('并排格和脚注照画，说明里的反引号渲染成代码', () => {
    const { container } = renderNodes([
      { kind: 'fixed', label: '发言间隔', children: [{ kind: 'desc', text: '0 表示不限制。' }] },
      { kind: 'note', text: '`/` 与 `／` 例外' },
    ]);

    expect(screen.getByText('发言间隔')).toBeInTheDocument();
    expect([...container.querySelectorAll('code')].map((node) => node.textContent)).toEqual(['/', '／']);
  });

  it('schema 里没有说明的键少画一格，不炸页面', () => {
    renderNodes([{ kind: 'field', key: 'ghost' }, { kind: 'desc', text: '后面照常' }]);

    expect(screen.getByText('后面照常')).toBeInTheDocument();
  });
});

describe('后端快照里的字段', () => {
  it('两页摆出来的每个字段都画得出控件：后端多一种控件而前端没跟上，会点名是哪个键', () => {
    const pages = [
      { name: 'QQ', schema: QQ_SCHEMA, scope: platformScope('qq') },
      { name: '全局', schema: FEATURES_SCHEMA, scope: FEATURES_SCOPE },
    ];
    const probes = pages.flatMap((page) => Object.keys(page.schema.fields).map((key) => ({ ...page, key })));
    expect(probes.length).toBeGreaterThan(0);

    const blank = probes.filter(({ key, schema, scope }) => {
      const { container, unmount } = render(
        <PlatformProvider platform={QQ_NAPCAT}>
          <ConfigScopeProvider scope={scope}>
            <SchemaFields fields={schema.fields} nodes={[{ kind: 'field', key }]} sectionId="probe" />
          </ConfigScopeProvider>
        </PlatformProvider>,
      );
      const empty = container.childElementCount === 0;
      unmount();
      return empty;
    }).map(({ key, name }) => `${name}:${key}`);

    expect(blank).toEqual([]);
  });
});

describe('反引号切段', () => {
  it('成对的反引号切出代码段', () => {
    expect(textRuns('前缀 `/` 例外')).toEqual([
      { code: false, text: '前缀 ' }, { code: true, text: '/' }, { code: false, text: ' 例外' },
    ]);
  });

  it('落单的反引号原样当文字', () => {
    expect(textRuns('a `b` c `d')).toEqual([
      { code: false, text: 'a ' }, { code: true, text: 'b' }, { code: false, text: ' c ' }, { code: false, text: '`d' },
    ]);
  });
});

describe('分区里前端自己画的部分', () => {
  it('custom 节点按分区取组件，没登记的名字只是少一块', () => {
    renderNodes(
      [{ kind: 'custom', name: 'probe' }, { kind: 'custom', name: 'missing' }],
      { parts: { demo: { components: { probe: () => <p>自定义块</p> } }, other: { components: { missing: () => <p>别的分区</p> } } } },
    );

    expect(screen.getByText('自定义块')).toBeInTheDocument();
    expect(screen.queryByText('别的分区')).not.toBeInTheDocument();
  });
});

describe('按配置作用域分家的草稿与 API', () => {
  it('两个平台和全局各攒各的草稿，互不干扰', () => {
    platformConsoleStore('qq').getState().setDraft('enable_echo', false);
    platformConsoleStore('telegram').getState().setDraft('enable_echo', false);
    configStore(FEATURES_SCOPE).getState().setDraft('enable_echo', true);
    platformConsoleStore('telegram').getState().discard();

    expect(platformConsoleStore('qq').getState().draft).toEqual({ enable_echo: false });
    expect(platformConsoleStore('telegram').getState().draft).toEqual({});
    expect(configStore(FEATURES_SCOPE).getState().draft).toEqual({ enable_echo: true });
  });

  it('同一个键在不同作用域里各读各的', () => {
    platformConsoleStore('telegram').setState({ live: live({ enable_echo: false }, {}), draft: {} });
    configStore(FEATURES_SCOPE).setState({ live: live({ enable_echo: true }, {}), draft: {} });
    const nodes: ConsoleNode[] = [{ kind: 'field', key: 'enable_echo' }];

    renderNodes(nodes);
    expect(screen.getByRole('checkbox', { name: /跟复读/ })).toBeChecked();

    cleanup();
    renderNodes(nodes, { platform: TELEGRAM });
    expect(screen.getByRole('checkbox', { name: /跟复读/ })).not.toBeChecked();

    cleanup();
    renderNodes(nodes, {
      platform: TELEGRAM,
      wrap: (child) => <ConfigScopeProvider scope={FEATURES_SCOPE}>{child}</ConfigScopeProvider>,
    });
    expect(screen.getByRole('checkbox', { name: /跟复读/ })).toBeChecked();
  });

  it('平台的读写走 /v1/platforms/{id}，平台 id 带进每一次请求', async () => {
    vi.mocked(api.getPlatformState).mockResolvedValue(live({}, {}));
    vi.mocked(api.getPlatformRaw).mockResolvedValue({ content: '', exists: false });

    await platformConsoleStore('telegram').getState().refresh('fixture-token');

    expect(api.getPlatformState).toHaveBeenCalledWith('fixture-token', 'telegram');
    expect(api.getPlatformRaw).toHaveBeenCalledWith('fixture-token', 'telegram');
    expect(api.getFeaturesState).not.toHaveBeenCalled();
  });

  it('全局的读写走 /v1/features，不碰任何平台', async () => {
    vi.mocked(api.getFeaturesState).mockResolvedValue({ ...live({}, {}), platforms: [] });
    vi.mocked(api.getFeaturesRaw).mockResolvedValue({ content: '', exists: false });

    await configStore(FEATURES_SCOPE).getState().refresh('fixture-token');

    expect(api.getFeaturesState).toHaveBeenCalledWith('fixture-token');
    expect(api.getPlatformState).not.toHaveBeenCalled();
  });

  it('同一个平台永远拿到同一个作用域对象', () => {
    expect(platformScope('qq')).toBe(platformScope('qq'));
    expect(platformScope('qq')).not.toBe(platformScope('telegram'));
  });
});

describe('schema 缓存', () => {
  const schema = { version: 1, sections: [], fields: {} };

  it('同一个作用域并发的几次加载只发一个请求，拉到就不再拉', async () => {
    vi.mocked(api.getPlatformSchema).mockResolvedValue(schema);

    await Promise.all([
      useSchemaStore.getState().load('fixture-token', platformScope('qq')),
      useSchemaStore.getState().load('fixture-token', platformScope('qq')),
    ]);
    await useSchemaStore.getState().load('fixture-token', platformScope('qq'));

    expect(api.getPlatformSchema).toHaveBeenCalledTimes(1);
    expect(api.getPlatformSchema).toHaveBeenCalledWith('fixture-token', 'qq');
    expect(useSchemaStore.getState().schemas[platformScope('qq').id]).toBe(schema);
  });

  it('全局那份走自己的接口', async () => {
    vi.mocked(api.getFeaturesSchema).mockResolvedValue(schema);

    await useSchemaStore.getState().load('fixture-token', FEATURES_SCOPE);

    expect(api.getFeaturesSchema).toHaveBeenCalledWith('fixture-token');
    expect(api.getPlatformSchema).not.toHaveBeenCalled();
  });

  it('404 点名要重启 supervisor，下次加载会重试', async () => {
    vi.mocked(api.getPlatformSchema).mockRejectedValueOnce(new Error('404 Not Found')).mockResolvedValueOnce(schema);

    await useSchemaStore.getState().load('fixture-token', platformScope('qq'));
    expect(useSchemaStore.getState().errors[platformScope('qq').id]).toMatch(/重启 supervisor/);

    await useSchemaStore.getState().load('fixture-token', platformScope('qq'));
    expect(useSchemaStore.getState().errors[platformScope('qq').id]).toBeUndefined();
    expect(useSchemaStore.getState().schemas[platformScope('qq').id]).toBe(schema);
  });

  it('后端说明了原因的 404 照原话显示，不让人去重启 supervisor', async () => {
    vi.mocked(api.getPlatformSchema).mockRejectedValueOnce(new Error('404 Telegram 没有控制台配置界面'));

    await useSchemaStore.getState().load('fixture-token', platformScope('telegram'));

    expect(useSchemaStore.getState().errors[platformScope('telegram').id]).toBe('读不到控制台布局：Telegram 没有控制台配置界面');
  });
});

describe('平台清单', () => {
  it('后端公布的描述换掉内置那份，认不出的通道种类丢掉', () => {
    const descriptor = toDescriptor({
      id: 'telegram',
      display_name: 'Telegram',
      short_name: 'TG',
      id_shape: 'string',
      channels: [
        { name: 'tg_group', kind: 'group', hashed: true },
        { name: 'tg_weird', kind: 'nonsense', hashed: true },
      ],
      has_config: true,
      hooks: [],
    });

    expect(descriptor).toMatchObject({ id: 'telegram', displayName: 'Telegram', shortName: 'TG' });
    expect(descriptor!.channels.map((channel) => channel.name)).toEqual(['tg_group']);
  });

  it('缺 id 的行直接丢掉，不会拿一个空前缀去认会话键', () => {
    expect(toDescriptor({ id: '' } as never)).toBeNull();
  });
});
