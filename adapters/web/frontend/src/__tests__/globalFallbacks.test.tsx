// 全局页标出平台兜底：全局文件没写的只认全局的键，平台实际用的是自己的老位置或 env，页面上得写清是谁在用什么。
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  CentralSwitchState,
  ConfigSource,
  ConsoleFieldSchema,
  FeatureFallback,
  FeaturesLiveState,
} from '../api/supervisorClient';
import { ConfigScopeProvider, FEATURES_SCOPE, platformScope, type ConfigScope } from '../console/configScope';
import { configStore, platformConsoleStore, useGlobalFallbacks, useLayerState } from '../console/consoleStore';
import { PercentField } from '../console/fields';
import { asPercent, fieldValueText } from '../console/format';
import { fallbackText, LayerSwitch } from '../console/LayerSwitch';

const spec = (overrides: Partial<ConsoleFieldSchema> & Pick<ConsoleFieldSchema, 'kind' | 'label'>): ConsoleFieldSchema => ({
  path: 'extensions.sticker.library_limit',
  layers: ['global'],
  default: null,
  ...overrides,
});

const LIBRARY = spec({ kind: 'number', label: '图库', unit: '张', step: 50, default: 1000 });
const AUTO_TAG = spec({ kind: 'bool', label: '自动打标签', path: 'extensions.sticker.auto_tag', default: true });

const fallback = (overrides: Partial<FeatureFallback> = {}): FeatureFallback => ({
  platform: 'qq',
  display_name: 'QQ',
  source: 'legacy',
  file: 'qq.yaml',
  value: 500,
  ...overrides,
});

const central = (enabled: boolean, setGlobally = false): CentralSwitchState => ({
  enabled,
  set_globally: setGlobally,
});

/** 全局页的生效快照。不给 sources 就是不公布来源的旧版生效方。 */
const featuresLive = ({ values = {}, sources, fallbacks, central: switches }: {
  values?: Record<string, unknown>;
  sources?: Record<string, ConfigSource>;
  fallbacks?: Record<string, FeatureFallback[]>;
  central?: Record<string, CentralSwitchState>;
} = {}): FeaturesLiveState => ({
  published: true,
  applied: true,
  file: { exists: true, mtime_ns: 1, size: 1 },
  platforms: [],
  state: {
    values,
    paths: { sticker_auto_tag: 'extensions.sticker.auto_tag', sticker_library_limit: 'extensions.sticker.library_limit' },
    ...(sources ? { sources } : {}),
  },
  ...(fallbacks ? { fallbacks } : {}),
  ...(switches ? { central: switches } : {}),
});

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const renderSwitch = (configKey: string, field: ConsoleFieldSchema, scope: ConfigScope = FEATURES_SCOPE) => render(
  <ConfigScopeProvider scope={scope}>
    <LayerSwitch configKey={configKey} spec={field} />
  </ConfigScopeProvider>,
);

/** useLayerState 的读数，渲染成一行好断言的文字。 */
const Probe = ({ name }: { name: string }) => {
  const { known, own, source } = useLayerState(name);
  return <span data-testid="layer">{`${known}|${own}|${source ?? '-'}`}</span>;
};

