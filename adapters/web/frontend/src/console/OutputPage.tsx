// 输出格式：消息发出去之前的处理。
import { Block, Desc, Fixed, Footnote, Grid } from './components';
import { BoolOption, NumberField } from './fields';

export const OutputPage = () => (
  <>
    <Block hint="消息发出去之前的处理" title="回复格式">
      <Grid>
        <BoolOption
          configKey="reply_to_trigger"
          desc="回复时引用触发它的那条消息，群里热闹时能看清在回谁。"
          fallback
          label="引用原消息"
        />
        <BoolOption
          configKey="strip_asterisk_styles"
          desc="QQ 不渲染 Markdown，*强调* 会原样显示成星号。"
          fallback
          label="去掉星号强调"
        />
        <BoolOption
          configKey="strip_underscore_styles"
          desc="下划线同理。但代码和文件名里常有下划线，默认不动。"
          label="去掉下划线强调"
        />
        <Fixed name="长度上限">
          <Desc>超长的回复会被截断，避免被 QQ 拒收。</Desc>
          <NumberField configKey="message_limit" label="单条消息" step={100} unit="字" />
          <NumberField configKey="history_text_limit" label="历史里每条" step={50} unit="字" />
        </Fixed>
        <BoolOption
          configKey="enable_image_forward_merge"
          desc="多张图合并成一条转发发出，而不是一张张发。"
          label="合并发图"
        >
          <NumberField configKey="image_forward_merge_threshold" label="至少" unit="张才合并" />
          <Desc>
            默认关是有原因的：合并转发会长时间占住那条唯一的反向 WS，期间发消息、贴表情全部排队。发现卡顿就关掉，即时生效。
          </Desc>
        </BoolOption>
      </Grid>
    </Block>

    <Block hint="告诉模型有哪些收藏表情可用" title="表态">
      <Grid>
        <BoolOption
          configKey="enable_reactions"
          desc="给群消息贴表情回应。"
          fallback
          label="表情回应"
        />
        <Fixed name="表情提示">
          <Desc>告诉模型有哪些收藏表情可用。0 表示不告诉它。</Desc>
          <NumberField configKey="face_prompt_limit" label="最多列出" unit="个" />
        </Fixed>
      </Grid>
    </Block>

    <Footnote>
      这些是 QQ 这一侧的发送处理，改完点顶部的「应用」。bot 是谁、说话什么风格在「机器人 › 人格」里改。
    </Footnote>
  </>
);
