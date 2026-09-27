// 分区里前端自己画的部分：布局编不成字段的那几块（扫码、试听、能力清单），和收起时的一行摘要。
// 页面把整张表交给 Provider，渲染器按「分区 + 名字」取，不靠模块加载时往全局表里登记。
import { createContext, useContext, type ReactNode } from 'react';

export type SectionComponent = () => JSX.Element | null;

export interface SectionParts {
  /** 收起时那一行。只读生效值，不看草稿。 */
  Summary?: () => JSX.Element;
  /** 布局里 custom 节点点名的组件。 */
  components?: Readonly<Record<string, SectionComponent>>;
}

/** 分区 id → 这个分区的前端部分。 */
export type PageParts = Readonly<Record<string, SectionParts>>;

export const NO_PARTS: PageParts = Object.freeze({});

const PartsContext = createContext<PageParts>(NO_PARTS);

export const PartsProvider = ({ children, parts }: { children: ReactNode; parts: PageParts }) => (
  <PartsContext.Provider value={parts}>{children}</PartsContext.Provider>
);

export const useSectionParts = (sectionId: string): SectionParts | undefined => useContext(PartsContext)[sectionId];
