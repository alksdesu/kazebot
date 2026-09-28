// 多开时「这个号归谁管」的判定。判错的后果是同一个号被两个实例双登，
// 互相踢下线且数据分成两份，所以这里逐条钉住。
import { describe, expect, it } from 'vitest';

import {
  attachmentHref,
  instanceConsoleHref,
  instanceOwning,
  isValidInstanceId,
  ownedByAnotherInstance,
  type ConsoleInstance,
} from '../api/supervisorClient';

const row = (
  id: string,
  path: string,
  current: boolean,
  platform?: string,
  account?: string,
): ConsoleInstance => ({ id, label: `bot-${id}`, path, current, platform, account });

describe('isValidInstanceId', () => {
  it.each(['xiaoyu', 'bot_2', '1000000002', 'ab', 'a'.repeat(32), '10000'])('收下 %s', id => {
    expect(isValidInstanceId(id)).toBe(true);
  });

  it.each([
    ['stickers', '数据根下的共享表情包库占着这个名字'],
    ['Xiaoyu', '大写'],
    ['bot-2', '连字符'],
    ['a', '单字符'],
    ['a'.repeat(33), '33 字符'],
    ['0123456', '前导零'],
    ['1abc', '数字开头又混字母'],
    ['1234', '太短的纯数字'],
    ['', '空名'],
  ])('挡掉 %s（%s）', id => {
    expect(isValidInstanceId(id)).toBe(false);
  });
});

describe('instanceOwning', () => {
  it('本实例登记的号也算归它管', () => {
    const instances = [row('main', '', true, 'qq', '111')];
    expect(instanceOwning('qq', '111', instances)?.id).toBe('main');
  });

  it('别的实例登记的号返回那个实例', () => {
    const instances = [row('main', '', true, 'qq', '111'), row('xiaoyu', '/i/xiaoyu', false, 'qq', '222')];
    expect(instanceOwning('qq', '222', instances)?.path).toBe('/i/xiaoyu');
  });

  it('空账号谁都不归 —— 后端没登记时字段就是空串', () => {
    const instances = [row('main', '', true, 'qq', '')];
    expect(instanceOwning('qq', '', instances)).toBeNull();
  });

  it('别的平台记着同一串数字不算', () => {
    const instances = [row('xiaoyu', '/i/xiaoyu', false, 'telegram', '222')];
    expect(instanceOwning('qq', '222', instances)).toBeNull();
  });

  it('老清单没有 platform 时谁都不归它', () => {
    expect(instanceOwning('qq', '222', [row('xiaoyu', '/i/xiaoyu', false)])).toBeNull();
  });

  it('同一个号登记在多条上时取第一条', () => {
    const instances = [row('a', '/i/a', false, 'qq', '222'), row('b', '/i/b', false, 'qq', '222')];
    expect(instanceOwning('qq', '222', instances)?.id).toBe('a');
  });
});

describe('ownedByAnotherInstance', () => {
  it('单实例部署（清单为空）时谁都不拦', () => {
    // 清单读不到也走这条路：宁可不拦，也不能把所有号都锁死。
    expect(ownedByAnotherInstance('qq', '1000000001', [])).toBeNull();
  });

  it('号归别的实例时返回那个实例', () => {
    const instances = [row('main', '', true, 'qq', '111'), row('xiaoyu', '/i/xiaoyu', false, 'qq', '222')];
    expect(ownedByAnotherInstance('qq', '222', instances)?.path).toBe('/i/xiaoyu');
  });

  it('本实例自己的号不拦', () => {
    const instances = [row('main', '', true, 'qq', '111'), row('xiaoyu', '/i/xiaoyu', false, 'qq', '222')];
    expect(ownedByAnotherInstance('qq', '111', instances)).toBeNull();
  });

  it('不在清单里的号不拦', () => {
    expect(ownedByAnotherInstance('qq', '999', [row('main', '', true, 'qq', '111')])).toBeNull();
  });

  it('账号按字符串精确比，不做数字宽松匹配', () => {
    const instances = [row('xiaoyu', '/i/xiaoyu', false, 'qq', '111')];
    expect(ownedByAnotherInstance('qq', '1110', instances)).toBeNull();
    expect(ownedByAnotherInstance('qq', '11', instances)).toBeNull();
  });

  it('只按账号判，实例名撞上同一串数字不算', () => {
    const instances = [row('1000002', '/i/1000002', false, 'qq', '2000002')];
    expect(ownedByAnotherInstance('qq', '1000002', instances)).toBeNull();
  });

  it('别的平台的同号不拦', () => {
    const instances = [row('xiaoyu', '/i/xiaoyu', false, 'telegram', '222')];
    expect(ownedByAnotherInstance('qq', '222', instances)).toBeNull();
  });

  it('空账号不拦', () => {
    const instances = [row('xiaoyu', '/i/xiaoyu', false, 'qq', '')];
    expect(ownedByAnotherInstance('qq', '', instances)).toBeNull();
  });
});

describe('instanceConsoleHref', () => {
  it('根实例拼出的是站点根下的控制台', () => {
    expect(instanceConsoleHref('')).toBe('/web/');
  });

  it('带前缀的实例拼在前缀下', () => {
    expect(instanceConsoleHref('/i/222')).toBe('/i/222/web/');
  });
});

describe('attachmentHref', () => {
  it('把工作区相对路径挂到附件端点下', () => {
    expect(attachmentHref('data/attachments/g/a.png', '')).toBe('/v1/files/data/attachments/g/a.png');
  });

  it('带 token 时走 query —— img 标签带不了请求头', () => {
    expect(attachmentHref('data/attachments/a.png', 'tok en')).toBe(
      '/v1/files/data/attachments/a.png?token=tok%20en',
    );
  });

  it('逐段转义，斜杠保持为路径分隔符', () => {
    expect(attachmentHref('data/attachments/g 1/a b.png', '')).toBe(
      '/v1/files/data/attachments/g%201/a%20b.png',
    );
  });
});
