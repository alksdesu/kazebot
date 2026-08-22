// provider 下拉的两组选项。渠道名带着地址密钥，裸线格式只定请求怎么发。
import { describe, expect, it } from 'vitest';

import { channelChoices, type ProvidersResponse } from '../api/supervisorClient';

const block = (over: Record<string, unknown> = {}) => ({
  base_url: '', model: '', base_url_raw: '', model_raw: '',
  api_key_present: true, api_key_redacted: '****',
  supports_vision: null, type: '', type_explicit: false, label: '', ...over,
}) as any;

const resp = (providers: Record<string, unknown>, registered: string[]): ProvidersResponse => ({
  active_provider: 'openai',
  providers: providers as any,
  fallbacks: [],
  node_fallbacks: {},
  registered,
});

describe('渠道选项', () => {
  it('每个配过的块都是一个渠道', () => {
    const { channels } = channelChoices(resp({
      'gemini-中转A': block({ type: 'gemini' }),
      openai: block(),
    }, ['openai', 'gemini']));

    expect(channels.map((c) => c.value)).toEqual(['gemini-中转A', 'openai']);
  });

  it('备注跟在名字后面，没备注就只有名字', () => {
    const { channels } = channelChoices(resp({
      a: block({ label: '便宜那个' }),
      b: block(),
    }, []));

    expect(channels[0].label).toBe('a · 便宜那个');
    expect(channels[1].label).toBe('b');
  });

  it('线格式取块里的 type，没写就退回块名', () => {
    const { channels } = channelChoices(resp({
      'relay-A': block({ type: 'gemini' }),
      deepseek: block(),
    }, []));

    expect(channels.find((c) => c.value === 'relay-A')!.wire).toBe('gemini');
    expect(channels.find((c) => c.value === 'deepseek')!.wire).toBe('deepseek');
  });

  it('块名大写照原样留着 —— 后端按原名查块', () => {
    const { channels } = channelChoices(resp({ 'Relay-A': block({ type: 'openai' }) }, []));

    expect(channels[0].value).toBe('Relay-A');
    expect(channels[0].wire).toBe('openai');
  });

  it('已经配过块的那家不再出现在线格式里', () => {
    // 有同名块时这个名字一定被解析成渠道，两个选项结果完全一样。
    const { wires } = channelChoices(resp(
      { deepseek: block() }, ['openai', 'deepseek', 'gemini'],
    ));

    expect(wires).toEqual(['openai', 'gemini']);
  });

  it('一个渠道都没配时线格式全列出来', () => {
    expect(channelChoices(resp({}, ['openai', 'gemini'])).wires).toEqual(['openai', 'gemini']);
  });

  it('拿不到数据时两组都是空的，不炸', () => {
    expect(channelChoices(null)).toEqual({ channels: [], wires: [] });
    expect(channelChoices(undefined)).toEqual({ channels: [], wires: [] });
  });
});
