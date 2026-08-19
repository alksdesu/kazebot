// 与后端 clonoth_runtime.resolve_env_ref 同一套写法：${VAR} / $ENV{VAR} / $ENV{NEW|OLD}。
// 前端只做识别和展示，展开永远在服务端。

// 严格锚定：要摘出变量名就得结构完整。${} 和 $ENV{} 不算引用，后端同样把它们当字面量。
const ENV_REF = /^\$(?:ENV)?\{(.+)\}$/;

export function envRefNames(value: string): string[] {
  const match = ENV_REF.exec((value || '').trim());
  if (!match) return [];
  return match[1].split('|').map((name) => name.trim()).filter(Boolean);
}

export function describeEnvRef(value: string): string {
  const names = envRefNames(value);
  return names.length ? `环境变量 ${names.join(' 或 ')}` : value;
}
