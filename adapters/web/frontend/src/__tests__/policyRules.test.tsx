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
let codeWriteCalls = 0;

function stubPolicy(policy = POLICY, codeWrite: () => Response = () => jsonResponse({ ok: true })) {
  lastPut = null;
  codeWriteCalls = 0;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).includes('/runtime/code-write')) {
      codeWriteCalls += 1;
      return codeWrite();
    }
    if (init?.method === 'PUT') {
      lastPut = JSON.parse(String(init.body)).policy;
      return jsonResponse({ policy: lastPut, hash: 'p2' });
    }
    return jsonResponse({ policy, hash: 'p1' });
  }));
}

async function renderPolicy(policy = POLICY, codeWrite?: () => Response) {
  stubPolicy(policy, codeWrite);
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
    fireEvent.click(screen.getByRole('button', { name: /命令/ }));
    expect(screen.getByText('rm\\s+-rf\\s+/')).toBeInTheDocument();

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

  // 后端只给多开实例开代码目录写权限：根实例上摆出这个勾选项，勾了必然失败。
  it('根实例确认全自动放行时不给代码目录写入的选项，也不碰宿主机接口', async () => {
    await renderPolicy();

    fireEvent.click(screen.getByRole('button', { name: '一键全自动放行' }));
    expect(screen.queryByLabelText(/同时开放/)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '确认并保存全自动放行' }));

    expect(await screen.findByText('已保存，立即生效，不用重启')).toBeInTheDocument();
    expect(codeWriteCalls).toBe(0);
  });

  // MOUNT 在页面模块载入时从地址栏取一次：先换到多开实例的地址，再重新载入页面和它读的设置。
  describe('多开实例的代码目录写权限', () => {
    const INSTANCE = 'beta_bot';
    const CODE_WRITE_LABEL = /同时开放 \/opt\/kazebot 写入并重启本实例 beta_bot（/;
    const SUBMITTED = '策略已保存，已提交 /opt/kazebot 写入权限，实例 beta_bot 随后重启';
    let MountedSection: typeof PolicyRulesSection;

    beforeEach(async () => {
      window.history.replaceState(null, '', `/i/${INSTANCE}/web/`);
      vi.resetModules();
      const [page, settings] = await Promise.all([
        import('../components/settings/pages/PolicyRulesSection'),
        import('../store/settingsStore'),
      ]);
      settings.useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
      MountedSection = page.PolicyRulesSection;
    });

    afterEach(() => {
      window.history.replaceState(null, '', '/');
    });

    const renderMounted = async (codeWrite = () => jsonResponse({ ok: true, instance_id: INSTANCE, workspace: '/w' })) => {
      stubPolicy(POLICY, codeWrite);
      render(<MountedSection />);
      await screen.findByRole('button', { name: /写入/ });
    };

    const confirmWithCodeWrite = () => {
      fireEvent.click(screen.getByRole('button', { name: '一键全自动放行' }));
      fireEvent.click(screen.getByLabelText(CODE_WRITE_LABEL));
      fireEvent.click(screen.getByRole('button', { name: '确认并保存全自动放行' }));
    };

    it('不勾代码目录写入就只存策略，不碰宿主机接口', async () => {
      await renderMounted();

      fireEvent.click(screen.getByRole('button', { name: '一键全自动放行' }));
      expect(screen.getByLabelText(CODE_WRITE_LABEL)).not.toBeChecked();
      fireEvent.click(screen.getByRole('button', { name: '确认并保存全自动放行' }));

      expect(await screen.findByText('已保存，立即生效，不用重启')).toBeInTheDocument();
      expect(codeWriteCalls).toBe(0);
    });

    it('勾上之后存完策略再请求宿主机，勾选项和提示都说本实例的名字', async () => {
      await renderMounted();

      fireEvent.click(screen.getByRole('button', { name: '一键全自动放行' }));
      expect(screen.getByText(/一旦开启，Bot 将可以完全控制此设备（其它未修改的 systemd 保护仍按现状生效）/)).toBeInTheDocument();
      fireEvent.click(screen.getByLabelText(CODE_WRITE_LABEL));
      fireEvent.click(screen.getByRole('button', { name: '确认并保存全自动放行' }));

      expect(await screen.findByText(SUBMITTED)).toBeInTheDocument();
      expect(codeWriteCalls).toBe(1);
      expect(lastPut.execute_command).toMatchObject({
        default: 'auto', deny_patterns: [], sensitive_patterns: [], sensitive_path_patterns: [],
      });
      expect(screen.queryByRole('alert')).toBeNull();
    });

    // 重启已经排上了：当成没开成，人会再开一次、再重启一回。
    it('写权限交上了只是回执读不懂：照样交代会重启，回执的事另起一行', async () => {
      await renderMounted(() => jsonResponse({ ok: true }));
      confirmWithCodeWrite();

      expect(await screen.findByText(SUBMITTED)).toBeInTheDocument();
      expect(screen.getByText('代码目录写权限已经提交，可回执格式不对：缺 instance_id')).toHaveAttribute('role', 'alert');
      expect(codeWriteCalls).toBe(1);
    });

    it('策略存上了、代码目录写权限没开成：存上的照常交代，没开成的另起一行报错', async () => {
      await renderMounted(() => errorResponse(503, '宿主机拒绝了'));
      confirmWithCodeWrite();

      expect(await screen.findByText('代码目录写权限没能启用：503 宿主机拒绝了')).toHaveAttribute('role', 'alert');
      expect(screen.getByRole('status')).toHaveTextContent('策略已保存。');
      expect(codeWriteCalls).toBe(1);
    });
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
        return reads === 1 ? Promise.resolve(jsonResponse({ policy: POLICY, hash: 'p1' })) : new Promise<Response>(() => {});
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

      await rotateThenLand(put, jsonResponse({ policy: REWRITTEN, hash: 'p2' }));

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

      await rotateThenLand(put, jsonResponse({ policy: REWRITTEN, hash: 'p2' }));

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

      expect(await screen.findByText(CONFLICT)).toHaveAttribute('role', 'alert');
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

  // 补成空文档的话，每一类都摆成「没有规则」，照着存一次就把整份策略清空了。
  it('读回来缺 policy 时报格式不对，不摆一份空策略让人改', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ hash: 'p1' })));
    render(<PolicyRulesSection />);

    expect(await screen.findByText('服务端策略格式不对：缺 policy')).toHaveAttribute('role', 'alert');
    expect(screen.queryByRole('button', { name: '保存策略' })).toBeNull();
  });

  // 回执是存完才发的：当成没存上，人会再存一次撞上冲突；拿它当新底子，下一次保存就不带指纹。
  it('存上了只是回执读不懂：交代已保存、回执的事另起一行，页面换成服务器上那一份', async () => {
    const reads: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'PUT') return jsonResponse({ policy: REWRITTEN });
      reads.push(reads.length ? 'p2' : 'p1');
      return jsonResponse({ policy: reads.length === 1 ? POLICY : REWRITTEN, hash: reads[reads.length - 1] });
    }));
    render(<PolicyRulesSection />);
    await screen.findByRole('button', { name: /写入/ });

    fireEvent.change(screen.getByLabelText('兜底档位'), { target: { value: 'deny' } });
    fireEvent.click(screen.getByRole('button', { name: '保存策略' }));

    expect(await screen.findByText('已保存，立即生效，不用重启')).toBeInTheDocument();
    expect(screen.getByText('策略已经存上，可回执格式不对：缺 hash')).toHaveAttribute('role', 'alert');
    expect(await screen.findByText(SERVER_RULE)).toBeInTheDocument();
    expect(reads).toEqual(['p1', 'p2']);
  });

  // 授权时长单独一个页签，保存仍是整份 PUT：读到的这一段要原样带回去，改了的要按数字带上。
  describe('审批授权', () => {
    const DEFAULT_LABEL = '默认时长（分钟）';
    const MAX_LABEL = '时长上限（分钟）';
    const GRANTED_POLICY = { ...POLICY, approval_grant: { default_minutes: 30, max_minutes: 1440 } };
    const CUSTOM_GRANT = { default_minutes: 45, max_minutes: 600 };
    const CUSTOM_GRANT_POLICY = { ...POLICY, approval_grant: CUSTOM_GRANT };
    const CAP_BELOW_ONE = '时长上限要填正整数（分钟），至少 1。';

    const openGrantTab = () => fireEvent.click(screen.getByRole('button', { name: /审批授权/ }));
    const setMinutes = (label: string, value: string) => (
      fireEvent.change(screen.getByLabelText(label), { target: { value } })
    );
    const saveButton = () => screen.getByRole('button', { name: '保存策略' });
    const permanentSwitch = () => screen.getByRole('checkbox', { name: /管理员名单里的人可以要永久授权/ });

    it('读到的两个时长摆在「审批授权」页签里', async () => {
      await renderPolicy(GRANTED_POLICY);

      expect(screen.queryByLabelText(DEFAULT_LABEL)).toBeNull();
      openGrantTab();

      expect(screen.getByLabelText(DEFAULT_LABEL)).toHaveValue(30);
      expect(screen.getByLabelText(MAX_LABEL)).toHaveValue(1440);
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it('改了的时长按数字随整份策略保存，别的段一条不少', async () => {
      await renderPolicy(GRANTED_POLICY);
      openGrantTab();

      setMinutes(DEFAULT_LABEL, '120');
      setMinutes(MAX_LABEL, '480');
      fireEvent.click(saveButton());

      await waitFor(() => expect(lastPut).not.toBeNull());
      // 服务端只收整数：带成字符串 "120" 会被整份拒掉。
      expect(lastPut.approval_grant).toEqual({ default_minutes: 120, max_minutes: 480 });
      expect(lastPut.execute_command.deny_patterns).toEqual(['rm\\s+-rf\\s+/']);
      expect(lastPut.write_file.rules).toHaveLength(2);
      expect(lastPut.restart.default).toBe('approval_required');
      expect(await screen.findByText('已保存，立即生效，不用重启')).toBeInTheDocument();
    });

    it('永久授权开关：服务端没给就按开着显示，不动它就不往回写', async () => {
      await renderPolicy(GRANTED_POLICY);
      openGrantTab();

      expect(permanentSwitch()).toBeChecked();

      setMinutes(DEFAULT_LABEL, '45');
      fireEvent.click(saveButton());

      await waitFor(() => expect(lastPut).not.toBeNull());
      expect(lastPut.approval_grant).toEqual({ default_minutes: 45, max_minutes: 1440 });
    });

    it('关掉永久授权随整份策略保存', async () => {
      await renderPolicy(GRANTED_POLICY);
      openGrantTab();

      fireEvent.click(permanentSwitch());
      expect(permanentSwitch()).not.toBeChecked();
      fireEvent.click(saveButton());

      await waitFor(() => expect(lastPut).not.toBeNull());
      expect(lastPut.approval_grant).toEqual({ default_minutes: 30, max_minutes: 1440, permanent_for_admins: false });
    });

    it('服务端给的是关着：照样显示关着，改别的段原样带回去', async () => {
      const switchedOff = { default_minutes: 30, max_minutes: 1440, permanent_for_admins: false };
      const switchedOffPolicy = { ...POLICY, approval_grant: switchedOff };
      await renderPolicy(switchedOffPolicy);
      openGrantTab();

      expect(permanentSwitch()).not.toBeChecked();

      fireEvent.click(screen.getByRole('button', { name: /写入/ }));
      fireEvent.change(screen.getByLabelText('兜底档位'), { target: { value: 'deny' } });
      fireEvent.click(saveButton());

      await waitFor(() => expect(lastPut).not.toBeNull());
      expect(lastPut.approval_grant).toEqual(switchedOff);
    });

    it('改别的段再保存，读到的时长原样带回去', async () => {
      await renderPolicy(CUSTOM_GRANT_POLICY);

      fireEvent.change(screen.getByLabelText('兜底档位'), { target: { value: 'deny' } });
      fireEvent.click(saveButton());

      await waitFor(() => expect(lastPut).not.toBeNull());
      expect(lastPut.write_file.default).toBe('deny');
      expect(lastPut.approval_grant).toEqual(CUSTOM_GRANT);
    });

    it('全自动放行只改四类档位，授权时长照旧', async () => {
      await renderPolicy(CUSTOM_GRANT_POLICY);

      fireEvent.click(screen.getByRole('button', { name: '一键全自动放行' }));
      fireEvent.click(screen.getByRole('button', { name: '确认并保存全自动放行' }));

      await waitFor(() => expect(lastPut).not.toBeNull());
      expect(lastPut.restart.default).toBe('auto');
      expect(lastPut.approval_grant).toEqual(CUSTOM_GRANT);
    });

    it('服务端没给这一段时按代码默认显示，不改就不往回写', async () => {
      await renderPolicy();
      openGrantTab();

      expect(screen.getByLabelText(DEFAULT_LABEL)).toHaveValue(30);
      expect(screen.getByLabelText(MAX_LABEL)).toHaveValue(1440);

      fireEvent.click(screen.getByRole('button', { name: /写入/ }));
      fireEvent.change(screen.getByLabelText('兜底档位'), { target: { value: 'deny' } });
      fireEvent.click(saveButton());

      await waitFor(() => expect(lastPut).not.toBeNull());
      expect(lastPut).not.toHaveProperty('approval_grant');
    });

    it('服务端没给这一段时只改一项，另一项按代码默认补齐', async () => {
      await renderPolicy();
      openGrantTab();

      setMinutes(MAX_LABEL, '60');
      fireEvent.click(saveButton());

      await waitFor(() => expect(lastPut).not.toBeNull());
      expect(lastPut.approval_grant).toEqual({ default_minutes: 30, max_minutes: 60 });
    });

    it('默认时长填 0 是合法值：默认不授权，照样能存', async () => {
      await renderPolicy(GRANTED_POLICY);
      openGrantTab();

      setMinutes(DEFAULT_LABEL, '0');

      expect(screen.queryByRole('alert')).toBeNull();
      expect(screen.getByLabelText(DEFAULT_LABEL)).toHaveAttribute('aria-invalid', 'false');
      fireEvent.click(saveButton());
      await waitFor(() => expect(lastPut?.approval_grant).toEqual({ default_minutes: 0, max_minutes: 1440 }));
    });

    it('默认时长比上限长时提示会按上限算，不拦保存', async () => {
      await renderPolicy(GRANTED_POLICY);
      openGrantTab();

      setMinutes(DEFAULT_LABEL, '2000');

      expect(screen.getByText('默认时长比上限还长，保存后按上限 1440 分钟算。')).toBeInTheDocument();
      expect(screen.queryByRole('alert')).toBeNull();
      fireEvent.click(saveButton());
      await waitFor(() => expect(lastPut?.approval_grant).toEqual({ default_minutes: 2000, max_minutes: 1440 }));
    });

    it.each([
      [DEFAULT_LABEL, '', '默认时长要填 0 或正整数（分钟）。'],
      [DEFAULT_LABEL, '-1', '默认时长要填 0 或正整数（分钟）。'],
      [DEFAULT_LABEL, '1.5', '默认时长要填 0 或正整数（分钟）。'],
      [MAX_LABEL, '', CAP_BELOW_ONE],
      [MAX_LABEL, '0', CAP_BELOW_ONE],
      [MAX_LABEL, '525601', '时长上限最多 525600 分钟（365 天）。'],
    ])('%s 填成「%s」：当场指出，保存被拦下、一个字节都不写', async (label, value, problem) => {
      await renderPolicy(GRANTED_POLICY);
      openGrantTab();

      setMinutes(label, value);

      expect(screen.getByRole('alert')).toHaveTextContent(problem);
      expect(screen.getByLabelText(label)).toHaveAttribute('aria-invalid', 'true');
      fireEvent.click(saveButton());

      expect(await screen.findByText(`没有保存：${problem}`)).toHaveAttribute('role', 'alert');
      expect(lastPut).toBeNull();
      // 清空的框不能悄悄变成 0：存成 0 等于关掉默认授权。草稿照原样留着等人改。
      expect(screen.getByLabelText(label)).toHaveValue(value === '' ? null : Number(value));
    });

    it('在别的页签点保存也会拦下，并切回「审批授权」', async () => {
      await renderPolicy(GRANTED_POLICY);
      openGrantTab();
      setMinutes(MAX_LABEL, '0');
      fireEvent.click(screen.getByRole('button', { name: /写入/ }));
      expect(screen.queryByLabelText(MAX_LABEL)).toBeNull();

      fireEvent.click(saveButton());

      expect(await screen.findByText(`没有保存：${CAP_BELOW_ONE}`)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /审批授权/ })).toHaveAttribute('aria-pressed', 'true');
      expect(screen.getByLabelText(MAX_LABEL)).toHaveValue(0);
      expect(lastPut).toBeNull();
    });

    it('改对之后照常保存，拦下时的报错随之清掉', async () => {
      await renderPolicy(GRANTED_POLICY);
      openGrantTab();
      setMinutes(MAX_LABEL, '0');
      fireEvent.click(saveButton());
      await screen.findByText(`没有保存：${CAP_BELOW_ONE}`);

      setMinutes(MAX_LABEL, '60');
      expect(screen.getByLabelText(MAX_LABEL)).toHaveAttribute('aria-invalid', 'false');
      fireEvent.click(saveButton());

      await waitFor(() => expect(lastPut?.approval_grant).toEqual({ default_minutes: 30, max_minutes: 60 }));
      expect(await screen.findByText('已保存，立即生效，不用重启')).toBeInTheDocument();
      expect(screen.queryByText(/没有保存/)).toBeNull();
    });

    it('保存后显示服务端生效的那份时长', async () => {
      // 服务端会把超过上限的默认时长截到上限：界面要换成生效的值，不能还停在填的那个数上。
      vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === 'PUT') {
          const sent = JSON.parse(String(init.body)).policy;
          return jsonResponse({ policy: { ...sent, approval_grant: { default_minutes: 60, max_minutes: 60 } }, hash: 'p2' });
        }
        return jsonResponse({ policy: GRANTED_POLICY, hash: 'p1' });
      }));
      render(<PolicyRulesSection />);
      await screen.findByRole('button', { name: /写入/ });
      openGrantTab();

      setMinutes(DEFAULT_LABEL, '2000');
      setMinutes(MAX_LABEL, '60');
      fireEvent.click(saveButton());

      expect(await screen.findByText('已保存，立即生效，不用重启')).toBeInTheDocument();
      expect(screen.getByLabelText(DEFAULT_LABEL)).toHaveValue(60);
      expect(screen.getByLabelText(MAX_LABEL)).toHaveValue(60);
      expect(screen.queryByText(/默认时长比上限还长/)).toBeNull();
    });
  });
});
