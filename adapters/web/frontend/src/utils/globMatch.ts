// 复刻 Python fnmatch.fnmatchcase 的语义，服务端 supervisor/policy.py 用的就是它。
// 关键差异：这里的 * 跨目录分隔符照吃，config/* 和 config/** 是同一回事 ——
// 按 shell glob 的直觉写匹配器，界面会告诉用户一条根本不会命中的规则命中了。

function escapeLiteral(ch: string): string {
  return /[\\^$.|?*+()[\]{}]/.test(ch) ? `\\${ch}` : ch;
}

/** 把一条 glob 编成正则。语法非法时按字面量处理，和 fnmatch 一样不抛错。 */
export function globToRegExp(pattern: string): RegExp {
  let out = '';
  let i = 0;
  while (i < pattern.length) {
    const ch = pattern[i];
    i += 1;
    if (ch === '*') {
      out += '.*';
    } else if (ch === '?') {
      out += '.';
    } else if (ch === '[') {
      let j = i;
      if (j < pattern.length && pattern[j] === '!') j += 1;
      // 紧跟在 [ 或 [! 后面的 ] 是字面量，不闭合字符类。
      if (j < pattern.length && pattern[j] === ']') j += 1;
      while (j < pattern.length && pattern[j] !== ']') j += 1;
      if (j >= pattern.length) {
        out += '\\[';
      } else {
        let body = pattern.slice(i, j);
        i = j + 1;
        if (body === '!') {
          out += '.';
        } else {
          // ] 在 Python 字符类里放首位就是字面量，JS 里必须转义，否则 []] 会被当成空类。
          body = body.replace(/\\/g, '\\\\').replace(/\]/g, '\\]');
          if (body.startsWith('!')) body = `^${body.slice(1)}`;
          else if (body.startsWith('^')) body = `\\${body}`;
          out += `[${body}]`;
        }
      }
    } else {
      out += escapeLiteral(ch);
    }
  }
  return new RegExp(`^${out}$`, 's');
}

export function globMatches(path: string, pattern: string): boolean {
  return globToRegExp(pattern).test(path);
}

/** 自上而下第一条命中的下标，没有命中返回 -1 —— 和 policy.py 的取舍顺序一致。 */
export function firstMatchIndex(path: string, patterns: readonly string[]): number {
  return patterns.findIndex((pattern) => globMatches(path, pattern));
}
