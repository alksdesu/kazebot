// Shared raw text editor for YAML, Markdown and Python settings files; callers own saving and discarding.
import type { ChangeEvent } from 'react';

interface YamlEditorProps {
  value: string;
  onChange: (value: string) => void;
  readOnly?: boolean;
  height?: number | string;
  placeholder?: string;
  'aria-label'?: string;
}

function normalizeEditorHeight(height: number | string): string {
  return typeof height === 'number' ? `${height}px` : height;
}

// Only the most common breakage is flagged here: YAML rejects tab indentation. Full parsing goes through js-yaml.
export function hasSuspiciousYamlTabs(value: string): boolean {
  return value.split('\n').some((line) => /^\t+/.test(line));
}

export const YamlEditor = ({ value, onChange, readOnly = false, height = '300px', placeholder, 'aria-label': ariaLabel = 'YAML 编辑器' }: YamlEditorProps) => (
  <textarea
    aria-label={ariaLabel}
    className="w-full resize-y border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3 font-mono text-xs leading-5 text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)] disabled:opacity-70"
    onChange={(event: ChangeEvent<HTMLTextAreaElement>) => onChange(event.target.value)}
    placeholder={placeholder}
    readOnly={readOnly}
    spellCheck={false}
    style={{ height: normalizeEditorHeight(height) }}
    value={value}
  />
);
