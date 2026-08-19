// 地址填成别家的官方域名时提醒一句。判据来自后端下发的 provider 声明，界面不自己写一份。
//
// 这个提示只提示不阻断：中转站的域名看不出出身，拦错比放过更烦人。
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { HostMismatchHint, hostMismatch } from '../console/channelFields';

const PROFILES = {
  options: {},
  wireFormats: {
    openai: 'openai',
    deepseek: 'openai',
    anthropic: 'anthropic',
    gemini: 'gemini',
    'openai-responses': 'openai-responses',
  },
  defaultVision: { openai: true, deepseek: false, anthropic: true, gemini: true },
  hostProfiles: {
    'api.openai.com': ['openai', 'openai-responses'],
    'api.deepseek.com': ['openai'],
    'api.anthropic.com': ['anthropic'],
    'generativelanguage.googleapis.com': ['gemini'],
  },
};

describe('认得出填串了', () => {
  it('anthropic 的地址填进 openai 渠道要说一句', () => {
    const hit = hostMismatch('https://api.anthropic.com', 'openai', PROFILES);

    expect(hit).toEqual({ looksLike: 'anthropic', wire: 'openai' });
  });

  it('不带协议头也认得出来', () => {
    expect(hostMismatch('api.anthropic.com/v1', 'openai', PROFILES)).toBeTruthy();
  });

  it('gemini 的地址填进 anthropic 渠道也算', () => {
    expect(hostMismatch('https://generativelanguage.googleapis.com', 'anthropic', PROFILES))
      .toEqual({ looksLike: 'gemini', wire: 'anthropic' });
  });

  it('一个域名收两族时把两族都报出来', () => {
    expect(hostMismatch('https://api.openai.com/v1', 'anthropic', PROFILES)?.looksLike)
      .toBe('openai 或 openai-responses');
  });
});

describe('该闭嘴的时候闭嘴', () => {
  it('deepseek 的地址填进 openai 渠道是正常用法', () => {
    // DeepSeek 就是 OpenAI 格式，这么填能跑，提示会变成噪音。
    expect(hostMismatch('https://api.deepseek.com', 'openai', PROFILES)).toBeNull();
  });

  it('同族的另一个 provider 也不提示', () => {
    expect(hostMismatch('https://api.openai.com', 'deepseek', PROFILES)).toBeNull();
  });

  it('填对了当然不提示', () => {
    expect(hostMismatch('https://api.anthropic.com', 'anthropic', PROFILES)).toBeNull();
  });

  it('中转站的域名认不出，一律放行', () => {
    expect(hostMismatch('https://api.oneapi.example/v1', 'openai', PROFILES)).toBeNull();
    expect(hostMismatch('https://relay.mycompany.cn/v1', 'anthropic', PROFILES)).toBeNull();
  });

  it('变量引用不当成地址', () => {
    expect(hostMismatch('${MY_BASE_URL}', 'openai', PROFILES)).toBeNull();
  });

  it('地址留空不提示', () => {
    expect(hostMismatch('', 'openai', PROFILES)).toBeNull();
  });

  it('渠道名为空不提示', () => {
    // 系统槽位「跟随主渠道」时不知道最终发哪种格式，猜不了就别猜。
    expect(hostMismatch('https://api.anthropic.com', '', PROFILES)).toBeNull();
  });

  it('后端那份清单没拿到就不提示', () => {
    expect(hostMismatch('https://api.anthropic.com', 'openai', null)).toBeNull();
  });

  it('注册表里没有的渠道名按它自己一族算', () => {
    expect(hostMismatch('https://api.anthropic.com', 'anthropic', {
      ...PROFILES, wireFormats: {},
    })).toBeNull();
  });
});

describe('提示文案', () => {
  it('说清楚是哪一家、会怎样、下一步做什么', () => {
    render(<HostMismatchHint baseUrl="https://api.anthropic.com" profiles={PROFILES} provider="openai" />);

    const text = screen.getByText(/这个地址是 anthropic 的/).textContent || '';
    expect(text).toContain('会被对面拒掉');
    expect(text).toContain('新建一个 anthropic 渠道');
  });

  it('没问题时什么都不渲染', () => {
    const { container } = render(
      <HostMismatchHint baseUrl="https://api.deepseek.com" profiles={PROFILES} provider="openai" />,
    );

    expect(container.innerHTML).toBe('');
  });
});
