// 按后端 schema 渲染分区正文。每个节点对着一个既有控件，这里只做分派，不藏任何业务规则。
import { Fragment, type ReactNode } from 'react';

import type { ConsoleFieldSchema, ConsoleNode } from '../api/supervisorClient';
import { Block, Desc, Fixed, Footnote, Grid, Loose, Panel } from './components';
import { useLiveValue } from './consoleStore';
import { BoolOption, ChoiceField, NumberField, PercentField, SubOption, TextField, TristateOption } from './fields';
import { IdList } from './IdList';
import { LayerSwitch } from './LayerSwitch';
import { RichText } from './RichText';
import { useSectionParts } from './sectionParts';
import { WordList } from './WordList';

export type SchemaFieldMap = Readonly<Record<string, ConsoleFieldSchema>>;

interface Context {
  fields: SchemaFieldMap;
  sectionId: string;
}

type FieldNodeSpec = Extract<ConsoleNode, { kind: 'field' }>;

const rich = (text: string | undefined): ReactNode => (text ? <RichText text={text} /> : undefined);

const Children = ({ context, nodes }: { context: Context; nodes?: readonly ConsoleNode[] }) => (
  <>{(nodes || []).map((node, index) => <SchemaNode context={context} key={index} node={node} />)}</>
);

/** 没配时跟着另一个键推导的三态。读那个键要占一次 hook，所以单独一层组件。 */
const DerivedTristate = ({ aside, configKey, follows, spec }: {
  aside?: ReactNode;
  configKey: string;
  follows: string;
  spec: ConsoleFieldSchema;
}) => {
  const source = useLiveValue<unknown>(follows, '');
  const when = spec.follows_when || [];
  return (
    <TristateOption
      aside={aside}
      configKey={configKey}
      desc={rich(spec.desc)}
      fallback={when.includes(String(source).toLowerCase())}
      label={spec.label}
    />
  );
};

const CustomNode = ({ context, name }: { context: Context; name: string }) => {
  const Component = useSectionParts(context.sectionId)?.components?.[name];
  // 没登记就什么也不画：少一块内容，好过整页崩在一个拼错的名字上。
  return Component ? <Component /> : null;
};

const FieldControl = ({ context, node }: { context: Context; node: FieldNodeSpec }) => {
  const spec = context.fields[node.key];
  // 后端建 schema 时校验过摆出来的键都有说明；缺了只可能是前后端版本错开，少画一格比崩掉整页好。
  if (!spec) return null;
  const children = node.children?.length ? <Children context={context} nodes={node.children} /> : undefined;
  // 全局那层写得进的键才谈得上跟随：平台页上是「跟随全局 / 本平台」，全局页上每一项都能回到默认。
  const aside = spec.layers.includes('global') ? <LayerSwitch configKey={node.key} spec={spec} /> : undefined;
  switch (spec.kind) {
    case 'bool':
      return node.sub
        ? <SubOption aside={aside} configKey={node.key} fallback={spec.default === true} label={spec.label} />
        : (
          <BoolOption aside={aside} configKey={node.key} desc={rich(spec.desc)} fallback={spec.default === true} label={spec.label}>
            {children}
          </BoolOption>
        );
    case 'tristate':
      return spec.follows
        ? <DerivedTristate aside={aside} configKey={node.key} follows={spec.follows} spec={spec} />
        : (
          <TristateOption
            aside={aside}
            configKey={node.key}
            desc={rich(spec.desc)}
            fallback={spec.unset === true}
            label={spec.label}
          />
        );
    case 'number':
      return (
        <NumberField
          aside={aside}
          configKey={node.key}
          fallback={typeof spec.default === 'number' ? spec.default : 0}
          label={spec.label}
          max={spec.max}
          scale={spec.scale}
          step={spec.step}
          unit={spec.unit}
        />
      );
    case 'percent':
      return <PercentField aside={aside} configKey={node.key} label={spec.label} />;
    case 'text':
      return <TextField aside={aside} configKey={node.key} label={spec.label} />;
    case 'choice':
      return (
        <ChoiceField
          aside={aside}
          choices={spec.choices || []}
          configKey={node.key}
          fallback={typeof spec.default === 'string' ? spec.default : ''}
          label={spec.label}
        />
      );
    case 'words':
      return (
        <WordList
          aside={aside}
          configKey={node.key}
          empty={spec.empty || ''}
          label={spec.label}
          placeholder={spec.placeholder || ''}
          spacing={node.spacing}
        />
      );
    case 'ids':
      return (
        <IdList
          aside={aside}
          configKey={node.key}
          empty={spec.empty || ''}
          label={spec.label}
          placeholder={spec.placeholder || ''}
          spacing={node.spacing}
        />
      );
    default:
      return null;
  }
};

const SchemaNode = ({ context, node }: { context: Context; node: ConsoleNode }) => {
  switch (node.kind) {
    case 'block':
      return (
        <Block frame={node.frame} hint={node.hint} title={node.title}>
          <Children context={context} nodes={node.children} />
        </Block>
      );
    case 'grid':
      return <Grid><Children context={context} nodes={node.children} /></Grid>;
    case 'panel':
      return <Panel><Children context={context} nodes={node.children} /></Panel>;
    case 'loose':
      return <Loose><Children context={context} nodes={node.children} /></Loose>;
    case 'fixed':
      return (
        <Fixed desc={rich(node.desc)} name={node.label}>
          {node.children?.length ? <Children context={context} nodes={node.children} /> : undefined}
        </Fixed>
      );
    case 'field':
      return <FieldControl context={context} node={node} />;
    case 'desc':
      return <Desc indent={node.indent}><RichText text={node.text} /></Desc>;
    case 'note':
      return <Footnote><RichText text={node.text} /></Footnote>;
    case 'custom':
      return <CustomNode context={context} name={node.name} />;
    default:
      return null;
  }
};

/** 一个分区的正文。fields 是整页 schema 的控件说明，按键名查。 */
export const SchemaFields = ({ fields, nodes, sectionId }: {
  fields: SchemaFieldMap;
  nodes: readonly ConsoleNode[];
  sectionId: string;
}) => (
  <Fragment>
    {nodes.map((node, index) => <SchemaNode context={{ fields, sectionId }} key={index} node={node} />)}
  </Fragment>
);
