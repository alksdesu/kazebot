// 与后端 clonoth_runtime.resolve_env_ref 同一套写法：${VAR} / $ENV{VAR} / $ENV{NEW|OLD}。
// 前端只做识别和展示，展开永远在服务端。

// 后端先去掉首尾空白再认：$ENV{...} 里写什么都算引用，$ENV{} 展开成空串；${} 里没东西就原样当字面量。没闭合的不算引用。
const ENV_BLOCK = /^\$ENV\{(.*)\}$/;
const BRACED = /^\$\{(.+)\}$/;

/** 值是环境变量引用时给写着的变量名（$ENV{} 一个都没有），不是引用给 null。 */
export function parseEnvRef(value: string): string[] | null {
  const text = (value || '').trim();
  const match = ENV_BLOCK.exec(text) || BRACED.exec(text);
  if (!match) return null;
  return match[1].split('|').map((name) => name.trim()).filter(Boolean);
}

export function envRefNames(value: string): string[] {
  return parseEnvRef(value) ?? [];
}

/** 引用换成变量名给人看。一个变量名都没写的引用展开就是空串，给空串，由调用方摆它那一处没设置时的说法。 */
export function describeEnvRef(value: string): string {
  const names = parseEnvRef(value);
  if (!names) return value;
  return names.length ? `环境变量 ${names.join(' 或 ')}` : '';
}
