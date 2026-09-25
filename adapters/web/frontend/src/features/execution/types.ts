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
export interface ParameterSchema { type?: string; description?: string; enum?: unknown[]; minimum?: number; maximum?: number }
export interface ToolOption {
  name: string; description: string; effect: string;
  input_schema: { properties?: Record<string, ParameterSchema>; required?: string[] };
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
