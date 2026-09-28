// 覆盖层里配置文件的来源标签，以及删掉本实例那份时的说法。节点、节点文件、工具三处共用。
import type { OverlaySource, ToolOrigin } from '../api/supervisorClient';

export function overlaySourceLabel(source: OverlaySource | undefined, sameAsDefault = false): string {
  switch (source) {
    case 'system':
      return '系统';
    case 'default':
      return '仓库默认';
    case 'override':
      return sameAsDefault ? '改过 · 与默认相同' : '改过';
    case 'custom':
      return '自建';
    default:
      return '';
  }
}

export function toolOriginLabel(origin: ToolOrigin | undefined): string {
  switch (origin) {
    case 'bundled':
      return '仓库自带';
    case 'override':
      return '改过';
    case 'custom':
      return '自建';
    default:
      return '';
  }
}

export interface RemovalAction {
  label: string;
  confirm: string;
  done: string;
  /** true = 删的是改过的仓库默认，删完回到默认，东西还在。 */
  restores: boolean;
}

/** 改过的仓库默认是恢复默认，自己加的是删除；没改过的仓库默认和系统的没有本实例的文件可删。 */
export function removalFor(source: OverlaySource | ToolOrigin | undefined, subject: string): RemovalAction | null {
  if (source === 'override') {
    return {
      label: '恢复默认',
      confirm: `把${subject}恢复成仓库默认？本实例对它的修改会删掉。`,
      done: `${subject}已恢复成仓库默认`,
      restores: true,
    };
  }
  if (source === 'custom') {
    return {
      label: '删除',
      confirm: `确定删除${subject}吗？对应的文件会被删掉。`,
      done: `${subject}已删除`,
      restores: false,
    };
  }
  return null;
}
