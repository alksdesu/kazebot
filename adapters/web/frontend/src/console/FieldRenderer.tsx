// 按声明渲染分区正文。每个节点对着一个既有控件，这里只做分派，不藏任何业务规则。
import { Fragment } from 'react';

import { Block, Desc, Fixed, Footnote, Grid, Loose, Panel } from './components';
import { useLiveValue } from './consoleStore';
import { sectionComponent, type FieldSpec } from './fieldSchema';
import { BoolOption, ChoiceField, NumberField, PercentField, SubOption, TextField, TristateOption } from './fields';
import { IdList } from './IdList';
import { WordList } from './WordList';

interface Context {
  platformId: string;
  sectionId: string;
}

const Children = ({ context, specs }: { context: Context; specs?: FieldSpec[] }) => (
  <>{(specs || []).map((spec, index) => <FieldNode context={context} key={index} spec={spec} />)}</>
);

/** 默认值跟着另一个键走的三态。读那个键要占一次 hook，所以单独一层组件。 */
const DerivedTristate = ({ from, spec, when }: {
  from: string;
  spec: Extract<FieldSpec, { kind: 'tristate' }>;
  when: (value: unknown) => boolean;
}) => (
  <TristateOption
    configKey={spec.key}
    desc={spec.desc}
    fallback={when(useLiveValue<unknown>(from, ''))}
    label={spec.label}
  />
);

const CustomNode = ({ context, name }: { context: Context; name: string }) => {
  const Component = sectionComponent(context.platformId, context.sectionId, name);
  // 没登记就什么也不画：少一块内容，好过整页崩在一个拼错的名字上。
  return Component ? <Component /> : null;
};

const FieldNode = ({ context, spec }: { context: Context; spec: FieldSpec }) => {
  switch (spec.kind) {
    case 'block':
      return (
        <Block frame={spec.frame} hint={spec.hint} title={spec.title}>
          <Children context={context} specs={spec.children} />
        </Block>
      );
    case 'grid':
      return <Grid><Children context={context} specs={spec.children} /></Grid>;
    case 'panel':
      return <Panel className={spec.className}><Children context={context} specs={spec.children} /></Panel>;
    case 'loose':
      return <Loose><Children context={context} specs={spec.children} /></Loose>;
    case 'bool':
      return (
        <BoolOption configKey={spec.key} desc={spec.desc} fallback={spec.fallback} label={spec.label}>
          {spec.children?.length ? <Children context={context} specs={spec.children} /> : undefined}
        </BoolOption>
      );
    case 'tristate':
      return typeof spec.fallback === 'boolean'
        ? <TristateOption configKey={spec.key} desc={spec.desc} fallback={spec.fallback} label={spec.label} />
        : <DerivedTristate from={spec.fallback.from} spec={spec} when={spec.fallback.when} />;
    case 'sub':
      return <SubOption configKey={spec.key} fallback={spec.fallback} label={spec.label} />;
    case 'number':
      return (
        <NumberField
          configKey={spec.key}
          label={spec.label}
          max={spec.max}
          scale={spec.scale}
          step={spec.step}
          unit={spec.unit}
        />
      );
    case 'percent':
      return <PercentField configKey={spec.key} label={spec.label} />;
    case 'text':
      return <TextField configKey={spec.key} label={spec.label} />;
    case 'choice':
      return <ChoiceField choices={spec.choices} configKey={spec.key} fallback={spec.fallback} label={spec.label} />;
    case 'words':
      return (
        <WordList
          configKey={spec.key}
          empty={spec.empty}
          label={spec.label}
          placeholder={spec.placeholder}
          spacing={spec.spacing}
        />
      );
    case 'ids':
      return (
        <IdList
          configKey={spec.key}
          empty={spec.empty}
          label={spec.label}
          placeholder={spec.placeholder}
          spacing={spec.spacing}
        />
      );
    case 'fixed':
      return (
        <Fixed desc={spec.desc} name={spec.label}>
          {spec.children?.length ? <Children context={context} specs={spec.children} /> : undefined}
        </Fixed>
      );
    case 'desc':
      return <Desc indent={spec.indent}>{spec.text}</Desc>;
    case 'note':
      return <Footnote>{spec.text}</Footnote>;
    case 'custom':
      return <CustomNode context={context} name={spec.name} />;
  }
};

export const SchemaFields = ({ platformId, sectionId, spec }: {
  platformId: string;
  sectionId: string;
  spec: readonly FieldSpec[];
}) => (
  <Fragment>
    {spec.map((node, index) => <FieldNode context={{ platformId, sectionId }} key={index} spec={node} />)}
  </Fragment>
);
