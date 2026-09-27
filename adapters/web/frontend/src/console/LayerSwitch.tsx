// 能写在全局那层的键：跟随下一层，还是在本层单独写一行。平台页是「跟随全局 / 本平台」，全局页是「默认 / 自定义」。
import { Segmented } from './components';
import { useConfigScope, type ConfigScope } from './configScope';
import { useConsoleStore, useLayerState } from './consoleStore';

type LayerMode = 'inherit' | 'own';

const MODES: readonly LayerMode[] = ['inherit', 'own'];

const MODE_TEXT: Record<ConfigScope['kind'], Readonly<Record<LayerMode, string>>> = {
  platform: { inherit: '跟随全局', own: '本平台' },
  features: { inherit: '默认', own: '自定义' },
};

export const LayerSwitch = ({ configKey, label }: { configKey: string; label: string }) => {
  const scope = useConfigScope();
  const { known, own, source } = useLayerState(configKey);
  const inherit = useConsoleStore((state) => state.inherit);
  const pin = useConsoleStore((state) => state.pin);
  if (!known) return null;
  const text = MODE_TEXT[scope.kind];
  return (
    <span className="flex flex-none flex-wrap items-center gap-1.5">
      {/* 全局没写这一项时 bot 先用平台自己的 env，「跟随全局」得说清眼下其实是它。 */}
      {!own && source === 'env' && <span className="text-xs text-[var(--duties-tertiary)]">眼下用的是环境变量</span>}
      <Segmented
        choices={MODES.map((mode) => [mode, text[mode]] as const)}
        label={`${label}的来源`}
        onPick={(mode) => (mode === 'own' ? pin(configKey) : inherit(configKey))}
        size="compact"
        value={own ? 'own' : 'inherit'}
      />
    </span>
  );
};
