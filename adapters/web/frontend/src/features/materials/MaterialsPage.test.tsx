import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { featureRequest } from '../client';
import { useSettingsStore } from '../../store/settingsStore';
import { buildMaterialSpec, MaterialsPage } from './MaterialsPage';
import { scopeIdentity } from '../conversationNames';

vi.mock('../client', async original => ({ ...await original<typeof import('../client')>(), featureRequest: vi.fn() }));

const mockedRequest = vi.mocked(featureRequest);
let approved = false;
let jobs: Record<string, unknown>[] = [];
let generateCalls = 0;

beforeEach(() => {
  vi.clearAllMocks(); approved = false; jobs = []; generateCalls = 0;
  useSettingsStore.setState({ adminToken: 'test-secret', isAuthenticated: true });
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => new Response(new Blob(['synthetic PNG']), { status: 200 })));
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:material-preview'), revokeObjectURL: vi.fn() }));
  mockedRequest.mockImplementation(async (path, options) => {
    if (path.endsWith('/capabilities')) return { office_renderer: true, modules: { pdf_preview: true } } as never;
    if (path.endsWith('/scopes')) return [] as never;
    if (path.endsWith('/settings')) return { journal_enabled: options?.method === 'PUT' } as never;
    if (path.endsWith('/sources')) return [{ id: 's1', name: 'Source', status: 'registered', mime_type: 'text/plain' }] as never;
    if (path.endsWith('/extractions')) return [] as never;
    if (path.endsWith('/jobs')) return jobs as never;
    if (path.endsWith('/artifacts')) return [{ id: 'a1', name: 'Report' }] as never;
    if (path.endsWith('/artifacts/a1')) return { id: 'a1', name: 'Report', current_version: 'v1', versions: [{
      id: 'v1', artifact_id: 'a1', number: 1, status: approved ? 'approved' : 'preview_ready', sha256: 'immutable-sha', page_count: 2, format: 'docx',
    }] } as never;
    if (path.endsWith('/approve')) { approved = true; return { status: 'approved' } as never; }
    if (path.endsWith('/send')) return { message: '已提交发送；实际投递由平台确认。' } as never;
    if (path.endsWith('/artifacts/generate')) { generateCalls++; return { id: 'j1', artifact_id: 'a1', status: 'queued' } as never; }
    if (path.endsWith('/retry')) return { id: 'j2', artifact_id: 'a1', status: 'queued' } as never;
    if (path.endsWith('/spans')) return { results: [{ id: 's1:2', text: 'Exact original', citation: 'Source · 第 2 行', locator: { label: '第 2 行' } }], warnings: [] } as never;
    if (path.endsWith('/cite')) return { label: 'Source · 第 2 行', quote: 'Exact original' } as never;
    throw new Error(`Unhandled ${path}`);
  });
});

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

async function openReport() {
  render(<MaterialsPage />);
  await screen.findByRole('option', { name: 'Report' });
  fireEvent.change(screen.getByLabelText('选择作品'), { target: { value: 'a1' } });
  await screen.findByAltText('预览第1页');
  await screen.findByAltText('预览第2页');
}

describe('materials preview confirmation', () => {
  it('requires every preview page and binds confirmation to immutable hash', async () => {
    await openReport();
    const confirm = screen.getByRole('button', { name: '确认当前版本' });
    fireEvent.click(screen.getByLabelText('我已核对全部预览页，内容与排版无误'));
    fireEvent.load(screen.getByAltText('预览第1页'));
    expect(confirm).toBeDisabled();
    fireEvent.load(screen.getByAltText('预览第2页'));
    expect(confirm).toBeEnabled();
    expect(screen.getByRole('button', { name: '发送到当前会话' })).toBeDisabled();
    fireEvent.click(confirm);
    await waitFor(() => expect(mockedRequest).toHaveBeenCalledWith('/v1/materials/versions/v1/approve', expect.objectContaining({
      scope: 'web:materials', method: 'POST', body: { sha256: 'immutable-sha', preview_inspected: true },
    })));
    await waitFor(() => expect(screen.getByRole('button', { name: '发送到当前会话' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '发送到当前会话' }));
    await screen.findByText('已提交发送；实际投递由平台确认。');
  });

  it('loads preview blobs with Authorization headers and never a token URL', async () => {
    await openReport();
    const requests = vi.mocked(fetch).mock.calls;
    expect(requests.length).toBe(2);
    for (const [url, options] of requests) {
      expect(String(url)).not.toContain('test-secret');
      expect(options?.headers).toEqual({ Authorization: 'Bearer test-secret' });
    }
  });

  it('blocks confirmation when preview rendering cannot be displayed', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response('', { status: 503 }));
    render(<MaterialsPage />);
    await screen.findByRole('option', { name: 'Report' });
    fireEvent.change(screen.getByLabelText('选择作品'), { target: { value: 'a1' } });
    await screen.findAllByText(/预览加载失败/);
    fireEvent.click(screen.getByLabelText('我已核对全部预览页，内容与排版无误'));
    expect(screen.getByRole('button', { name: '确认当前版本' })).toBeDisabled();
  });
});

