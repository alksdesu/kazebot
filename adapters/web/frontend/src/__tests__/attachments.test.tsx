// 消息附件：工作区文件带令牌取回成 blob 再显示或下载，令牌不进地址；只有普通位图能在新标签页直接打开；外部链接不自动加载。
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AttachmentList } from '../components/chat/AttachmentList';
import { useSettingsStore } from '../store/settingsStore';

const TOKEN = 'attachment-token';

const fileResponse = (body: string, type: string): Response => new Response(body, { status: 200, headers: { 'Content-Type': type } });
const authorization = (init?: RequestInit): string | null => new Headers(init?.headers).get('Authorization');

let objectUrls = 0;

beforeEach(() => {
  objectUrls = 0;
  useSettingsStore.setState({ adminToken: TOKEN });
  vi.stubGlobal('URL', Object.assign(URL, {
    createObjectURL: vi.fn(() => `blob:attachment-${++objectUrls}`),
    revokeObjectURL: vi.fn(),
  }));
});

afterEach(() => {
  useSettingsStore.setState({ adminToken: '' });
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('工作区里的图片', () => {
  it('带着请求头取回再显示，地址里没有令牌；位图可以在新标签页打开', async () => {
    const fetchMock = vi.fn(async () => fileResponse('png-bytes', 'image/png'));
    vi.stubGlobal('fetch', fetchMock);

    render(<AttachmentList attachments={[{ name: 'pic.png', type: 'image', path: 'data/attachments/a b/pic.png' }]} />);

    await waitFor(() => expect(screen.getByRole('img', { name: 'pic.png' })).toHaveAttribute('src', 'blob:attachment-1'));
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/v1/files/data/attachments/a%20b/pic.png');
    expect(url).not.toContain(TOKEN);
    expect(authorization(init)).toBe(`Bearer ${TOKEN}`);
    const link = screen.getByRole('link');
    expect(link).toHaveAttribute('href', 'blob:attachment-1');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  // blob 地址和控制台同源：SVG、HTML 在新标签页打开就能以控制台的身份跑脚本。
  it('SVG 只当图片显示，不给新标签页打开的链接', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => fileResponse('<svg xmlns="http://www.w3.org/2000/svg"/>', 'image/svg+xml')));

    render(<AttachmentList attachments={[{ name: 'logo.svg', mime_type: 'image/svg+xml', path: 'data/attachments/logo.svg' }]} />);

    await waitFor(() => expect(screen.getByRole('img', { name: 'logo.svg' })).toHaveAttribute('src', 'blob:attachment-1'));
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('取不回来就说读取失败', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'File not found' }), { status: 404 })));

    render(<AttachmentList attachments={[{ name: 'gone.png', type: 'image', path: 'data/attachments/gone.png' }]} />);

    expect(await screen.findByText('gone.png（图片读取失败）')).toBeInTheDocument();
    expect(screen.queryByRole('img', { name: 'gone.png' })).toBeNull();
  });

  it('卸载时中止请求并释放 object URL', async () => {
    const fetchMock = vi.fn(async () => fileResponse('png-bytes', 'image/png'));
    vi.stubGlobal('fetch', fetchMock);

    const view = render(<AttachmentList attachments={[{ name: 'pic.png', type: 'image', path: 'data/attachments/pic.png' }]} />);
    await waitFor(() => expect(screen.getByRole('img', { name: 'pic.png' })).toHaveAttribute('src', 'blob:attachment-1'));
    view.unmount();

    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:attachment-1');
    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(init.signal?.aborted).toBe(true);
  });
});

describe('工作区里的文件', () => {
  it('点一下带着请求头下载，存成原来的文件名', async () => {
    const fetchMock = vi.fn(async () => fileResponse('report', 'application/pdf'));
    vi.stubGlobal('fetch', fetchMock);
    const clicked: HTMLAnchorElement[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { clicked.push(this); });

    render(<AttachmentList attachments={[{ name: 'report.pdf', size: 2048, path: 'data/attachments/report.pdf' }]} />);
    fireEvent.click(screen.getByRole('button', { name: /report\.pdf \(2 KB\)/ }));

    await waitFor(() => expect(clicked).toHaveLength(1));
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/v1/files/data/attachments/report.pdf');
    expect(authorization(init)).toBe(`Bearer ${TOKEN}`);
    expect(clicked[0].download).toBe('report.pdf');
    expect(clicked[0].getAttribute('href')).toBe('blob:attachment-1');
  });

  it('下载失败把原因说出来', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'File not found' }), { status: 404 })));

    render(<AttachmentList attachments={[{ name: 'report.pdf', path: 'data/attachments/report.pdf' }]} />);
    fireEvent.click(screen.getByRole('button', { name: /report\.pdf/ }));

    expect(await screen.findByRole('alert')).toHaveTextContent('404 File not found');
  });
});

describe('不在工作区里的附件', () => {
  // 外链图片一显示就会被请求，地址里可能带着对话内容；只给链接，点了才去。
  it('外部图片只给链接，不自动加载', () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    render(<AttachmentList attachments={[{ name: 'remote.png', type: 'image', url: 'https://img.example/remote.png' }]} />);

    expect(screen.queryByRole('img')).toBeNull();
    const link = screen.getByRole('link', { name: /remote\.png/ });
    expect(link).toHaveAttribute('href', 'https://img.example/remote.png');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('刚选的本地文件预览直接显示', () => {
    render(<AttachmentList attachments={[{ name: 'local.png', type: 'image', url: 'blob:local-preview' }]} />);

    expect(screen.getByRole('img', { name: 'local.png' })).toHaveAttribute('src', 'blob:local-preview');
  });

  it('认不出的地址不给可点的链接', () => {
    render(<AttachmentList attachments={[{ name: 'odd.txt', url: 'javascript:alert(1)' }]} />);

    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.getByText('odd.txt')).toBeInTheDocument();
  });
});
