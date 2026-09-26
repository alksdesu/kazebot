// 声明驱动：字段按 schema 渲染、复杂件走注册表、草稿与 API 按平台分家。
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../api/supervisorClient';
import type { PlatformLiveState } from '../api/supervisorClient';
import { platformConsoleStore } from '../console/consoleStore';
import { SchemaFields } from '../console/FieldRenderer';
import { registerSectionComponents, sectionComponent, type FieldSpec } from '../console/fieldSchema';
import { PlatformProvider } from '../console/platformContext';
import { QQ_NAPCAT, type PlatformDescriptor } from '../platforms/registry';
import { toDescriptor } from '../platforms/usePlatformRegistry';

vi.mock('../api/supervisorClient', async importOriginal => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getPlatformRaw: vi.fn(), getPlatformState: vi.fn(), updatePlatformRaw: vi.fn(),
}));

const TELEGRAM: PlatformDescriptor = {
  id: 'telegram',
  displayName: 'Telegram',
  shortName: 'TG',
  channels: [{ name: 'tg_group', kind: 'group', hashed: true }],
  hasConfig: true,
};

const live = (values: Record<string, unknown>, paths: Record<string, string>): PlatformLiveState => ({
  published: true,
  applied: true,
  file: { exists: true, mtime_ns: 1, size: 1 },
  state: { values, paths },
});

const renderSpec = (spec: FieldSpec[], platform = QQ_NAPCAT) => render(
  <PlatformProvider platform={platform}>
    <SchemaFields platformId={platform.id} sectionId="demo" spec={spec} />
  </PlatformProvider>,
);

beforeEach(() => {
  platformConsoleStore('qq').setState({
    live: live({ enable_echo: true, echo_threshold: 3, group_trigger: 'prefix' }, { enable_echo: 'extensions.echo.enabled' }),
    draft: {},
  });
  platformConsoleStore('telegram').setState({ live: null, draft: {} });
});

afterEach(cleanup);

describe('按 schema 渲染字段', () => {
  it('开关、数字、说明按声明摆出来，改动写进当前平台的草稿', () => {
    renderSpec([{
      kind: 'block',
      title: '群内互动',
      hint: '除了说话之外还能做什么',
      children: [{
        kind: 'grid',
        children: [{
          kind: 'bool',
          key: 'enable_echo',
          label: '跟复读',
          desc: '连着几个人刷同一句时，也跟一条。',
          children: [
            { kind: 'number', key: 'echo_threshold', label: '连续', unit: '条才跟' },
            { kind: 'desc', text: '同一个人连刷不算。' },
          ],
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

  it('三态的默认值能跟着另一个键推导', () => {
    renderSpec([{
      kind: 'tristate',
      key: 'signal_prefix',
      label: '前缀触发',
      desc: '以配置的前缀开头。',
      fallback: { from: 'group_trigger', when: (value) => value === 'prefix' },
    }]);

    expect(screen.getByRole('checkbox', { name: /前缀触发/ })).toBeChecked();
    expect(screen.getByText(/当前跟随旧的触发模式/)).toBeInTheDocument();
  });
});

describe('自定义组件注册表', () => {
  it('按平台加分区取组件，没登记的名字只是少一块，不炸页面', () => {
    registerSectionComponents(QQ_NAPCAT.id, 'demo', { probe: () => <p>自定义块</p> });

    renderSpec([{ kind: 'custom', name: 'probe' }, { kind: 'custom', name: 'missing' }]);

    expect(screen.getByText('自定义块')).toBeInTheDocument();
    expect(sectionComponent(QQ_NAPCAT.id, 'demo', 'missing')).toBeNull();
    expect(sectionComponent('telegram', 'demo', 'probe')).toBeNull();
  });
});

describe('按平台分家的草稿与 API', () => {
  it('两个平台各攒各的草稿，互不干扰', () => {
    platformConsoleStore('qq').getState().setDraft('enable_echo', false);
    platformConsoleStore('telegram').getState().setDraft('enable_echo', false);
    platformConsoleStore('telegram').getState().discard();

    expect(platformConsoleStore('qq').getState().draft).toEqual({ enable_echo: false });
    expect(platformConsoleStore('telegram').getState().draft).toEqual({});
  });

  it('同一个键在两个平台的分区里各读各的', () => {
    platformConsoleStore('telegram').setState({ live: live({ enable_echo: false }, {}), draft: {} });

    const spec: FieldSpec[] = [{ kind: 'bool', key: 'enable_echo', label: '跟复读' }];
    renderSpec(spec);
    expect(screen.getByRole('checkbox', { name: /跟复读/ })).toBeChecked();

    cleanup();
    renderSpec(spec, TELEGRAM);
    expect(screen.getByRole('checkbox', { name: /跟复读/ })).not.toBeChecked();
  });

  it('读写走 /v1/platforms/{id}，平台 id 带进每一次请求', async () => {
    vi.mocked(api.getPlatformState).mockResolvedValue(live({}, {}));
    vi.mocked(api.getPlatformRaw).mockResolvedValue({ content: '', exists: false });

    await platformConsoleStore('telegram').getState().refresh('fixture-token');

    expect(api.getPlatformState).toHaveBeenCalledWith('fixture-token', 'telegram');
    expect(api.getPlatformRaw).toHaveBeenCalledWith('fixture-token', 'telegram');
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
