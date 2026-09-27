// 控制台各页共用的展示格式化。

const CJK_CHAR = /[\u3000-\u303f\u3400-\u9fff\uf900-\ufaff\uff00-\uffef\u{20000}-\u{3134f}]/u;

/** 名字后面接一句中文：名字以拉丁字母或数字结尾时隔一个空格（「NapCat 已连接」），以汉字结尾时直接连上（「平台已连接」）。 */
export function nameWith(name: string, text: string): string {
  // 按码点取：扩展区的汉字占两个 UTF-16 单元，slice(-1) 只拿到后半个。
  const last = Array.from(name).pop() || '';
  return !last || CJK_CHAR.test(last) ? `${name}${text}` : `${name} ${text}`;
}

export function sizeText(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
