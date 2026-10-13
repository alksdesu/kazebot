// 策略规则的顺序有语义：supervisor/policy.py 自上而下取第一条命中的。
// 界面必须让人看得见顺序、改得动顺序，否则加的规则会被兜底规则挡住而毫无提示。
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { PolicyRulesSection } from '../components/settings/pages/PolicyRulesSection';
import { useSettingsStore } from '../store/settingsStore';

const POLICY = {
  version: 1,
  extra_roots: ['/opt/kazebot'],
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

const ROTATED_TOKEN = 'rotated-token';
const SAVE_FAILURE = '策略写不进去';
// 服务端回写的文档多一条规则：真落到界面上，第一眼就看得见。
const SERVER_RULE = 'server/rewrote/**';
const REWRITTEN = {
  ...POLICY,
  write_file: {
    ...POLICY.write_file,
    rules: [{ pattern: SERVER_RULE, decision: 'deny', reason: '服务端补的' }, ...POLICY.write_file.rules],
  },
};

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

function errorResponse(status: number, detail: string): Response {
  return new Response(JSON.stringify({ detail }), { status, headers: { 'Content-Type': 'application/json' } });
}

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

let lastPut: any = null;

function stubPolicy(policy = POLICY) {
  lastPut = null;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'PUT') {
      lastPut = JSON.parse(String(init.body)).policy;
      return jsonResponse({ policy: lastPut });
    }
    return jsonResponse({ policy });
  }));
}

