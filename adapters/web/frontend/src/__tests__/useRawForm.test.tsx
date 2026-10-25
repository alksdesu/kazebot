// 「表单 + 原文」共用一份原文：表单改动立刻写进原文，原文改了表单跟着变，写不进去或解析不了时不让保存。
import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { editYamlKeys, parseYamlMapping, WHOLE_FILE_REWRITTEN } from '../console/yamlRewrite';
import { unsavableReason, useRawForm, type RawFormCodec } from '../hooks/useRawForm';

interface Form {
  name: string;
  count: string;
}

const CODEC: RawFormCodec<Form> = {
  parse: (raw) => {
    const doc = parseYamlMapping(raw);
    return { name: String(doc.name ?? ''), count: doc.count === undefined ? '' : String(doc.count) };
  },
  serialize: (raw, form) => {
    if (form.count !== '' && !/^\d+$/.test(form.count)) throw new Error('count 必须是整数');
    return editYamlKeys(raw, [
      { path: ['name'], value: form.name || undefined },
      { path: ['count'], value: form.count === '' ? undefined : Number(form.count) },
    ]);
  },
};

const EMPTY: Form = { name: '', count: '' };
const RAW = '# 说明\nname: a # 名字\ncount: 1\n';

function setup(raw = RAW) {
  const hook = renderHook(() => useRawForm(CODEC, EMPTY));
  act(() => hook.result.current.reset(raw));
  return hook;
}

describe('useRawForm', () => {
  it('读入后表单从原文解析出来，没有改动', () => {
    const { result } = setup();

    expect(result.current.form).toEqual({ name: 'a', count: '1' });
    expect(result.current.dirty).toBe(false);
    expect(unsavableReason(result.current)).toBe('');
  });

  it('表单改一处就写进原文，注释照旧', () => {
    const { result } = setup();

    act(() => result.current.update({ name: 'b' }));

    expect(result.current.raw).toBe('# 说明\nname: b # 名字\ncount: 1\n');
    expect(result.current.form).toEqual({ name: 'b', count: '1' });
    expect(result.current.dirty).toBe(true);
  });

  it('原文改了表单跟着重新解析', () => {
    const { result } = setup();

    act(() => result.current.setRaw('name: c\ncount: 5\n'));

    expect(result.current.form).toEqual({ name: 'c', count: '5' });
    expect(result.current.dirty).toBe(true);
  });

  it('改回读入时的样子就不算改动', () => {
    const { result } = setup();

    act(() => result.current.update({ name: 'b' }));
    act(() => result.current.update({ name: 'a' }));

    expect(result.current.raw).toBe(RAW);
    expect(result.current.dirty).toBe(false);
  });

  it('原文解析不了时表单停在上一次的样子，改表单也不动原文，不让保存', () => {
    const { result } = setup();

    act(() => result.current.setRaw('name: [\n'));
    act(() => result.current.update({ name: 'zzz' }));

    expect(result.current.raw).toBe('name: [\n');
    expect(result.current.form).toEqual({ name: 'a', count: '1' });
    expect(result.current.parseError).toMatch(/^原文不是合法的 YAML/);
    expect(unsavableReason(result.current)).toBe(`没有保存：原文解析不了。${result.current.parseError}`);

    act(() => result.current.setRaw('name: d\n'));
    expect(result.current.parseError).toBe('');
    expect(result.current.form).toEqual({ name: 'd', count: '' });
  });

  it('写不进原文的值留在表单上，原文不变，改好之前不让保存', () => {
    const { result } = setup();

    act(() => result.current.update({ count: '1.5' }));

    expect(result.current.raw).toBe(RAW);
    expect(result.current.form.count).toBe('1.5');
    expect(result.current.dirty).toBe(true);
    expect(unsavableReason(result.current)).toBe('没有保存：表单里的改动还没写进原文。count 必须是整数');

    act(() => result.current.update({ count: '2' }));
    expect(result.current.editError).toBe('');
    expect(result.current.raw).toBe('# 说明\nname: a # 名字\ncount: 2\n');
  });

  // 原文是保存的那一份：在原文里改过之后，表单上写不进去的那处改动就作废了。
  it('改原文会收掉表单上写不进去的那处改动', () => {
    const { result } = setup();

    act(() => result.current.update({ count: 'x' }));
    act(() => result.current.setRaw('name: a\ncount: 3\n'));

    expect(result.current.editError).toBe('');
    expect(result.current.form).toEqual({ name: 'a', count: '3' });
  });

  it('整份重写但原文没有注释和空行可丢时不提醒', () => {
    const { result } = setup('{name: a, count: 1}\n');

    act(() => result.current.update({ name: 'b' }));
    expect(result.current.raw).toBe('name: b\ncount: 1\n');
    expect(result.current.note).toBe('');
  });

  it('注释没保住的交代一直留到下次读入', () => {
    const { result } = setup('# 名字和计数\n{name: a, count: 1}\n');

    act(() => result.current.update({ name: 'b' }));
    expect(result.current.raw).toBe('name: b\ncount: 1\n');
    expect(result.current.note).toBe(WHOLE_FILE_REWRITTEN);

    act(() => result.current.update({ name: 'c' }));
    expect(result.current.note).toBe(WHOLE_FILE_REWRITTEN);

    act(() => result.current.reset('name: c\ncount: 1\n'));
    expect(result.current.note).toBe('');
    expect(result.current.dirty).toBe(false);
  });
});
