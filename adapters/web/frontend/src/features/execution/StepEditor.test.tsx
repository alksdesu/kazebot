import { useState } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { StepEditor } from './StepEditor';
import { readExecutionTools, type PlanStep, type ToolOption } from './types';

const tools: ToolOption[] = [{ name: 'example', description: '测试工具的详细说明', effect: 'read', input_schema: { required: ['count'], properties: {
  enabled: { type: 'boolean', description: '是否启用' }, count: { type: 'number', description: '数量' }, options: { type: 'object', description: '配置对象' },
} } }];
function Editor({ changed = () => {}, valid = () => {} }: { changed?: (steps: PlanStep[]) => void; valid?: (value: boolean) => void }) {
  const [steps, setSteps] = useState<PlanStep[]>([{ id: 'one', title: '第一步', kind: 'tool', operation: 'example', arguments: { enabled: false, count: 1, options: {} }, dependencies: [], input_index: null, instruction: '', status: 'pending', error: '', result: '', attempt: 0, task_id: '' }]);
  return <StepEditor steps={steps} tools={tools} onChange={next => { setSteps(next); changed(next); }} onValidityChange={valid} />;
}

describe('typed step parameters', () => {
  it('changes boolean values without requiring text entry', () => {
    const changed = vi.fn(); render(<Editor changed={changed} />);
    fireEvent.change(screen.getByLabelText('是否启用'), { target: { value: 'true' } });
    expect(changed.mock.lastCall?.[0][0].arguments.enabled).toBe(true);
    expect(screen.getByLabelText('是否启用')).toHaveValue('true');
  });
  it('preserves numeric and JSON editing states and reports invalid input', async () => {
    const valid = vi.fn(); const changed = vi.fn(); render(<Editor changed={changed} valid={valid} />);
    const count = screen.getByLabelText('数量');
    fireEvent.change(count, { target: { value: '-' } });
    expect(count).toHaveValue('-'); expect(await screen.findByText('请输入有效数字')).toBeInTheDocument();
    await waitFor(() => expect(valid).toHaveBeenLastCalledWith(false));
    for (const value of ['1', '1.', '1.5']) fireEvent.change(count, { target: { value } });
    expect(count).toHaveValue('1.5'); expect(changed.mock.lastCall?.[0][0].arguments.count).toBe(1.5);
    const options = screen.getByLabelText('配置对象');
    fireEvent.change(options, { target: { value: '{' } }); expect(options).toHaveValue('{');
    await waitFor(() => expect(valid).toHaveBeenLastCalledWith(false));
    fireEvent.change(options, { target: { value: '{"mode":"safe"}' } });
    await waitFor(() => expect(valid).toHaveBeenLastCalledWith(true));
    expect(changed.mock.lastCall?.[0][0].arguments.options).toEqual({ mode: 'safe' });
  });
  it('allows adding a step after deleting the final step', () => {
    render(<Editor />); fireEvent.click(screen.getByRole('button', { name: '删除此步骤' }));
    fireEvent.click(screen.getByRole('button', { name: '添加步骤' }));
    expect(screen.getByLabelText('步骤 1 名称')).toHaveValue('新步骤');
  });
});

const TOOLS = '可用工具';
const tool = (name: string, input_schema: unknown) => ({ name, description: '', effect: 'read', input_schema });

describe('one tool with a schema the editor cannot read', () => {
  it('stays listed with what is wrong, and the other tools stay usable', () => {
    const read = readExecutionTools(TOOLS, { tools: [
      tool('fine', { properties: { path: { type: 'string', description: '路径' } }, required: ['path'] }),
      tool('loose_required', { properties: {}, required: 'path' }),
      tool('loose_enum', { properties: { mode: { enum: 'fast' } } }),
      tool('described_by_object', { properties: { mode: { description: { zh: '模式' } } } }),
      tool('nullable', { properties: { note: { type: ['string', 'null'] } } }),
    ] });
    expect(read.map(item => [item.name, item.schemaProblem])).toEqual([
      ['fine', undefined],
      ['loose_required', '参数说明格式不对：input_schema.required 不是字符串数组'],
      ['loose_enum', '参数说明格式不对：input_schema.properties.mode.enum 不是数组'],
      ['described_by_object', '参数说明格式不对：input_schema.properties.mode.description 不是字符串'],
      ['nullable', undefined],
    ]);
  });
  it('still refuses a reply that is not a tool list at all', () => {
    expect(() => readExecutionTools(TOOLS, { tools: [tool('', {})] })).toThrow('可用工具格式不对：tools[0].name 不是非空字符串');
  });
  it('says why its parameters cannot be filled in and keeps the step from passing', async () => {
    const [broken] = readExecutionTools(TOOLS, { tools: [tool('broken', { properties: { mode: { type: 'string', enum: 'fast' } } })] });
    const valid = vi.fn();
    render(<StepEditor steps={[{ id: 'one', title: '第一步', kind: 'tool', operation: 'broken', arguments: {}, dependencies: [], input_index: null, instruction: '', status: 'pending', error: '', result: '', attempt: 0, task_id: '' }]} tools={[broken]} onChange={() => {}} onValidityChange={valid} />);
    expect(screen.getByRole('alert')).toHaveTextContent('参数说明格式不对：input_schema.properties.mode.enum 不是数组。这里填不了它的参数');
    expect(screen.queryByLabelText('mode')).toBeNull();
    expect(screen.getByRole('option', { name: 'broken（参数说明读不懂）' })).toBeInTheDocument();
    await waitFor(() => expect(valid).toHaveBeenLastCalledWith(false));
  });
});
