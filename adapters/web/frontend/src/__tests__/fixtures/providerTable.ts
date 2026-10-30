// 渠道表桩：字段按接口类型给齐，用例只写它关心的那几项。getProviders 按形状读，缺一项整张表就读不成。
import type { FallbackEntryPublic, ProviderConfigPublic, ProvidersResponse } from '../../api/supervisorClient';

export function providerBlock(overrides: Partial<ProviderConfigPublic> = {}): ProviderConfigPublic {
  return {
    base_url: '', model: '', base_url_raw: '', model_raw: '', api_key_present: false, api_key_redacted: '',
    supports_vision: null, type: 'openai', type_explicit: false, label: '', ...overrides,
  };
}

export function fallbackEntry(overrides: Partial<FallbackEntryPublic> = {}): FallbackEntryPublic {
  return {
    provider: 'openai', base_url: '', model: '', base_url_raw: '', model_raw: '', api_key_present: false,
    api_key_redacted: '', supports_vision: false, options: {}, ...overrides,
  };
}

export function providerTable(overrides: Partial<ProvidersResponse> = {}): ProvidersResponse {
  return {
    active_provider: 'openai', providers: {}, fallbacks: [], node_fallbacks: {}, registered: [], reserved_names: [],
    ...overrides,
  };
}
