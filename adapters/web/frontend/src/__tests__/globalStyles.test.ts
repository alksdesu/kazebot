// @vitest-environment node -- esbuild cannot run under jsdom, and the font test drives a real Vite build.
// Why: the Duties theme now owns global scrollbar styling, and regressions are easy to miss in component tests.
// How: read the global stylesheet as source text and assert both Firefox and WebKit/Blink scrollbar rules exist.
// Purpose: keep all scrollable panes aligned with the low-contrast Duties visual language without adding runtime code.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import tailwindcss from '@tailwindcss/vite';
import { build } from 'vite';
import { describe, expect, it } from 'vitest';

const stylesheetPath = resolve(__dirname, '../styles/index.css');
const stylesheet = readFileSync(stylesheetPath, 'utf8');

// 拉丁子集的三档。字重写在这里而不是从 css 里扫：接进 Tailwind 之后字重来自
// font-medium/font-semibold 这类 utility，css 文本里根本没有 font-weight 声明。
const FONT_WEIGHTS = ['400', '500', '600'];

type BuiltFile = { fileName: string; source?: string | Uint8Array };

async function buildStylesheet(): Promise<BuiltFile[]> {
  const result = (await build({
    configFile: false,
    logLevel: 'silent',
    plugins: [tailwindcss()],
    build: {
      write: false,
      rollupOptions: { input: stylesheetPath },
    },
  })) as unknown as { output: BuiltFile[] } | Array<{ output: BuiltFile[] }>;
  return Array.isArray(result) ? result[0].output : result.output;
}

describe('global Duties styles', () => {
  it('defines cross-browser custom scrollbar styling', () => {
    expect(stylesheet).toContain('scrollbar-width: thin;');
    expect(stylesheet).toContain('scrollbar-color: var(--duties-scrollbar-thumb) var(--duties-scrollbar-track);');
    expect(stylesheet).toContain('::-webkit-scrollbar');
    expect(stylesheet).toContain('width: 0.45rem;');
    expect(stylesheet).toContain('::-webkit-scrollbar-thumb:hover');
    expect(stylesheet).toContain('background: var(--duties-scrollbar-thumb-hover);');
  });
});

describe('IBM Plex Mono', () => {
  // 字体曾经只由 console.css 引入。那个文件一删，全站的等宽字体会静默退回系统栈，
  // 而 font-family 声明还在，看不出任何异常。
  it('is imported by the global stylesheet, not by any one surface', () => {
    expect(stylesheet).toContain('@fontsource/ibm-plex-mono/latin-400.css');
    expect(stylesheet).toContain('@fontsource/ibm-plex-mono/latin-500.css');
    expect(stylesheet).toContain('@fontsource/ibm-plex-mono/latin-600.css');
  });

  // 光有 font-family 声明说明不了什么：@import 可能在构建时被解析掉，
  // 留下一个没有 @font-face 的字体栈和一次无声的系统回退。
  it('ships one face per weight through the CSS build', async () => {
    const output = await buildStylesheet();

    const css = String(output.find((file) => file.fileName.endsWith('.css'))?.source ?? '');
    const faceBodies = [...css.matchAll(/@font-face\s*\{([^}]*)\}/g)].map((match) => match[1]);
    const shippedWeights = faceBodies
      .filter((body) => /font-family:\s*['"]?IBM Plex Mono['"]?/.test(body))
      .map((body) => body.match(/font-weight:\s*(\d+)/)?.[1] ?? '');

    expect([...shippedWeights].sort()).toEqual([...FONT_WEIGHTS].sort());

    const woff2 = output.filter((file) => file.fileName.endsWith('.woff2'));
    expect(woff2).toHaveLength(FONT_WEIGHTS.length);
  }, 30000);
});