beforeEach(() => {
  configStore(FEATURES_SCOPE).setState({
    live: null, draft: {}, raw: '', rawExists: true, loading: false, saving: false, error: '', notice: '',
  });
  platformConsoleStore('qq').setState({
    live: null, draft: {}, raw: '', rawExists: true, loading: false, saving: false, error: '', notice: '',
  });
});

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('fallbackText', () => {
  it('老位置上的数字带上单位', () => {
    expect(fallbackText([fallback()], undefined, LIBRARY)).toBe('QQ 眼下用着 qq.yaml 老位置上的「500 张」');
  });

  it('环境变量里的开关写成开或关', () => {
    const env = fallback({ source: 'env', file: '', value: false });

    expect(fallbackText([env], undefined, AUTO_TAG)).toBe('QQ 眼下用着环境变量给的「关」');
  });

  it('平台名以汉字结尾时不空一格', () => {
    const wechat = fallback({ platform: 'wechat', display_name: '微信' });

    expect(fallbackText([wechat], undefined, LIBRARY)).toBe('微信眼下用着 qq.yaml 老位置上的「500 张」');
  });

  it('界面和配置单位不同的项按倍数换算', () => {
    const image = spec({ kind: 'number', label: '单图上限', unit: 'MB', scale: 1048576, default: 10485760 });

    expect(fallbackText([fallback({ value: 5242880 })], undefined, image)).toBe(
      'QQ 眼下用着 qq.yaml 老位置上的「5 MB」',
    );
  });

  it('各平台各用各的，之间用分号隔开', () => {
    const rows = [fallback(), fallback({ platform: 'vault', display_name: '保险库', source: 'env', file: '', value: 800 })];

    expect(fallbackText(rows, undefined, LIBRARY)).toBe(
      'QQ 眼下用着 qq.yaml 老位置上的「500 张」；保险库眼下用着环境变量给的「800 张」',
    );
  });

  it('统一跑的开关只有一个结果，各平台的兜底只是依据', () => {
    const rows = [
      fallback({ value: false }),
      fallback({ platform: 'vault', display_name: '保险库', source: 'env', file: '', value: false }),
    ];

    expect(fallbackText(rows, central(false), AUTO_TAG)).toBe(
      '实际关着：QQ 的 qq.yaml 老位置是「关」、保险库的环境变量是「关」',
    );
  });

  it('有一个平台开着，统一跑的就是开着', () => {
    const rows = [fallback({ value: false }), fallback({ platform: 'vault', display_name: '保险库', value: true, file: 'vault.yaml' })];

    expect(fallbackText(rows, central(true), AUTO_TAG)).toBe(
      '实际开着：QQ 的 qq.yaml 老位置是「关」、保险库的 vault.yaml 老位置是「开」',
    );
  });

  it('全局写了这一项就不再解释兜底，按各用各的说', () => {
    // set_globally 为真时页面上显示的就是生效值，剩下的只是「这几个平台的老写法还留着」。
    expect(fallbackText([fallback({ value: false })], central(false, true), AUTO_TAG)).toBe(
      'QQ 眼下用着 qq.yaml 老位置上的「关」',
    );
  });

  it('没有兜底就什么也不说', () => {
    expect(fallbackText([], central(false), AUTO_TAG)).toBe('');
    expect(fallbackText([], undefined, LIBRARY)).toBe('');
  });
});

describe('fieldValueText', () => {
  it('开关写成开或关', () => {
    expect(fieldValueText({ kind: 'bool' }, true)).toBe('开');
    expect(fieldValueText({ kind: 'bool' }, false)).toBe('关');
  });

  it('比例写成百分数', () => {
    expect(fieldValueText({ kind: 'percent' }, 0.2)).toBe('20%');
    expect(fieldValueText({ kind: 'percent' }, 'oops')).toBe('oops');
  });

  it('数字按倍数换算后带单位，没有单位就只写数字', () => {
    expect(fieldValueText({ kind: 'number', unit: '张' }, 500)).toBe('500 张');
    expect(fieldValueText({ kind: 'number', unit: 'MB', scale: 1048576 }, 5242880)).toBe('5 MB');
    expect(fieldValueText({ kind: 'number' }, 12)).toBe('12');
    expect(fieldValueText({ kind: 'number', unit: '张' }, null)).toBe('null');
  });

  it('词表用顿号连，文本原样写', () => {
    expect(fieldValueText({ kind: 'words' }, ['咪啪', '猫'])).toBe('咪啪、猫');
    expect(fieldValueText({ kind: 'words' }, [])).toBe('');
    expect(fieldValueText({ kind: 'text' }, 'chat.intent')).toBe('chat.intent');
    expect(fieldValueText({ kind: 'tristate' }, null)).toBe('');
  });
});

describe('asPercent', () => {
  it('小数放大成百分数时不留浮点尾巴', () => {
    expect(asPercent(0.15)).toBe(15);
    expect(asPercent(0.123456)).toBe(12.35);
    expect(asPercent(0)).toBe(0);
    expect(asPercent(1)).toBe(100);
  });
});

