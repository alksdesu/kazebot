// 结构化表单写回配置文件只走这一条：读最新一版，只改这次动过的键，带着这一版的指纹写回。
// 读和写之间又被改过时后端回 409；给了打开时的原文，还会逐键核对这几个键在那之后有没有被别处改过。
import { ApiError } from '../api/request';
import { getRuntimeFile, updateRuntimeRaw, type RuntimeFile } from '../api/supervisorClient';
import { editYamlKeys, parseYamlMapping, readYamlPath, sameData, type YamlKeyEdit } from './yamlRewrite';

export interface YamlSource {
  content: string;
  hash: string | null;
}

export interface YamlCommit<R> {
  /** 写进去的原文。 */
  content: string;
  /** 注释没保住时交代的那句，保住了是空串。 */
  note: string;
  result: R;
}

export interface YamlCommitOptions<S extends YamlSource, R> {
  read: () => Promise<S>;
  write: (content: string, hash: string | null) => Promise<R>;
  /** 给函数时按读到的最新原文现算：列表里的一项要按 id 找它现在排第几，加删也要以最新那份为底。 */
  edits: readonly YamlKeyEdit[] | ((base: string) => readonly YamlKeyEdit[]);
  /** 打开编辑时读到的原文。给了就逐键核对：要改的键在那之后被别处改过，就报冲突，不覆盖。 */
  loaded?: string;
  /** 在哪份原文上改；默认就是读到的那份。 */
  baseOf?: (source: S) => string;
  /** 读回来、写之前再问一次：换了登录或页面已经关了就不写，抛 AbortError。 */
  proceed?: () => boolean;
}

export type RuntimeCommitOptions = Pick<YamlCommitOptions<RuntimeFile, unknown>, 'loaded' | 'proceed'>;

/** 要改的键在打开之后被别处改过。和后端的 409 一样算编辑冲突，只是发现得更早、说得出是哪几个键。 */
export class StaleKeysError extends ApiError {
  readonly paths: string[];

  constructor(paths: string[]) {
    super(409, `${paths.join('、')} 在打开之后被别处改过，没有覆盖`);
    this.name = 'StaleKeysError';
    this.paths = paths;
  }
}

function assertUntouched(loaded: string, latest: string, edits: readonly YamlKeyEdit[]): void {
  const before = parseYamlMapping(loaded);
  const now = parseYamlMapping(latest);
  const moved = edits.filter((edit) => !sameData(readYamlPath(before, edit.path), readYamlPath(now, edit.path)));
  if (moved.length) throw new StaleKeysError(moved.map((edit) => edit.path.join('.')));
}

export async function commitYamlKeys<S extends YamlSource, R>(options: YamlCommitOptions<S, R>): Promise<YamlCommit<R>> {
  const latest = await options.read();
  if (options.proceed && !options.proceed()) throw new DOMException('写回已取消', 'AbortError');
  const base = options.baseOf ? options.baseOf(latest) : latest.content;
  const edits = typeof options.edits === 'function' ? options.edits(base) : options.edits;
  if (options.loaded !== undefined) {
    // 要逐键核对的编辑器也得能核对整份：没有指纹就没法发现读写之间的改动。
    if (typeof latest.hash !== 'string') throw new Error('服务端未返回配置版本，无法安全保存，请更新服务后重试。');
    assertUntouched(options.loaded, latest.content, edits);
  }
  const { content, note } = editYamlKeys(base, edits);
  const result = await options.write(content, latest.hash);
  return { content, note, result };
}

/** 本实例那一层 runtime.yaml。还没有自己那份时读到的是仓库默认，写回要从空文档开始，
 *  否则默认全文会被抄成本实例的一份，之后仓库默认再怎么改都盖不过来。 */
export const runtimeInstanceLayer = (file: RuntimeFile): string => (file.source === 'override' ? file.content : '');

export function commitRuntimeKeys(token: string, edits: readonly YamlKeyEdit[], options: RuntimeCommitOptions = {}) {
  return commitYamlKeys({
    ...options,
    read: () => getRuntimeFile(token),
    write: (content, hash) => updateRuntimeRaw(token, content, hash),
    edits,
    baseOf: runtimeInstanceLayer,
  });
}