async function renderPolicy(policy = POLICY) {
  stubPolicy(policy);
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

  it('把代码根目录放在规则列表第一项并加粗', async () => {
    const policy = {
      ...POLICY,
      write_file: {
        ...POLICY.write_file,
        rules: [
          { pattern: '/opt/kazebot', decision: 'approval_required', reason: '代码目录需要单独关注' },
          ...POLICY.write_file.rules,
        ],
      },
    };
    await renderPolicy(policy);

    const root = screen.getByText('/opt/kazebot');
    expect(root.tagName).toBe('CODE');
    expect(root).toHaveClass('font-bold');
    expect(root.closest('li')).toBe(ruleItems()[0]);
    expect(screen.queryByText('策略扩展目录')).toBeNull();
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

  it('全自动放行先生成草稿，二次确认后才保存并清空三组命令列表', async () => {
    await renderPolicy();

    fireEvent.click(screen.getByRole('button', { name: '一键全自动放行' }));

    expect(lastPut).toBeNull();
    expect(screen.getByRole('button', { name: '确认并保存全自动放行' })).toBeInTheDocument();
    expect(screen.getByText('已生成全自动放行草稿；请确认后保存。')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(screen.getByRole('button', { name: '一键全自动放行' })).toBeInTheDocument();
    expect(lastPut).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '一键全自动放行' }));
    fireEvent.click(screen.getByRole('button', { name: '确认并保存全自动放行' }));

    await waitFor(() => expect(lastPut).not.toBeNull());
    expect(lastPut.read_file.default).toBe('auto');
    expect(lastPut.read_file.rules.every((rule: any) => rule.decision === 'auto')).toBe(true);
    expect(lastPut.write_file.default).toBe('auto');
    expect(lastPut.write_file.rules.every((rule: any) => rule.decision === 'auto')).toBe(true);
    expect(lastPut.execute_command).toMatchObject({
      default: 'auto', deny_patterns: [], sensitive_patterns: [], sensitive_path_patterns: [],
    });
    expect(lastPut.restart.default).toBe('auto');
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

  describe('保存回来时 token 已经换掉', () => {
    // 新会话那次读故意挂着：读回来会把本地改动一并冲掉，就看不出旧会话有没有乱写了。
    const saveInFlight = async (): Promise<Deferred<Response>> => {
      const put = deferred<Response>();
      let reads = 0;
      vi.stubGlobal('fetch', vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === 'PUT') return put.promise;
        reads += 1;
        return reads === 1 ? Promise.resolve(jsonResponse({ policy: POLICY })) : new Promise<Response>(() => {});
      }));
      render(<PolicyRulesSection />);
      await screen.findByRole('button', { name: /写入/ });
      fireEvent.change(screen.getByLabelText('兜底档位'), { target: { value: 'approval_required' } });
      fireEvent.click(screen.getByRole('button', { name: '保存策略' }));
      await waitFor(() => expect(screen.getByRole('button', { name: '正在保存…' })).toBeDisabled());
      return put;
    };

    const rotateThenLand = async (put: Deferred<Response>, response: Response) => {
      act(() => { useSettingsStore.setState({ adminToken: ROTATED_TOKEN }); });
      await act(async () => { put.resolve(response); });
    };

    it('不把服务端回写的文档落到新会话，也不写已保存那句', async () => {
      const put = await saveInFlight();

      await rotateThenLand(put, jsonResponse({ policy: REWRITTEN }));

      expect(screen.queryByText(SERVER_RULE)).toBeNull();
      expect(screen.queryByText('已保存，立即生效，不用重启')).toBeNull();
      expect(screen.getByLabelText('兜底档位')).toHaveValue('approval_required');
    });

    it('保存失败也不把失败原因写到新会话', async () => {
      const put = await saveInFlight();

      await rotateThenLand(put, errorResponse(500, SAVE_FAILURE));

      expect(screen.queryByText(new RegExp(SAVE_FAILURE))).toBeNull();
      expect(screen.getByRole('status')).toHaveTextContent('有未保存的改动');
    });

    it('提前 return 之后保存按钮不卡在正在保存', async () => {
      const put = await saveInFlight();

      await rotateThenLand(put, jsonResponse({ policy: REWRITTEN }));

      expect(screen.queryByRole('button', { name: '正在保存…' })).toBeNull();
      expect(screen.getByRole('button', { name: '保存策略' })).toBeEnabled();
    });
  });

  // 整份 PUT 覆盖：中途被共享组里别的实例改过还照写，那边加的规则连个提示都不留就没了。
  describe('中途被别处改过', () => {
    const CONFLICT = '策略在打开之后被别处改过，没有保存：点「丢弃」读入最新内容，再把改动重做一遍。';

    /** 读给 read 那一版指纹，写按 current 判：对不上回 409。两者不同就是「读完之后被别处改过」。 */
    const stubVersioned = (read = 'p1', currentHash = read) => {
      const puts: any[] = [];
      let current = currentHash;
      vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method !== 'PUT') return jsonResponse({ policy: POLICY, hash: read });
        const body = JSON.parse(String(init.body));
        puts.push(body);
        if (body.base_hash !== current) return errorResponse(409, 'policy.yaml 已被别处改过，刷新后再改');
        current = 'p2';
        return jsonResponse({ policy: body.policy, hash: current });
      }));
      return puts;
    };

    const edit = () => fireEvent.change(screen.getByLabelText('兜底档位'), { target: { value: 'deny' } });
    const saveButton = () => screen.getByRole('button', { name: '保存策略' });

    it('保存交回读到的那一版指纹，存完换成结果里的新指纹', async () => {
      const puts = stubVersioned();
      render(<PolicyRulesSection />);
      await screen.findByRole('button', { name: /写入/ });

      edit();
      fireEvent.click(saveButton());
      await screen.findByText('已保存，立即生效，不用重启');
      fireEvent.change(screen.getByLabelText('兜底档位'), { target: { value: 'auto' } });
      fireEvent.click(saveButton());
      await waitFor(() => expect(puts).toHaveLength(2));

      expect(puts.map((body) => body.base_hash)).toEqual(['p1', 'p2']);
    });

    it('指纹对不上时说清楚没保存，服务器上那份没被动过', async () => {
      const puts = stubVersioned('p1', 'changed-elsewhere');
      render(<PolicyRulesSection />);
      await screen.findByRole('button', { name: /写入/ });

      edit();
      fireEvent.click(saveButton());

      expect(await screen.findByText(CONFLICT)).toBeInTheDocument();
      expect(puts).toHaveLength(1);
      // 草稿还在手里，重做一遍之前不能被清掉。
      expect(screen.getByLabelText('兜底档位')).toHaveValue('deny');
    });

    it('「丢弃」重读服务器，换来的是现在磁盘上那一份', async () => {
      vi.spyOn(window, 'confirm').mockReturnValue(true);
      let served = POLICY;
      const reads: number[] = [];
      vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === 'PUT') return jsonResponse({ policy: POLICY, hash: 'p2' });
        reads.push(reads.length);
        return jsonResponse({ policy: served, hash: 'p1' });
      }));
      render(<PolicyRulesSection />);
      await screen.findByRole('button', { name: /写入/ });
      edit();

      served = REWRITTEN;
      fireEvent.click(screen.getByRole('button', { name: '丢弃' }));

      expect(await screen.findByText(SERVER_RULE)).toBeInTheDocument();
      expect(reads).toHaveLength(2);
      expect(screen.getByLabelText('兜底档位')).toHaveValue('auto');
    });

    it('「丢弃」被拒绝就不重读，草稿留着', async () => {
      vi.spyOn(window, 'confirm').mockReturnValue(false);
      const puts = stubVersioned();
      render(<PolicyRulesSection />);
      await screen.findByRole('button', { name: /写入/ });
      edit();

      fireEvent.click(screen.getByRole('button', { name: '丢弃' }));

      expect(screen.getByLabelText('兜底档位')).toHaveValue('deny');
      expect(puts).toHaveLength(0);
    });
  });
});