describe('LayerSwitch 上的兜底说明', () => {
  it('全局页上没写本层、平台有兜底时写清是谁在用', () => {
    configStore(FEATURES_SCOPE).setState({
      live: featuresLive({
        values: { sticker_library_limit: 1000 },
        sources: { sticker_library_limit: 'default' },
        fallbacks: { sticker_library_limit: [fallback()] },
      }),
      draft: {},
    });

    renderSwitch('sticker_library_limit', LIBRARY);

    expect(screen.getByText('QQ 眼下用着 qq.yaml 老位置上的「500 张」')).toBeInTheDocument();
  });

  it('统一跑的开关写的是实际开不开', () => {
    configStore(FEATURES_SCOPE).setState({
      live: featuresLive({
        values: { sticker_auto_tag: true },
        sources: { sticker_auto_tag: 'default' },
        fallbacks: { sticker_auto_tag: [fallback({ value: false })] },
        central: { sticker_auto_tag: central(false) },
      }),
      draft: {},
    });

    renderSwitch('sticker_auto_tag', AUTO_TAG);

    expect(screen.getByText('实际关着：QQ 的 qq.yaml 老位置是「关」')).toBeInTheDocument();
  });

  it('选了「自定义」之后这一项归全局文件管，不再说兜底', () => {
    configStore(FEATURES_SCOPE).setState({
      live: featuresLive({
        values: { sticker_library_limit: 1000 },
        sources: { sticker_library_limit: 'default' },
        fallbacks: { sticker_library_limit: [fallback()] },
      }),
      draft: {},
    });
    configStore(FEATURES_SCOPE).getState().pin('sticker_library_limit');

    renderSwitch('sticker_library_limit', LIBRARY);

    expect(screen.queryByText(/眼下用着/)).toBeNull();
  });

  it('平台页上没有这句话', () => {
    // 兜底只有全局页要标：平台页上显示的本来就是这个平台自己的生效值。
    platformConsoleStore('qq').setState({
      live: featuresLive({ values: { sticker_library_limit: 500 }, sources: { sticker_library_limit: 'legacy' } }),
      draft: {},
    });

    renderSwitch('sticker_library_limit', LIBRARY, platformScope('qq'));

    expect(screen.queryByText(/眼下用着/)).toBeNull();
  });

  it('平台页上用着环境变量时原来那句照旧', () => {
    platformConsoleStore('qq').setState({
      live: featuresLive({ values: { enable_echo: true }, sources: { enable_echo: 'env' } }),
      draft: {},
    });

    renderSwitch('enable_echo', spec({ kind: 'bool', label: '跟复读', layers: ['global', 'platform'] }), platformScope('qq'));

    expect(screen.getByText('眼下用的是环境变量')).toBeInTheDocument();
  });
});

describe('useGlobalFallbacks', () => {
  it('没有兜底时每次都是同一个空表', () => {
    // 每次返回新的 []，订阅方就会跟着 store 的每一次变化空转重渲染。
    const seen: (readonly FeatureFallback[])[] = [];
    const Collect = ({ name }: { name: string }) => {
      seen.push(useGlobalFallbacks(name).fallbacks);
      return null;
    };
    configStore(FEATURES_SCOPE).setState({ live: featuresLive({ values: { sticker_auto_tag: true } }), draft: {} });

    render(
      <ConfigScopeProvider scope={FEATURES_SCOPE}>
        <Collect name="sticker_auto_tag" />
        <Collect name="sticker_library_limit" />
      </ConfigScopeProvider>,
    );

    expect(seen).toHaveLength(2);
    expect(seen[0]).toHaveLength(0);
    expect(seen[1]).toBe(seen[0]);
  });

  it('旧后端不给这两项时当没有兜底', () => {
    configStore(FEATURES_SCOPE).setState({
      live: featuresLive({ values: { sticker_auto_tag: true }, sources: { sticker_auto_tag: 'default' } }),
      draft: {},
    });

    renderSwitch('sticker_auto_tag', AUTO_TAG);

    expect(screen.queryByText(/实际|眼下用着/)).toBeNull();
    expect(screen.getByRole('group', { name: '自动打标签的来源' })).toBeInTheDocument();
  });
});

