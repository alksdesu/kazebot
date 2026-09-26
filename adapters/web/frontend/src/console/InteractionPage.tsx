// 群互动：除了说话之外还能做什么。
import { Block, Desc, Footnote, Grid } from './components';
import { BoolOption, NumberField } from './fields';

export const InteractionPage = () => (
  <>
    <Block hint="除了说话之外还能做什么" title="群内互动">
      <Grid>
        <BoolOption configKey="enable_auto_like" desc="给群友的资料卡点赞。" label="自动点赞">
          <NumberField configKey="auto_like_times" label="每人每天" unit="次" />
        </BoolOption>
        <BoolOption
          configKey="enable_preempt"
          desc="上一条还没答完就来了新消息时，放弃旧的去答新的。"
          label="抢答"
        />
        <BoolOption
          configKey="enable_echo"
          desc="连着几个人刷同一句时，也跟一条。"
          label="跟复读"
        >
          <NumberField configKey="echo_threshold" label="连续" unit="条才跟" />
          <NumberField configKey="echo_max_length" label="最长" unit="字" />
          <NumberField configKey="echo_window_sec" label="时间窗" unit="秒" />
          <NumberField configKey="echo_cooldown_sec" label="跟完歇" unit="秒" />
          <Desc>
            必须是不同的人发的，同一个人连刷不算。命令、@、回复和长文本一律不跟；表情包按图片本身比对，
            三个人发三张不同的图不会被当成复读。
          </Desc>
        </BoolOption>
      </Grid>
    </Block>

    <Footnote>
      这几项只在群里成立，私聊不受影响。改完点顶部的「应用」。
    </Footnote>
  </>
);
