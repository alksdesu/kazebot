// 「表单 + 原文」双编辑器共用一份原文：表单每改一处立刻写进原文，原文改了表单跟着重新解析，保存的永远是原文。
import { useCallback, useMemo, useState } from 'react';

import type { YamlRewrite } from '../console/yamlRewrite';

export interface RawFormCodec<F> {
  parse: (raw: string) => F;
  /** 把表单写进原文，只动和原文不一样的字段。表单里有写不进去的值就抛，原文不变。 */
  serialize: (raw: string, form: F) => YamlRewrite;
}

interface RawFormState<F> {
  raw: string;
  /** 最近一次读入或保存的原文。 */
  saved: string;
  /** 表单上显示的值。保留输入框里的原样文字，原文解析不了时停在最后一次解析成功的样子。 */
  form: F;
  /** 原文解析不了的原因。不为空时表单只读，先在原文里修好。 */
  parseError: string;
  /** 表单最近的改动写不进原文的原因。不为空时不能保存。 */
  editError: string;
  /** 表单改动没保住注释时的交代，留到下一次读入或保存。 */
  note: string;
}

export interface RawForm<F> extends RawFormState<F> {
  dirty: boolean;
  /** 读入或保存之后调用：这一版成了新的基线，表单从它重新解析。 */
  reset: (raw: string) => void;
  setRaw: (raw: string) => void;
  update: (patch: Partial<F>) => void;
}

const reason = (error: unknown): string => (error instanceof Error ? error.message : String(error));

/** 现在不能保存的原因，能保存时是空串。 */
export function unsavableReason(source: Pick<RawFormState<unknown>, 'parseError' | 'editError'>): string {
  if (source.parseError) return `没有保存：原文解析不了。${source.parseError}`;
  if (source.editError) return `没有保存：表单里的改动还没写进原文。${source.editError}`;
  return '';
}

function parsed<F>(codec: RawFormCodec<F>, raw: string, fallback: F): Pick<RawFormState<F>, 'form' | 'parseError'> {
  try {
    return { form: codec.parse(raw), parseError: '' };
  } catch (error) {
    return { form: fallback, parseError: reason(error) };
  }
}

export function useRawForm<F>(codec: RawFormCodec<F>, initial: F): RawForm<F> {
  const [state, setState] = useState<RawFormState<F>>(() => ({
    raw: '', saved: '', form: initial, parseError: '', editError: '', note: '',
  }));

  const reset = useCallback((raw: string) => {
    setState((current) => ({ raw, saved: raw, ...parsed(codec, raw, current.form), editError: '', note: '' }));
  }, [codec]);

  // 原文改动覆盖表单上还没写进去的那处改动：保存的是原文，表单只是它的另一种看法。
  const setRaw = useCallback((raw: string) => {
    setState((current) => ({ ...current, raw, ...parsed(codec, raw, current.form), editError: '' }));
  }, [codec]);

  const update = useCallback((patch: Partial<F>) => {
    setState((current) => {
      if (current.parseError) return current;
      const form = { ...current.form, ...patch };
      try {
        const { content, note } = codec.serialize(current.raw, form);
        return { ...current, raw: content, form, editError: '', note: note || current.note };
      } catch (error) {
        return { ...current, form, editError: reason(error) };
      }
    });
  }, [codec]);

  return useMemo(() => ({
    ...state,
    dirty: state.raw !== state.saved || state.editError !== '',
    reset,
    setRaw,
    update,
  }), [state, reset, setRaw, update]);
}
