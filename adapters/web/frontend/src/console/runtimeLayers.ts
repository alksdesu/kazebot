// runtime 表单照实际生效的值显示，并标出哪些项本实例没写、跟着默认走。生效值由后端照运行时那套合并算好。
import type { RuntimeLayers } from '../api/supervisorClient';
import { runtimeInstanceLayer } from './yamlCommit';
import { parseYamlMapping, readYamlPath, yamlMappingProblem } from './yamlRewrite';

/** 这一项实际在用的值；哪一层都没写是 undefined。 */
export const effectiveValue = (layers: RuntimeLayers, path: readonly string[]): unknown => readYamlPath(layers.effective, path);

/** 本实例那份没写这一项，生效的是默认那层的值。那份写坏了读不出来时说不准，按没跟随默认算，不贴这个标。 */
export function followsDefault(layers: RuntimeLayers, path: readonly string[]): boolean {
  const own = runtimeInstanceLayer(layers);
  if (yamlMappingProblem(own)) return false;
  return readYamlPath(parseYamlMapping(own), path) === undefined;
}
