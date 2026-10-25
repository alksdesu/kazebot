// 带令牌取文件：附件、预览图、导出件都走请求头鉴权，地址里不放令牌。
import { request, type RequestOptions } from './request';

export async function fetchBlob(url: string, options: RequestOptions = {}): Promise<Blob> {
  return (await request(url, options)).blob();
}

/** 交给浏览器存成文件。点击之后下载已经开始，object URL 下一轮就释放。 */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

export async function downloadFile(url: string, filename: string, options: RequestOptions = {}): Promise<void> {
  saveBlob(await fetchBlob(url, options), filename);
}

// 只有这些在新标签页里直接打开：blob 地址和控制台同源，SVG、HTML 打开就能以控制台身份跑脚本。
const INLINE_VIEWABLE_TYPES: ReadonlySet<string> = new Set([
  'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp', 'image/avif',
]);

export function isInlineViewable(type: string): boolean {
  return INLINE_VIEWABLE_TYPES.has(type.split(';')[0].trim().toLowerCase());
}
