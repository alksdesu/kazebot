// 输入处理：群友发来的东西里，哪些交给模型看。
import { QQ_NAPCAT } from '../platforms/registry';
import { SchemaFields } from './FieldRenderer';
import type { FieldSpec } from './fieldSchema';

const SECTION = 'input';

const MB = 1024 * 1024;

const SCHEMA: FieldSpec[] = [
  {
    kind: 'block',
    title: '能接收什么',
    hint: '群友发来的东西里，哪些交给模型看',
    children: [{
      kind: 'grid',
      children: [
        {
          kind: 'bool',
          key: 'enable_image_input',
          label: '看图片',
          desc: '把图片交给视觉节点识别。',
          fallback: true,
          children: [
            { kind: 'number', key: 'max_images_per_turn', label: '每轮最多', unit: '张' },
            { kind: 'number', key: 'image_max_bytes', label: '单图上限', scale: MB, step: 1, unit: 'MB' },
            { kind: 'number', key: 'image_wait_after_text_sec', label: '等图窗口', step: 0.5, unit: '秒' },
            { kind: 'desc', text: 'QQ 常把「文字＋图」拆成两条发过来，收到纯文本后等这么久看有没有图跟上。' },
          ],
        },
        {
          kind: 'bool',
          key: 'enable_file_input',
          label: '读文件',
          desc: '读取群文件的内容。',
          fallback: true,
          children: [
            { kind: 'number', key: 'max_files_per_turn', label: '每轮最多', unit: '个' },
            { kind: 'number', key: 'file_max_bytes', label: '单文件上限', scale: MB, step: 1, unit: 'MB' },
            {
              kind: 'words',
              key: 'file_extra_allowed_extensions',
              label: '额外放行',
              empty: '只收内置白名单里的类型',
              placeholder: '输入一个扩展名，回车添加',
            },
            { kind: 'desc', text: '可执行文件与脚本类型不受这里影响，填了也不会收。' },
          ],
        },
        {
          kind: 'bool',
          key: 'enable_forward_msg_input',
          label: '读合并转发',
          desc: '展开合并转发的聊天记录。',
          fallback: true,
          children: [
            { kind: 'number', key: 'forward_msg_max_depth', label: '嵌套深度', unit: '层' },
            { kind: 'number', key: 'forward_msg_max_messages', label: '最多', step: 10, unit: '条' },
            { kind: 'number', key: 'forward_msg_text_limit', label: '总字数', step: 1000, unit: '字' },
            { kind: 'number', key: 'forward_msg_timeout_sec', label: '展开超时', step: 0.5, unit: '秒' },
            { kind: 'sub', key: 'enable_forward_msg_media', label: '里面的图片和文件也收', fallback: true },
            { kind: 'sub', key: 'forward_msg_expand_for_trigger', label: '群里靠关键词也能触发', fallback: true },
            { kind: 'desc', text: '后一项关掉，转发消息就只有 @ 或回复才理。' },
          ],
        },
      ],
    }],
  },
  {
    kind: 'note',
    text: '看图走的是「渠道」页系统槽位里的读图渠道，没配模型时图片收得到也看不了。改完点顶部的「应用」。',
  },
];

export const InputPage = () => (
  <SchemaFields platformId={QQ_NAPCAT.id} sectionId={SECTION} spec={SCHEMA} />
);
