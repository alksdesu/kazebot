// 关键词输入框按逗号拆，写成 /正则/ 的那段整段算一个：拆开的两截都不成正则，存下的和召回时认的就对不上了。
import { describe, expect, it } from 'vitest';

import { splitKeywords } from '../utils/keywords';

describe('splitKeywords', () => {
  it('splits plain words on commas and drops the empty ones', () => {
    expect(splitKeywords(' 早安, 晚安 ,,午安, ')).toEqual(['早安', '晚安', '午安']);
  });

  it.each([
    [String.raw`/\d{1,3}/, 房间`, [String.raw`/\d{1,3}/`, '房间']],
    ['/早,?安/i, 问候', ['/早,?安/i', '问候']],
    ['/a{1, 3}/', ['/a{1, 3}/']],
    ['/a,b/, /c,d/sm', ['/a,b/', '/c,d/sm']],
    ['前缀, /x,y/', ['前缀', '/x,y/']],
  ])('keeps a /regex/ with commas in it as one word (%j)', (text, words) => {
    expect(splitKeywords(text)).toEqual(words);
  });

  // 注入时只有整段形如 /pattern/flags 才当正则，flags 只认 i、s、m；凑不成的照普通词拆。
  it.each([
    ['/api, config', ['/api', 'config']],
    ['/api/config, 早安', ['/api/config', '早安']],
    ['/a,b/g', ['/a', 'b/g']],
  ])('splits a slash-led piece that never closes into a /regex/ like any other word (%j)', (text, words) => {
    expect(splitKeywords(text)).toEqual(words);
  });

  // 「/help, /sim」接起来正好像一段带 s、i、m 的正则，其实是两条命令。
  it.each([
    ['/help, /sim', ['/help', '/sim']],
    [String.raw`/help, /\d+/`, ['/help', String.raw`/\d+/`]],
  ])('a piece that starts with a slash begins the next word (%j)', (text, words) => {
    expect(splitKeywords(text)).toEqual(words);
  });
});
