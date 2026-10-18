export const MOUNT = (() => {
  // 前缀本身可能含 web，控制台路径要从最后一段识别。
  const at = window.location.pathname.lastIndexOf('/web/');
  return at > 0 ? window.location.pathname.slice(0, at) : '';
})();

export function isInstancePath(value: unknown): value is string {
  return typeof value === 'string' && (value === '' || (
    /^\/[A-Za-z0-9_.~-]+(?:\/[A-Za-z0-9_.~-]+)*$/.test(value)
    && value.slice(1).split('/').every(part => part !== '.' && part !== '..')
  ));
}

export function instanceConsoleHref(path: string, preserveLocation = true): string {
  if (!isInstancePath(path)) throw new Error('实例地址无效');
  const query = new URLSearchParams(preserveLocation ? window.location.search : '');
  for (const key of [...query.keys()]) {
    if (['token', 'entry'].includes(key.toLowerCase())) query.delete(key);
  }
  const search = query.toString();
  return `${path}/web/${search ? `?${search}` : ''}`;
}
