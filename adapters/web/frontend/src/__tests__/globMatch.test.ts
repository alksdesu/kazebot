// 对照 Python fnmatch.fnmatchcase 的实际输出写的。界面上「这条路径命中哪条规则」
// 要是和服务端算得不一样，那比不给这个功能更糟 —— 用户会照着错的答案去改策略。
import { describe, expect, it } from 'vitest';

import { firstMatchIndex, globMatches } from '../utils/globMatch';

const MATCHES: Array<[string, string]> = [
  ['data/napcat-account.json', 'data/napcat-account.*'],
  ['.env', '.env'],
  ['engine/.env', '**/.env'],
  ['a/b/.env', '**/.env'],
  ['config/nodes/qq.yaml', 'config/nodes/**'],
  ['config/nodes/sub/deep.yaml', 'config/nodes/**'],
  ['data/a/b/c', 'data/**'],
  ['tools/t.py', 'tools/**'],
  ['main.py', 'main.py'],
  ['.secret/key', '.secret/**'],
  ['abc', 'a?c'],
  ['ax', '[abc]x'],
  ['bx', '[abc]x'],
  ['dx', '[!abc]x'],
  [']x', '[!abc]x'],
  [']x', '[]]x'],
  ['unclosed[abc', 'unclosed[abc'],
];

const NON_MATCHES: Array<[string, string]> = [
  ['engine/x.py', 'config/**'],
  ['a/b/.env', '.env'],
  ['data/x', 'tools/**'],
  ['ax', '[!abc]x'],
];

describe('glob 匹配跟服务端一致', () => {
  it.each(MATCHES)('%s 命中 %s', (path, pattern) => {
    expect(globMatches(path, pattern)).toBe(true);
  });

  it.each(NON_MATCHES)('%s 不命中 %s', (path, pattern) => {
    expect(globMatches(path, pattern)).toBe(false);
  });

  it('星号连目录分隔符一起吃 —— config/* 和 config/** 是同一回事', () => {
    // 按 shell glob 的直觉，config/* 只该匹配一层。Python fnmatch 不是这样，
    // 而策略判定用的正是 fnmatch。这条错了，界面会说一条永不命中的规则命中了。
    expect(globMatches('config/nodes/qq.yaml', 'config/*')).toBe(true);
    expect(globMatches('a/b.py', '*.py')).toBe(true);
  });

  it('取第一条命中的，和策略自上而下的取舍一样', () => {
    const patterns = ['config/nodes/**', 'config/**', 'data/**'];

    expect(firstMatchIndex('config/nodes/qq.yaml', patterns)).toBe(0);
    expect(firstMatchIndex('config/runtime.yaml', patterns)).toBe(1);
    expect(firstMatchIndex('engine/x.py', patterns)).toBe(-1);
  });

  it('兜底规则排在前面就把后面的全挡了', () => {
    // 界面「添加」只会往末尾追加，而 read_file 最后一条正是兜底的 data/**，
    // 于是从界面给 data/ 下任何路径加规则都永远不生效。
    const patterns = ['data/**', 'data/attachments/**'];

    expect(firstMatchIndex('data/attachments/x.png', patterns)).toBe(0);
  });
});