describe('materials operations', () => {
  it('verifies a citation against the selected source span', async () => {
    render(<MaterialsPage />);
    await screen.findByRole('option', { name: /Source/ });
    fireEvent.change(screen.getByLabelText('选择资料'), { target: { value: 's1' } });
    fireEvent.change(screen.getByLabelText('文件检索词'), { target: { value: 'original' } });
    fireEvent.click(screen.getByRole('button', { name: '解析并检索' }));
    await screen.findByText('Exact original');
    fireEvent.click(screen.getByRole('button', { name: '核验这段引用' }));
    await waitFor(() => expect(mockedRequest).toHaveBeenCalledWith('/v1/materials/sources/s1/cite', expect.objectContaining({ body: { span_id: 's1:2', quote: 'Exact original' } })));
  });

  it('does not create another local generation while the displayed job is active', async () => {
    render(<MaterialsPage />);
    await screen.findByRole('option', { name: 'Report' });
    fireEvent.change(screen.getByLabelText('成品标题'), { target: { value: 'Synthetic report' } });
    fireEvent.change(screen.getByLabelText('成品内容'), { target: { value: 'Synthetic body' } });
    const button = screen.getByRole('button', { name: '生成并渲染预览' });
    fireEvent.click(button);
    await screen.findByText(/任务 j1：排队中/);
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(generateCalls).toBe(1);
  });

  it('shows interrupted persisted jobs and retries only on request', async () => {
    jobs = [{ id: 'old', artifact_id: 'a1', status: 'interrupted', error: '服务重启中断' }];
    render(<MaterialsPage />);
    const retry = await screen.findByRole('button', { name: '重试本地生成' });
    expect(mockedRequest.mock.calls.some(([path]) => path.endsWith('/retry'))).toBe(false);
    fireEvent.click(retry);
    await screen.findByText(/任务 j2：排队中/);
    expect(mockedRequest).toHaveBeenCalledWith('/v1/materials/jobs/old/retry', expect.objectContaining({ method: 'POST' }));
  });

  it('keeps tabular cells and slide boundaries in generation specifications', () => {
    expect(buildMaterialSpec('xlsx', 'Table', 'Code\tValue\n0012\t0')).toEqual({ title: 'Table', sheets: [{ name: '数据', rows: [['Code', 'Value'], ['0012', '0']] }] });
    expect(buildMaterialSpec('pptx', 'Deck', 'First\nPoint\n\nSecond\nNext')).toEqual({ title: 'Deck', slides: [{ title: 'First', bullets: ['Point'] }, { title: 'Second', bullets: ['Next'] }] });
  });
});


it('discards a file search response after switching scopes', async () => {
  const original = mockedRequest.getMockImplementation()!;
  let finish!: (value: unknown) => void;
  mockedRequest.mockImplementation(async (path, options) => path.endsWith('/spans') ? new Promise(resolve => { finish = resolve; }) : original(path, options));
  render(<MaterialsPage />); await screen.findByRole('option', { name: /Source/ });
  fireEvent.change(screen.getByLabelText('选择资料'), { target: { value: 's1' } });
  fireEvent.click(screen.getByRole('button', { name: '解析并检索' }));
  fireEvent.change(screen.getByLabelText('资料会话范围'), { target: { value: 'qq_group:b' } });
  fireEvent.click(screen.getByRole('button', { name: '切换会话范围' }));
  await waitFor(() => expect(mockedRequest).toHaveBeenCalledWith('/v1/materials/sources', expect.objectContaining({ scope: 'qq_group:b' })));
  fireEvent.change(screen.getByLabelText('资料会话范围'), { target: { value: 'web:materials' } });
  fireEvent.click(screen.getByRole('button', { name: '切换会话范围' }));
  await screen.findByText('当前操作范围：资料工作区');
  await act(async () => finish({ results: [{ id: 'old', text: '上一会话的原文', citation: '旧来源', locator: { label: 'p1' } }], warnings: [] }));
  expect(screen.queryByText('上一会话的原文')).not.toBeInTheDocument();
});

