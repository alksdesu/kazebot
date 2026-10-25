// @vitest-environment node -- 只读源码文本，不需要 DOM。
// 颜色只从主题变量取：写死的调色板色和奶油底的深浅对不上，换主题时也没人记得改。
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

import { TONE_BOX, type Tone } from '../styles/tones';

const SRC = resolve(__dirname, '..');
const STYLESHEET = readFileSync(resolve(SRC, 'styles/index.css'), 'utf8');

const PALETTE = 'slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose';
const UTILITY = 'bg|text|border(?:-[trblxyse])?|ring|ring-offset|outline|divide|fill|stroke|from|via|to|decoration|placeholder|accent|caret|shadow';
/** 调色板色和纯黑纯白，带不带透明度、挂不挂 hover: 这类变体都算。 */
const HARDCODED = new RegExp(`(?<![\\w-])(?:${UTILITY})-(?:(?:${PALETTE})-\\d{2,3}|white|black)(?:/\\d+)?(?![\\w-])`, 'g');

const SEMANTIC_TONES = ['success', 'danger', 'warning', 'info', 'meta'];

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === '__tests__' ? [] : sources(path);
    return /\.tsx?$/.test(name) ? [path] : [];
  });
}

const where = (file: string): string => relative(SRC, file).replace(/\\/g, '/');

const SOURCES = sources(SRC);

const DEFINED = new Set([...STYLESHEET.matchAll(/(--duties-[a-z0-9-]+)\s*:/g)].map((match) => match[1]));

describe('颜色走主题变量', () => {
  it('源码里没有写死的调色板色', () => {
    const offenders = SOURCES.flatMap((file) => readFileSync(file, 'utf8').split('\n').flatMap((line, index) => (
      (line.match(HARDCODED) || []).map((hit) => `${where(file)}:${index + 1} ${hit}`)
    )));

    expect(offenders).toEqual([]);
  });

  it('认得出变体和透明度写法', () => {
    for (const sample of ['hover:bg-red-100', 'border-green-500/40', 'bg-white/45', 'text-black', 'border-l-blue-400']) {
      expect(sample.match(HARDCODED), sample).not.toBeNull();
    }
    for (const sample of ['bg-[var(--duties-danger-bg)]', 'border-current/10', 'text-xs', 'border-l-2']) {
      expect(sample.match(HARDCODED), sample).toBeNull();
    }
  });

  it('引用的 --duties-* 都在样式表里定义过', () => {
    // 变量名拼错一个字母，浏览器不报错，那一处就退回成继承色。
    const missing = SOURCES.flatMap((file) => [...readFileSync(file, 'utf8').matchAll(/var\((--duties-[a-z0-9-]+)\)/g)]
      .map((match) => match[1])
      .filter((token) => !DEFINED.has(token))
      .map((token) => `${where(file)} ${token}`));

    expect([...new Set(missing)]).toEqual([]);
  });

  it('每组语义色都有字色、底色、边框、悬停四档', () => {
    for (const tone of SEMANTIC_TONES) {
      for (const suffix of ['', '-bg', '-border', '-hover']) {
        expect(DEFINED, `--duties-${tone}${suffix}`).toContain(`--duties-${tone}${suffix}`);
      }
    }
  });

  it('成套的框、底、字一次给齐，同一属性不叠两个', () => {
    for (const [tone, classes] of Object.entries(TONE_BOX) as Array<[Tone, string]>) {
      const utilities = classes.split(' ').map((name) => name.slice(0, name.indexOf('-[')));
      expect(utilities.sort(), tone).toEqual(['bg', 'border', 'text']);
    }
  });
});
