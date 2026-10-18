import { Component, type ReactNode } from 'react';
import { extensions, useExtensionRevision, type ExtensionKind } from './registry';
import { reportPluginError } from './status';

class ContributionBoundary extends Component<{ owner: string; generation: string; fallback: ReactNode; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(error: Error) { reportPluginError(this.props.owner, this.props.generation, error); }
  render() { return this.state.failed ? this.props.fallback : this.props.children; }
}
function RenderValue<T>({ value, render }: { value: T; render: (value: T) => ReactNode }) { return <>{render(value)}</>; }

export function PluginRender<T>({ kind, id, base, render, resetKey = '' }: {
  kind: ExtensionKind; id: string; base?: T; render: (value: T) => ReactNode; resetKey?: string;
}) {
  useExtensionRevision();
  const layers = extensions.chain<T>(kind, id, base);
  let result: ReactNode = layers.length ? <p role="alert">插件区域暂时无法显示，可在插件设置中查看错误。</p> : null;
  for (const layer of layers) {
    if (layer.owner === 'builtin') { result = <RenderValue value={layer.value} render={render} />; continue; }
    result = <ContributionBoundary key={`${layer.owner}:${layer.generation}:${resetKey}`} owner={layer.owner} generation={layer.generation} fallback={result}>
      <RenderValue value={layer.value} render={render} />
    </ContributionBoundary>;
  }
  return <>{result}</>;
}