it('binds correction drafts to their base revision instead of silently rebasing them', async () => {
  const original = mockedRequest.getMockImplementation()!;
  let revision = 1;
  mockedRequest.mockImplementation(async (path, options) => {
    if (path.endsWith('/extractions')) return [{ id: 'x1', source_id: 's1', status: 'needs_review', revision, fields: [{ id: 'amount', label: '金额', value: revision === 1 ? '100' : '200', uncertain: true }] }];
    if (path.endsWith('/extractions/x1')) return {};
    return original(path, options);
  });
  render(<MaterialsPage />); fireEvent.change(await screen.findByLabelText('x1 金额'), { target: { value: '110' } });
  revision = 2; fireEvent.click(screen.getByRole('button', { name: '刷新' }));
  await screen.findByText(/校对已有新的修订/);
  expect(screen.getByLabelText('x1 金额')).toHaveValue('110');
  const save = screen.getByRole('button', { name: '保存并确认校对' }); expect(save).toBeDisabled();
  fireEvent.click(save);
  expect(mockedRequest.mock.calls.some(([path]) => path.endsWith('/extractions/x1'))).toBe(false);
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
  fireEvent.click(screen.getByRole('button', { name: '重新载入校对' })); expect(screen.getByLabelText('x1 金额')).toHaveValue('200');
  fireEvent.change(screen.getByLabelText('x1 金额'), { target: { value: '220' } }); fireEvent.click(save);
  await waitFor(() => expect(mockedRequest).toHaveBeenCalledWith('/v1/materials/extractions/x1', expect.objectContaining({ body: { expected_revision: 2, values: { amount: '220' }, confirm: true } })));
  confirm.mockRestore();
});

it('keeps a creation draft when a scope change is declined', async () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  render(<MaterialsPage />); await screen.findByRole('option', { name: 'Report' });
  fireEvent.change(screen.getByLabelText('成品标题'), { target: { value: '未保存作品' } });
  fireEvent.change(screen.getByLabelText('资料会话范围'), { target: { value: 'qq_group:b' } });
  fireEvent.click(screen.getByRole('button', { name: '切换会话范围' }));
  expect(screen.getByLabelText('成品标题')).toHaveValue('未保存作品');
  expect(mockedRequest.mock.calls.some(([, options]) => options?.scope === 'qq_group:b')).toBe(false);
  confirm.mockRestore();
});

it('uses human names while preserving the exact material scope and bot identity', async () => {
  const original = mockedRequest.getMockImplementation()!;
  const scope = 'qq_group:1234567890abcdef12345678';
  mockedRequest.mockImplementation(async (path, options) => path.endsWith('/scopes') ? [
    { scope, bot_scope: 'bot-current-internal-key', current_account: true, owner: { kind: 'group', label: '同名资料群' } },
    { scope, bot_scope: 'bot-old-internal-key', current_account: false, owner: { kind: 'group', label: '同名资料群' } },
  ] : original(path, options));
  render(<MaterialsPage />);
  const choices = await screen.findAllByRole('option', { name: /^同名资料群/ });
  expect(choices).toHaveLength(2); expect(choices[0].textContent).not.toBe(choices[1].textContent);
  const historical = choices.find(option => option.textContent?.includes('历史账号')) as HTMLOptionElement;
  fireEvent.change(screen.getByLabelText('选择已有资料会话'), { target: { value: historical.value } });
  await waitFor(() => expect(mockedRequest).toHaveBeenCalledWith('/v1/materials/sources', expect.objectContaining({ scope, params: { bot_scope: 'bot-old-internal-key' } })));
  expect(historical.value).toBe(scopeIdentity(scope, 'bot-old-internal-key'));
  expect(screen.getByText(/^当前操作范围：同名资料群/)).toHaveTextContent('历史账号');
  expect(document.body.textContent).not.toContain(scope); expect(document.body.textContent).not.toContain('bot-old-internal-key');
});

it('drops old material names on authentication change and ignores late scope responses', async () => {
  const original = mockedRequest.getMockImplementation()!;
  let finishOld!: (value: unknown) => void; let scopesCall = 0;
  mockedRequest.mockImplementation(async (path, options) => {
    if (!path.endsWith('/scopes')) return original(path, options);
    scopesCall++;
    if (scopesCall === 1) return [{ scope: 'qq_group:a', bot_scope: 'a', owner: { kind: 'group', label: '旧认证群名' } }];
    if (scopesCall === 2) return new Promise(resolve => { finishOld = resolve; });
    return [{ scope: 'qq_private:b', bot_scope: 'b', owner: { kind: 'private', label: '当前认证联系人' } }];
  });
  render(<MaterialsPage />); await screen.findByRole('option', { name: '旧认证群名' });
  act(() => useSettingsStore.setState({ adminToken: 'second-auth' })); expect(screen.queryByText('旧认证群名')).not.toBeInTheDocument();
  act(() => useSettingsStore.setState({ adminToken: 'third-auth' })); await screen.findByRole('option', { name: '当前认证联系人' });
  await act(async () => finishOld([{ scope: 'qq_group:old', bot_scope: 'old', owner: { kind: 'group', label: '迟到的旧名称' } }]));
  expect(screen.queryByText('迟到的旧名称')).not.toBeInTheDocument();
});

