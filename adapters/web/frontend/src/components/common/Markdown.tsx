// 对话内容的 Markdown：链接一律新标签页打开，图片不自动加载，只给一个链接。
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

const EXTERNAL_REL = 'noopener noreferrer';

// 远程图片一渲染就会向对方发请求，图片地址里能拼进对话内容；点了才打开。
const components: Components = {
  a: ({ node: _node, ...props }) => <a {...props} rel={EXTERNAL_REL} target="_blank" />,
  img: ({ src, alt, title }) => {
    const label = `图片：${alt?.trim() || title?.trim() || '未命名'}`;
    return src
      ? <a href={src} rel={EXTERNAL_REL} target="_blank" title={title}>{label}</a>
      : <span>{label}</span>;
  },
};

export const Markdown = ({ children }: { children: string }) => (
  <ReactMarkdown components={components} remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
);