describe('保存之后先按写进去的算', () => {
  /** 写回成功，但生效方还没认账：写完之后读到的原文是别人又改过的那份，settle 不会拿它盖掉刚写进去的值。 */
  const serveDiverged = (): void => {
    let reads = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'PUT') return json({ ok: true, warnings: [] });
      if (String(input).endsWith('/config/state')) {
        return json({ published: true, applied: false, file: {}, state: { values: {}, paths: {} }, platforms: [] });
      }
      reads += 1;
      return json({ content: reads === 1 ? 'version: 1\n' : 'version: 2\n', exists: true });
    }));
  };

  it('写进去的键立刻算成本层写着，来源也跟着落到本层', async () => {
    configStore(FEATURES_SCOPE).setState({
      live: featuresLive({ values: { sticker_auto_tag: true }, sources: { sticker_auto_tag: 'default' } }),
      draft: {},
    });
    configStore(FEATURES_SCOPE).getState().setDraft('sticker_auto_tag', false);
    serveDiverged();

    const outcome = await configStore(FEATURES_SCOPE).getState().save('token', ['sticker_auto_tag']);

    expect(outcome?.ok).toBe(true);
    render(<ConfigScopeProvider scope={FEATURES_SCOPE}><Probe name="sticker_auto_tag" /></ConfigScopeProvider>);
    expect(screen.getByTestId('layer')).toHaveTextContent('true|true|global');
    expect(configStore(FEATURES_SCOPE).getState().live?.state?.values?.sticker_auto_tag).toBe(false);
  });

  it('生效方不公布来源时不凭空造一份出来', async () => {
    // 造出来的话，没公布来源的旧版生效方也会长出一个来源切换，选「默认」就删掉了运营者没写过的行。
    configStore(FEATURES_SCOPE).setState({
      live: featuresLive({ values: { sticker_auto_tag: true } }),
      draft: {},
    });
    configStore(FEATURES_SCOPE).getState().setDraft('sticker_auto_tag', false);
    serveDiverged();

    await configStore(FEATURES_SCOPE).getState().save('token', ['sticker_auto_tag']);

    expect(configStore(FEATURES_SCOPE).getState().live?.state?.sources).toBeUndefined();
    expect(configStore(FEATURES_SCOPE).getState().live?.state?.values?.sticker_auto_tag).toBe(false);
    render(<ConfigScopeProvider scope={FEATURES_SCOPE}><Probe name="sticker_auto_tag" /></ConfigScopeProvider>);
    expect(screen.getByTestId('layer')).toHaveTextContent('false|false|-');
  });

  it('删行不叠：删完落到哪一层只有生效方算得出来', async () => {
    configStore(FEATURES_SCOPE).setState({
      live: featuresLive({ values: { sticker_auto_tag: false }, sources: { sticker_auto_tag: 'global' } }),
      draft: {},
    });
    configStore(FEATURES_SCOPE).getState().inherit('sticker_auto_tag');

    serveDiverged();
    await configStore(FEATURES_SCOPE).getState().save('token', ['sticker_auto_tag']);

    const { live } = configStore(FEATURES_SCOPE).getState();
    expect(live?.state?.values?.sticker_auto_tag).toBe(false);
    expect(live?.state?.sources?.sticker_auto_tag).toBe('global');
  });
});

describe('PercentField', () => {
  it('比例按百分数显示，不带浮点尾巴', () => {
    configStore(FEATURES_SCOPE).setState({
      live: featuresLive({ values: { sticker_send_probability: 0.15 } }),
      draft: {},
    });

    render(
      <ConfigScopeProvider scope={FEATURES_SCOPE}>
        <PercentField configKey="sticker_send_probability" label="每轮出现概率" />
      </ConfigScopeProvider>,
    );

    expect(screen.getByLabelText('每轮出现概率')).toHaveValue(15);
  });
});
