// @vitest-environment node -- 只读源码文本，不需要 DOM。
// 控制台、设置页、布局三块的排版数值必须走 token；写死的任意值会绕开设计系统且无人发现。
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const SRC = resolve(__dirname, '..');
const ROOTS = ['console', 'components/settings', 'components/layout'];
const STYLESHEET = readFileSync(resolve(SRC, 'styles/index.css'), 'utf8');

/** Tailwind 任意值写法。结尾的 `(?!:)` 排掉 `aria-[...]`、`min-[...]:` 这类变体选择器。 */
const ARBITRARY = /[a-z][a-z0-9]*(?:-[a-z0-9]+)*-\[[^\]]+\](?!:)/g;

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.tsx?$/.test(name) ? [path] : [];
  });
}

describe('控制台排版 token', () => {
  it('三块界面里没有写死的任意值', () => {
    const offenders: string[] = [];
    for (const root of ROOTS) {
      for (const file of sources(join(SRC, root))) {
        readFileSync(file, 'utf8').split('\n').forEach((line, index) => {
          for (const hit of line.match(ARBITRARY) || []) {
            const value = hit.slice(hit.indexOf('[') + 1, -1);
            if (!/\d/.test(value) || value.includes('var(--')) continue;
            offenders.push(`${relative(SRC, file).replace(/\\/g, '/')}:${index + 1} ${hit}`);
          }
        });
      }
    }
    expect(offenders).toEqual([]);
  });

  it('字号、字距、动效由 @theme 出 utility', () => {
    for (const token of ['--text-mini', '--text-micro', '--text-nano', '--tracking-caps', '--tracking-figure', '--tracking-ident', '--animate-verdict-flip', '--animate-section-open', '--animate-section-fade']) {
      expect(STYLESHEET).toContain(`${token}:`);
    }
  });

  it('尺寸类 token 挂在 --duties-* 上', () => {
    for (const token of [
      '--duties-pane-height', '--duties-pane-height-tall', '--duties-pane-min-height',
      '--duties-label-min-width', '--duties-id-min-width', '--duties-count-width',
      '--duties-prose-max-width', '--duties-thumb-height', '--duties-marker-width',
      '--duties-audition-indent', '--duties-split-columns', '--duties-split-columns-narrow',
      '--duties-entry-columns', '--duties-thumb-grid', '--duties-app-height',
      '--duties-right-main-height', '--duties-right-log-height',
    ]) {
      expect(STYLESHEET).toContain(`${token}:`);
    }
  });
});
