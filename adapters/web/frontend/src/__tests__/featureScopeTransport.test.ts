import { describe, expect, it } from 'vitest';
import { featureUrl } from '../features/client';

describe('资料分区查询参数', () => {
  it('显式空bot_scope保留在URL，不能退回默认分区', () => {
    const url = new URL(featureUrl('/v1/materials/sources', { scope: 'web:materials', params: { bot_scope: '', query: '' } }), 'http://fixture.invalid');
    expect(url.searchParams.has('bot_scope')).toBe(true);
    expect(url.searchParams.get('bot_scope')).toBe(''); expect(url.searchParams.has('query')).toBe(false);
  });
  it.each([undefined, null, 'bot-one'])('未指定与非空bot_scope保持原语义：%s', value => {
    const url = new URL(featureUrl('/v1/materials/sources', { params: { bot_scope: value } }), 'http://fixture.invalid');
    expect(url.searchParams.has('bot_scope')).toBe(value === 'bot-one');
    if (value === 'bot-one') expect(url.searchParams.get('bot_scope')).toBe(value);
  });
});
