import { NON_EMPTY_TEXT, NUMBER, OBJECT, TEXT, TEXT_LIST, at, optional, shaped, shapedRecord, shapedRows, type Kind } from '../../api/shape';

export interface PlanInput { kind: 'text' | 'url' | 'file'; value: string; label: string }
export interface PlanStep {
  id: string; title: string; kind: 'read_input' | 'model' | 'tool' | 'artifact'; dependencies: string[];
  input_index: number | null; operation: string; arguments: Record<string, unknown>; instruction: string;
  status: string; error: string; result: string; attempt: number; task_id: string;
}
export interface PlanArtifact { id: string; step_id: string; name: string; size: number }
export interface ExecutionPlan {
  id: string; goal: string; work_scope: string; scope: string; revision: number; version: number; status: string;
  inputs: PlanInput[]; steps: PlanStep[]; artifacts: PlanArtifact[];
  items: { index: number; label: string; status: string; step_ids: string[]; errors?: string[]; artifacts?: PlanArtifact[] }[];
}
/** type 照 JSON Schema 可以写好几个（比如 ["string", "null"]），这种按普通文本填。 */
export interface ParameterSchema { type?: string | string[]; description?: string; enum?: unknown[]; minimum?: number; maximum?: number }
export interface ToolOption {
  name: string; description: string; effect: string;
  input_schema: { properties?: Record<string, ParameterSchema>; required?: string[] };
  /** 参数说明读不懂时写着哪里不对：工具照样列着，步骤里填不了它的参数。 */
  schemaProblem?: string;
}
export const statusLabel: Record<string, string> = {
  draft: '待确认', queued: '排队中', running: '执行中', pending: '待执行', awaiting_approval: '等待审批',
  completed: '全部完成', succeeded: '完成', partial: '部分完成', failed: '失败', blocked: '等待依赖',
  cancelled: '已取消', cancelling: '正在停止', outcome_unknown: '需核对实际结果',
};

function record(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function strings(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(item => typeof item === 'string');
}

function fields(value: unknown, names: string[]): value is Record<string, unknown> {
  return record(value) && names.every(name => typeof value[name] === 'string');
}

function artifact(value: unknown): boolean {
  return fields(value, ['id', 'name', 'step_id']) && typeof value.size === 'number';
}

export function readExecutionPlan(value: unknown): ExecutionPlan {
  const valid = fields(value, ['id', 'goal', 'work_scope', 'scope', 'status'])
    && Number.isInteger(value.revision) && Number.isInteger(value.version)
    && Array.isArray(value.inputs) && value.inputs.every(item => fields(item, ['kind', 'value', 'label']))
    && Array.isArray(value.steps) && value.steps.every(step => fields(step, ['id', 'title', 'kind', 'status', 'error', 'result', 'operation', 'instruction', 'task_id']) && strings(step.dependencies) && record(step.arguments) && Number.isInteger(step.attempt))
    && Array.isArray(value.artifacts) && value.artifacts.every(artifact)
    && Array.isArray(value.items) && value.items.every(item => fields(item, ['label', 'status']) && Number.isInteger(item.index) && strings(item.step_ids) && (item.errors === undefined || strings(item.errors)) && (item.artifacts === undefined || Array.isArray(item.artifacts) && item.artifacts.every(artifact)));
  if (!valid) throw new Error('执行计划响应格式无效，请刷新或检查服务版本。');
  return value as unknown as ExecutionPlan;
}

export function readExecutionPlans(value: unknown): ExecutionPlan[] {
  if (!record(value) || !Array.isArray(value.plans)) throw new Error('执行计划列表响应格式无效，请刷新或检查服务版本。');
  return value.plans.map(readExecutionPlan);
}

const TOOL_OPTION = { name: NON_EMPTY_TEXT, description: TEXT, effect: TEXT, input_schema: OBJECT };
const TOOL_SCHEMA = { properties: optional(OBJECT), required: optional(TEXT_LIST) };
const LIST: Kind<unknown[]> = { ok: (value): value is unknown[] => Array.isArray(value), mismatch: '不是数组' };
const PARAMETER_TYPE: Kind<string | string[]> = {
  ok: (value): value is string | string[] => TEXT.ok(value) || TEXT_LIST.ok(value),
  mismatch: '不是类型名，也不是类型名数组',
};
// 步骤编辑器照这几项画输入框：enum 不是数组、说明是个对象，画的时候整页都会崩
const PARAMETER = { type: optional(PARAMETER_TYPE), description: optional(TEXT), enum: optional(LIST), minimum: optional(NUMBER), maximum: optional(NUMBER) };
const SCHEMA = '参数说明';

function schemaProblem(schema: Record<string, unknown>): string {
  try {
    const { properties } = shaped(SCHEMA, schema, TOOL_SCHEMA, 'input_schema');
    if (properties) shapedRecord(SCHEMA, properties, PARAMETER, at('input_schema', 'properties'));
    return '';
  } catch (error) {
    return error instanceof Error ? error.message : `${SCHEMA}格式不对`;
  }
}

/** 步骤里能选的工具。读成空表，页面就说没有可用工具，步骤也选不了工具。参数说明读不懂只记在那个工具身上，别的工具照用。 */
export function readExecutionTools(what: string, value: unknown): ToolOption[] {
  return shapedRows(what, shaped(what, value, {}).tools, TOOL_OPTION, 'tools').map((row) => {
    const problem = schemaProblem(row.input_schema);
    return (problem ? { ...row, schemaProblem: problem } : row) as unknown as ToolOption;
  });
}

export type PlanAction = 'confirm' | 'cancel' | 'retry' | 'resolve-step';

/** 计划动作已经交上时的那半句话，回执读不懂时接在前面。 */
export const PLAN_ACTION_RECEIPTS: Record<PlanAction, string> = {
  confirm: '计划已经确认，可回执',
  cancel: '计划已经叫停，可回执',
  retry: '失败的步骤已经重新排上，可回执',
  'resolve-step': '这一步的核对结果已经记下，可回执',
};
