// 策略规则的顺序有语义：supervisor/policy.py 自上而下取第一条命中的。
// 界面必须让人看得见顺序、改得动顺序，否则加的规则会被兜底规则挡住而毫无提示。
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { PolicyRulesSection } from '../components/settings/pages/PolicyRulesSection';
import { useSettingsStore } from '../store/settingsStore';

const POLICY = {
  version: 1,
  extra_roots: [],
  read_file: {
    default: 'auto',
    rules: [
      { pattern: 'engine/system_nodes/**', decision: 'deny', reason: '系统提示词是内部的' },
      { pattern: 'data/**', decision: 'auto', reason: '兜底' },
    ],
  },
  write_file: {
    default: 'auto',
    rules: [
      { pattern: 'tools/**', decision: 'approval_required', reason: '建改工具需要审批' },
      { pattern: 'data/policy.yaml', decision: 'deny', reason: '策略只能人改' },
    ],
  },
  execute_command: {
    default: 'approval_required',
    deny_patterns: ['rm\\s+-rf\\s+/'],
    sensitive_patterns: ['curl.*\\|\\s*sh'],
    sensitive_path_patterns: ['.ssh/'],
  },
  restart: { default: 'approval_required' },
};

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

let lastPut: any = null;

function stubPolicy() {
  lastPut = null;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'PUT') {
      lastPut = JSON.parse(String(init.body)).policy;
      return jsonResponse({ policy: lastPut });
    }
    return jsonResponse({ policy: POLICY });
  }));
}

async function renderPolicy() {
  stubPolicy();
  render(<PolicyRulesSection />);
  await screen.findByRole('button', { name: /写入/ });
}

const ruleItems = () => screen.getAllByRole('listitem');

describe('服务端策略', () => {
  beforeEach(() => {
    useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('四类操作分开放，一次只摆一类', async () => {
    await renderPolicy();

    // 默认落在写入：规则最多、也最常改。
    expect(screen.getByText('tools/**')).toBeInTheDocument();
    expect(screen.queryByText('engine/system_nodes/**')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /读取/ }));

    expect(screen.getByText('engine/system_nodes/**')).toBeInTheDocument();
    expect(screen.queryByText('tools/**')).toBeNull();
  });

  it('新规则插在最前面 —— 追加到末尾会被兜底规则永久挡住', async () => {
    await renderPolicy();
    fireEvent.click(screen.getByRole('button', { name: /读取/ }));

    fireEvent.change(screen.getByLabelText('读取新增路径'), { target: { value: 'data/attachments/**' } });
    fireEvent.click(screen.getByRole('button', { name: '插到最前' }));

    expect(within(ruleItems()[0]).getByText('data/attachments/**')).toBeInTheDocument();
  });

  it('能把规则往下移，给更靠前的规则让路', async () => {
    await renderPolicy();

    fireEvent.click(screen.getByRole('button', { name: '把 tools/** 下移' }));

    expect(within(ruleItems()[0]).getByText('data/policy.yaml')).toBeInTheDocument();
    expect(within(ruleItems()[1]).getByText('tools/**')).toBeInTheDocument();
  });

  it('第一条不能再上移，最后一条不能再下移', async () => {
    await renderPolicy();

    expect(screen.getByRole('button', { name: '把 tools/** 上移' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '把 data/policy.yaml 下移' })).toBeDisabled();
  });

  it('试一条路径，报出第一条命中的规则', async () => {
    await renderPolicy();

    fireEvent.change(screen.getByLabelText('试一条路径'), { target: { value: 'tools/sub/x.py' } });

    const verdict = screen.getByText(/第 1 条/);
    expect(verdict).toHaveTextContent('tools/**');
    expect(verdict).toHaveTextContent('需审批');
  });

  it('没有规则命中时说清楚会走兜底', async () => {
    await renderPolicy();

    fireEvent.change(screen.getByLabelText('试一条路径'), { target: { value: 'engine/runner.py' } });

    expect(screen.getByText(/没有规则命中/)).toBeInTheDocument();
  });

  it('筛选同时看路径和理由', async () => {
    await renderPolicy();

    fireEvent.change(screen.getByLabelText('筛选写入规则'), { target: { value: '只能人改' } });

    expect(screen.getByText('data/policy.yaml')).toBeInTheDocument();
    expect(screen.queryByText('tools/**')).toBeNull();
  });

  it('兜底档位能在界面上改，不用去编辑 YAML 原文', async () => {
    await renderPolicy();

    fireEvent.change(screen.getByLabelText('兜底档位'), { target: { value: 'approval_required' } });
    fireEvent.click(screen.getByRole('button', { name: '保存策略' }));

    await waitFor(() => expect(lastPut?.write_file?.default).toBe('approval_required'));
  });

  it('命令的硬拦截和敏感正则摆出来了', async () => {
    await renderPolicy();

    fireEvent.click(screen.getByRole('button', { name: /命令/ }));

    expect(screen.getByText('rm\\s+-rf\\s+/')).toBeInTheDocument();
    expect(screen.getByText('curl.*\\|\\s*sh')).toBeInTheDocument();
    expect(screen.getByText('.ssh/')).toBeInTheDocument();
  });

  it('保存整份文档，没显示的字段不会被吞掉', async () => {
    // PUT 是整份替换：漏带 deny_patterns 等于把 12 条硬拦截规则悄悄删了。
    await renderPolicy();

    fireEvent.change(screen.getByLabelText('兜底档位'), { target: { value: 'deny' } });
    fireEvent.click(screen.getByRole('button', { name: '保存策略' }));

    await waitFor(() => expect(lastPut).not.toBeNull());
    expect(lastPut.execute_command.deny_patterns).toEqual(['rm\\s+-rf\\s+/']);
    expect(lastPut.read_file.rules).toHaveLength(2);
    expect(lastPut.restart.default).toBe('approval_required');
  });

  it('重启档位也能改', async () => {
    await renderPolicy();

    fireEvent.click(screen.getByRole('button', { name: /重启/ }));
    fireEvent.change(screen.getByLabelText('档位'), { target: { value: 'deny' } });
    fireEvent.click(screen.getByRole('button', { name: '保存策略' }));

    await waitFor(() => expect(lastPut?.restart?.default).toBe('deny'));
  });
});
