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

const consoleStylesheetPath = resolve(__dirname, '../console/console.css');
const consoleStylesheet = readFileSync(consoleStylesheetPath, 'utf8');

// 400 is never spelled out: it is what every console element falls back to.
const consoleFontWeights = new Set([
  '400',
  ...[...consoleStylesheet.matchAll(/font-weight:\s*(\d+)/g)].map((match) => match[1]),
]);

type BuiltFile = { fileName: string; source?: string | Uint8Array };

async function buildConsoleStylesheet(): Promise<BuiltFile[]> {
  const result = (await build({
    configFile: false,
    logLevel: 'silent',
    plugins: [tailwindcss()],
    build: {
      write: false,
      rollupOptions: { input: consoleStylesheetPath },
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

describe('console monospace font', () => {
  it('names IBM Plex Mono first in the console monospace stack', () => {
    const stack = consoleStylesheet.match(/--qc-mono:\s*([^;]+);/)?.[1];
    expect(stack?.split(',')[0].trim()).toBe("'IBM Plex Mono'");
  });

  // A font-family declaration proves nothing on its own: an @import can be resolved away
  // during the CSS build, leaving the stack with no @font-face and a silent system fallback.
  it('ships one IBM Plex Mono face per console font weight through the CSS build', async () => {
    const output = await buildConsoleStylesheet();

    const css = String(output.find((file) => file.fileName.endsWith('.css'))?.source ?? '');
    const faceBodies = [...css.matchAll(/@font-face\s*\{([^}]*)\}/g)].map((match) => match[1]);
    const shippedWeights = faceBodies
      .filter((body) => /font-family:\s*['"]?IBM Plex Mono['"]?/.test(body))
      .map((body) => body.match(/font-weight:\s*(\d+)/)?.[1] ?? '');

    expect([...shippedWeights].sort()).toEqual([...consoleFontWeights].sort());

    const woff2 = output.filter((file) => file.fileName.endsWith('.woff2'));
    expect(woff2).toHaveLength(consoleFontWeights.size);
  }, 30000);
});
