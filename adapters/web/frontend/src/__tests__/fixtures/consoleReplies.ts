// 热载配置的生效状态和试听的回包桩，照 supervisor 的回包和 bot 公布的快照给齐：客户端按形状读，桩缺一项就会被当成读不懂。
import type {
  ConfigFileFingerprint,
  ConfigInputRule,
  ConfigLiveState,
  ConfigSource,
  FeaturesLiveState,
  PlatformDryRunResult,
} from '../../api/supervisorClient';

type LiveSnapshot = NonNullable<ConfigLiveState['state']>;

export const MISSING_FILE: ConfigFileFingerprint = { exists: false, mtime_ns: 0, size: 0 };

export function fingerprint(overrides: Partial<ConfigFileFingerprint> = {}): ConfigFileFingerprint {
  return { exists: true, mtime_ns: 1, size: 1, ...overrides };
}

/** bot 还没公布生效配置：只有原因和磁盘上那份文件的指纹。 */
export function unpublishedStateReply(file: ConfigFileFingerprint = MISSING_FILE): ConfigLiveState {
  return { published: false, reason: 'bot 进程尚未公布生效配置（未启动，或还没跑完第一轮对齐）', file };
}

const QQ_SOURCES: Record<string, ConfigSource> = { signal_at: 'platform', keyword_words: 'default', allowed_groups: 'platform' };
const QQ_INPUT_RULES: Record<string, ConfigInputRule> = {
  keyword_words: { kind: 'word', trim: true },
  allowed_groups: { kind: 'id', pattern: '^[0-9]+$', min: 1, max: 9007199254740991 },
};

/** QQ 的 bot 公布的快照：热载配置、链路实况、能力清单和运行期事实。客户端类型没列的几项照 bot 带上。 */
export function qqSnapshot(overrides: Partial<LiveSnapshot> = {}): LiveSnapshot {
  const snapshot = {
    published_at: 1761613200.5,
    connected: true,
    account: '1000001',
    backlog: 0,
    config_path: '/srv/kazebot/config/qq.yaml',
    file: fingerprint(),
    loaded: fingerprint(),
    parse_error: '',
    layer: 'platform',
    values: { signal_at: true, keyword_words: ['早安'], allowed_groups: [101] },
    sources: QQ_SOURCES,
    layers: { signal_at: ['global', 'platform'], keyword_words: ['global', 'platform'], allowed_groups: ['platform'] },
    reload: { signal_at: 'live', keyword_words: 'live', allowed_groups: 'reconciled' },
    notes: {},
    paths: { signal_at: 'trigger.signals.at', keyword_words: 'trigger.keyword.words', allowed_groups: 'channels.allowed_groups' },
    input_rules: QQ_INPUT_RULES,
    defaults: { config_path: '/srv/kazebot/config/features.yaml', file: fingerprint(), loaded: fingerprint(), parse_error: '' },
    capabilities: [{
      key: 'clear_memory',
      name: '清空群记忆',
      desc: '清掉某个群的对话记忆。给到群主时只能清他自己那个群。',
      default: 'admin',
      group_scoped: true,
      grants: ['off', 'admin', 'owner', 'group_admin'],
    }],
    runtime: {
      pid: 4242,
      queue_workers_running: 0,
      forward_bridge_running: false,
      forward_bridge_endpoint: 'http://127.0.0.1:8769/qq_forward',
      forward_bridge_token_set: false,
      group_history_capacities: [20],
      environment: {
        supervisor_url: 'http://127.0.0.1:8765', workspace: '/srv/kazebot', hash_secret_set: true, conversation_digest_salted: true,
      },
      volatile: { cached_groups: 0, history_gap_groups: 0 },
    },
    group_names: {},
    ...overrides,
  };
  return snapshot;
}

/** bot 公布过的平台生效状态。 */
export function publishedStateReply(
  state: LiveSnapshot = qqSnapshot(), overrides: Partial<ConfigLiveState> = {},
): ConfigLiveState {
  return { published: true, age_sec: 1.5, stale: false, applied: true, file: fingerprint(), state, ...overrides };
}

/** 全局设置的生效状态由 supervisor 当场算，总是 published；platforms 只列本实例在跑的那个平台。 */
export function featuresStateReply(overrides: Partial<FeaturesLiveState> = {}): FeaturesLiveState {
  return {
    published: true,
    applied: true,
    file: fingerprint(),
    state: {
      config_path: '/srv/kazebot/config/features.yaml',
      file: fingerprint(),
      loaded: fingerprint(),
      parse_error: '',
      layer: 'global',
      values: { sticker_auto_tag: false },
      sources: { sticker_auto_tag: 'default' },
      layers: { sticker_auto_tag: ['global'] },
      reload: { sticker_auto_tag: 'live' },
      notes: {},
      paths: { sticker_auto_tag: 'extensions.sticker.auto_tag' },
      input_rules: {},
    },
    platforms: [{ id: 'qq', display_name: 'QQ · NapCat', published: true, age_sec: 1.5, stale: false, applied: true }],
    fallbacks: {
      sticker_auto_tag: [{ platform: 'qq', display_name: 'QQ · NapCat', source: 'legacy', file: 'qq.yaml', value: true }],
    },
    central: { sticker_auto_tag: { enabled: true, set_globally: false } },
    ...overrides,
  };
}

/** 一条叫到名字、在场判断开着的试听。 */
export function dryRunReply(overrides: Partial<PlatformDryRunResult> = {}): PlatformDryRunResult {
  return {
    enabled_signals: ['at', 'reply', 'name'],
    cooldown_applied: true,
    presence: {
      active: true,
      window_sec: 90,
      absent_judge_probability: 0.1,
      quiet_wait_sec: 4,
      quiet_max_wait_sec: 20,
      density_limit: 3,
      density_window_sec: 300,
    },
    results: [{
      index: 0,
      text: '小风在吗',
      at_sec: 0,
      triggered: true,
      signal: 'name',
      blocked_by: '',
      reason: '叫到了名字',
      undetermined: false,
      cooldown_remaining: 0,
      source: 'name',
      presence: 'present',
      quiet: '',
      gate: '',
      gate_probability: 0,
      density_left: null,
      present_until: 90,
    }],
    ...overrides,
  };
}
