// 号码表与本地备注。备注绝不能混进 qq.yaml —— 群名往往带真实身份。
import { beforeEach, describe, expect, it } from 'vitest';

import type { QqInputRule } from '../api/supervisorClient';
import { readAliases, writeAlias } from '../console/aliases';
import { addId, addWord, idIsValid, mergeDraft, useConsoleStore } from '../console/consoleStore';

// bot 公布的那份规则，逐字照抄 live_config 的 ID_INPUT_RULE。
const ID_RULE: QqInputRule = { kind: 'id', pattern: '^[0-9]+$', min: 1, max: 9007199254740991 };

describe('addId', () => {
  it('appends a numeric id', () => {
    expect(addId([111], '222', ID_RULE)).toEqual([111, 222]);
  });

  it('trims what was typed', () => {
    expect(addId([], ' 12345 ', ID_RULE)).toEqual([12345]);
  });

  it('rejects anything the published rule does not match', () => {
    // 后端也拒；两边规则由 bot 公布的 input_rules 对齐。
    const ids = [111];

    expect(addId(ids, '12a45', ID_RULE)).toBe(ids);
    expect(addId(ids, 'me', ID_RULE)).toBe(ids);
    expect(addId(ids, '-500', ID_RULE)).toBe(ids);
    expect(addId(ids, '+500', ID_RULE)).toBe(ids);
    expect(addId(ids, '1_0', ID_RULE)).toBe(ids);
    expect(addId(ids, '１２３', ID_RULE)).toBe(ids);
    expect(addId(ids, '0', ID_RULE)).toBe(ids);
    expect(addId(ids, '', ID_RULE)).toBe(ids);
  });

  it('rejects a duplicate', () => {
    const ids = [111];

    expect(addId(ids, '111', ID_RULE)).toBe(ids);
  });

  it('rejects a number too big to survive a round trip', () => {
    const ids: number[] = [];

    expect(addId(ids, '99999999999999999999', ID_RULE)).toBe(ids);
  });

  it('keeps a short id, the way the bot does', () => {
    // 仓库里的测试与本地调试配置全是 7 / 998877 这种短号。
    expect(addId([], '7', ID_RULE)).toEqual([7]);
  });

  it('keeps the configured order', () => {
    // 审批通知按名单顺序逐个私发，顺序稳定日志才可读。
    expect(addId(addId([], '300', ID_RULE), '100', ID_RULE)).toEqual([300, 100]);
  });
});

describe('the published rule is the only rule', () => {
  it('refuses to add anything when the bot published none', () => {
    // 跑着却不公布规则 = 版本不匹配。此时按前端猜的规则收下输入正是漂移本身。
    const ids = [111];
    const words = ['咪啪'];

    expect(addId(ids, '222', null)).toBe(ids);
    expect(addWord(words, '早安', null)).toBe(words);
    expect(idIsValid('222', null)).toBe(false);
  });

  it('honours the max the bot published', () => {
    const rule: QqInputRule = { ...ID_RULE, max: 999 };

    expect(addId([], '999', rule)).toEqual([999]);
    expect(addId([], '1000', rule)).toEqual([]);
  });

  it('honours the min the bot published', () => {
    const rule: QqInputRule = { ...ID_RULE, min: 5 };

    expect(addId([], '4', rule)).toEqual([]);
    expect(addId([], '5', rule)).toEqual([5]);
  });

  it('honours the pattern the bot published', () => {
    const rule: QqInputRule = { kind: 'id', pattern: '^1[0-9]+$', min: 1, max: 999 };

    expect(addId([], '222', rule)).toEqual([]);
    expect(addId([], '123', rule)).toEqual([123]);
  });

  it('refuses a rule meant for another kind of list', () => {
    const words = ['咪啪'];

    expect(addId([], '222', { kind: 'word', trim: true })).toEqual([]);
    expect(addWord(words, '早安', ID_RULE)).toBe(words);
  });

  it('refuses an id rule that carries no pattern', () => {
    // 没有 pattern 就没有值域，此时放行等于回到「前端自己猜」。
    expect(addId([], '222', { kind: 'id', min: 1, max: 999 })).toEqual([]);
  });

  it('still refuses a number that cannot survive JSON even if the bot allows it', () => {
    const rule: QqInputRule = { ...ID_RULE, max: Number.MAX_VALUE };

    expect(addId([], '99999999999999999999', rule)).toEqual([]);
  });
});

describe('local aliases', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('remembers a note for an id', () => {
    writeAlias(700, '测试群');

    expect(readAliases()['700']).toBe('测试群');
  });

  it('drops the entry when the note is cleared', () => {
    writeAlias(700, '测试群');

    writeAlias(700, '   ');

    expect(readAliases()).toEqual({});
  });

  it('survives corrupt storage', () => {
    localStorage.setItem('qq_console_aliases', '{not json');

    expect(readAliases()).toEqual({});
  });

  it('returns nothing when storage was never written', () => {
    expect(readAliases()).toEqual({});
  });

  it('ignores a stored null', () => {
    // JSON.parse('null') 给的是 null 而不是抛，不挡住就会去读 null 的属性。
    localStorage.setItem('qq_console_aliases', 'null');

    expect(readAliases()).toEqual({});
  });

  it('ignores non-string values that got in somehow', () => {
    localStorage.setItem('qq_console_aliases', JSON.stringify({ 700: 42, 800: 'ok' }));

    expect(readAliases()).toEqual({ 800: 'ok' });
  });

  it('never reaches the yaml that gets saved', () => {
    // 备注只活在浏览器里。它要是能进 qq.yaml，群名就会跟着进模型的上下文。
    writeAlias(700, '公司内部群');
    useConsoleStore.setState({
      live: {
        published: true,
        applied: true,
        file: { exists: true, mtime_ns: 1, size: 1 },
        state: { values: { allowed_groups: [] }, paths: { allowed_groups: 'channels.allowed_groups' } },
      },
      draft: {},
    });
    useConsoleStore.getState().setDraft('allowed_groups', [700]);

    const out = mergeDraft('', useConsoleStore.getState().draft, { allowed_groups: 'channels.allowed_groups' });

    expect(out).toContain('700');
    expect(out).not.toContain('公司内部群');
  });
});
