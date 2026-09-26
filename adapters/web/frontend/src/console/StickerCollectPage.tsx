// 表情包在 QQ 这一侧的收集与发送。图库本身是全平台共用的，在「机器人 › 表情包图库」里管。
import { Block, Desc, Fixed, Footnote, Grid } from './components';
import { BoolOption, ChoiceField, NumberField, SubOption } from './fields';
import { IdList } from './IdList';

const STRATEGY_CHOICES: ReadonlyArray<readonly [string, string]> = [
  ['strict', '严格'],
  ['loose', '宽松'],
  ['none', '全收'],
];

export const StickerCollectPage = () => (
  <>
    <Block hint="群里刷过的表情包攒进图库的规则" title="收集">
      <Grid>
        <BoolOption
          configKey="sticker_collect"
          desc="把群里刷过的表情包攒进图库。"
          label="收集表情包"
        >
          <Desc>
            严格只收 QQ 标了表情包的，宽松再放行看着不像截图的，全收连群友随手发的照片都要。
          </Desc>
          <ChoiceField
            choices={STRATEGY_CHOICES}
            configKey="sticker_strategy"
            fallback="loose"
            label="收多严"
          />
          <SubOption configKey="sticker_auto_accept" label="收到就直接入库，跳过人工筛选" />
          <IdList
            configKey="sticker_groups"
            empty="所有已授权的群都收"
            label="只从这些群收"
            placeholder="输入群号，回车添加"
          />
        </BoolOption>

        <Fixed name="容量">
          <Desc>待审池满了就不再收新的；图库超出上限时最旧的先出局。</Desc>
          <NumberField configKey="sticker_pending_limit" label="待审池" step={10} unit="张" />
          <NumberField configKey="sticker_library_limit" label="图库" step={50} unit="张" />
          <NumberField configKey="sticker_pending_ttl_days" label="待审保留" unit="天" />
        </Fixed>

        <BoolOption
          configKey="sticker_auto_tag"
          desc="收进来的图自动送去看图渠道打标签。关掉就不再产生这笔开销，但没标签的图检索不到，也就发不出去。"
          fallback
          label="自动打标签"
        />
      </Grid>
    </Block>

    <Block hint="什么时候把表情包摊给模型挑" title="发送">
      <Grid>
        <Fixed name="发图">
          <Desc>清单按概率整份摊给模型，发不发、发哪张由它自己定。库比上限大就先给发得最少的。</Desc>
          <NumberField
            configKey="sticker_send_probability"
            label="每轮出现概率"
            max={1}
            step={0.05}
          />
          <NumberField configKey="sticker_prompt_limit" label="最多列出" unit="张" />
          <NumberField configKey="sticker_repeat_window_sec" label="同图间隔" step={60} unit="秒" />
        </Fixed>

        <BoolOption
          configKey="sticker_combat"
          desc="群里连着刷图时跟着接一张。要求连图来自至少两个人，中间夹了纯文本就不算。"
          label="接梗"
        >
          <NumberField
            configKey="sticker_burst_probability"
            label="发完再补一张的概率"
            max={1}
            step={0.05}
          />
        </BoolOption>
      </Grid>
    </Block>

    <Footnote>
      只有在库且打过标的图才会进候选 —— 没打标的图对模型来说是不可描述的，给了也挑不出来。
      打标走「机器人 › 表情包图库」里显示的那条看图渠道，跟着 bot 进程慢慢跑，刚收进来的图要等一会儿才可用。
    </Footnote>
  </>
);
