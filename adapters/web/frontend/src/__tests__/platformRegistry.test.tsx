// 通用组件不再写死 QQ：会话种类、本地备注和按钮色调都要认注册表。
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import * as common from '../components/common';
import { Button } from '../components/settings/pages/settingsControls';
import { aliasKey, readAliases, writeAlias } from '../console/aliases';
import { conversationNames } from '../features/conversationNames';
import {
  adoptPlatforms,
  conversationKindForScope,
  isConversationScope,
  looksLikeScopeKey,
  platformForScope,
  resetPlatforms,
  QQ_NAPCAT,
  type PlatformDescriptor,
} from '../platforms/registry';

const TELEGRAM: PlatformDescriptor = {
  id: 'telegram',
  displayName: 'Telegram',
  shortName: 'TG',
  channels: [
    { name: 'tg_group', kind: 'group', hashed: true },
    { name: 'tg_private', kind: 'private', hashed: true },
  ],
  hasConfig: true,
};

describe('平台注册表', () => {
  afterEach(resetPlatforms);

  it('按通道前缀认会话种类，转发桥和兜底通道不算对话', () => {
    expect(conversationKindForScope('qq_group:abc')).toBe('group');
    expect(conversationKindForScope('qq_private:abc')).toBe('private');
    expect(conversationKindForScope('qq_forward:123')).toBeNull();
    expect(conversationKindForScope('qq_unknown:abc')).toBeNull();
    expect(conversationKindForScope('web:console')).toBeNull();
  });

  it('光有前缀没有目标不算会话键', () => {
    expect(isConversationScope('qq_group:')).toBe(false);
    expect(isConversationScope('qq_group:7')).toBe(true);
    // 校验作用域时挡不住它，策略就会挂在一个空目标上。
    expect(looksLikeScopeKey('qq_group:')).toBe(true);
  });

  it('换上后端清单之后认得新平台，空清单不覆盖内置的', () => {
    adoptPlatforms([QQ_NAPCAT, TELEGRAM]);
    expect(conversationKindForScope('tg_group:abc')).toBe('group');
    expect(platformForScope('tg_private:abc')?.displayName).toBe('Telegram');

    adoptPlatforms([]);
    expect(conversationKindForScope('tg_group:abc')).toBe('group');
  });
});

describe('会话名称', () => {
  afterEach(resetPlatforms);

  it('新平台的群聊和私聊不再退化成「会话」', () => {
    adoptPlatforms([QQ_NAPCAT, TELEGRAM]);
    const names = conversationNames([
      { scope: 'tg_group:aaa' },
      { scope: 'tg_private:bbb' },
    ]);

    expect([...names.values()].join(' ')).toContain('群聊');
    expect([...names.values()].join(' ')).toContain('私聊');
  });

  it('名字本身就是会话键时不当成人取的名字', () => {
    adoptPlatforms([QQ_NAPCAT, TELEGRAM]);
    const names = conversationNames([
      { scope: 'tg_group:aaa', owner: { kind: 'group', label: 'tg_private:bbb' } },
    ]);

    expect([...names.values()][0]).not.toBe('tg_private:bbb');
  });
});

describe('本地备注', () => {
  afterEach(() => { localStorage.clear(); });

  it('按平台加 ID 存，不同平台的同号不串', () => {
    writeAlias('qq', 700, 'QQ 群');
    writeAlias('telegram', 700, 'TG 群');

    expect(readAliases()[aliasKey('qq', 700)]).toBe('QQ 群');
    expect(readAliases()[aliasKey('telegram', 700)]).toBe('TG 群');
  });

  it('改版前按裸 ID 存的备注划到 NapCat QQ 名下', () => {
    localStorage.setItem('qq_console_aliases', JSON.stringify({ 700: '老备注' }));

    expect(readAliases()).toEqual({ 'qq:700': '老备注' });
  });
});

describe('按钮只剩一套', () => {
  afterEach(cleanup);

  it('common 不再导出第二个 Button', () => {
    expect('Button' in common).toBe(false);
  });

  it('tone 决定色调，loading 时转圈并自锁', () => {
    render(<Button loading tone="danger">删除</Button>);

    const button = screen.getByRole('button', { name: '删除' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('aria-busy', 'true');
    expect(button.querySelector('.app-button-spinner')).not.toBeNull();
    expect(button.className).toContain('--duties-danger-bg');
  });
});
