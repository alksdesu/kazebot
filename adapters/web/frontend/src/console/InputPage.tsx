// 输入处理：群友发来的东西里，哪些交给模型看。
import { Block, Desc, Footnote, Grid } from './components';
import { BoolOption, NumberField, SubOption } from './fields';
import { WordList } from './WordList';

export const InputPage = () => (
  <>
    <Block hint="群友发来的东西里，哪些交给模型看" title="能接收什么">
      <Grid>
        <BoolOption configKey="enable_image_input" desc="把图片交给视觉节点识别。" fallback label="看图片">
          <NumberField configKey="max_images_per_turn" label="每轮最多" unit="张" />
          <NumberField configKey="image_max_bytes" label="单图上限" scale={1024 * 1024} step={1} unit="MB" />
          <NumberField configKey="image_wait_after_text_sec" label="等图窗口" step={0.5} unit="秒" />
          <Desc>QQ 常把「文字＋图」拆成两条发过来，收到纯文本后等这么久看有没有图跟上。</Desc>
        </BoolOption>
        <BoolOption configKey="enable_file_input" desc="读取群文件的内容。" fallback label="读文件">
          <NumberField configKey="max_files_per_turn" label="每轮最多" unit="个" />
          <NumberField configKey="file_max_bytes" label="单文件上限" scale={1024 * 1024} step={1} unit="MB" />
          <WordList
            configKey="file_extra_allowed_extensions"
            empty="只收内置白名单里的类型"
            label="额外放行"
            placeholder="输入一个扩展名，回车添加"
          />
          <Desc>可执行文件与脚本类型不受这里影响，填了也不会收。</Desc>
        </BoolOption>
        <BoolOption
          configKey="enable_forward_msg_input"
          desc="展开合并转发的聊天记录。"
          fallback
          label="读合并转发"
        >
          <NumberField configKey="forward_msg_max_depth" label="嵌套深度" unit="层" />
          <NumberField configKey="forward_msg_max_messages" label="最多" step={10} unit="条" />
          <NumberField configKey="forward_msg_text_limit" label="总字数" step={1000} unit="字" />
          <NumberField configKey="forward_msg_timeout_sec" label="展开超时" step={0.5} unit="秒" />
          <SubOption configKey="enable_forward_msg_media" fallback label="里面的图片和文件也收" />
          <SubOption configKey="forward_msg_expand_for_trigger" fallback label="群里靠关键词也能触发" />
          <Desc>后一项关掉，转发消息就只有 @ 或回复才理。</Desc>
        </BoolOption>
      </Grid>
    </Block>

    <Footnote>
      看图走的是「渠道」页系统槽位里的读图渠道，没配模型时图片收得到也看不了。改完点顶部的「应用」。
    </Footnote>
  </>
);
