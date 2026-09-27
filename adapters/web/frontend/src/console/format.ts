// 控制台各页共用的展示格式化。
import type { ConsoleFieldSchema } from '../api/supervisorClient';

const CJK_CHAR = /[\u3000-\u303f\u3400-\u9fff\uf900-\ufaff\uff00-\uffef\u{20000}-\u{3134f}]/u;

/** 名字后面接一句中文：名字以拉丁字母或数字结尾时隔一个空格（「NapCat 已连接」），以汉字结尾时直接连上（「平台已连接」）。 */
export function nameWith(name: string, text: string): string {
  if (!text) return name;
  // 按码点取：扩展区的汉字占两个 UTF-16 单元，slice(-1) 只拿到后半个。
  const last = Array.from(name).pop() || '';
  return !last || CJK_CHAR.test(last) ? `${name}${text}` : `${name} ${text}`;
}

/** 名字夹在两段中文之间，两侧各按名字的首尾定隔不隔空格：「这个 QQ 号」「这个微信号」。 */
export function nameBetween(before: string, name: string, after: string): string {
  const first = Array.from(name)[0] || '';
  const lead = !before || !first || CJK_CHAR.test(first) ? before : `${before} `;
  return `${lead}${nameWith(name, after)}`;
}

/** 比例换成百分数。0.15 * 100 会算出 15.000000000000002，定到小数点后两位。 */
export function asPercent(ratio: number): number {
  return Math.round(ratio * 10000) / 100;
}

/** 按控件说明把一个配置值写成给人看的样子：开关写开或关，数字按 scale 换算后带上单位。 */
export function fieldValueText(spec: Pick<ConsoleFieldSchema, 'kind' | 'scale' | 'unit'>, value: unknown): string {
  switch (spec.kind) {
    case 'bool':
      return value ? '开' : '关';
    case 'percent':
      return typeof value === 'number' ? `${asPercent(value)}%` : String(value);
    case 'number': {
      if (typeof value !== 'number') return String(value);
      const shown = spec.scale ? value / spec.scale : value;
      return spec.unit ? `${shown} ${spec.unit}` : String(shown);
    }
    default:
      return Array.isArray(value) ? value.join('、') : String(value ?? '');
  }
}

export function sizeText(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
