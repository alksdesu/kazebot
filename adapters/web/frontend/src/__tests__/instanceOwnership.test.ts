// 多开时「这个号归谁管」的判定。判错的后果是同一个 QQ 被两个实例双登，
// 互相踢下线且数据分成两份，所以这里逐条钉住。
import { describe, expect, it } from 'vitest';

import {
  attachmentHref,
  instanceConsoleHref,
  ownedByAnotherInstance,
  type ConsoleInstance,
} from '../api/supervisorClient';

const row = (uin: string, path: string, current: boolean): ConsoleInstance => ({
  uin,
  label: `bot-${uin}`,
  path,
  current,
});

describe('ownedByAnotherInstance', () => {
  it('单实例部署（清单为空）时谁都不拦', () => {
    // 清单读不到也走这条路：宁可不拦，也不能把所有号都锁死。
    expect(ownedByAnotherInstance('1000000001', [])).toBeNull();
  });

  it('号归别的实例时返回那个实例', () => {
    const instances = [row('111', '', true), row('222', '/i/222', false)];
    expect(ownedByAnotherInstance('222', instances)?.path).toBe('/i/222');
  });

  it('本实例自己的号不拦', () => {
    const instances = [row('111', '', true), row('222', '/i/222', false)];
    expect(ownedByAnotherInstance('111', instances)).toBeNull();
  });

  it('不在清单里的号不拦', () => {
    const instances = [row('111', '', true)];
    expect(ownedByAnotherInstance('999', instances)).toBeNull();
  });

  it('uin 按字符串精确比，不做数字宽松匹配', () => {
    const instances = [row('111', '/i/111', false)];
    expect(ownedByAnotherInstance('1110', instances)).toBeNull();
    expect(ownedByAnotherInstance('11', instances)).toBeNull();
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
