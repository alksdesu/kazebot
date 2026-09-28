// 能写在全局那层的键：跟随下一层，还是在本层单独写一行。平台页是「跟随全局 / 本平台」，全局页是「默认 / 自定义」。
import type { CentralSwitchState, ConsoleFieldSchema, FeatureFallback } from '../api/supervisorClient';
import { Segmented } from './components';
import { useConfigScope, type ConfigScope } from './configScope';
import { useConsoleStore, useGlobalFallbacks, useLayerState } from './consoleStore';
import { fieldValueText, nameWith } from './format';

type LayerMode = 'inherit' | 'own';

const MODES: readonly LayerMode[] = ['inherit', 'own'];

const MODE_TEXT: Record<ConfigScope['kind'], Readonly<Record<LayerMode, string>>> = {
  platform: { inherit: '跟随全局', own: '本平台' },
  features: { inherit: '默认', own: '自定义' },
};

const NOTE = 'text-xs text-[var(--duties-tertiary)]';

const fallbackUse = (fallback: FeatureFallback, value: string): string => (
  fallback.source === 'env'
    ? nameWith(fallback.display_name, `眼下用着环境变量给的「${value}」`)
    : nameWith(fallback.display_name, `眼下用着 ${fallback.file} 老位置上的「${value}」`)
);

const fallbackPlace = (fallback: FeatureFallback, value: string): string => (
  fallback.source === 'env'
    ? nameWith(fallback.display_name, `的环境变量是「${value}」`)
    : nameWith(fallback.display_name, `的 ${fallback.file} 老位置是「${value}」`)
);

/** 全局页上一项旁边的说明：全局文件没写它，平台实际用的是自己的兜底，不是页面上显示的默认值。 */
export function fallbackText(
  fallbacks: readonly FeatureFallback[],
  central: CentralSwitchState | undefined,
  spec: Pick<ConsoleFieldSchema, 'kind' | 'scale' | 'unit'>,
): string {
  if (!fallbacks.length) return '';
  // 统一跑的开关只有一个结果，平台的兜底只是依据；别的键就是平台实际用着的值。
  if (central && !central.set_globally) {
    const basis = fallbacks.map((fallback) => fallbackPlace(fallback, fieldValueText(spec, fallback.value)));
    return `实际${central.enabled ? '开着' : '关着'}：${basis.join('、')}`;
  }
  return fallbacks.map((fallback) => fallbackUse(fallback, fieldValueText(spec, fallback.value))).join('；');
}

export const LayerSwitch = ({ configKey, spec }: { configKey: string; spec: ConsoleFieldSchema }) => {
  const scope = useConfigScope();
  const { known, own, source } = useLayerState(configKey);
  const { fallbacks, central } = useGlobalFallbacks(configKey);
  const inherit = useConsoleStore((state) => state.inherit);
  const pin = useConsoleStore((state) => state.pin);
  if (!known) return null;
  const text = MODE_TEXT[scope.kind];
  const borrowed = own ? '' : fallbackText(fallbacks, central, spec);
  return (
    <span className="flex flex-none flex-wrap items-center gap-1.5">
      {/* 全局没写这一项时 bot 先用平台自己的 env，「跟随全局」得说清眼下其实是它。 */}
      {!own && source === 'env' && <span className={NOTE}>眼下用的是环境变量</span>}
      {borrowed && <span className={NOTE}>{borrowed}</span>}
      <Segmented
        choices={MODES.map((mode) => [mode, text[mode]] as const)}
        label={`${spec.label}的来源`}
        onPick={(mode) => (mode === 'own' ? pin(configKey) : inherit(configKey))}
        size="compact"
        value={own ? 'own' : 'inherit'}
      />
    </span>
  );
};
