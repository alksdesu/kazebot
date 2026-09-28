// 通用组件不再写死 QQ：会话种类、本地备注和按钮色调都要认注册表。
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import * as common from '../components/common';
import { Button } from '../components/settings/pages/settingsControls';
import { aliasKey, readAliases, writeAlias } from '../console/aliases';
import { conversationNames } from '../features/conversationNames';
import {
  adoptPlatforms,
  connectionPeerName,
  conversationKindForScope,
  currentPlatformId,
  entrypointNote,
  isConversationScope,
  looksLikeScopeKey,
  platformForScope,
  platformRegistryState,
  resetPlatforms,
  settlePlatforms,
  subscribePlatforms,
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
  entrypoints: ['telegram_bot.py'],
  connectionPeer: 'Telegram Bot API',
  connectionHint: '检查 bot token 与出网代理',
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

describe('本实例在跑的平台', () => {
  afterEach(resetPlatforms);

  it('后端说了跑哪个，要列页的就只有它，认会话键的还是整张清单', () => {
    adoptPlatforms([QQ_NAPCAT, TELEGRAM], 'telegram');

    expect(currentPlatformId()).toBe('telegram');
    expect(platformRegistryState().running).toEqual([TELEGRAM]);
    expect(platformRegistryState().platforms).toEqual([QQ_NAPCAT, TELEGRAM]);
    // 迁移过来的历史会话出自别的平台，前缀照样要认得出。
    expect(conversationKindForScope('qq_group:abc')).toBe('group');
  });

  it('后端不说跑哪个时照旧全列 —— 旧版后端没有这个字段', () => {
    adoptPlatforms([QQ_NAPCAT, TELEGRAM]);

    expect(currentPlatformId()).toBeNull();
    expect(platformRegistryState().running).toEqual([QQ_NAPCAT, TELEGRAM]);
  });

  it('后端说的平台不在它自己给的清单里时，宁可多列也不能一页不剩', () => {
    adoptPlatforms([QQ_NAPCAT, TELEGRAM], 'nowhere');

    expect(currentPlatformId()).toBe('nowhere');
    expect(platformRegistryState().running).toEqual([QQ_NAPCAT, TELEGRAM]);
  });

  it('清单没到之前不知道跑哪个，reset 之后也回到不知道', () => {
    expect(currentPlatformId()).toBeNull();
    expect(platformRegistryState().running).toEqual([QQ_NAPCAT]);

    adoptPlatforms([QQ_NAPCAT, TELEGRAM], 'telegram');
    resetPlatforms();

    expect(currentPlatformId()).toBeNull();
    expect(platformRegistryState().running).toEqual([QQ_NAPCAT]);
  });

  it('清单拿不到时只是不用再等，跑哪个还是不知道', () => {
    settlePlatforms();

    expect(currentPlatformId()).toBeNull();
    expect(platformRegistryState().running).toEqual([QQ_NAPCAT]);
    expect(platformRegistryState().settled).toBe(true);
  });

  it('空清单不换内置那份，也不认后端顺带说的那个 current', () => {
    adoptPlatforms([], 'telegram');

    expect(currentPlatformId()).toBeNull();
    expect(platformRegistryState().platforms).toEqual([QQ_NAPCAT]);
  });
});

describe('平台清单的订阅', () => {
  const watchers: (() => void)[] = [];

  const watch = () => {
    const seen = vi.fn();
    watchers.push(subscribePlatforms(seen));
    return seen;
  };

  afterEach(() => {
    while (watchers.length) watchers.pop()!();
    resetPlatforms();
  });

  it('清单没到之前给的是内置那份', () => {
    expect(platformRegistryState().platforms).toEqual([QQ_NAPCAT]);
    expect(platformRegistryState().settled).toBe(false);
  });

  it('换清单时整份换掉：订阅方按引用比，引用没变就是没变', () => {
    const before = platformRegistryState();
    const seen = watch();

    adoptPlatforms([QQ_NAPCAT, TELEGRAM]);

    const after = platformRegistryState();
    expect(seen).toHaveBeenCalledTimes(1);
    expect(after).not.toBe(before);
    expect(after.platforms).not.toBe(before.platforms);
    expect(after.settled).toBe(true);
  });

  it('退订之后不再收到通知', () => {
    const seen = vi.fn();
    const stop = subscribePlatforms(seen);

    adoptPlatforms([QQ_NAPCAT, TELEGRAM]);
    stop();
    resetPlatforms();

    expect(seen).toHaveBeenCalledTimes(1);
  });

  it('空清单不换内置那份，但不用再等了', () => {
    const seen = watch();

    adoptPlatforms([]);

    expect(platformRegistryState().platforms).toEqual([QQ_NAPCAT]);
    expect(platformRegistryState().settled).toBe(true);
    expect(seen).toHaveBeenCalledTimes(1);
  });

  it('settle 幂等：到过一次就不再惊动订阅方', () => {
    settlePlatforms();
    const seen = watch();

    settlePlatforms();
    settlePlatforms();

    expect(seen).not.toHaveBeenCalled();
  });

  it('adopt 过之后再 settle 也不重复通知，清单也不退回内置的', () => {
    adoptPlatforms([QQ_NAPCAT, TELEGRAM]);
    const seen = watch();

    settlePlatforms();

    expect(seen).not.toHaveBeenCalled();
    expect(platformRegistryState().platforms).toEqual([QQ_NAPCAT, TELEGRAM]);
  });

  it('reset 回到内置那份，并且重新开始等清单', () => {
    adoptPlatforms([QQ_NAPCAT, TELEGRAM]);
    const seen = watch();

    resetPlatforms();

    expect(seen).toHaveBeenCalledTimes(1);
    expect(platformRegistryState().platforms).toEqual([QQ_NAPCAT]);
    expect(platformRegistryState().settled).toBe(false);
  });
});

describe('平台文案零件', () => {
  it('入口脚本写成一段括号，多个用顿号连', () => {
    expect(entrypointNote(QQ_NAPCAT)).toBe('（bot.py）');
    expect(entrypointNote({ ...TELEGRAM, entrypoints: ['bot.py', 'tg.py'] })).toBe('（bot.py、tg.py）');
  });

  it('没声明入口就整段不出现，不留一个空括号', () => {
    expect(entrypointNote({ ...TELEGRAM, entrypoints: [] })).toBe('');
  });

  it('没说连的是谁就叫「平台」', () => {
    expect(connectionPeerName(QQ_NAPCAT)).toBe('NapCat');
    expect(connectionPeerName(TELEGRAM)).toBe('Telegram Bot API');
    expect(connectionPeerName({ ...TELEGRAM, connectionPeer: '' })).toBe('平台');
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
