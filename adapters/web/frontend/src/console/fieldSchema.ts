// 平台分区的字段声明。页面只描述「有哪些键、长什么样」，怎么渲染由 FieldRenderer 决定。
// 复杂到编不出来的那几块（扫码、试听、能力清单）按平台加分区注册进组件表，用 custom 节点点名。
import type { ReactNode } from 'react';

import type { BlockFrame } from '../components/settings/pages/settingsControls';
import type { ListSpacing } from './WordList';

/** 三态开关的默认值可能由另一个键推导，比如信号跟随旧的 group_mode 枚举。 */
export type TristateFallback = boolean | { from: string; when: (value: unknown) => boolean };

export type FieldSpec =
  | { kind: 'block'; title?: string; hint?: string; frame?: BlockFrame; children: FieldSpec[] }
  | { kind: 'grid'; children: FieldSpec[] }
  | { kind: 'panel'; className?: string; children: FieldSpec[] }
  | { kind: 'loose'; children: FieldSpec[] }
  | { kind: 'bool'; key: string; label: string; desc?: string; fallback?: boolean; children?: FieldSpec[] }
  | { kind: 'tristate'; key: string; label: string; desc: string; fallback: TristateFallback }
  | { kind: 'sub'; key: string; label: string; fallback?: boolean }
  | { kind: 'number'; key: string; label: string; unit?: string; step?: number; max?: number; scale?: number }
  | { kind: 'percent'; key: string; label: string }
  | { kind: 'text'; key: string; label: string }
  | { kind: 'choice'; key: string; label: string; choices: ReadonlyArray<readonly [string, string]>; fallback: string }
  | { kind: 'words'; key: string; label: string; empty: string; placeholder: string; spacing?: ListSpacing }
  | { kind: 'ids'; key: string; label: string; empty: string; placeholder: string; spacing?: ListSpacing }
  | { kind: 'fixed'; label: string; desc?: string; children?: FieldSpec[] }
  | { kind: 'desc'; text: ReactNode; indent?: boolean }
  | { kind: 'note'; text: ReactNode }
  | { kind: 'custom'; name: string };

export type SectionComponents = Record<string, () => JSX.Element | null>;

const registry = new Map<string, SectionComponents>();

const slot = (platformId: string, sectionId: string): string => `${platformId}:${sectionId}`;

/** 登记一个分区里编不出来的组件。同一个分区重复登记按后来的算，热更新时才不会卡着旧的。 */
export function registerSectionComponents(
  platformId: string,
  sectionId: string,
  components: SectionComponents,
): void {
  registry.set(slot(platformId, sectionId), { ...registry.get(slot(platformId, sectionId)), ...components });
}

export function sectionComponent(
  platformId: string,
  sectionId: string,
  name: string,
): (() => JSX.Element | null) | null {
  return registry.get(slot(platformId, sectionId))?.[name] || null;
}
