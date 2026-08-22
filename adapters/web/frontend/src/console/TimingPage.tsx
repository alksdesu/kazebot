// 开口时机。七个信号里的三态那几项留空表示跟随旧的 group_mode 推导，界面上要说清。
import { Audition } from './Audition';
import { Block, Desc, Fixed, Footnote, Grid, Loose } from './components';
import { useLiveValue } from './consoleStore';
import {
  BoolOption,
  NumberField,
  PercentField,
  SubOption,
  TextField,
  TristateOption,
} from './fields';
import { WordList } from './WordList';

const CODE = 'bg-[var(--duties-muted)] px-1 py-px font-mono text-[0.65rem]';

export const TimingPage = () => {
  const legacyMode = useLiveValue<string>('group_trigger', 'mention_only').toLowerCase();
  const legacyAll = legacyMode === 'all' || legacyMode === 'always';
  const legacyPrefix = ['prefix', 'prefix_or_mention', 'mention_or_prefix'].includes(legacyMode);

  return (
    <>
      <Block hint="命中任意一条即回应" title="触发条件">
        <Grid>
          <TristateOption
            desc="群里任何人说任何话都回。开着的时候下面几条都不再影响判定。"
            fallback={legacyAll}
            label="全量回应"
            configKey="signal_all"
          />
          <TristateOption
            desc="消息里任何位置 @ 到 bot，不限句首。"
            fallback
            label="被 @"
            configKey="signal_at"
          />
          <TristateOption
            desc="有人引用 bot 说过的话。"
            fallback
            label="被回复"
            configKey="signal_reply"
          />
          <BoolOption
            desc="消息里提到 bot 的名字。原文会完整交给模型，不会被剥掉。"
            label="出现名字"
            configKey="signal_name"
          >
            <WordList
              configKey="name_words"
              empty="还没有名字，这一条不会命中"
              label="认这些名字"
              placeholder="输入一个名字，回车添加"
            />
            <SubOption configKey="name_anywhere" fallback label="不限句首，句中提到也算" />
          </BoolOption>
          <TristateOption
            desc="以配置的前缀开头。"
            fallback={legacyPrefix}
            label="前缀触发"
            configKey="signal_prefix"
          />
          <BoolOption
            desc="聊到特定话题时主动接话。"
            label="关键词插话"
            configKey="signal_keyword"
          >
            <WordList
              configKey="keyword_words"
              empty="还没有关键词，这一条不会命中"
              label="听这些词"
              placeholder="输入一个关键词，回车添加"
            />
          </BoolOption>
          <BoolOption
            desc="按概率随便说两句。骰子在运行时才掷，试听里只标注为「看运行时」。"
            label="随机插话"
            configKey="signal_random"
          >
            <PercentField configKey="random_probability" label="每条消息" />
          </BoolOption>
        </Grid>
        <Loose>
          <WordList
            configKey="trigger_prefixes"
            empty="没有前缀，前缀触发不会命中"
            label="触发前缀"
            placeholder="输入一个前缀，回车添加"
            spacing="loose"
          />
          <Footnote>
            前缀同时用于剥离正文，但 <code className={CODE}>/</code> 与 <code className={CODE}>／</code> 例外 ——
            <code className={CODE}>/draw</code> 这类命令要求字面斜杠，剥掉会失配。
          </Footnote>
        </Loose>
      </Block>

      <Block hint="前面全不命中时才问模型" title="接话意愿判断">
        <Grid>
          <BoolOption
            desc="问一次轻量模型「这句话需要你接话吗」。每条不命中的群消息都会产生一次模型往返，务必配独立的便宜渠道。"
            label="让模型判断"
            configKey="llm_intent_enabled"
          >
            <TextField configKey="llm_intent_node_id" label="判定节点" />
            <NumberField configKey="llm_intent_context_messages" label="带上最近" unit="条" />
          </BoolOption>
          <Fixed name="并发上限">
            <Desc>判定会占住 engine worker，占满了真实对话就得排队。</Desc>
            <NumberField label="同时最多" configKey="llm_intent_max_inflight" unit="个" />
            <NumberField
              label="超时"
              configKey="llm_intent_timeout_sec"
              step={0.5}
              unit="秒"
            />
          </Fixed>
        </Grid>
      </Block>

      <Block hint="命中之后仍可能不说话" title="冷却">
        <Grid>
          <Fixed name="发言间隔">
            <Desc>刚说过话的群里先歇一会儿。0 表示不限制。</Desc>
            <NumberField label="每群" configKey="cooldown_group_sec" unit="秒" />
            <NumberField label="每人" configKey="cooldown_user_sec" unit="秒" />
          </Fixed>
          <BoolOption
            desc="有人直接叫 bot，就不该被静默忽略。"
            fallback
            label="被 @ 时无视冷却"
            configKey="cooldown_exempt_at"
          />
        </Grid>
      </Block>

      <Block hint="判定由 bot 那份代码算，不是界面自己猜的" title="试听">
        <Audition />
      </Block>

      <Footnote>
        真实判定由 bot 进程执行。改动要点「应用」写入 qq.yaml 之后才生效，顶部状态条会显示 bot
        是否已经读到这一份。试听用的是编辑中的配置，所以它显示的是「应用之后会怎样」。
      </Footnote>
    </>
  );
};