it('retries a failed material name directory without changing the selected raw scope', async () => {
  const original = mockedRequest.getMockImplementation()!; let calls = 0;
  mockedRequest.mockImplementation(async (path, options) => {
    if (!path.endsWith('/scopes')) return original(path, options);
    if (++calls === 1) throw Error('offline');
    return [{ scope: 'qq_private:someone', bot_scope: 'fixture-bot', owner: { kind: 'private', label: '小雨' } }];
  });
  render(<MaterialsPage />); fireEvent.click(await screen.findByRole('button', { name: '重试会话名称' }));
  expect(await screen.findByRole('option', { name: '小雨' })).toBeInTheDocument();
  expect(screen.getByText('当前操作范围：资料工作区')).toBeInTheDocument();
});

it('distinguishes the default partition from an explicit empty partition for the same scope', async () => {
  const original = mockedRequest.getMockImplementation()!;
  mockedRequest.mockImplementation(async (path, options) => path.endsWith('/scopes') ? [{ scope: 'web:materials', bot_scope: '', current_account: false }] : original(path, options));
  render(<MaterialsPage />);
  await waitFor(() => expect(screen.getByLabelText('选择已有资料会话').querySelectorAll('option')).toHaveLength(2));
  const initial = mockedRequest.mock.calls.find(([path]) => path === '/v1/materials/sources')?.[1];
  expect(initial?.params?.bot_scope).toBeNull();
  fireEvent.change(screen.getByLabelText('选择已有资料会话'), { target: { value: scopeIdentity('web:materials', '') } });
  await waitFor(() => expect(mockedRequest).toHaveBeenCalledWith('/v1/materials/sources', expect.objectContaining({ scope: 'web:materials', params: { bot_scope: '' } })));
  const before = mockedRequest.mock.calls.filter(([path]) => path === '/v1/materials/sources').length;
  (screen.getByText('高级：会话标识').closest('details') as HTMLDetailsElement).open = true;
  fireEvent.click(screen.getByRole('button', { name: '切换会话范围' }));
  await waitFor(() => expect(mockedRequest.mock.calls.filter(([path]) => path === '/v1/materials/sources')).toHaveLength(before + 1));
  expect(mockedRequest.mock.calls.filter(([path]) => path === '/v1/materials/sources').at(-1)?.[1]?.params?.bot_scope).toBeNull();
});

it.each([null, '', 'fixture-bot'])('uses the same bot partition for upload, preview and download: %s', async botScope => {
  approved = true;
  const original = mockedRequest.getMockImplementation()!;
  mockedRequest.mockImplementation(async (path, options) => path.endsWith('/scopes') ? (botScope === null ? [] : [{ scope: 'web:materials', bot_scope: botScope }]) : original(path, options));
  vi.mocked(fetch).mockImplementation(async url => String(url).includes('/sources/upload')
    ? new Response(JSON.stringify({ id: 's1', name: 'fixture.txt' }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    : new Response(new Blob(['synthetic preview or document']), { status: 200 }));
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  try {
    render(<MaterialsPage />); await screen.findByRole('option', { name: 'Report' });
    if (botScope !== null) {
      await waitFor(() => expect(screen.getByLabelText('选择已有资料会话').querySelectorAll('option')).toHaveLength(2));
      fireEvent.change(screen.getByLabelText('选择已有资料会话'), { target: { value: scopeIdentity('web:materials', botScope) } });
    }
    await waitFor(() => expect(mockedRequest).toHaveBeenCalledWith('/v1/materials/sources', expect.objectContaining({ params: { bot_scope: botScope } })));
    fireEvent.change(screen.getByLabelText('上传资料'), { target: { files: [new File(['fixture'], 'fixture.txt', { type: 'text/plain' })] } });
    await screen.findByText('已登记 fixture.txt。');
    fireEvent.change(screen.getByLabelText('选择作品'), { target: { value: 'a1' } });
    await screen.findByAltText('预览第1页'); await screen.findByAltText('预览第2页');
    fireEvent.click(screen.getByRole('button', { name: '下载成品' }));
    await waitFor(() => expect(click).toHaveBeenCalledOnce());
    const urls = vi.mocked(fetch).mock.calls.map(([url]) => new URL(String(url), 'http://fixture.invalid'));
    expect(urls).toHaveLength(4);
    expect(urls.some(url => url.pathname.endsWith('/sources/upload'))).toBe(true);
    expect(urls.filter(url => url.pathname.includes('/preview/'))).toHaveLength(2);
    expect(urls.some(url => url.pathname.endsWith('/download'))).toBe(true);
    for (const url of urls) {
      expect(url.searchParams.get('scope')).toBe('web:materials');
      expect(url.searchParams.has('bot_scope')).toBe(botScope !== null);
      expect(url.searchParams.get('bot_scope')).toBe(botScope);
    }
  } finally { click.mockRestore(); }
});
